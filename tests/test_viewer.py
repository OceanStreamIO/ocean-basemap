"""Checks on the viewer's JavaScript that Python can make cheaply."""

from __future__ import annotations

import base64
import bisect
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


def _sea_ramp() -> list[tuple[float, tuple[int, int, int]]]:
    from basemap.config import RAMP_DIR

    stops = []
    for line in (RAMP_DIR / "bathymetry-water.txt").read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 4:
            stops.append((float(parts[0]), tuple(int(v) for v in parts[1:4])))
    return sorted(stops)


def test_shore_water_is_the_ramp_surface():
    """The flat shore fill and the ramp's first pixel have to be one colour.

    SHORE_WATER paints the strip between the OSM shoreline and where the
    rendered tiles begin, and whole bodies of water no archive reaches. The
    bathymetry raster paints the pixel next to it from the ramp's surface stop.
    They live in different files -- app.js and color-ramps/bathymetry-water.txt
    -- so nothing but this stops them drifting into a seam along every coast.
    """
    match = re.search(r"const SHORE_WATER = '#([0-9a-fA-F]{6})'", _app_js())
    assert match, "SHORE_WATER not found in app.js"
    shore = tuple(int(match.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    assert shore == _sea_ramp()[-1][1]


def test_depth_stops_come_from_the_ramp():
    """The legend's swatches are the ramp sampled at each break, not typed.

    Typed by hand they drifted: the stop at -40 m was lighter than the stops
    either side of it, so the legend showed a pale band that is not on the map
    and cannot be, since the ramp it is drawn from rises monotonically.
    """
    ramp = _sea_ramp()
    elevations = [e for e, _ in ramp]

    def sampled(depth: float) -> tuple[int, ...]:
        if depth <= elevations[0]:
            return ramp[0][1]
        if depth >= elevations[-1]:
            return ramp[-1][1]
        i = bisect.bisect_left(elevations, depth)
        (e0, c0), (e1, c1) = ramp[i - 1], ramp[i]
        t = (depth - e0) / (e1 - e0)
        return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))

    stops = re.findall(
        r"depth:\s*(-?\d+), color: '#([0-9a-fA-F]{6})', label:", _app_js()
    )
    assert len(stops) >= 20, "DEPTH_STOPS not found in app.js"
    for depth, colour in stops:
        rgb = tuple(int(colour[i : i + 2], 16) for i in (0, 2, 4))
        assert rgb == sampled(float(depth)), f"{depth} m: {colour} is not the ramp"


def test_the_ramp_leaves_the_hillshade_somewhere_to_go():
    """No stop may be so bright that the shading clips it.

    render_tile multiplies the ramp colour by the light and clips at 255, so a
    stop whose brightest channel is already near 255 turns white on every lit
    slope -- losing the hue, not just the detail. The ramp's cap and the
    bathymetry's shading ceiling are chosen together; this keeps them together.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "src" / "basemap" / "pipeline" / "bathymetry.py"
    ).read_text()
    match = re.search(r"Shading\([^)]*ceiling=([0-9.]+)", source)
    assert match, "no bathymetry Shading ceiling found"
    ceiling = float(match.group(1))

    brightest = max(max(rgb) for _, rgb in _sea_ramp())
    assert brightest * ceiling <= 275, (
        f"brightest ramp channel {brightest} times a ceiling of {ceiling} "
        f"is {brightest * ceiling:.0f}, far enough past 255 to wash out"
    )


def test_archives_are_read_from_the_version_map_when_there_is_one():
    """Both pages must build archive URLs through the deploy's version map.

    An archive republished under a fixed name lets a browser hold a PMTiles
    directory page from one build and read tile bytes out of the next. Nothing
    about the URL changes, so no cache lifetime ends it -- which is how the map
    came to show flat, straight-edged holes that a reload would not clear. The
    deploy writes each archive's content digest into config.js and both pages
    read it, so a republish changes the URL and the old one keeps working for
    anyone pinned to it.
    """
    viewer = APP_JS.parent
    for page in ("app.js", "globe.html"):
        text = (viewer / page).read_text()
        assert "window.BASEMAP_TILE_VERSIONS" in text, f"{page} ignores the version map"
        assert "archivePath(" in text, f"{page} does not route archive names through it"
        assert not re.search(r"\$\{(?:name|BASE)\}[^`\n]*/\$\{name\}\.pmtiles", text), (
            f"{page} still builds an unversioned archive URL directly"
        )


def test_the_globe_loads_the_generated_config():
    """globe.html gets its origin and versions from config.js like index.html.

    It did not, for a while: it fell back to a bucket URL written into the page,
    so the globe kept reading the unversioned archives while the 2D map had
    moved on, and pointing a deployment at another origin left the globe behind.
    """
    text = (APP_JS.parent / "globe.html").read_text()
    assert re.search(r'<script src="config\.js(\?[^"]*)?"></script>', text), (
        "globe.html does not load config.js"
    )


def test_the_deploy_scripts_write_the_version_map():
    """Both viewer deploys must append the version block to config.js.

    They generate config.js separately -- one into a ConfigMap, one into the
    bucket -- and a viewer deployed without the block silently falls back to
    unversioned names, which is the bug this is all for.
    """
    deploy = Path(__file__).resolve().parent.parent / "deploy" / "edito"
    for script in ("apply.sh", "publish-viewer.sh"):
        text = (deploy / script).read_text()
        assert "tile-versions.sh" in text, f"{script} does not source the helper"
        assert "tile_versions_js" in text, f"{script} does not call it"
