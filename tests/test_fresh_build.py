"""What a build from a fresh clone needs and used to trip on.

Each of these once stopped a first `oceanbase run all` on a machine that was
not the maintainers': a default data directory on their drive, GEBCO without
the overviews the bathymetry reads, a land relief and a flat tree that wanted
steps `run all` did not run, and a flat tree that published itself.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from typer.testing import CliRunner

from basemap.cli import FULL_ORDER, RUN_ORDER, app
from basemap.utils.overviews import ensure_overviews, overview_path

runner = CliRunner()


def _grid(path: Path, width: int = 512, height: int = 256) -> Path:
    data = np.arange(width * height, dtype=np.int16).reshape(height, width)
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=1, dtype="int16",
        crs="EPSG:4326", transform=from_bounds(-180, -90, 180, 90, width, height),
        compress="lzw", tiled=True,
    ) as ds:
        ds.write(data, 1)
    return path


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# -- the data directory -------------------------------------------------------


def test_default_data_dir_is_in_the_home_directory(monkeypatch):
    from basemap.config import get_data_dir

    monkeypatch.delenv("BASEMAP_DATA_DIR", raising=False)
    assert get_data_dir() == Path.home() / "basemap-data"


def test_run_makes_the_default_data_dir(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.delenv("BASEMAP_DATA_DIR", raising=False)
    with patch("basemap.cli.get_data_dir", return_value=home / "basemap-data"), \
         patch("basemap.cli._run_step"):
        result = runner.invoke(app, ["run", "hillshade"])
    assert result.exit_code == 0, result.output
    assert (home / "basemap-data").is_dir()


def test_run_does_not_make_a_named_data_dir(tmp_path: Path, monkeypatch):
    """A named directory that is missing may be a drive that is not mounted."""
    named = tmp_path / "drive" / "basemap"
    monkeypatch.setenv("BASEMAP_DATA_DIR", str(named))
    with patch("basemap.cli._run_step"):
        result = runner.invoke(app, ["run", "hillshade"])
    assert result.exit_code != 0
    assert not named.exists()


# -- GEBCO's overviews --------------------------------------------------------


def test_overviews_are_built_beside_the_grid(tmp_path: Path):
    grid = _grid(tmp_path / "GEBCO_2026.tif")
    before = _md5(grid)
    ovr = ensure_overviews(grid, (2, 4, 8))
    assert ovr == overview_path(grid) and ovr.is_file()
    with rasterio.open(grid) as ds:
        assert ds.overviews(1) == [2, 4, 8]
    # The grid itself is not written, and nothing temporary is left behind.
    assert _md5(grid) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["GEBCO_2026.tif", "GEBCO_2026.tif.ovr"]


def test_overviews_already_there_are_kept(tmp_path: Path):
    grid = _grid(tmp_path / "g.tif")
    ovr = ensure_overviews(grid, (2, 4))
    stamp = ovr.stat().st_mtime_ns
    ensure_overviews(grid, (2, 4))
    assert ovr.stat().st_mtime_ns == stamp


def test_an_interrupted_build_is_started_again(tmp_path: Path):
    """What an interrupted build leaves has the temporary name, never the real one."""
    grid = _grid(tmp_path / "g.tif")
    (tmp_path / "g.tif.building.tif").symlink_to("g.tif")
    (tmp_path / "g.tif.building.tif.ovr").write_bytes(b"cut short")
    ensure_overviews(grid, (2, 4))
    with rasterio.open(grid) as ds:
        assert ds.overviews(1) == [2, 4]
    assert not (tmp_path / "g.tif.building.tif").exists()


def test_download_adds_overviews_to_a_grid_already_there(tmp_path: Path):
    from basemap.config import get_gebco_tif
    from basemap.pipeline import download

    grid = get_gebco_tif(tmp_path)
    grid.parent.mkdir(parents=True)
    _grid(grid)
    with patch.object(download, "download_file") as fetch:
        download.run(tmp_path)
    fetch.assert_not_called()
    assert overview_path(grid).is_file()


def test_bathymetry_builds_missing_overviews(tmp_path: Path):
    from basemap.config import get_gebco_tif
    from basemap.pipeline.bathymetry import gebco_source

    grid = get_gebco_tif(tmp_path)
    grid.parent.mkdir(parents=True)
    _grid(grid)
    assert gebco_source(tmp_path).uri == str(grid)
    assert overview_path(grid).is_file()


# -- the steps run all runs ---------------------------------------------------


def test_run_all_stages_the_land_cover_before_the_land():
    assert "worldcover" in RUN_ORDER
    assert FULL_ORDER.index("worldcover") < FULL_ORDER.index("hypsometric")
    assert FULL_ORDER.index("worldcover") < FULL_ORDER.index("globe")


def test_run_all_stages_the_rivers_before_the_tree():
    assert "rivers" in RUN_ORDER
    # Cut to the OSM land that land-tiles downloads, and read by the tree.
    assert FULL_ORDER.index("land-tiles") < FULL_ORDER.index("rivers") < FULL_ORDER.index("xyz")


def test_rivers_fetch_the_published_network(tmp_path: Path, monkeypatch):
    from basemap.pipeline import merit_rivers, rivers
    from basemap.utils import download

    monkeypatch.setattr("basemap.config.OFFLINE", False)
    fetched: list[tuple[str, Path]] = []

    def fake_download(url, dest, **kwargs):
        fetched.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"network")
        return dest

    monkeypatch.setattr(download, "download_file", fake_download)
    monkeypatch.setattr(merit_rivers, "is_current", lambda path: True)
    fgb = rivers._network(tmp_path)
    assert fetched == [(merit_rivers.PUBLISHED_NETWORK, fgb.with_name("merit_rivers.fgb.part"))]
    assert fgb.read_bytes() == b"network"
    assert not fgb.with_name("merit_rivers.fgb.part").exists()


def test_rivers_offline_do_not_fetch(tmp_path: Path, monkeypatch):
    from basemap.pipeline import rivers
    from basemap.utils import download

    monkeypatch.setattr("basemap.config.OFFLINE", True)
    monkeypatch.setattr(download, "download_file", lambda *a, **k: pytest.fail("fetched"))
    with pytest.raises(SystemExit):
        rivers._network(tmp_path)


# -- publishing ---------------------------------------------------------------


def test_skip_upload_keeps_the_tree_on_disk(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BASEMAP_XYZ_STAGE_ONLY", raising=False)
    seen: dict[str, str | None] = {}

    def fake_run_step(name, data_dir):
        if name == "xyz":
            seen["stage_only"] = os.environ.get("BASEMAP_XYZ_STAGE_ONLY")

    with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
         patch("basemap.cli._run_step", side_effect=fake_run_step):
        result = runner.invoke(app, ["run", "all", "--skip-upload"])
    assert result.exit_code == 0, result.output
    assert seen["stage_only"] == "1"
    # For the run only.
    assert "BASEMAP_XYZ_STAGE_ONLY" not in os.environ


def test_publishing_without_credentials_stops_first(monkeypatch):
    from basemap.pipeline import publish

    monkeypatch.delenv("BASEMAP_S3_CREDENTIALS_DIR", raising=False)

    class NoCredentials:
        def get_credentials(self):
            return None

    monkeypatch.setattr("boto3.Session", NoCredentials)
    with pytest.raises(SystemExit, match="--skip-upload"):
        publish.require_credentials()


def test_publishing_with_credentials_goes_ahead(monkeypatch, tmp_path: Path):
    from basemap.pipeline import publish

    (tmp_path / "AWS_ACCESS_KEY_ID").write_text("key")
    (tmp_path / "AWS_SECRET_ACCESS_KEY").write_text("secret")
    monkeypatch.setenv("BASEMAP_S3_CREDENTIALS_DIR", str(tmp_path))
    publish.require_credentials()
