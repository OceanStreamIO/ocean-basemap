"""Basemap CLI — Typer-based command interface for the ocean basemap pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from basemap.config import GEBCO_RELEASE, get_data_dir, get_gebco_tif, get_tile_dir
from basemap.utils.paths import file_size_mb, require_mount

app = typer.Typer(
    name="oceanbase",
    help="OceanStream ocean basemap pipeline — GEBCO-based tiles for MapLibre.",
    no_args_is_help=True,
)

console = Console()

DataDirOpt = Annotated[
    Path | None,
    typer.Option(
        "--data-dir", "-d",
        help="Data directory (default: $BASEMAP_DATA_DIR)",
    ),
]

# ---------------------------------------------------------------------------
# Pipeline steps — mapping name → module
# ---------------------------------------------------------------------------

STEPS: dict[str, str] = {
    "download": "basemap.pipeline.download",
    "hillshade": "basemap.pipeline.hillshade",
    "depth-polygons": "basemap.pipeline.depth_polygons",
    "emodnet": "basemap.pipeline.emodnet",
    "hr-bathymetry": "basemap.pipeline.hr_bathymetry",
    "contours": "basemap.pipeline.contours",
    "ocean-labels": "basemap.pipeline.ocean_labels",
    "seamounts": "basemap.pipeline.seamounts",
    "land-tiles": "basemap.pipeline.land_tiles",
    "sea-mask": "basemap.pipeline.sea_mask",
    "lakes": "basemap.pipeline.lakes",
    "surface-water": "basemap.pipeline.surface_water",
    "places": "basemap.pipeline.places",
    "boundaries": "basemap.pipeline.boundaries",
    "hypsometric": "basemap.pipeline.hypsometric",
    "bathymetry": "basemap.pipeline.bathymetry",
    "globe": "basemap.pipeline.globe",
    "xyz": "basemap.pipeline.xyz",
    # Parked, deliberately out of FULL_ORDER: `oceanbase run reference` still
    # builds it, but a full run does not. See its module docstring.
    "reference": "basemap.pipeline.reference",
    "upload": "basemap.pipeline.upload_s3",
}

# Regional add-ons: large downloads with Europe-only coverage, so they are
# excluded from `run all` unless requested with --with.
OPTIONAL_STEPS: set[str] = {"emodnet", "hr-bathymetry"}

# Canonical order of every step
FULL_ORDER = [
    "download",
    "hillshade",
    "depth-polygons",
    "emodnet",
    "hr-bathymetry",
    "contours",
    "ocean-labels",
    "seamounts",
    "land-tiles",
    # After land-tiles: the mask is built from the OSM land polygons that step
    # downloads.
    "sea-mask",
    "lakes",
    # Observed surface water, which the sea mask and the bathymetry are cut
    # against so that lagoons and estuaries stop being land. Before sea-mask
    # and bathymetry, which read it; after lakes, whose polygons win over it.
    "surface-water",
    # After lakes: the place names come out of the same N50 geodatabase.
    "places",
    "boundaries",
    "hypsometric",
    # After the bathymetry sources (depth-polygons, emodnet, hr-bathymetry) and
    # land-tiles, whose GEBCO overviews, DTM mosaic, grids and OSM land it reads.
    "bathymetry",
    # After both: the geographic archives are the same specs on another grid.
    "globe",
    # After every raster archive: the flat tile tree is composited from them.
    "xyz",
    "upload",
]

# Execution order for `run all`
RUN_ORDER = [s for s in FULL_ORDER if s not in OPTIONAL_STEPS]


def _resolve_run_order(include: list[str] | None, skip_upload: bool) -> list[str]:
    """Steps for `run all`, plus any opt-in extras."""
    extras = set(include or [])
    if "all" in extras:
        extras = set(OPTIONAL_STEPS)
    unknown = extras - OPTIONAL_STEPS
    if unknown:
        console.print(f"[red]Not an optional step:[/red] {', '.join(sorted(unknown))}")
        console.print(f"Available: all, {', '.join(sorted(OPTIONAL_STEPS))}")
        raise typer.Exit(1)

    return [
        s for s in FULL_ORDER
        if (s not in OPTIONAL_STEPS or s in extras) and not (skip_upload and s == "upload")
    ]


def _run_step(name: str, data_dir: Path | None) -> None:
    """Dynamically import and run a pipeline step."""
    import importlib

    module_path = STEPS[name]
    mod = importlib.import_module(module_path)
    mod.run(data_dir=data_dir)


@app.command()
def run(
    step: Annotated[
        str,
        typer.Argument(help=f"Step to run, or 'all'. Options: {', '.join(['all', *STEPS.keys()])}"),
    ],
    data_dir: DataDirOpt = None,
    skip_upload: Annotated[
        bool,
        typer.Option("--skip-upload", help="Skip the upload step when running 'all'."),
    ] = False,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--with",
            help=(
                "Add an opt-in step to 'all' — "
                f"{', '.join(sorted(OPTIONAL_STEPS))}, or 'all' for every one."
            ),
        ),
    ] = None,
) -> None:
    """Run a pipeline step (or 'all' to run the full pipeline)."""
    base = get_data_dir(data_dir)
    require_mount(base)

    if step == "all":
        steps = _resolve_run_order(include, skip_upload)
        console.print(f"\n[bold]Running full pipeline ({len(steps)} steps)[/bold]\n")
        for name in steps:
            _run_step(name, data_dir)
        console.print("\n[bold green]Pipeline complete![/bold green]")
    elif step in STEPS:
        _run_step(step, data_dir)
    else:
        console.print(f"[red]Unknown step:[/red] {step}")
        console.print(f"Available: all, {', '.join(STEPS.keys())}")
        raise typer.Exit(1)


@app.command("xyz-stage")
def xyz_stage(
    data_dir: DataDirOpt = None,
    shard: Annotated[int, typer.Option("--shard", help="This shard's index, from 0.")] = 0,
    of: Annotated[int, typer.Option("--of", help="How many shards the tree is split into.")] = 1,
    stage: Annotated[
        Path | None,
        typer.Option("--stage", help="Staging database path (default: beside the archives)."),
    ] = None,
    max_zoom: Annotated[int, typer.Option("--max-zoom")] = 12,
) -> None:
    """Render one shard of the flat tile tree, without publishing it.

    `run xyz` renders the whole tree and publishes it, which is right on one
    machine. This renders a stride of it into its own staging database so
    several machines can share the work; the shards are published together
    afterwards with `xyz-publish`.

    Put --stage on local disk when the tree lives on a network filesystem: the
    database is opened write-ahead, and WAL needs a shared-memory mapping that
    NFS and GPFS do not support.
    """
    import os

    # Set before the renderer is imported: how far a tile may reach for a
    # stand-in is derived from this at import time, and a tile that cannot
    # reach one is simply not written.
    os.environ.setdefault("BASEMAP_XYZ_MAX_ZOOM", str(max_zoom))

    from basemap.pipeline.xyz import Shard
    from basemap.pipeline.xyz import run as render

    base = get_data_dir(data_dir)
    require_mount(base)
    render(data_dir, max_zoom=max_zoom, shard=Shard(index=shard, count=of), stage=stage)


@app.command("xyz-publish")
def xyz_publish(
    data_dir: DataDirOpt = None,
    stages: Annotated[
        list[Path] | None,
        typer.Option("--stage", help="A staging database to publish. Repeatable."),
    ] = None,
    processes: Annotated[
        int | None,
        typer.Option("--processes", help="Sending processes (default: 1, or "
                     "$BASEMAP_XYZ_PUBLISH_PROCESSES)."),
    ] = None,
    workers: Annotated[
        int | None,
        typer.Option("--workers", help="Threads per process (default 64; higher is slower)."),
    ] = None,
    expect: Annotated[
        int | None,
        typer.Option("--expect", help="Refuse to start unless the stages hold "
                     "this many tiles."),
    ] = None,
    marks: Annotated[
        Path | None,
        typer.Option("--marks", help="Where to keep the resume marks "
                     "(default: beside the first stage)."),
    ] = None,
) -> None:
    """Publish staged flat tiles to S3.

    One process tops out near 400 PUT/s whatever its thread count, so --processes
    is the knob that matters: eight of them reach about 1,930/s. Resume is per
    column, so an interrupted run costs only the column in flight.
    """
    from basemap.pipeline.xyz import publish

    publish(
        data_dir,
        stages=list(stages) if stages else None,
        marks=marks,
        processes=processes,
        workers=workers,
        expect_tiles=expect,
    )


@app.command()
def status(data_dir: DataDirOpt = None) -> None:
    """Show status of generated tiles and intermediate files."""
    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)

    table = Table(title="Basemap Pipeline Status")
    table.add_column("Asset", style="bold")
    table.add_column("Status")
    table.add_column("Size")

    # GEBCO source
    gebco = get_gebco_tif(base)
    ok = "[green]✓[/green]"
    no = "[red]✗[/red]"
    table.add_row(f"GEBCO {GEBCO_RELEASE}", ok if gebco.exists() else no, file_size_mb(gebco))

    # Hillshade tiles
    hs_dir = tile_dir / "hillshade" / "0"
    table.add_row("Hillshade tiles", ok if hs_dir.exists() else no, "—")

    # PMTiles
    pmtiles_names = [
        "depth_zones", "depth_zones_eu", "depth_zones_hr", "depth_contours",
        "ocean_labels", "seamounts", "land_base", "sea_mask", "lakes", "boundaries",
        "bathy_global", "bathy_eu", "bathy_hr", "land_relief", "land_relief_eu",
    ]
    for name in pmtiles_names:
        p = tile_dir / f"{name}.pmtiles"
        table.add_row(f"{name}.pmtiles", ok if p.exists() else no, file_size_mb(p))

    console.print(table)
    console.print(f"\nData dir: {base}")
    console.print(f"Tile dir: {tile_dir}")


@app.command()
def serve(
    data_dir: DataDirOpt = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 9191,
) -> None:
    """Start a local tile server for visual inspection."""
    from basemap.server.serve import start_server

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)
    start_server(tile_dir, port=port)


@app.command()
def viewer(
    data_dir: DataDirOpt = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 9191,
    no_open: Annotated[bool, typer.Option("--no-open", help="Don't open the browser")] = False,
) -> None:
    """Launch the interactive tile viewer in the browser."""
    import threading
    import webbrowser

    from basemap.server.serve import start_server

    base = get_data_dir(data_dir)
    tile_dir = get_tile_dir(base)

    if not no_open:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}/")).start()

    start_server(tile_dir, port=port)


if __name__ == "__main__":
    app()
