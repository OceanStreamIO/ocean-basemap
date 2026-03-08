"""HTTP download helpers with progress bars, resume, and checksum verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()

_DOWNLOAD_PROGRESS_COLUMNS = (
    TextColumn("[bold blue]{task.fields[filename]}"),
    BarColumn(),
    DownloadColumn(),
    TransferSpeedColumn(),
    TimeRemainingColumn(),
)


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    url: str,
    dest: Path,
    *,
    checksum: str | None = None,
    resume: bool = True,
    label: str | None = None,
    timeout: float = 300,
) -> Path:
    """Download *url* to *dest* with progress bar.

    - Skips if *dest* exists and (if given) *checksum* matches.
    - Supports HTTP resume via Range header when *resume* is True.
    """
    filename = label or dest.name

    if dest.is_file():
        if checksum:
            if _md5_file(dest) == checksum:
                console.print(f"  [dim]{filename}: checksum OK, skipping[/dim]")
                return dest
            console.print(f"  [yellow]{filename}: checksum mismatch, re-downloading[/yellow]")
            dest.unlink()
        else:
            console.print(f"  [dim]{filename}: exists, skipping[/dim]")
            return dest

    dest.parent.mkdir(parents=True, exist_ok=True)

    headers: dict[str, str] = {}
    initial_size = 0
    mode = "wb"
    if resume and dest.is_file():
        initial_size = dest.stat().st_size
        headers["Range"] = f"bytes={initial_size}-"
        mode = "ab"

    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) + initial_size

        with Progress(*_DOWNLOAD_PROGRESS_COLUMNS, console=console) as progress:
            task = progress.add_task("download", total=total or None, filename=filename)
            if initial_size:
                progress.advance(task, initial_size)

            with open(dest, mode) as f:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
                    progress.advance(task, len(chunk))

    if checksum:
        actual = _md5_file(dest)
        if actual != checksum:
            console.print(f"  [red]Checksum mismatch for {filename}[/red]")
            console.print(f"    expected: {checksum}")
            console.print(f"    got:      {actual}")
            dest.unlink()
            raise RuntimeError(f"Checksum mismatch: {filename}")

    return dest
