"""Tests for config module — data directory resolution."""

from pathlib import Path

from basemap.config import (
    CEDA_BASE,
    DEPTH_BREAKS,
    DEPTH_COLORS,
    GEBCO_RELEASE,
    GEBCO_TILES,
    get_data_dir,
    get_tile_dir,
    get_work_dir,
)


def test_get_data_dir_override():
    assert get_data_dir("/custom/path") == Path("/custom/path")


def test_get_data_dir_env(monkeypatch):
    monkeypatch.setenv("BASEMAP_DATA_DIR", "/env/path")
    assert get_data_dir() == Path("/env/path")


def test_get_data_dir_default(monkeypatch):
    monkeypatch.delenv("BASEMAP_DATA_DIR", raising=False)
    result = get_data_dir()
    assert "basemap" in str(result)


def test_depth_breaks_ordered():
    assert DEPTH_BREAKS == sorted(DEPTH_BREAKS)
    assert DEPTH_BREAKS[0] == -11000
    assert DEPTH_BREAKS[-1] == 0


def test_depth_colors_match_breaks():
    """Each color entry should correspond to a depth break."""
    for depth in DEPTH_COLORS:
        assert depth in DEPTH_BREAKS, f"Color depth {depth} not in DEPTH_BREAKS"


def test_get_work_dir():
    base = Path("/data")
    assert get_work_dir(base) == Path(f"/data/gebco{GEBCO_RELEASE}/work")


def test_get_tile_dir():
    base = Path("/data")
    assert get_tile_dir(base) == Path(f"/data/gebco{GEBCO_RELEASE}/tiles")


def test_gebco_tiles_match_release():
    """Every tile filename is built from the configured release."""
    assert len(GEBCO_TILES) == 8
    for filename, _md5 in GEBCO_TILES:
        assert filename.startswith(f"gebco_{GEBCO_RELEASE}_")
        assert filename.endswith(".tif")


def test_ceda_base_matches_release():
    assert f"gebco_{GEBCO_RELEASE}" in CEDA_BASE
