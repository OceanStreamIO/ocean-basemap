"""External overviews for a raster, as GDAL's ``<file>.ovr`` beside it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.console import Console

console = Console()


def overview_path(src: Path) -> Path:
    return Path(f"{src}.ovr")


def ensure_overviews(src: Path, levels: Sequence[int]) -> Path:
    """Build ``<src>.ovr`` unless it is there; *src* itself is never written.

    Built with rasterio rather than gdaladdo, because the build Job's image has
    GDAL only inside the rasterio wheel. A GeoTIFF opened for update writes its
    overviews into itself; TIFF_USE_OVR sends them to a separate file instead,
    and the grid's bytes stay as they were.

    The file is built under a temporary name and renamed once complete. GDAL
    takes any ``.ovr`` beside a file as its overviews, so one cut short by an
    interrupted run would otherwise be read as finished. GDAL names the file
    after the path the raster was opened by, so the build goes through a
    symlink with the temporary name.
    """
    import rasterio
    from rasterio.enums import Resampling

    ovr = overview_path(src)
    if ovr.is_file():
        return ovr

    link = src.with_name(f"{src.name}.building{src.suffix}")
    building = overview_path(link)
    for stale in (link, building):
        if stale.is_symlink() or stale.exists():
            stale.unlink()

    console.print(f"  [cyan]Overviews for {src.name}[/cyan] "
                  f"[dim](levels {', '.join(map(str, levels))}; a few minutes)[/dim]")
    link.symlink_to(src.name)
    try:
        with rasterio.Env(TIFF_USE_OVR=True), rasterio.open(link, "r+") as ds:
            ds.build_overviews(list(levels), Resampling.average)
        building.replace(ovr)
    finally:
        link.unlink(missing_ok=True)
    console.print(f"  [green]Done:[/green] {ovr.name}")
    return ovr
