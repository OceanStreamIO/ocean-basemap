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


def test_every_source_carries_an_attribution():
    """No archive may be drawn without crediting where it came from.

    The attribution travels in the style, so anyone who loads style.json gets
    it and MapLibre renders it for them. A source without one is credited
    nowhere -- and for the CC BY 4.0 sets here (GeoNames, geoBoundaries,
    HydroLAKES, Kartverket) attribution is a licence condition, not a courtesy.
    """
    text = _app_js()
    block = text[text.index("  sources: {") : text.index("  layers: [")]
    missing = [
        m.group(1)
        for m in re.finditer(r"^\s{4}(\w+):\s*\{(?:(?!\n\s{4}\w+:).)*?\n?\s*\},?\s*$",
                             block, re.M | re.S)
        if "attribution" not in m.group(0)
    ]
    assert not missing, f"sources with no attribution: {missing}"


def test_the_multi_source_archives_credit_every_source_in_them():
    """Two archives merge datasets, and both halves have to be named.

    places.pmtiles is Kartverket's N50 for Norway and GeoNames for the rest of
    the world; boundaries.pmtiles is geoBoundaries plus N50's kommuner. Both
    were credited only to whichever source came first, which names one CC BY
    dataset and silently omits another.
    """
    text = _app_js()

    def value_of(const: str, depth: int = 3) -> str:
        """The constant's text, with any ${ATTR_*} it interpolates expanded."""
        match = re.search(rf"^const {const} =(.*?);$", text, re.M | re.S)
        assert match, f"{const} is not defined"
        body = match.group(1)
        if depth:
            for ref in set(re.findall(r"\$\{(ATTR_\w+)\}", body)):
                body = body.replace("${" + ref + "}", value_of(ref, depth - 1))
        return body

    for source, required in [
        ("places", ["Kartverket", "GeoNames"]),
        ("boundaries", ["geoBoundaries", "Kartverket", "Natural Earth"]),
    ]:
        match = re.search(rf"^\s+{source}: \{{[^}}]*attribution: (\w+)", text, re.M)
        if match is None:
            continue  # that archive is no longer in the style

        credited = value_of(match.group(1))
        for name in required:
            assert name in credited, f"{source} does not credit {name}"


def _layer_objects(text: str) -> list[str]:
    """Every top-level object literal inside the style's `layers: [...]`."""
    block = text[text.index("  layers: [") :]
    i, depth, entry, out = block.index("["), 0, None, []
    while i < len(block):
        char = block[i]
        if char in "[{":
            depth += 1
            if depth == 2 and char == "{":
                entry = i
        elif char in "]}":
            depth -= 1
            if depth == 1 and entry is not None:
                out.append(block[entry : i + 1])
                entry = None
            if depth == 0:
                break
        i += 1
    return out


def test_every_style_layer_is_a_whole_layer():
    """No layer entry may be missing its id or type.

    Syntax checking does not catch this: an empty `{}` or a stray `paint`
    block left behind by an edit is valid JavaScript, and the failure is
    MapLibre refusing the entire style at runtime -- `layers[28]: either
    "type" or "ref" is required` -- so the map goes blank rather than losing
    one layer. That is exactly what removing six layers by hand produced, on
    the two whose comment sits inside the braces rather than above them.
    """
    layers = _layer_objects(_app_js())
    assert len(layers) > 20, f"only found {len(layers)} layers; the parse is wrong"
    broken = [
        (i, " ".join(text.split())[:60])
        for i, text in enumerate(layers)
        if not re.search(r"\bid: '", text) or not re.search(r"\btype: '", text)
    ]
    assert not broken, f"malformed layer entries: {broken}"


