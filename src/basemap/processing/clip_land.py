"""Clip bathymetry polygons back to the OSM coastline.

Every bathymetry source ends at its own land/sea boundary, and none of them
agree with the OSM coastline: GEBCO is a 463 m grid, EMODnet a 115 m one, and
both are polygonised on a raster lattice. The result overlaps real land by a
belt up to half a cell wide. While an opaque land polygon was painted on top
that belt was invisible, but it is exactly what stops satellite imagery -- or
any other basemap underneath -- from meeting the shore cleanly.

Clipping is done per grid cell so the work parallelises and no single GEOS
call sees the whole world. The clip region for a cell is the cell rectangle
minus the OSM land within it, which avoids needing the (900 MB) water polygon
download: it is the complement of the land polygons already on disk.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from shapely import box, from_geojson, union_all
from shapely.geometry import mapping

console = Console()

Bbox = tuple[float, float, float, float]

_EXTENT_RE = re.compile(
    r"Extent:\s*\(([-\d.eE+]+),\s*([-\d.eE+]+)\)\s*-\s*\(([-\d.eE+]+),\s*([-\d.eE+]+)\)"
)
_COUNT_RE = re.compile(r"Feature Count:\s*(\d+)")


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {proc.stderr.strip()[:400]}")


def _feature_count(path: Path) -> int:
    """Count features without scanning -- FlatGeobuf keeps it in its header."""
    out = subprocess.run(
        ["ogrinfo", "-ro", "-al", "-so", str(path)], capture_output=True, text=True
    ).stdout
    match = _COUNT_RE.search(out)
    return int(match.group(1)) if match else 0


def layer_extent(src: Path, layer: str) -> Bbox:
    """Read a layer's bounding box without scanning its features."""
    out = subprocess.run(
        ["ogrinfo", "-ro", "-so", str(src), layer], capture_output=True, text=True
    ).stdout
    match = _EXTENT_RE.search(out)
    if not match:
        raise RuntimeError(f"could not read extent of {layer} in {src}")
    west, south, east, north = (float(g) for g in match.groups())
    return west, south, east, north


def grid_cells(extent: Bbox, step: float) -> list[Bbox]:
    """Split *extent* into a grid of at most *step*-degree cells."""
    if step <= 0:
        raise ValueError("step must be positive")
    west, south, east, north = extent
    cells: list[Bbox] = []
    lat = south
    while lat < north:
        lat_hi = min(lat + step, north)
        lon = west
        while lon < east:
            lon_hi = min(lon + step, east)
            cells.append((lon, lat, lon_hi, lat_hi))
            lon = lon_hi
        lat = lat_hi
    return cells


def _water_geojson(cell: Bbox, land_src: Path, dest: Path) -> bool:
    """Write cell-minus-land to *dest*. False when the cell holds no land."""
    dump = subprocess.run(
        ["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", str(land_src), "-spat", *map(str, cell)],
        capture_output=True, text=True, check=True,
    ).stdout
    # GeoJSON carries a feature per row even when its geometry is null.
    geoms = [
        from_geojson(json.dumps(f["geometry"]))
        for f in json.loads(dump).get("features", [])
        if f.get("geometry")
    ]
    if not geoms:
        return False

    land = union_all(geoms)
    water = box(*cell).difference(land)
    if water.is_empty:
        dest.write_text('{"type":"FeatureCollection","features":[]}')
        return True

    dest.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": mapping(water)}],
    }))
    return True


