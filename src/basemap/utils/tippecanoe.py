"""Subprocess wrappers for tippecanoe tile generation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()


def _require() -> str:
    path = shutil.which("tippecanoe")
    if not path:
        console.print("[red bold]ERROR:[/] 'tippecanoe' not found on PATH.")
        console.print("[dim]Install with:[/dim]")
        console.print("[dim]  macOS:  brew install tippecanoe[/dim]")
        console.print(
            "[dim]  Ubuntu: sudo apt install build-essential libsqlite3-dev zlib1g-dev[/dim]"
        )
        console.print(
            "[dim]          git clone https://github.com/felt/tippecanoe.git && cd tippecanoe[/dim]"
        )
        console.print("[dim]          make -j && sudo make install[/dim]")
        raise SystemExit(1)
    return path


def generate_pmtiles(
    inputs: list[Path] | Path,
    output: Path,
    *,
    layer: str | None = None,
    min_zoom: int = 0,
    max_zoom: int = 10,
    drop_densest: bool = False,
    drop_smallest: bool = False,
    coalesce_densest: bool = False,
    extend_zooms: bool = False,
    extra_args: list[str] | None = None,
) -> Path:
    """Generate a PMTiles archive from one or more GeoJSON/FGB inputs.

    For single-layer simple cases, pass a ``layer`` name.
    For multi-layer builds, use :func:`generate_pmtiles_multilayer` instead.
    """
    if isinstance(inputs, Path):
        inputs = [inputs]

    args = [
        _require(),
        "-o", str(output),
        f"-z{max_zoom}", f"-Z{min_zoom}",
        "--force",
    ]
    if layer:
        args.extend(["-l", layer])
    if drop_densest:
        args.append("--drop-densest-as-needed")
    if drop_smallest:
        args.append("--drop-smallest-as-needed")
    if coalesce_densest:
        args.append("--coalesce-densest-as-needed")
    if extend_zooms:
        args.append("--extend-zooms-if-still-dropping")
    if extra_args:
        args.extend(extra_args)

    args.extend([str(p) for p in inputs])
    _run(args, label=f"Tippecanoe → {output.name}")
    return output


def generate_pmtiles_multilayer(
    layers: list[dict],
    output: Path,
    *,
    min_zoom: int = 0,
    max_zoom: int = 10,
    extra_args: list[str] | None = None,
) -> Path:
    """Generate a PMTiles archive with multiple named layers.

    Each dict in *layers* has keys:
      - ``file``:  Path to input GeoJSON / FGB
      - ``layer``: layer name in the output
      - ``minzoom`` / ``maxzoom``: (optional) per-layer zoom range
    """
    args = [
        _require(),
        "-o", str(output),
        f"-z{max_zoom}", f"-Z{min_zoom}",
        "--force",
    ]
    if extra_args:
        args.extend(extra_args)

    # Build -L named-layer specs
    for spec in layers:
        layer_def: dict = {"file": str(spec["file"]), "layer": spec["layer"]}
        if "minzoom" in spec:
            layer_def["minzoom"] = spec["minzoom"]
        if "maxzoom" in spec:
            layer_def["maxzoom"] = spec["maxzoom"]
        args.extend(["-L", json.dumps(layer_def)])

    _run(args, label=f"Tippecanoe multi-layer → {output.name}")
    return output


def _run(args: list[str], *, label: str | None = None) -> subprocess.CompletedProcess[str]:
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
        raise RuntimeError(f"tippecanoe failed with exit code {result.returncode}")
    return result
