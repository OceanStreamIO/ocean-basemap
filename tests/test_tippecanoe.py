"""Tests for utils.tippecanoe — tippecanoe subprocess wrappers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from basemap.utils.tippecanoe import generate_pmtiles, generate_pmtiles_multilayer

_WHICH = "shutil.which"
_RUN = "basemap.utils.tippecanoe._run"


def _completed():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestRequireTippecanoe:
    def test_missing(self):
        with patch(_WHICH, return_value=None):
            with pytest.raises(SystemExit):
                generate_pmtiles(Path("in.geojson"), Path("out.pmtiles"))


class TestGeneratePmtiles:
    def test_basic(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(src, out, layer="depth", min_zoom=2, max_zoom=8)
            args = run.call_args[0][0]
            assert "-o" in args
            assert str(out) in args
            assert "-z8" in args
            assert "-Z2" in args
            assert "-l" in args
            assert "depth" in args

    def test_single_path_input(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(src, out)
            args = run.call_args[0][0]
            assert str(src) in args

    def test_flags(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(
                src, out,
                drop_densest=True, drop_smallest=True,
                coalesce_densest=True, extend_zooms=True,
            )
            args = run.call_args[0][0]
            assert "--drop-densest-as-needed" in args
            assert "--drop-smallest-as-needed" in args
            assert "--coalesce-densest-as-needed" in args
            assert "--extend-zooms-if-still-dropping" in args

    def test_extra_args(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(src, out, extra_args=["--simplification=2"])
            args = run.call_args[0][0]
            assert "--simplification=2" in args


    def test_detail_sets_full_detail(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(src, out, detail=15)
            assert "--full-detail=15" in run.call_args[0][0]

    def test_detail_is_omitted_by_default(self, tmp_path: Path):
        src = tmp_path / "input.geojson"
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles(src, out)
            assert not any(str(a).startswith("--full-detail") for a in run.call_args[0][0])


class TestGeneratePmtilesMultilayer:
    def test_each_zoom_range_is_tiled_separately(self, tmp_path: Path):
        """tippecanoe ignores minzoom/maxzoom inside -L, so ranges become separate runs."""
        layers = [
            {"file": tmp_path / "a.geojson", "layer": "major", "minzoom": 2},
            {"file": tmp_path / "b.geojson", "layer": "minor", "minzoom": 4, "maxzoom": 8},
        ]
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, side_effect=lambda tool: f"/usr/bin/{tool}"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles_multilayer(layers, out, min_zoom=0, max_zoom=10)

        calls = [c[0][0] for c in run.call_args_list]
        assert len(calls) == 3, "two zoom bands plus the join"

        band_2_10, band_4_8, join = calls

        # The zoom range reaches tippecanoe as -Z/-z, never as -L JSON.
        assert "-Z2" in band_2_10 and "-z10" in band_2_10
        assert "-Z4" in band_4_8 and "-z8" in band_4_8

        for band, layer in ((band_2_10, "major"), (band_4_8, "minor")):
            specs = [json.loads(band[i + 1]) for i, a in enumerate(band) if a == "-L"]
            assert [s["layer"] for s in specs] == [layer]
            assert all("minzoom" not in s and "maxzoom" not in s for s in specs)

        assert join[0].endswith("tile-join")
        assert join[join.index("-o") + 1] == str(out)

    def test_single_zoom_range_skips_the_join(self, tmp_path: Path):
        layers = [
            {"file": tmp_path / "a.geojson", "layer": "major"},
            {"file": tmp_path / "b.geojson", "layer": "minor"},
        ]
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, side_effect=lambda tool: f"/usr/bin/{tool}"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles_multilayer(layers, out, min_zoom=0, max_zoom=10)

        assert run.call_count == 1
        args = run.call_args[0][0]
        assert args[args.index("-o") + 1] == str(out)
        assert len([a for a in args if a == "-L"]) == 2

    def test_detail_reaches_every_zoom_band(self, tmp_path: Path):
        out = tmp_path / "out.pmtiles"
        specs = [
            {"file": tmp_path / "coarse.geojson", "layer": "land", "minzoom": 0, "maxzoom": 4},
            {"file": tmp_path / "fine.fgb", "layer": "land", "minzoom": 5, "maxzoom": 10},
        ]
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles_multilayer(specs, out, detail=15)
            # _require() is mocked for tile-join too, so the join call looks
            # like tippecanoe by its path. Only the band runs carry -Z.
            bands = [
                c[0][0] for c in run.call_args_list
                if any(str(a).startswith("-Z") for a in c[0][0])
            ]
            assert len(bands) == 2
            assert all("--full-detail=15" in args for args in bands)