def test_no_layer_or_source_is_referenced_after_being_removed():
    """A panel group, a layer and its source have to go together.

    Half a removal is as broken as none: a group pointing at a layer that no
    longer exists silently does nothing, and a layer pointing at a source that
    no longer exists makes MapLibre reject the style.
    """
    text = _app_js()
    layer_ids = set(re.findall(r"id: '([a-z0-9-]+)', type: '", text))
    sources_block = text[text.index("  sources: {") : text.index("  layers: [")]
    source_ids = set(re.findall(r"^\s{4}(\w+): \{", sources_block, re.M))

    for layer, source in re.findall(r"id: '([a-z0-9-]+)', type: '\w+', source: '(\w+)'", text):
        assert source in source_ids, f"layer {layer} points at missing source {source}"

    # The graticule and the measuring tools build their layers at runtime, so
    # they are not in mapStyle; export-style.py strips the same prefixes.
    runtime = ("measure-", "graticule-", "overlay-")
    for group in re.findall(r"layerIds: \[([^\]]*)\]", text):
        for layer in re.findall(r"'([a-z0-9-]+)'", group):
            if layer.startswith(runtime):
                continue
            assert layer in layer_ids, f"panel group points at missing layer {layer}"


def test_globe_tiles_own_their_pixels():
    """A decoded tile must not depend on a blob URL that has been revoked.

    Cesium uploads the texture on a later frame than the one the decode
    resolved on. Returning an HTMLImageElement and revoking its object URL in
    a `finally` is correct at that instant and wrong a moment afterwards: if
    the browser has dropped the decoded bitmap it goes back to a URL that no
    longer exists, and the tile is drawn black. Nothing reports it -- the
    provider returned an image, no request failed, no error reached the
    console -- and on a globe with no base layer beneath, black is space. It
    measured 3.7% of the globe's disc, and 0.0% once the pixels were copied
    into a canvas the tile owns.
    """
    source = (APP_JS.parent / "cesium-provider.js").read_text()
    decode = source[source.index("async _decode(") : source.index("async requestImage(")]
    assert "createElement('canvas')" in decode and "drawImage(" in decode, (
        "_decode must copy the pixels into a canvas"
    )
    assert not re.search(r"return image;", decode), (
        "_decode still returns the Image, whose blob URL is revoked beneath it"
    )


def _deploy_dir():
    return Path(__file__).resolve().parent.parent / "deploy"


def test_both_deploys_resolve_the_same_tile_origin():
    """apply.sh and publish-viewer.sh must not disagree about where tiles live.

    Each generates its own config.js, and each used to carry its own default
    pointing at the bucket, with the CDN passed in by hand on every run. A
    plain ./apply.sh then moved the published map back onto CloudFerro without
    saying anything -- no error, nothing visible in the page, just a slower
    load and a CDN quietly bypassed. Both now read one resolver.
    """
    for script in ("apply.sh", "publish-viewer.sh"):
        text = (_deploy_dir() / "edito" / script).read_text()
        assert "tile-base.sh" in text, f"{script} does not source the resolver"
        assert "resolve_tile_base" in text, f"{script} does not call it"
        assert "cloudferro.com/basemap}" not in text, (
            f"{script} still carries its own bucket default"
        )


def test_the_recorded_origin_is_a_public_base_url():
    """deploy/cdn/origin is what an unattended deploy will publish against.

    It deliberately does not have to end in "/basemap". Where the prefix sits
    is a property of the distribution, not of the URL: the CDN serves the
    bucket's basemap/ prefix at its own root, so the custom domain is a bare
    host, while the bucket fallback still needs the prefix spelled out.
    """
    origin = _deploy_dir() / "cdn" / "origin"
    if not origin.is_file():
        return  # no CDN configured; the resolver falls back to the bucket
    value = origin.read_text().strip()
    assert value.startswith("https://"), f"origin is not an https base: {value!r}"
    assert not value.endswith("/"), "origin should have no trailing slash"
    assert " " not in value, f"origin is not a single URL: {value!r}"


