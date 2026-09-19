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
import math
import re
import shutil
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape

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

# OGR intersects every feature against the whole clip geometry, so a patch
# costs roughly features x coastline vertices. Splitting one divides both,
# which is why a dense archipelago is far cheaper handled in pieces.
_MAX_PATCH_FEATURES = 1000


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
    index: int, cell: Bbox, src: Path, layer: str, land_src: Path, tmp: Path,
    fields: Sequence[str],
) -> Path | None:
    """Clip one cell. Returns the part file, or None when it holds no ocean."""
    part = tmp / f"part_{index}.fgb"
    done = tmp / f"done_{index}"
    if done.exists():
        return part if part.exists() else None

    # A part without its marker is the debris of an interrupted pass, and no
    # GDAL vector driver will overwrite an existing file.
    part.unlink(missing_ok=True)
    window = tmp / f"win_{index}.fgb"
    try:
        # Take the cell's slice of the source before looking at land. This is
        # index-accelerated and roughly nine cells in ten hold no bathymetry,
        # so it keeps the land file out of the loop for the common case --
        # which is what stops the whole pass from thrashing the page cache.
        _run([
            "ogr2ogr", "-f", "FlatGeobuf", str(window), str(src),
            "-spat", *map(str, cell), "-clipsrc", *map(str, cell),
            "-nln", layer, "-nlt", "GEOMETRY",
        ])
        result = (
            _clip_patch(window, cell, layer, land_src, tmp, str(index), fields)
            if window.exists() else None
        )
    finally:
        window.unlink(missing_ok=True)
    done.touch()
    return result


def _clip_patch(
    src: Path, cell: Bbox, layer: str, land_src: Path, tmp: Path, tag: str,
    fields: Sequence[str],
) -> Path | None:
    """Clip already-extracted features, splitting the patch if it is dense."""
    count = _feature_count(src)
    if count == 0:
        return None
    if count > _MAX_PATCH_FEATURES:
        return _split_patch(src, cell, layer, land_src, tmp, tag, count, fields)

    part = tmp / f"part_{tag}.fgb"
    water = tmp / f"water_{tag}.geojson"
    mixed = tmp / f"mixed_{tag}.fgb"
    part.unlink(missing_ok=True)
    try:
        # Intersecting two polygon layers yields GeometryCollections wherever
        # an edge merely touches the clip boundary, which FlatGeobuf refuses.
        # Land them in a mixed-geometry file, then keep the polygonal parts.
        source = src
        if _water_geojson(cell, land_src, water):
            if json.loads(water.read_text())["features"] == []:
                return None  # patch is entirely land
            _run([
                "ogr2ogr", "-f", "FlatGeobuf", str(mixed), str(src),
                "-clipsrc", str(water), "-nln", layer, "-nlt", "GEOMETRY",
            ])
            source = mixed

        # A patch whose only intersection with the clip region is a shared
        # edge extracts to NULL, and FlatGeobuf will not index a NULL geometry.
        extract = "ST_CollectionExtract(geometry, 3)"
        keep = ", ".join(f'"{field}"' for field in fields)
        _run([
            "ogr2ogr", "-f", "FlatGeobuf", str(part), str(source),
            "-nlt", "PROMOTE_TO_MULTI", "-nln", layer, "-dialect", "SQLite",
            "-sql", (
                f'SELECT {keep}, {extract} AS geometry FROM "{layer}" '
                f"WHERE geometry IS NOT NULL AND {extract} IS NOT NULL "
                f"AND NOT ST_IsEmpty({extract})"
            ),
        ])
    finally:
        water.unlink(missing_ok=True)
        mixed.unlink(missing_ok=True)

    if part.exists() and _feature_count(part):
        return part
    part.unlink(missing_ok=True)
    return None


def _split_patch(
    src: Path, cell: Bbox, layer: str, land_src: Path, tmp: Path, tag: str,
    count: int, fields: Sequence[str],
) -> Path | None:
    """Clip a dense patch as a grid of smaller ones, then stitch them back."""
    side = max(2, math.ceil(math.sqrt(count / _MAX_PATCH_FEATURES)))
    west, south, east, north = cell
    step = max(east - west, north - south) / side
    parts: list[Path] = []
    for i, sub in enumerate(grid_cells(cell, step)):
        window = tmp / f"win_{tag}_{i}.fgb"
        try:
            _run([
                "ogr2ogr", "-f", "FlatGeobuf", str(window), str(src),
                "-spat", *map(str, sub), "-clipsrc", *map(str, sub),
                "-nln", layer, "-nlt", "GEOMETRY",
            ])
            if window.exists():
                part = _clip_patch(
                    window, sub, layer, land_src, tmp, f"{tag}_{i}", fields
                )
                if part:
                    parts.append(part)
        finally:
            window.unlink(missing_ok=True)

    if not parts:
        return None
    merged = tmp / f"part_{tag}.fgb"
    merged.unlink(missing_ok=True)
    merge_layers(parts, merged, layer, tmp, name=f"union_{tag}.vrt")
    for part in parts:
        part.unlink(missing_ok=True)
    return merged


