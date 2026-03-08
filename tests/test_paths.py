"""Tests for utils.paths — path helpers and idempotency."""

from pathlib import Path

import pytest

from basemap.utils.paths import ensure_dir, file_size_mb, resolve_gebco, skip_if_exists


def test_ensure_dir(tmp_dir: Path):
    nested = tmp_dir / "a" / "b" / "c"
    result = ensure_dir(nested)
    assert result == nested
    assert nested.is_dir()


def test_skip_if_exists_true(tmp_dir: Path):
    f = tmp_dir / "existing.txt"
    f.write_text("data")
    assert skip_if_exists(f) is True


def test_skip_if_exists_false(tmp_dir: Path):
    f = tmp_dir / "missing.txt"
    assert skip_if_exists(f) is False


def test_file_size_mb(tmp_dir: Path):
    f = tmp_dir / "test.bin"
    f.write_bytes(b"\x00" * (1024 * 1024))  # 1 MB
    assert file_size_mb(f) == "1.0 MB"


def test_file_size_mb_missing(tmp_dir: Path):
    f = tmp_dir / "nope.bin"
    assert file_size_mb(f) == "N/A"


def test_resolve_gebco_2025(tmp_dir: Path):
    gebco = tmp_dir / "gebco2025" / "GEBCO_2025.tif"
    gebco.parent.mkdir(parents=True)
    gebco.write_text("fake")
    assert resolve_gebco(tmp_dir) == gebco


def test_resolve_gebco_2024_fallback(tmp_dir: Path):
    gebco = tmp_dir / "work" / "gebco_2024.tif"
    gebco.parent.mkdir(parents=True)
    gebco.write_text("fake")
    assert resolve_gebco(tmp_dir) == gebco


def test_resolve_gebco_missing(tmp_dir: Path):
    with pytest.raises(SystemExit):
        resolve_gebco(tmp_dir)
