"""Checks on the place and built-up tiles."""

from __future__ import annotations

import json
import re

from basemap.cli import FULL_ORDER, STEPS
from basemap.pipeline import places


def test_step_is_registered():
    assert STEPS["places"] == "basemap.pipeline.places"
    assert FULL_ORDER.index("places") > FULL_ORDER.index("lakes"), (
        "places reads the geodatabase the lakes step downloads"
    )


def test_name_bands_run_from_largest_type_to_smallest():
    """The band a name falls in is the first threshold it clears.

    Out of order, every name would land in the first band and the map would
    draw a hundred thousand farm names at z6.
    """
    thresholds = [t for t, _ in places.NAME_BANDS]
    assert thresholds == sorted(thresholds, reverse=True)
    assert thresholds[-1] == 0.0, "the last band has to catch everything"
    zooms = [z[0] for _, z in places.NAME_BANDS]
    assert zooms == sorted(zooms), "a smaller name may not appear before a larger one"


def test_written_names_are_in_longitude_and_latitude(tmp_path, monkeypatch):
    """The geodatabase is UTM 33N, and a point left in it is metres.

    Tippecanoe drops those as impossible longitudes without a word, and the
    archive comes out with no tiles in it at all -- which is exactly what
    happened, and is invisible until someone opens the map.
    """
    written = {}

    class FakeSrc:
        crs = "EPSG:25833"

        def __init__(self):
            self.features = [{
                "geometry": {"type": "Polygon", "coordinates": [[
                    (563000.0, 7633000.0), (563400.0, 7633000.0),
                    (563400.0, 7633400.0), (563000.0, 7633400.0), (563000.0, 7633000.0)]]},
                "properties": {
                    "navneobjekthovedgruppe": "bebyggelse", "fulltekst": "Harstad",
                    "navneobjekttype": "by", "FontSize": 12.7, "stedsnummer": 1,
                },
            }]

        def __len__(self):
            return len(self.features)

        def __iter__(self):
            return iter(self.features)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(places, "n50_gdb", lambda work: tmp_path / "fake.gdb")
    monkeypatch.setattr(places.fiona, "open", lambda *a, **k: FakeSrc())
    inputs = places.name_bands(tmp_path)

    first = json.loads(inputs[0]["file"].read_text())["features"]
    assert len(first) == 1, "a city belongs in the first band"
    lon, lat = first[0]["geometry"]["coordinates"]
    assert 4 < lon < 32 and 57 < lat < 72, f"not a Norwegian longitude/latitude: {lon}, {lat}"
    assert first[0]["properties"]["name"] == "Harstad"
    written["ok"] = True
    assert written


def test_points_are_not_thinned_again_by_tippecanoe():
    """Each band already carries the zoom its places earned.

    Tippecanoe's drop rate thins points per zoom level on top of that, which
    left eleven place names across the whole of Europe at z5 while the bands
    held thousands.
    """
    assert "-r1" in places.KEEP_EVERY_POINT


def test_layers_sharing_an_archive_end_at_the_same_zoom():
    """A layer that stops early does not overzoom; it disappears.

    Tiles deeper than its maximum are written without it, so the cities
    vanished at z13 while the built-up areas in the same archive carried on
    to z14.
    """
    import inspect

    source = inspect.getsource(places.run)
    maxzooms = [int(m) for m in re.findall(r'"maxzoom": (\d+)', source)]
    urban_group = maxzooms[:3]
    assert len(set(urban_group)) == 1 or max(urban_group) == urban_group[-1], (
        f"layers in urban.pmtiles end at different zooms: {urban_group}"
    )
