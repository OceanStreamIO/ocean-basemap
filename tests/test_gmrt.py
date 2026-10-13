"""Staging GMRT's masked swath grid."""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from basemap.pipeline import bathymetry, gmrt, xyz


# --- the request limit ------------------------------------------------------

def test_every_region_fits_one_request():
    """The service refuses more than 20 by 20 degrees, which is 2 GB at 100 m.

    A region over the limit fails in the first second of the step with its
    name, not an hour in with a server error.
    """
    for name, box in gmrt.REGIONS.items():
        gmrt.check_box(name, box)


def test_a_box_over_the_limit_or_inside_out_is_refused():
    with pytest.raises(SystemExit, match="spans more"):
        gmrt.check_box("wide", (-100.0, 8.0, -79.0, 31.0))
    with pytest.raises(SystemExit, match="inside out"):
        gmrt.check_box("flipped", (-80.0, 31.0, -100.0, 8.0))


def test_regions_are_chosen_not_defaulted(monkeypatch):
    """Unset means nothing, as BlueTopo's region does: a global pull from a
    shared research service is a decision, never a side effect."""
    monkeypatch.delenv("BASEMAP_GMRT_REGIONS", raising=False)
    assert gmrt.wanted_regions() == []
    monkeypatch.setenv("BASEMAP_GMRT_REGIONS", "hawaii us-west")
    assert gmrt.wanted_regions() == ["hawaii", "us-west"]
    monkeypatch.setenv("BASEMAP_GMRT_REGIONS", "atlantis")
    with pytest.raises(SystemExit, match="no such GMRT region"):
        gmrt.wanted_regions()


# --- staging one box --------------------------------------------------------

def _geotiff(values: np.ndarray, box) -> bytes:
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=values.shape[1], height=values.shape[0], count=1,
                      dtype="float32", crs="EPSG:4326",
                      transform=from_bounds(*box, values.shape[1], values.shape[0])) as ds:
            ds.write(values.astype("float32"), 1)       # no nodata tag, as the service sends it
        return mem.read()


class _Client:
    """Stands in for httpx: streams whatever bytes it was given."""

    def __init__(self, body: bytes):
        self.body = body
        self.params = None

    @contextmanager
    def stream(self, method, url, params=None):
        self.params = params
        body = self.body

        class Response:
            def raise_for_status(self):
                pass

            def iter_bytes(self, n):
                for i in range(0, len(body), n):
                    yield body[i:i + n]

        yield Response()


def test_a_staged_box_is_a_cog_with_nan_declared_as_nodata(tmp_path):
    """The service's GeoTIFF has NaN where there is no swath and no nodata tag,
    so read as it comes the warper would take the NaNs for elevation. Staged,
    the tag is set and the grid has overviews, like every other staged grid."""
    box = (-122.6, 36.4, -121.7, 37.0)
    # Wider than one COG block, or the driver has nothing to build overviews
    # from and the assertion below would be about the fixture.
    values = np.full((600, 1100), np.nan)
    values[:, :550] = -500.0                     # swath on the left half
    values[:8, :8] = 120.0                        # a corner of land
    client = _Client(_geotiff(values, box))
    dst = tmp_path / "monterey.tif"

    share = gmrt.stage_one("monterey", box, dst, client=client)

    assert client.params["layer"] == "topo-mask", "the masked layer, not the filled one"
    assert client.params["mresolution"] == gmrt.RESOLUTION_M
    assert dst.is_file() and not list(tmp_path.glob("*.dl.tif")) and not list(tmp_path.glob("*.part.tif"))
    with rasterio.open(dst) as ds:
        assert np.isnan(ds.nodata)
        assert ds.overviews(1), "no overviews: a z9 tile would read the whole box"
        data = ds.read(1, masked=True)
    assert data.mask[0, -1] and not data.mask[0, 0]
    assert share == pytest.approx(0.5, abs=0.01)


def test_a_reply_that_is_not_a_geotiff_is_an_error_and_leaves_nothing(tmp_path):
    dst = tmp_path / "x.tif"
    with pytest.raises(SystemExit, match="did not return a GeoTIFF"):
        gmrt.stage_one("x", (-10.0, 0.0, 0.0, 10.0), dst, client=_Client(b"<html>down</html>"))
    assert not list(tmp_path.iterdir())


# --- where it sits in the stack --------------------------------------------

def test_the_archive_shares_emodnets_band_and_sits_under_the_surveys():
    """bathy_gmrt is a z9-11 archive like bathy_eu, with its own bounds; in
    bathy_hr the swath grid comes after the surveys and EMODnet and before
    GEBCO, so a half-covered survey tile shows swath rather than 460 m."""
    assert bathymetry.GMRT_ZOOMS == (9, 11)
    source = inspect.getsource(bathymetry.specs)
    assert '"bathy_gmrt"' in source and "gmrt_sources(work)" in source
    hr = source[source.index('out["bathy_hr"]'):]
    assert hr.index("emodnet, *gmrt, gebco") > 0


def test_the_tree_tries_gmrt_after_emodnet_in_the_same_band():
    names = [n for _, n in xyz.SEA_BY_ZOOM]
    zooms = dict((n, z) for z, n in xyz.SEA_BY_ZOOM)
    assert names.index("bathy_eu") < names.index("bathy_gmrt") < names.index("bathy_global")
    assert zooms["bathy_gmrt"] == zooms["bathy_eu"] == 9


def test_the_step_is_registered():
    from basemap.cli import STEPS
    assert STEPS["gmrt"] == "basemap.pipeline.gmrt"