def merge_layers(
    parts: list[Path], dst: Path, layer: str, tmp: Path, name: str = "union.vrt"
) -> None:
    """Concatenate same-schema vector files into *dst* in a single pass.

    Appending them one by one is quadratic: FlatGeobuf rewrites its packed
    R-tree on every append, so the cost per part grows with the destination.
    A union VRT lets one ogr2ogr build the index once instead.
    """
    layers = "".join(
        f'<OGRVRTLayer name="p{i}"><SrcDataSource>{escape(str(part))}</SrcDataSource>'
        f"<SrcLayer>{layer}</SrcLayer></OGRVRTLayer>"
        for i, part in enumerate(parts)
    )
    vrt = tmp / name
    vrt.write_text(
        f'<OGRVRTDataSource><OGRVRTUnionLayer name="{layer}">{layers}'
        "<FieldStrategy>FirstLayer</FieldStrategy></OGRVRTUnionLayer></OGRVRTDataSource>"
    )
    _run([
        "ogr2ogr", "-f", "FlatGeobuf", str(dst), str(vrt),
        "-nln", layer, "-nlt", "PROMOTE_TO_MULTI",
    ])
    vrt.unlink(missing_ok=True)


def _sea_cell(index: int, cell: Bbox, land_src: Path, layer: str, tmp: Path) -> Path | None:
    """Write one cell's sea polygon. Returns None when the cell has none."""
    part = tmp / f"part_{index}.fgb"
    done = tmp / f"done_{index}"
    if done.exists():
        return part if part.exists() else None

    part.unlink(missing_ok=True)
    water = tmp / f"water_{index}.geojson"
    try:
        # A cell with no land cannot hold a shoreline, and the bathymetry
        # fills already cover open ocean -- the mask is only ever visible in
        # the gap they leave along the coast. Skipping those cells keeps this
        # to the ~10% of the globe that has a coast in it, and avoids paving
        # the oceans with 2-degree rectangles whose shared edges would have to
        # be hidden afterwards.
        if _water_geojson(cell, land_src, water) and json.loads(
            water.read_text()
        )["features"]:
            # A fjord coast such as southern Chile is one polygon of hundreds
            # of thousands of vertices, past OGR's default GeoJSON object size.
            _run([
                "ogr2ogr", "--config", "OGR_GEOJSON_MAX_OBJ_SIZE", "0",
                "-f", "FlatGeobuf", str(part), str(water),
                "-nln", layer, "-nlt", "PROMOTE_TO_MULTI",
            ])
    finally:
        water.unlink(missing_ok=True)
    # Only on success: a failed cell must be retried on resume, not skipped.
    done.touch()
    return part if part.exists() else None


def build_sea_mask(
    land_src: Path,
    dst: Path,
    *,
    layer: str = "sea",
    extent: Bbox = (-180.0, -90.0, 180.0, 90.0),
    cell_size: float = 2.0,
    workers: int = 8,
) -> Path:
    """Build the complement of the OSM land polygons, writing *dst*.

    This is the same geometry :func:`clip_to_coastline` computes per cell and
    then throws away, kept this time as a layer of its own. Drawn between the
    satellite imagery and the bathymetry fills, it is what makes the shoreline
    the OSM edge rather than whichever depth polygon happens to reach furthest
    inshore: the bathymetry sources stop on their own grids, and the strip they
    leave short of the coast is exactly where the imagery used to show through.

    Finished cells are kept next to *dst* so an interrupted pass resumes.
    """
    cells = grid_cells(extent, cell_size)
    scratch = dst.parent / f"{dst.stem}_parts"
    scratch.mkdir(parents=True, exist_ok=True)
    resumed = len(list(scratch.glob("done_*")))
    console.print(
        f"  Building the [cyan]{layer}[/cyan] mask across {len(cells)} cells "
        f"of {cell_size}\u00b0"
        + (f" [yellow](resuming, {resumed} done)[/yellow]" if resumed else "")
    )

    dst.unlink(missing_ok=True)
    parts: list[Path] = []
    with Progress(console=console) as progress:
        task = progress.add_task("Masking cells", total=len(cells))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_sea_cell, i, cell, land_src, layer, scratch)
                for i, cell in enumerate(cells)
            ]
            for future in futures:
                part = future.result()
                if part:
                    parts.append(part)
                progress.advance(task)

    if not parts:
        raise RuntimeError("sea mask produced no features")

    console.print(f"  Merging {len(parts)} coastal cells")
    merge_layers(parts, dst, layer, scratch)

    shutil.rmtree(scratch, ignore_errors=True)
    console.print(f"  [green]Sea mask[/green]: {dst.name}")
    return dst


def require_ne_land(work_dir: Path) -> Path:
    """Locate the Natural Earth 10m land that the land-tiles step converts."""
    path = work_dir / "land" / "land_10m.geojson"
    if not path.is_file():
        console.print(
            "[red bold]ERROR:[/] Natural Earth land not found at "
            f"{path}.\n  Run [cyan]oceanbase run land-tiles[/cyan] first."
        )
        raise SystemExit(1)
    return path


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
    fields: Sequence[str] = ("min_depth",),
    extent: Bbox | None = None,
    cell_size: float = 2.0,
    workers: int = 8,
) -> Path:
    """Erase OSM land from the depth polygons in *src*, writing *dst*.

    *fields* are the source attributes carried through; the clip runs as a
    SQL projection, so anything left out is dropped.

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
                pool.submit(
                    _clip_cell, i, cell, src, layer, land_src, scratch, fields
                )
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
    merge_layers(parts, dst, layer, scratch)

    shutil.rmtree(scratch, ignore_errors=True)
    console.print(f"  [green]Clipped[/green]: {dst.name}")
    return dst
