"""Subprocess wrappers for tippecanoe tile generation."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

console = Console()


def _require(tool: str = "tippecanoe") -> str:
    path = shutil.which(tool)
    if not path:
        console.print(f"[red bold]ERROR:[/] '{tool}' not found on PATH.")
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
    # tippecanoe silently ignores minzoom/maxzoom inside -L, so a single run would
    # tile every input across the whole range and stack coarse sources on top of
    # fine ones. Tile each distinct range on its own, then merge.
    bands: dict[tuple[int, int], list[dict]] = {}
    for spec in layers:
        band = (spec.get("minzoom", min_zoom), spec.get("maxzoom", max_zoom))
        bands.setdefault(band, []).append(spec)

    if len(bands) == 1:
        (lo, hi), specs = next(iter(bands.items()))
        return _tippecanoe_band(specs, output, lo, hi, extra_args)

    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        parts = [
            _tippecanoe_band(specs, Path(tmp) / f"z{lo}-{hi}.mbtiles", lo, hi, extra_args)
            for (lo, hi), specs in sorted(bands.items())
        ]
        join = [
            _require("tile-join"),
            "-o", str(output),
            "--force", "--no-tile-size-limit",
            *[str(p) for p in parts],
        ]
        _run(join, label=f"Join {len(parts)} zoom bands → {output.name}")
    return output


def _tippecanoe_band(
    specs: list[dict],
    output: Path,
    lo: int,
    hi: int,
    extra_args: list[str] | None,
) -> Path:
    args = [
        _require(),
        "-o", str(output),
        f"-z{hi}", f"-Z{lo}",
        "--force",
    ]
    if extra_args:
        args.extend(extra_args)
    for spec in specs:
        args.extend(["-L", json.dumps({"file": str(spec["file"]), "layer": spec["layer"]})])

    names = ", ".join(sorted({s["layer"] for s in specs}))
    _run(args, label=f"Tippecanoe z{lo}-{hi} ({names}) → {output.name}")
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
