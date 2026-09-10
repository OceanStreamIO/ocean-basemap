"""Tests for utils.download — HTTP download with progress, resume, and checksum."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from basemap.utils.download import download_file


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class _FakeResponse:
    """Minimal mock for httpx.stream() context manager."""

    def __init__(self, content: bytes, status_code: int = 200):
        self._content = content
        self.status_code = status_code
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _patch_stream(content: bytes, status_code: int = 200):
    resp = _FakeResponse(content, status_code)
    return patch("httpx.stream", return_value=resp)


def _patch_head(size: int | None):
    """Mock httpx.head so size verification doesn't hit the network."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {} if size is None else {"content-length": str(size)}
    return patch("httpx.head", return_value=resp)


class TestDownloadFile:
    def test_downloads_new_file(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        data = b"hello world"
        with _patch_stream(data):
            result = download_file("https://example.com/file.bin", dest)
        assert result == dest
        assert dest.read_bytes() == data

    def test_skip_existing_no_checksum(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        dest.write_bytes(b"existing")
        with _patch_head(len(b"existing")), patch("httpx.stream") as mock_stream:
            result = download_file("https://example.com/file.bin", dest)
        assert result == dest
        mock_stream.assert_not_called()

    def test_skip_existing_when_size_unknown(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        dest.write_bytes(b"existing")
        with _patch_head(None), patch("httpx.stream") as mock_stream:
            result = download_file("https://example.com/file.bin", dest)
        assert result == dest
        mock_stream.assert_not_called()

    def test_redownload_on_size_mismatch(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        dest.write_bytes(b"too much data here")
        data = b"good data"
        with _patch_head(len(data)), _patch_stream(data):
            result = download_file("https://example.com/file.bin", dest, resume=False)
        assert result == dest
        assert dest.read_bytes() == data

    def test_incomplete_download_raises(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        truncated = _FakeResponse(b"abc")
        truncated.headers = {"content-length": "99"}
        with patch("httpx.stream", return_value=truncated):
            with pytest.raises(RuntimeError, match="Incomplete download"):
                download_file("https://example.com/file.bin", dest)

    def test_skip_existing_checksum_match(self, tmp_path: Path):
        data = b"correct data"
        dest = tmp_path / "file.bin"
        dest.write_bytes(data)
        checksum = _md5(data)
        with patch("httpx.stream") as mock_stream:
            result = download_file("https://example.com/file.bin", dest, checksum=checksum)
        assert result == dest
        mock_stream.assert_not_called()

    def test_redownload_checksum_mismatch(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        dest.write_bytes(b"bad data")
        good_data = b"good data"
        good_md5 = _md5(good_data)
        with _patch_stream(good_data):
            result = download_file("https://example.com/file.bin", dest, checksum=good_md5)
        assert result == dest
        assert dest.read_bytes() == good_data

    def test_checksum_failure_deletes_file(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        bad_data = b"data that won't match"
        with _patch_stream(bad_data):
            with pytest.raises(RuntimeError, match="Checksum mismatch"):
                download_file(
                    "https://example.com/file.bin", dest,
                    checksum="0000000000000000000000000000dead",
                )
        assert not dest.exists()

    def test_creates_parent_dirs(self, tmp_path: Path):
        dest = tmp_path / "sub" / "dir" / "file.bin"
        data = b"nested"
        with _patch_stream(data):
            download_file("https://example.com/file.bin", dest)
        assert dest.read_bytes() == data
