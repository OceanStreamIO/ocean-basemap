"""The checks export-style.py makes before a style is allowed out.

The style is exported from the running viewer, so its content is the viewer's
business. What this guards is the step after: a style naming archives that
are no longer on the server was published once and sat there for a month,
drawing no bathymetry at all, because nothing looked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export-style.py"


def _module():
    spec = importlib.util.spec_from_file_location("export_style", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STYLE = {
    "sources": {
        "bathy_global": {"type": "raster",
                         "url": "pmtiles://https://cdn.example/tiles/v/bathy_global-aaa.pmtiles"},
        "depth_zones": {"type": "vector",
                        "url": "pmtiles://https://cdn.example/tiles/v/depth_zones-bbb.pmtiles?v=3"},
        "esri_ocean_ref": {"type": "raster",
                           "tiles": ["https://services.arcgisonline.com/.../{z}/{y}/{x}"]},
        "plain": {"type": "raster", "url": "https://cdn.example/tiles/v/plain-ccc.pmtiles"},
    },
    "layers": [],
}


def test_archive_urls_names_only_our_archives_as_plain_https():
    urls = _module().archive_urls(STYLE)
    assert urls == [
        "https://cdn.example/tiles/v/bathy_global-aaa.pmtiles",
        "https://cdn.example/tiles/v/depth_zones-bbb.pmtiles",   # cache-bust stripped
        "https://cdn.example/tiles/v/plain-ccc.pmtiles",         # already plain, after --plain
    ]


def test_verify_reports_every_archive_the_server_lacks():
    statuses = {
        "https://cdn.example/tiles/v/bathy_global-aaa.pmtiles": 404,
        "https://cdn.example/tiles/v/depth_zones-bbb.pmtiles": 200,
        "https://cdn.example/tiles/v/plain-ccc.pmtiles": 206,    # a CDN answering from ranges
    }
    missing = _module().verify_sources(STYLE, head=statuses.__getitem__)
    assert missing == [("https://cdn.example/tiles/v/bathy_global-aaa.pmtiles", 404)]


def test_verify_passes_a_style_whose_archives_are_all_there():
    assert _module().verify_sources(STYLE, head=lambda _url: 200) == []


MANIFEST = {"archives": {
    "bathy_global": {"path": "v/bathy_global-111111111111.pmtiles"},
    "depth_zones": {"path": "v/depth_zones-222222222222.pmtiles"},
    "plain": {"path": "v/plain-333333333333.pmtiles"},
    "land_relief_eu": {"path": "v/land_relief_eu-444444444444.pmtiles"},
}}


def _v1():
    return {
        "sources": {
            "bathy_global": {
                "type": "raster",
                "url": "pmtiles://https://old.example/tiles/v/bathy_global-aaaaaaaaaaaa.pmtiles",
            },
            "land_relief_eu": {
                "type": "raster",
                "url": "pmtiles://https://old.example/tiles/v/land_relief_eu-bbbbbbbbbbbb.pmtiles?v=3",
            },
            "esri": {"type": "raster", "tiles": ["https://services.arcgisonline.com/{z}/{y}/{x}"]},
        },
        "layers": [{"id": "seamount-fill", "source": "bathy_global"}],
        "metadata": {"oceanstream:generated-by": "scripts/export-style.py"},
    }


def test_archive_name_drops_scheme_query_and_digest():
    name = _module().archive_name
    versioned = "pmtiles://https://x/tiles/v/land_relief_eu-f9118c40d151.pmtiles?v=2"
    assert name(versioned) == "land_relief_eu"
    assert name("https://x/tiles/lake_relief.pmtiles") == "lake_relief"
    assert name("pmtiles://https://x/tiles/water-a.pmtiles") == "water-a"


def test_repoint_moves_only_the_archive_urls():
    """v1's layers are a promise; an export would drop what the viewer dropped."""
    out = _module().repoint(_v1(), MANIFEST, "https://cdn.example/tiles/")
    assert out["sources"]["bathy_global"]["url"] == (
        "pmtiles://https://cdn.example/tiles/v/bathy_global-111111111111.pmtiles")
    assert out["sources"]["land_relief_eu"]["url"] == (
        "pmtiles://https://cdn.example/tiles/v/land_relief_eu-444444444444.pmtiles")
    assert out["layers"] == _v1()["layers"]
    assert out["metadata"] == _v1()["metadata"]
    assert out["sources"]["esri"] == _v1()["sources"]["esri"]


def test_repoint_refuses_an_archive_the_manifest_does_not_know():
    style = _v1()
    style["sources"]["gone"] = {"type": "raster",
                                "url": "pmtiles://https://x/v/gone-cccccccccccc.pmtiles"}
    with pytest.raises(SystemExit, match="gone"):
        _module().repoint(style, MANIFEST, "https://cdn.example/tiles")


def test_publish_never_overwrites_v1_with_the_export():
    script = (SCRIPT.parent.parent / "deploy" / "edito" / "publish-viewer.sh").read_text()
    assert 'put_plain "$STYLE_OUT" "$STYLE_ROOT/style/v1.json"' not in script
    assert 'put_plain "$V1_OUT" "$STYLE_ROOT/style/v1.json"' in script
    assert "--repoint" in script
