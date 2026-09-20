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


def _remote_size(url: str, timeout: float) -> int | None:
    """Content-length reported by the server, or None if unavailable."""
    try:
        r = httpx.head(url, follow_redirects=True, timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    length = r.headers.get("content-length")
    return int(length) if length else None


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

    - Skips if *dest* exists and matches *checksum*, or — when no checksum is
      known — the size the server reports.
    - Resumes partial downloads via Range header when *resume* is True.
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
            local_size = dest.stat().st_size
            expected = _remote_size(url, timeout)
            if expected is None or local_size == expected:
                console.print(f"  [dim]{filename}: exists, skipping[/dim]")
                return dest
            if local_size > expected or not resume:
                console.print(
                    f"  [yellow]{filename}: size mismatch "
                    f"({local_size} != {expected}), re-downloading[/yellow]"
                )
                dest.unlink()
            else:
                console.print(
                    f"  [yellow]{filename}: incomplete "
                    f"({local_size} of {expected} bytes), resuming[/yellow]"
                )

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
        # A compressed body is decoded on the way to disk, so Content-Length
        # describes the transfer rather than the file; there is nothing to compare.
        if r.headers.get("content-encoding"):
            total = 0
        else:
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
    elif total and dest.stat().st_size != total:
        console.print(f"  [red]Incomplete download for {filename}[/red]")
        console.print(f"    expected: {total} bytes")
        console.print(f"    got:      {dest.stat().st_size} bytes")
        raise RuntimeError(f"Incomplete download: {filename}")

    return dest


def download_shapefile(name: str, url: str, work_dir: Path) -> Path:
    """A zipped shapefile, downloaded and extracted once into *work_dir*.

    Natural Earth ships everything this way, and several steps want a different
    layer of it, so the fetch-and-unzip lives here rather than in each of them.
    """
    import zipfile

    shp = work_dir / f"{name}.shp"
    if shp.is_file():
        console.print(f"  [dim]{name} already extracted.[/dim]")
        return shp
    archive = work_dir / f"{name}.zip"
    download_file(url, archive, label=name)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work_dir)
    return shp
