"""Tests for pipeline.emodnet — WCS request grid and chunk fetching."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from basemap.pipeline.emodnet import _fetch_chunk, chunk_bboxes, chunk_name, coverage_url


class TestChunkBboxes:
    def test_exact_division(self):
        boxes = chunk_bboxes((0.0, 0.0, 10.0, 10.0), 5.0)
        assert len(boxes) == 4
        assert (0.0, 0.0, 5.0, 5.0) in boxes
        assert (5.0, 5.0, 10.0, 10.0) in boxes

    def test_clamps_to_extent(self):
        boxes = chunk_bboxes((0.0, 0.0, 7.0, 3.0), 5.0)
        assert boxes == [(0.0, 0.0, 5.0, 3.0), (5.0, 0.0, 7.0, 3.0)]

    def test_covers_full_extent(self):
        extent = (-36.0, 15.0, 43.0, 84.0)
        boxes = chunk_bboxes(extent, 5.0)
        assert min(b[0] for b in boxes) == extent[0]
        assert min(b[1] for b in boxes) == extent[1]
        assert max(b[2] for b in boxes) == extent[2]
        assert max(b[3] for b in boxes) == extent[3]

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            chunk_bboxes((0.0, 0.0, 1.0, 1.0), 0)


def test_coverage_url_uses_lat_long_subsets():
    url = coverage_url((3.0, 53.0, 4.0, 54.0))
    assert "request=GetCoverage" in url
    assert "subset=Lat(53,54)" in url
    assert "subset=Long(3,4)" in url
    assert "format=image/tiff" in url


def test_chunk_name():
    assert chunk_name((3.0, 53.0, 4.0, 54.0)) == "emodnet_n54_s53_w3_e4.tif"


class _FakeStream:
    def __init__(self, content: bytes, content_type: str):
        self._content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        pass

    def read(self):
        return self._content

    def iter_bytes(self, chunk_size: int = 65536):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_fetch_chunk_writes_tiff(tmp_path: Path):
    dest = tmp_path / "chunk.tif"
    with patch("httpx.stream", return_value=_FakeStream(b"II*\x00data", "image/tiff")):
        result = _fetch_chunk((0.0, 0.0, 1.0, 1.0), dest)
    assert result == dest
    assert dest.read_bytes() == b"II*\x00data"


def test_fetch_chunk_skips_xml_exception(tmp_path: Path):
    dest = tmp_path / "chunk.tif"
    body = b"<ExceptionReport>No such coverage</ExceptionReport>"
    with patch("httpx.stream", return_value=_FakeStream(body, "application/xml")):
        result = _fetch_chunk((0.0, 0.0, 1.0, 1.0), dest)
    assert result is None
    assert not dest.exists()


def test_fetch_chunk_skips_existing(tmp_path: Path):
    dest = tmp_path / "chunk.tif"
    dest.write_bytes(b"already here")
    with patch("httpx.stream") as mock_stream:
        result = _fetch_chunk((0.0, 0.0, 1.0, 1.0), dest)
    assert result == dest
    mock_stream.assert_not_called()
