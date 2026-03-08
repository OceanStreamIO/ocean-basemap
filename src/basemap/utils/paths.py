"""Path resolution, idempotency helpers, and prerequisite checks."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

console = Console()


def ensure_dir(path: Path) -> Path:
    """Create directory (and parents) if it doesn't exist. Returns *path*."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def skip_if_exists(path: Path, label: str | None = None) -> bool:
    """Return True (and print skip message) if *path* exists."""
    if path.exists():
        name = label or path.name
        console.print(f"  [dim]Skipping {name} (already exists)[/dim]")
        return True
    return False


def require_mount(path: Path) -> None:
    """Raise SystemExit if *path* doesn't exist (external drive not mounted)."""
    if not path.exists():
        console.print(f"[red bold]ERROR:[/] Data directory not found: {path}")
        console.print("[dim]Mount the external drive or set BASEMAP_DATA_DIR.[/dim]")
        raise SystemExit(1)


def resolve_gebco(data_dir: Path) -> Path:
    """Find the GEBCO GeoTIFF — prefer 2025, fall back to 2024."""
    candidates = [
        data_dir / "gebco2025" / "GEBCO_2025.tif",
        data_dir / "work" / "gebco_2024.tif",
    ]
    for p in candidates:
        if p.is_file():
            return p
    console.print("[red bold]ERROR:[/] No GEBCO GeoTIFF found.")
    console.print(f"  Expected one of: {', '.join(str(c) for c in candidates)}")
    raise SystemExit(1)


def file_size_mb(path: Path) -> str:
    """Human-readable file size in MB."""
    if path.is_file():
        return f"{path.stat().st_size / (1024 * 1024):.1f} MB"
    return "N/A"
