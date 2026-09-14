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
import subprocess
import tempfile
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


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {proc.stderr.strip()[:400]}")


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
    features = json.loads(dump).get("features", [])
    if not features:
        return False

    land = union_all([from_geojson(json.dumps(f["geometry"])) for f in features])
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
    water = tmp / f"water_{index}.geojson"
    has_land = _water_geojson(cell, land_src, water)
    if has_land and json.loads(water.read_text())["features"] == []:
        return None  # cell is entirely land

    clip: list[str] = (
        ["-clipsrc", str(water)] if has_land
        else ["-clipsrc", *map(str, cell)]
    )

    # Intersecting two polygon layers yields GeometryCollections wherever an
    # edge merely touches the clip boundary, which FlatGeobuf refuses. Land
    # them in a mixed-geometry file, then keep only the polygonal parts.
    mixed = tmp / f"mixed_{index}.fgb"
    _run([
        "ogr2ogr", "-f", "FlatGeobuf", str(mixed), str(src),
        "-spat", *map(str, cell), *clip, "-nlt", "GEOMETRY",
    ])
    if not mixed.exists():
        return None

    part = tmp / f"part_{index}.fgb"
    # A cell whose only intersection with the clip region is a shared edge
    # extracts to NULL, and FlatGeobuf will not index a NULL geometry.
    extract = "ST_CollectionExtract(geometry, 3)"
    _run([
        "ogr2ogr", "-f", "FlatGeobuf", str(part), str(mixed),
        "-nlt", "PROMOTE_TO_MULTI", "-nln", layer, "-dialect", "SQLite",
        "-sql", (
            f'SELECT min_depth, {extract} AS geometry FROM "{layer}" '
            f"WHERE geometry IS NOT NULL AND {extract} IS NOT NULL "
            f"AND NOT ST_IsEmpty({extract})"
        ),
    ])
    mixed.unlink(missing_ok=True)
    water.unlink(missing_ok=True)
    return part if part.exists() else None


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
    workers: int = 8,
) -> Path:
    """Erase OSM land from the depth polygons in *src*, writing *dst*.

    *extent* defaults to the layer's own envelope. Pass it explicitly when the
    envelope overstates the data: ``-spat`` keeps whole features, so a
    windowed extract still reports the full source extent.
    """
    extent = extent or layer_extent(src, layer)
    cells = grid_cells(extent, cell_size)
    console.print(
        f"  Clipping [cyan]{layer}[/cyan] to the OSM coastline "
        f"across {len(cells)} cells of {cell_size}°"
    )

    dst.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=dst.parent) as tmpdir:
        tmp = Path(tmpdir)
        parts: list[Path] = []
        with Progress(console=console) as progress:
            task = progress.add_task("Clipping cells", total=len(cells))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_clip_cell, i, cell, src, layer, land_src, tmp)
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

    console.print(f"  [green]Clipped[/green]: {dst.name}")
    return dst