# What each archive obliges the page to name, if the style still draws it.
# OpenStreetMap is ODbL; HydroLAKES, Kartverket, GeoNames and geoBoundaries are
# CC BY 4.0; GEBCO, EMODnet and Copernicus ask to be acknowledged.
CREDIT_OBLIGATIONS = {
    "land_base": ["OpenStreetMap"],
    "sea_mask": ["OpenStreetMap"],
    "lakes": ["HydroLAKES"],
    "places": ["Kartverket", "GeoNames"],
    "boundaries": ["geoBoundaries"],
    "bathy_global": ["GEBCO"],
    "bathy_eu": ["EMODnet"],
    "land_relief": ["Copernicus DEM"],
}


def _sources_in_style(text: str) -> set[str]:
    block = text[text.index("  sources: {") : text.index("  layers: [")]
    return set(re.findall(r"^\s{4}(\w+): \{", block, re.M))


def test_the_credit_line_names_every_source_it_still_draws():
    """Every archive in the style must be named in the page's credit line.

    Derived from the style rather than a fixed list, so the rule survives
    layers coming and going. It has already had to: geoBoundaries was required
    while boundaries.pmtiles was drawn, and stopped being required the moment
    the last layer using it was removed and nothing fetched it any more.
    Crediting a dataset the page does not use is its own kind of wrong.
    """
    text = _app_js()
    assert "attributionControl: false" in text, "MapLibre's control is still on"
    block = text[text.index("const CREDITS_ALWAYS") : text.index("const CREDITS_WHEN_ON")]
    present = _sources_in_style(text)
    for source, names in CREDIT_OBLIGATIONS.items():
        if source not in present:
            continue
        for name in names:
            assert name in block, f"{source} is drawn but the credit line omits {name}"


def test_the_credit_line_names_nothing_it_no_longer_draws():
    """And it must not carry credits for archives the style has dropped."""
    text = _app_js()
    block = text[text.index("const CREDITS_ALWAYS") : text.index("const CREDITS_WHEN_ON")]
    present = _sources_in_style(text)
    for source, names in CREDIT_OBLIGATIONS.items():
        if source in present:
            continue
        for name in names:
            # Only flag names no remaining source also requires.
            still_owed = any(
                name in owed for other, owed in CREDIT_OBLIGATIONS.items()
                if other in present
            )
            if not still_owed:
                assert name not in block, (
                    f"{name} is credited but {source} is no longer in the style"
                )


def test_optional_overlays_are_credited_only_while_they_are_on():
    """Esri and EOX are third-party services, credited when they are drawn."""
    text = _app_js()
    block = text[text.index("const CREDITS_WHEN_ON") : text.index("function updateCredits")]
    groups = set(re.findall(r"^\s{4}id: '([a-z0-9-]+)', name: ", text, re.M))
    for group in ("reference-labels", "satellite", "satellite-esri"):
        if group not in groups:
            continue
        assert f"'{group}'" in block or f"{group}:" in block, (
            f"{group} has no credit for when it is switched on"
        )
    assert "updateCredits()" in text[text.index("function buildLayerPanel"):], (
        "credits are never refreshed when a layer is toggled"
    )


def test_no_constant_is_referenced_after_being_removed():
    """Removing a layer must take its attribution constant with it, or neither.

    A half-finished removal leaves a source still naming a constant that is
    gone. That is valid JavaScript -- `node --check` passes -- and it throws
    the moment the file is evaluated, so the whole viewer goes blank. It has
    very nearly shipped twice: once when a regex ate the layer bodies but left
    their braces, and once when an edit removed ATTR_SEAMOUNTS while the
    source that used it stayed.
    """
    text = _app_js()
    declared = set(re.findall(r"^(?:const|let|var) ([A-Z][A-Z0-9_]+)", text, re.M))
    # Comments name constants that live in other files -- config.py's
    # DEPTH_BREAKS, for one -- so they are stripped before looking.
    code = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    code = re.sub(r"//.*$", "", code, flags=re.M)
    referenced = set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", code))
    known = {"URL", "JSON", "NaN"}
    missing = sorted(r for r in referenced - declared - known if r.startswith(
        ("ATTR_", "CREDITS_", "DEPTH_", "LAKE_", "SECTION_", "GROUP_", "LAYER_")))
    assert not missing, f"referenced but never declared: {missing}"