def _clip_cell(
    index: int, cell: Bbox, src: Path, layer: str, land_src: Path, tmp: Path
) -> Path | None:
    """Clip one cell. Returns the part file, or None when it holds no ocean."""
    part = tmp / f"part_{index}.fgb"
    done = tmp / f"done_{index}"
    if done.exists():
        return part if part.exists() else None

    window = tmp / f"win_{index}.fgb"
    water = tmp / f"water_{index}.geojson"
    mixed = tmp / f"mixed_{index}.fgb"

    def clip() -> Path | None:
        # Take the cell's slice of the source before looking at land. This is
        # index-accelerated and roughly nine cells in ten hold no bathymetry,
        # so it keeps the land file out of the loop for the common case --
        # which is what stops the whole pass from thrashing the page cache.
        _run([
            "ogr2ogr", "-f", "FlatGeobuf", str(window), str(src),
            "-spat", *map(str, cell), "-clipsrc", *map(str, cell),
            "-nln", layer, "-nlt", "GEOMETRY",
        ])
        if not window.exists() or _feature_count(window) == 0:
            return None

        # Intersecting two polygon layers yields GeometryCollections wherever
        # an edge merely touches the clip boundary, which FlatGeobuf refuses.
        # Land them in a mixed-geometry file, then keep the polygonal parts.
        source = window
        if _water_geojson(cell, land_src, water):
            if json.loads(water.read_text())["features"] == []:
                return None  # cell is entirely land
            _run([
                "ogr2ogr", "-f", "FlatGeobuf", str(mixed), str(window),
                "-clipsrc", str(water), "-nln", layer, "-nlt", "GEOMETRY",
            ])
            source = mixed

        # A cell whose only intersection with the clip region is a shared edge
        # extracts to NULL, and FlatGeobuf will not index a NULL geometry.
        extract = "ST_CollectionExtract(geometry, 3)"
        _run([
            "ogr2ogr", "-f", "FlatGeobuf", str(part), str(source),
            "-nlt", "PROMOTE_TO_MULTI", "-nln", layer, "-dialect", "SQLite",
            "-sql", (
                f'SELECT min_depth, {extract} AS geometry FROM "{layer}" '
                f"WHERE geometry IS NOT NULL AND {extract} IS NOT NULL "
                f"AND NOT ST_IsEmpty({extract})"
            ),
        ])
        return part if part.exists() and _feature_count(part) else None

    try:
        result = clip()
    finally:
        for scratch in (window, water, mixed):
            scratch.unlink(missing_ok=True)
    if result is None:
        part.unlink(missing_ok=True)
    done.touch()
    return result


def require_osm_land(work_dir: Path) -> Path:
    """Locate the OSM land polygons that the land-tiles step downloads."""
    path = work_dir / "land" / "osm_land.fgb"
    if not path.is_file():
        console.print(
            "[red bold]ERROR:[/] OSM land polygons not found at "
            f"{path}.\n  Run [cyan]oceanbase run land-tiles[/cyan] first "
            "- the coastline clip reuses its download."
        )
        raise SystemExit(1)
    return path


def clip_to_coastline(
    src: Path,
    dst: Path,
    land_src: Path,
    *,
    layer: str,
    extent: Bbox | None = None,
    cell_size: float = 2.0,
    workers: int = 4,
) -> Path:
    """Erase OSM land from the depth polygons in *src*, writing *dst*.

    *extent* defaults to the layer's own envelope. Pass it explicitly when the
    envelope overstates the data: ``-spat`` keeps whole features, so a
    windowed extract still reports the full source extent.

    Finished cells are kept next to *dst* so an interrupted pass resumes
    instead of starting over; the directory is removed once *dst* is written.
    """
    extent = extent or layer_extent(src, layer)
    cells = grid_cells(extent, cell_size)
    scratch = dst.parent / f"{dst.stem}_parts"
    scratch.mkdir(parents=True, exist_ok=True)
    resumed = len(list(scratch.glob("done_*")))
    console.print(
        f"  Clipping [cyan]{layer}[/cyan] to the OSM coastline "
        f"across {len(cells)} cells of {cell_size}°"
        + (f" [yellow](resuming, {resumed} done)[/yellow]" if resumed else "")
    )

    dst.unlink(missing_ok=True)
    parts: list[Path] = []
    with Progress(console=console) as progress:
        task = progress.add_task("Clipping cells", total=len(cells))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_clip_cell, i, cell, src, layer, land_src, scratch)
                for i, cell in enumerate(cells)
            ]
            for future in futures:
                part = future.result()
                if part:
                    parts.append(part)
                progress.advance(task)

    if not parts:
        raise RuntimeError(f"clip produced no features for {layer}")

    console.print(f"  Merging {len(parts)} clipped cells")
    for i, part in enumerate(parts):
        args = ["ogr2ogr", "-f", "FlatGeobuf"]
        args += ["-append"] if i else []
        args += [str(dst), str(part), "-nln", layer, "-nlt", "PROMOTE_TO_MULTI"]
        _run(args)

    shutil.rmtree(scratch, ignore_errors=True)
    console.print(f"  [green]Clipped[/green]: {dst.name}")
    return dst
