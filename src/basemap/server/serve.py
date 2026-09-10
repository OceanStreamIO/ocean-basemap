"""Local basemap tile server — serves static assets + tiles with Range/CORS support."""

from __future__ import annotations

import http.server
import socketserver
from pathlib import Path
from urllib.parse import unquote

from rich.console import Console

console = Console()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Handle each request in a separate thread for concurrent tile fetches."""
    daemon_threads = True

# Static assets from the viewer package
STATIC_DIR = Path(__file__).resolve().parent.parent / "viewer"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".pmtiles": "application/octet-stream",
    ".pbf": "application/x-protobuf",
    ".svg": "image/svg+xml",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}


def make_handler(tile_dir: Path):
    class TileHandler(http.server.BaseHTTPRequestHandler):
        def _cors(self):
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)

        def _serve_file(self, fpath: Path, head_only: bool = False):
            ext = fpath.suffix.lower()
            ctype = MIME_TYPES.get(ext, "application/octet-stream")
            fsize = fpath.stat().st_size

            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                parts = range_hdr[6:].split("-", 1)
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else fsize - 1
                end = min(end, fsize - 1)
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                self.send_header("Accept-Ranges", "bytes")
                self._cors()
                self.end_headers()

                if not head_only:
                    with open(fpath, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(fsize))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self._cors()
                self.end_headers()

                if not head_only:
                    with open(fpath, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)

        def _resolve(self, request_path: str) -> Path | None:
            path = unquote(request_path.split("?")[0].split("#")[0])

            if path in ("/", "/index.html"):
                return STATIC_DIR / "index.html"

            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                fpath = STATIC_DIR / rel
                if fpath.is_file() and STATIC_DIR in fpath.resolve().parents:
                    return fpath

            if path.startswith("/tiles/"):
                rel = path[len("/tiles/"):]
                fpath = tile_dir / rel
                if fpath.is_file():
                    return fpath

            return None

        def do_GET(self):
            fpath = self._resolve(self.path)
            if fpath:
                self._serve_file(fpath)
            else:
                self.send_error(404)

        def do_HEAD(self):
            fpath = self._resolve(self.path)
            if fpath:
                self._serve_file(fpath, head_only=True)
            else:
                self.send_error(404)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def log_message(self, fmt, *args):
            status = str(args[1]) if len(args) > 1 else ""
            path = str(args[0]) if args else ""
            if "404" in status or not any(
                x in path for x in ("/tiles/", ".png", ".pmtiles", ".pbf")
            ):
                super().log_message(fmt, *args)

    return TileHandler


def start_server(tile_dir: Path, *, port: int = 9191) -> None:
    if not tile_dir.exists():
        console.print(f"[red]Tile directory not found:[/red] {tile_dir}")
        raise SystemExit(1)

    console.print(f"Tile directory : {tile_dir}")
    console.print(f"Static assets  : {STATIC_DIR}")

    console.print("Available tiles:")
    for f in sorted(tile_dir.iterdir()):
        if f.name.startswith("."):
            continue
        if f.is_file():
            mb = f.stat().st_size / (1024 * 1024)
            console.print(f"  {f.name} ({mb:.1f} MB)")
        elif f.is_dir():
            console.print(f"  {f.name}/ (tile tree)")

    console.print(f"\n→ http://localhost:{port}/\n")

    server = ThreadedHTTPServer(("", port), make_handler(tile_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\nStopped.")
        server.server_close()
