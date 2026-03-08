"""Tests for config module — data directory resolution."""

from pathlib import Path

from basemap.config import DEPTH_BREAKS, DEPTH_COLORS, get_data_dir


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
