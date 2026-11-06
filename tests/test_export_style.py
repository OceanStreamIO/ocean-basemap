"""The checks export-style.py makes before a style is allowed out.

The style is exported from the running viewer, so its content is the viewer's
business. What this guards is the step after: a style naming archives that
are no longer on the server was published once and sat there for a month,
drawing no bathymetry at all, because nothing looked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
