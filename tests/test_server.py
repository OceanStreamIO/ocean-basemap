"""Tests for server.serve — tile server handler logic."""

from __future__ import annotations

import io
from http.server import HTTPServer
from pathlib import Path

import pytest

from basemap.server.serve import STATIC_DIR, make_handler


class _FakeRequest(io.BytesIO):
    """Writable stream that captures what the handler writes."""

    def makefile(self, *args, **kwargs):
        return self


def _make_request(handler_cls, method: str, path: str, headers: dict | None = None):
    """Simulate an HTTP request against the handler without binding a socket.

    Returns (status_code, response_headers dict, body bytes).
    """
    # Build raw request
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("")
    raw = "\r\n".join(lines).encode()

    rfile = io.BytesIO(raw)
    wfile = io.BytesIO()

    # Patch the handler so it doesn't try to use a real socket
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 12345)
    handler.server = HTTPServer.__new__(HTTPServer)
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.headers = {}
    handler.close_connection = True

    # Parse headers from raw
    from email.parser import Parser

    header_text = "\r\n".join(lines[1:])
    handler.headers = Parser().parsestr(header_text)

    # Call the appropriate method
    if method == "GET":
        handler.do_GET()
    elif method == "HEAD":
        handler.do_HEAD()
    elif method == "OPTIONS":
        handler.do_OPTIONS()

    wfile.seek(0)
    response = wfile.read()

    # Parse response
    resp_str = response.decode("utf-8", errors="replace")
    status_line = resp_str.split("\r\n", 1)[0]
    status_code = int(status_line.split(" ", 2)[1])

    # Parse response headers
    header_section, _, body = response.partition(b"\r\n\r\n")
    resp_headers = {}
    for line in header_section.decode().split("\r\n")[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            resp_headers[k] = v

    return status_code, resp_headers, body


@pytest.fixture
def tile_dir(tmp_path: Path) -> Path:
    """Create a fake tile directory with test files."""
    d = tmp_path / "tiles"
    d.mkdir()
    (d / "hillshade").mkdir()
    (d / "hillshade" / "0").mkdir()
    tile_file = d / "hillshade" / "0" / "test.png"
    tile_file.write_bytes(b"\x89PNG" + b"\x00" * 100)

    pmtiles = d / "test.pmtiles"
    pmtiles.write_bytes(b"\x00" * 200)

    return d


@pytest.fixture
def handler_cls(tile_dir: Path):
    return make_handler(tile_dir)


class TestPathResolution:
    def test_root_resolves_to_index(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(handler_cls, "GET", "/")
        # Should serve index.html if it exists in STATIC_DIR
        if (STATIC_DIR / "index.html").exists():
            assert status == 200
        else:
            assert status == 404

    def test_tiles_path(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "GET", "/tiles/hillshade/0/test.png",
        )
        assert status == 200
        assert b"\x89PNG" in body

    def test_missing_tile_404(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "GET", "/tiles/nonexistent.pmtiles",
        )
        assert status == 404


class TestCORS:
    def test_cors_on_get(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "GET", "/tiles/hillshade/0/test.png",
        )
        assert headers.get("Access-Control-Allow-Origin") == "*"

    def test_options_204(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(handler_cls, "OPTIONS", "/tiles/test.pmtiles")
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "*"


class TestRangeRequests:
    def test_range_request_206(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "GET", "/tiles/test.pmtiles",
            headers={"Range": "bytes=0-9"},
        )
        assert status == 206
        assert "Content-Range" in headers
        assert len(body) == 10

    def test_full_request_200(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "GET", "/tiles/test.pmtiles",
        )
        assert status == 200
        assert len(body) == 200


class TestHeadRequest:
    def test_head_no_body(self, handler_cls, tile_dir: Path):
        status, headers, body = _make_request(
            handler_cls, "HEAD", "/tiles/hillshade/0/test.png",
        )
        assert status == 200
        assert len(body) == 0
