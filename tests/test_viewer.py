"""Checks on the viewer's JavaScript that Python can make cheaply."""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path

from PIL import Image

APP_JS = Path(__file__).resolve().parent.parent / "src" / "basemap" / "viewer" / "app.js"


def test_blank_tile_is_fully_transparent():
    """The fallback for a tile an archive lacks must not paint anything.

    The raster archives are sparse, so this stands in wherever there is no sea,
    no land or no lake. The widely copied "blank" 1x1 PNG is in fact a
    half-opaque red pixel; with it, every missing bathymetry tile painted the
    land red.
    """
    match = re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", APP_JS.read_text())
    assert match, "no base64 blank tile found in app.js"
    image = Image.open(io.BytesIO(base64.b64decode(match.group(1)))).convert("RGBA")
    assert image.size == (1, 1)
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)


def test_coastal_lake_threshold_matches_the_pipeline():
    """The viewer and the renderer must agree on what counts as coastal.

    The pipeline splits lake beds into two archives on this elevation, and the
    viewer filters its two lake fills on it. If they drift, a lake gets its bed
    from one ramp and its fill from the other.
    """
    from basemap.config import COASTAL_LAKE_ELEV

    match = re.search(r"const COASTAL_LAKE_ELEV = (\d+(?:\.\d+)?)", APP_JS.read_text())
    assert match, "COASTAL_LAKE_ELEV not found in app.js"
    assert float(match.group(1)) == COASTAL_LAKE_ELEV


def _app_js() -> str:
    return APP_JS.read_text()


def test_every_panel_group_drives_a_real_layer():
    """Each layer the panel claims to control must exist in the style.

    The Country Borders toggle once did nothing, because the group listed
    layer ids that no layer had. Nothing fails loudly when that happens --
    ``map.getLayer`` returns undefined and the click is silently dropped --
    so the panel keeps offering a switch that is wired to nothing.
    """
    source = _app_js()
    # Layers are declared both inline in the style and later with addLayer,
    # so match an id followed by a type within the same object.
    declared = set(re.findall(r"\bid: '([a-z0-9-]+)',\s*\n?\s*type: '", source))
    listed: set[str] = set()
    for block in re.findall(r"layerIds: \[([^\]]*)\]", source):
        listed.update(re.findall(r"'([a-z0-9-]+)'", block))
    for block in re.findall(r"opacityProps: \{([^}]*)\}", source):
        listed.update(re.findall(r"'([a-z0-9-]+)':", block))
    assert listed, "no panel groups found in app.js"
    assert listed <= declared, f"panel controls missing layers: {sorted(listed - declared)}"


def test_no_layer_declares_layout_twice():
    """A repeated key silently discards the first one.

    Hiding a label layer by default meant adding ``visibility`` to layers that
    already had a ``layout`` block. Written as a second ``layout:`` the whole
    text placement is dropped instead, and the object stays valid JavaScript,
    so only the rendered map shows it.
    """
    for layer in re.findall(r"\{\s*\n?\s*id: '[a-z0-9-]+', type: '.*?\n    \},", _app_js(), re.S):
        assert layer.count("layout:") <= 1, f"duplicate layout key:\n{layer[:200]}"


def test_overlay_parameter_is_read_from_the_query_string():
    """User overlays are the documented way to add data without a rebuild.

    The viewer is static, so ``?overlay=`` is the whole extension mechanism;
    losing the parameter would leave no way in at all.
    """
    source = _app_js()
    assert "getAll('overlay')" in source
    assert "addOverlays()" in source


def test_layers_off_by_default_are_hidden_in_the_style_too():
    """A panel group's defaultVisible only draws the eye closed.

    The layer itself has to start hidden, or the map keeps drawing something
    the panel says is off -- which is what the built-up areas did.
    """
    source = _app_js()
    for block in re.findall(r"\{[^{}]*defaultVisible: false[^{}]*\}", source, re.S):
        ids = re.findall(r"'([a-z0-9-]+)'", block)
        for layer_id in ids:
            layer = re.search(
                rf"id: '{layer_id}', type: '[a-z]+',(.*?)\n    \}},", source, re.S
            )
            if layer:
                assert "visibility: 'none'" in layer.group(1), (
                    f"{layer_id} is off in the panel but visible in the style"
                )


def test_viewer_assets_are_referenced_relatively():
    """The page has to work wherever it is served from.

    Absolute paths like /static/app.js tie it to living at a site's root: on
    object storage, under a prefix, the browser asks the bucket root for the
    file and gets nothing. Relative paths work in both places, which is what
    lets the same files be a deployment and a static copy anyone can open.
    """
    html = (APP_JS.parent / "index.html").read_text()
    bad = re.findall(r'(?:src|href)="(/[^"]*)"', html)
    assert not bad, f"absolute asset paths tie the viewer to the site root: {bad}"
