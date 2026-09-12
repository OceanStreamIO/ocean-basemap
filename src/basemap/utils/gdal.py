"""Subprocess wrappers for GDAL command-line tools.

Each wrapper builds the argument list, logs it, runs it, and raises on failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from basemap.config import get_tile_processes

console = Console()


def _require(cmd: str) -> str:
    """Return full path to *cmd* or raise with install hint."""
    path = shutil.which(cmd)
    if not path:
        console.print(f"[red bold]ERROR:[/] '{cmd}' not found on PATH.")
        console.print("[dim]Install with: brew install gdal[/dim]")
        raise SystemExit(1)
    return path


def _run(args: list[str], *, label: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, logging the command and raising on failure."""
    if label:
        console.print(f"  [cyan]{label}[/cyan]")
    cmd_preview = " ".join(str(a) for a in args[:6])
    console.print(f"  [dim]$ {cmd_preview}{'…' if len(args) > 6 else ''}[/dim]")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]Command failed (exit {result.returncode}):[/red]")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-10:]:
                console.print(f"  [red]{line}[/red]")
        raise RuntimeError(f"{args[0]} failed with exit code {result.returncode}")
    return result


# -- GDAL tools --

GTIFF_OPTS = ["-co", "COMPRESS=LZW", "-co", "TILED=YES", "-co", "BIGTIFF=YES"]
GTIFF_THREADED = [*GTIFF_OPTS, "-co", "NUM_THREADS=ALL_CPUS"]


def gdaldem_hillshade(
    src: Path, dst: Path, *, z_factor: int = 8, multidirectional: bool = True
) -> Path:
    args = [_require("gdaldem"), "hillshade", str(src), str(dst)]
    if multidirectional:
        args.append("-multidirectional")
    args.extend(["-z", str(z_factor), "-compute_edges", *GTIFF_THREADED])
    _run(args, label="Multi-directional hillshade")
    return dst


def gdaldem_color_relief(
    src: Path, ramp: Path, dst: Path, *, alpha: bool = False
) -> Path:
    args = [_require("gdaldem"), "color-relief", str(src), str(ramp), str(dst)]
    if alpha:
        args.append("-alpha")
    args.extend(GTIFF_THREADED)
    _run(args, label="Color relief")
    return dst


def gdal_calc(
    src: Path, dst: Path, *, calc: str, output_type: str = "Byte", nodata: float | None = None
) -> Path:
    args = [
        _require("gdal_calc.py"),
        "-A", str(src),
        f"--outfile={dst}",
        f"--calc={calc}",
        f"--type={output_type}",
        "--co=COMPRESS=LZW", "--co=TILED=YES", "--co=BIGTIFF=YES",
        "--co=NUM_THREADS=ALL_CPUS",
    ]
    if nodata is not None:
        args.append(f"--NoDataValue={nodata}")
    _run(args, label="Raster calculation")
    return dst


def gdal_merge(inputs: list[Path], dst: Path) -> Path:
    args = [
        _require("gdal_merge.py"),
        "-o", str(dst),
        *[str(p) for p in inputs],
        "-co", "COMPRESS=LZW", "-co", "TILED=YES", "-co", "BIGTIFF=YES",
    ]
    _run(args, label="Merge rasters")
    return dst


def gdalbuildvrt(inputs: list[Path], dst: Path, *, resolution: str = "highest") -> Path:
    """Mosaic *inputs* into a virtual raster without copying pixel data."""
    listing = dst.with_suffix(".inputs.txt")
    listing.write_text("\n".join(str(p) for p in inputs) + "\n")
    args = [
        _require("gdalbuildvrt"),
        "-resolution", resolution,
        "-input_file_list", str(listing),
        str(dst),
    ]
    _run(args, label=f"Build VRT from {len(inputs)} rasters")
    return dst


def gdalwarp(
    src: Path, dst: Path, *, resolution: float, resampling: str = "near"
) -> Path:
    args = [
        _require("gdalwarp"),
        str(src), str(dst),
        "-tr", str(resolution), str(resolution),
        "-r", resampling,
        *GTIFF_OPTS,
        "-overwrite",
    ]
    _run(args, label=f"Warp to {resolution}°")
    return dst


def gdal_polygonize(src: Path, dst: Path, *, layer: str, field: str) -> Path:
    args = [
        _require("gdal_polygonize.py"),
        str(src), "-of", "FlatGeobuf", str(dst), layer, field, "-q",
    ]
    _run(args, label="Polygonize raster")
    return dst


def gdal_contour(
    src: Path,
    dst: Path,
    *,
    attribute: str = "depth",
    interval: int | None = None,
    fixed_levels: list[int] | None = None,
    output_format: str = "GeoJSON",
) -> Path:
    args = [_require("gdal_contour"), "-a", attribute]
    if interval:
        args.extend(["-i", str(interval)])
    if fixed_levels:
        args.extend(["-fl", *[str(v) for v in fixed_levels]])
    args.extend([str(src), str(dst), "-of", output_format])
    label_parts = []
    if interval:
        label_parts.append(f"{interval}m interval")
    if fixed_levels:
        label_parts.append(f"fixed levels: {fixed_levels}")
    _run(args, label=f"Contour ({', '.join(label_parts)})")
    return dst


def gdal2tiles(
    src: Path,
    dst_dir: Path,
    *,
    zoom: str = "0-10",
    processes: int | None = None,
    resampling: str = "lanczos",
) -> Path:
    args = [
        _require("gdal2tiles.py"),
        str(src), str(dst_dir),
        f"--zoom={zoom}",
        f"--processes={processes or get_tile_processes()}",
        "--xyz",
        f"--resampling={resampling}",
        "--tilesize=256",
        "--no-kml",
        "--webviewer=none",
    ]
    _run(args, label=f"Generate tiles z{zoom}")
    return dst_dir


def ogr2ogr(
    src: Path,
    dst: Path,
    *,
    output_format: str | None = None,
    where: str | None = None,
    t_srs: str | None = None,
    sql: str | None = None,
    nln: str | None = None,
) -> Path:
    args = [_require("ogr2ogr")]
    if output_format:
        args.extend(["-f", output_format])
    if t_srs:
        args.extend(["-t_srs", t_srs])
    if nln:
        args.extend(["-nln", nln])
    args.extend([str(dst), str(src)])
    if sql:
        args.extend(["-sql", sql])
    if where:
        args.extend(["-where", where])
    _run(args, label="ogr2ogr convert/filter")
    return dst
