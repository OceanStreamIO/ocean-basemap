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


class TestGeneratePmtilesMultilayer:
    def test_layer_specs(self, tmp_path: Path):
        layers = [
            {"file": tmp_path / "a.geojson", "layer": "major", "minzoom": 2},
            {"file": tmp_path / "b.geojson", "layer": "minor", "minzoom": 4, "maxzoom": 8},
        ]
        out = tmp_path / "output.pmtiles"
        with patch(_WHICH, return_value="/usr/bin/tippecanoe"), \
             patch(_RUN, return_value=_completed()) as run:
            generate_pmtiles_multilayer(layers, out, min_zoom=0, max_zoom=10)
            args = run.call_args[0][0]

            # Find -L arguments and parse the JSON specs
            l_indices = [i for i, a in enumerate(args) if a == "-L"]
            assert len(l_indices) == 2

            spec1 = json.loads(args[l_indices[0] + 1])
            assert spec1["layer"] == "major"
            assert spec1["minzoom"] == 2
            assert "maxzoom" not in spec1

            spec2 = json.loads(args[l_indices[1] + 1])
            assert spec2["layer"] == "minor"
            assert spec2["minzoom"] == 4
            assert spec2["maxzoom"] == 8
