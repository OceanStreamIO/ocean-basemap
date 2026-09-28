"""BASEMAP_OFFLINE: no network call, and no slow way to find that out.

Ten pipeline steps reach the network inside run(), not just `download`. On a
host with no route out, two of those were fatal in ways that looked like
something else: the Copernicus tile list had no cache, so the land relief died
on its first call however much DEM was already on disk; and download_file HEADed
the server for every file without a checksum -- every file but the GEBCO grid --
at 300 s each, which is five hours of timeouts for the surface-water tiles alone
when packets are dropped rather than refused.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

import basemap.config
from basemap.utils.download import Missing


@pytest.fixture
def offline(monkeypatch):
    """BASEMAP_OFFLINE, and a network that fails loudly if anything reaches it."""
    monkeypatch.setenv("BASEMAP_OFFLINE", "1")
    importlib.reload(basemap.config)

    def forbidden(*a, **kw):
        raise AssertionError("offline code touched the network")

    for name in ("head", "get", "stream", "Client"):
        monkeypatch.setattr(httpx, name, forbidden)
    yield
    monkeypatch.delenv("BASEMAP_OFFLINE", raising=False)
    importlib.reload(basemap.config)


def test_the_flag_reads_the_way_a_shell_writes_it(monkeypatch):
    for value, expected in [("", False), ("0", False), ("false", False), ("no", False),
                            ("1", True), ("true", True), ("yes", True)]:
        monkeypatch.setenv("BASEMAP_OFFLINE", value)
        importlib.reload(basemap.config)
        assert basemap.config.OFFLINE is expected, value
    monkeypatch.delenv("BASEMAP_OFFLINE", raising=False)
    importlib.reload(basemap.config)
    assert basemap.config.OFFLINE is False


def test_a_file_on_disk_is_returned_without_asking_the_server(tmp_path, offline):
    """This is the five-hour bug: the HEAD happened even when the file was there."""
    from basemap.utils import download as dl

    dest = tmp_path / "grid.tif"
    dest.write_bytes(b"x" * 32)
    assert dl.download_file("https://example.invalid/grid.tif", dest) == dest


def test_a_missing_file_fails_in_the_first_second_and_names_itself(tmp_path, offline):
    from basemap.utils import download as dl

    with pytest.raises(Missing) as caught:
        dl.download_file("https://example.invalid/grid.tif", tmp_path / "grid.tif")
    message = str(caught.value)
    assert "grid.tif" in message
    assert "https://example.invalid/grid.tif" in message
    assert "Stage it with the build inputs" in message


def test_a_checksum_is_not_a_reason_to_go_online(tmp_path, offline):
    """A staged file is trusted; there is nothing to re-fetch it from."""
    from basemap.utils import download as dl

    dest = tmp_path / "grid.tif"
    dest.write_bytes(b"x")
    assert dl.download_file("https://example.invalid/grid.tif", dest,
                            checksum="not-the-real-md5") == dest


def test_the_copernicus_tile_list_comes_from_its_cache(tmp_path, offline):
    from basemap.processing import copernicus

    cache = tmp_path / f"{copernicus.GLO90.name}.tileList.txt"
    cache.write_text("Copernicus_DSM_COG_30_N00_00_E006_00_DEM\n\n")
    assert copernicus.tile_list(copernicus.GLO90, client=None, cache_dir=tmp_path) == [
        "Copernicus_DSM_COG_30_N00_00_E006_00_DEM"
    ]


def test_an_absent_tile_list_is_an_error_not_a_request(tmp_path, offline):
    """Without this the land relief failed on its first call, with 135 GB staged."""
    from basemap.processing import copernicus

    with pytest.raises(Missing, match="tileList"):
        copernicus.tile_list(copernicus.GLO30, client=None, cache_dir=tmp_path)


def test_missing_dem_tiles_are_reported_once_not_once_per_thread(tmp_path, offline):
    from basemap.processing import copernicus

    with pytest.raises(Missing) as caught:
        copernicus.fetch(copernicus.GLO90, ["a", "b", "c"], tmp_path / "glo90")
    assert "3 of 3 tiles are missing" in str(caught.value)


def test_the_tile_list_is_cached_when_it_is_fetched(tmp_path, monkeypatch):
    """Online, the fetch writes the cache the offline host will read."""
    monkeypatch.delenv("BASEMAP_OFFLINE", raising=False)
    importlib.reload(basemap.config)
    from basemap.processing import copernicus

    class FakeResponse:
        text = "TILE_ONE\nTILE_TWO\n"

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url):
            assert url.endswith("/tileList.txt")
            return FakeResponse()

    got = copernicus.tile_list(copernicus.GLO30, FakeClient(), tmp_path)
    assert got == ["TILE_ONE", "TILE_TWO"]
    assert (tmp_path / f"{copernicus.GLO30.name}.tileList.txt").read_text() == FakeResponse.text
    # And the next call, offline, reads it back.
    monkeypatch.setenv("BASEMAP_OFFLINE", "1")
    importlib.reload(basemap.config)
    assert copernicus.tile_list(copernicus.GLO30, client=None, cache_dir=tmp_path) == got


def test_a_short_water_stage_is_refused(tmp_path, monkeypatch):
    """Offline, a tile never staged and a tile never published look identical.

    So the count is recorded where the downloading happened and asserted
    everywhere else -- otherwise the sea mask comes out with holes in it and
    nothing says so.
    """
    from basemap.pipeline import surface_water

    (tmp_path / ".tile-count").write_text("504\n")
    monkeypatch.setenv("BASEMAP_OFFLINE", "1")
    importlib.reload(basemap.config)

    with pytest.raises(SystemExit, match="renders a mask with holes"):
        surface_water._check_count(tmp_path, 400)
    surface_water._check_count(tmp_path, 504)          # the full set is fine
