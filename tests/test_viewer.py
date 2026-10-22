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
    # Split on braces rather than on a closing line at a guessed indent. The
    # old pattern ended at the first "\n    }," after a layer's id, which is
    # not that layer's own close: a layer added between one and that line was
    # swallowed into its neighbour's match, and the two layouts read as one
    # layer declaring it twice.
    source = _app_js()
    for start in [m.start() for m in re.finditer(r"id: '[a-z0-9-]+', type: '", source)]:
        opening = source.rfind("{", 0, start)
        depth, end = 0, None
        for i in range(opening, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        layer = source[opening:end]
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
    They live in different files, so nothing but this stops them drifting into a
    seam along every coast.

    There is now one per sea palette, and every one has to match its own ramp: a
    strip left on the published colour under a blue candidate is a white halo
    around every coast, which reads as a deliberate glow and is not one.
    """
    from basemap.config import RAMP_DIR
    from basemap.processing.raster_tiles import Ramp

    block = re.search(r"const SHORE_BY_PALETTE = \{(.*?)\};", _app_js(), re.S)
    assert block, "SHORE_BY_PALETTE not found in app.js"
    entries = dict(re.findall(r"'?([a-z]?)'?:\s*'#([0-9a-fA-F]{6})'", block.group(1)))
    assert entries, "no palettes in SHORE_BY_PALETTE"

    for name, hexv in entries.items():
        ramp_file = RAMP_DIR / (f"bathymetry-water-{name}.txt" if name
                                else "bathymetry-water.txt")
        assert ramp_file.is_file(), f"no ramp for palette {name or 'published'}"
        surface = tuple(Ramp.parse(ramp_file.read_text()).rgb[-1])
        shore = tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))
        assert shore == surface, (
            f"palette {name or 'published'}: shore strip {shore} "
            f"against a ramp surface of {surface}")


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


def test_the_lake_swatches_are_the_lake_ramp():
    """The viewer's flat lake fill meets the shaded bed raster inside the lake.

    lake-relief is inset from the shoreline so the vector fill and outline keep
    the edge, which means the two are side by side in every lake: if the fill
    is sampled from a different ramp than the bed, the inset boundary shows as
    a step. Nothing checked this while LAKE_STOPS was a hand-kept copy.
    """
    import numpy as np

    from basemap.config import RAMP_DIR
    from basemap.processing.raster_tiles import Ramp

    ramp = Ramp.parse((RAMP_DIR / "lakes.txt").read_text())
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "viewer" / "app.js").read_text()
    block = re.search(r"const LAKE_STOPS = \[\n(.*?)\n\];", source, re.S)
    assert block, "LAKE_STOPS not found in app.js"
    stops = re.findall(r"\{ depth: (\d+), color: '#([0-9a-f]{6})' \}", block.group(1))
    assert len(stops) >= 8
    for depth, colour in stops:
        rgb = ramp.colour(np.array([-float(depth)]))[0][:3]
        assert "".join(f"{int(v):02x}" for v in rgb) == colour, (
            f"{depth} m: app.js has #{colour}, the ramp gives "
            f"#{''.join(f'{int(v):02x}' for v in rgb)}")


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
    "bathy_gmrt": ["GMRT", "Geoscience Australia", "NOAA"],
    "bathy_hr": ["EMODnet", "NOAA", "Geoscience Australia"],
    "land_relief": ["Copernicus DEM", "ESA WorldCover"],
    "rivers": ["MERIT Hydro", "G-RUN"],
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


def test_the_xyz_reader_skips_archives_that_cannot_hold_the_tile():
    """Bounds and zoom come from the header, so a miss is never paid for.

    pmtiles.reader.Reader.get() re-reads the header and re-deserialises every
    directory on every call -- 10.8 ms for a hit, 15.9 ms for a miss, measured.
    compose() makes up to five lookups a tile, so across twenty-two million
    tiles that was the whole cost of the job. A miss is the dearest lookup of
    all, and the cheapest ones are those never made.
    """
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "pipeline" / "xyz.py").read_text()
    assert "def covers(" in source, "the reader no longer checks coverage"
    assert "_dirs" in source, "the directory cache is gone"
    start = source.index("    def raw(")
    raw = source[start : source.index("\n\n", start)]
    assert "covers(" in raw, "raw() looks up archives without checking coverage first"
    assert "archive.get(" in raw, "raw() no longer reads the tile"


def test_the_tiles_viewer_reads_the_tree_with_nothing_installed():
    """xyz.html is the proof that the flat *tree* stands on its own.

    It used to prove that by loading no PMTiles library at all. It now carries
    depth labels, which are vectors and do need one -- so the claim is narrowed
    rather than dropped: the basemap source is still a plain {z}/{x}/{y} raster
    with no protocol behind it, and that is the part a reader is deciding about.
    Anything stacked over it is the reader's choice, which is the same argument
    that already justified Esri's names being there.

    So what is pinned here is that the *map* never becomes a pmtiles source. If
    it ever does, the page has stopped demonstrating anything.

    It must also declare the tree's real depth: without maxzoom a client asks
    for tiles below the deepest level, which were never published.
    """
    page = (APP_JS.parent / "xyz.html").read_text()
    code = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    code = re.sub(r"<div class=\"tiles-note\">.*?</div>", "", code, flags=re.S)

    # Sliced to the source's own closing brace, not the first one: the tile
    # template contains {z}/{x}/{y} and would end the block three characters in.
    basemap = code[code.index("basemap: {"):]
    basemap = basemap[: basemap.index("\n      },")]
    assert "type: 'raster'" in basemap, "the basemap source is no longer a raster source"
    assert "pmtiles" not in basemap, "the basemap source now goes through pmtiles"
    assert "{z}/{x}/{y}.webp" in basemap, "it does not point at the tile tree"

    # The tree is chosen by ?v=, so every choice has to name a path and declare
    # its own native depth. A version rendered deeper than another is the whole
    # reason the depth cannot be one number on the page: asking for a level a
    # tree does not hold gets tiles that were never published.
    trees = code[code.index("const TREES = {"):]
    trees = trees[: trees.index("\n};")]
    for line in trees.splitlines():
        if "path:" not in line:
            continue
        assert "maxzoom:" in line, f"a tree with no declared depth: {line.strip()}"
    assert "xyz'" in trees, "no tree points at the unversioned path"

    # Every pmtiles reference must belong to the labels, never to the map.
    for match in re.finditer(r"pmtiles://[^'\"]+", code):
        assert "depth_contours" in match.group(0), f"unexpected pmtiles source: {match.group(0)}"

    assert "maxzoom: TREE.maxzoom" in code, "the source does not take its depth from the tree"

    # The panel above the map was stripped out before any of that, so its claims
    # have to be pinned separately -- and they are claims about the tree being
    # shown, not about the product. A hardcoded "maximumLevel 12" survived the
    # move to a thirteen-level tree and told readers to cap their client one
    # level short of what they were looking at.
    page_code = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    for element, source in (("depth-note", "TREE.maxzoom"),
                            ("cesium-level", "TREE.maxzoom"),
                            ("endpoint", "TREE.path")):
        written = re.search(
            rf"getElementById\('{element}'\)\.textContent\s*=\s*[^;]+;", page_code)
        assert written, f"the panel's {element} is never written from the tree"
        assert source in written.group(0), f"{element} does not come from {source}"


def test_the_plain_page_links_back_to_the_full_viewer():
    """The two pages are published at different depths, so the link has to climb.

    xyz.html goes under static/ and index.html does not, in every layout the
    viewer is served from -- the bucket, the EDITO deployment and the local
    server. A bare "index.html" therefore asks for static/index.html, which
    exists nowhere, and the CDN answers 403.
    """
    publish = (_deploy_dir() / "edito" / "publish-viewer.sh").read_text()
    assert '"$PREFIX/index.html"' in publish, "index.html is no longer published at the top"
    assert '"$PREFIX/static/xyz.html"' in publish, "xyz.html is no longer published under static/"

    html = (APP_JS.parent / "xyz.html").read_text()
    links = re.findall(r'href="([^"]*index\.html)"', html)
    assert links == ["../index.html"], f"the link to the full viewer is {links}"


def test_the_viewer_and_the_renderer_draw_coastal_water_the_same():
    """Two codebases, one tone: the map and the tiles show the same water.

    A lake at sea level is drawn on the sea's ramp, whose shallow end is
    near-white, so a metre-deep basin inside a lagoon renders as a white hole
    punched through continuous water -- which is what the fish farms in the
    Venice lagoon looked like. Both products floor it at the coastal tone, and
    a floor only works if both agree on what the tone is.
    """
    import re

    from basemap.pipeline.xyz import COASTAL_WATER_RGB

    js = APP_JS.read_text()
    found = re.search(r"const COASTAL_WATER = '#([0-9a-fA-F]{6})'", js)
    assert found, "the viewer has no coastal-water tone to keep in step"
    rgb = tuple(int(found.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    assert rgb == COASTAL_WATER_RGB, (
        f"the viewer draws coastal water {rgb} and the renderer {COASTAL_WATER_RGB}")


def test_both_deploys_publish_every_viewer_page():
    """A page that exists only in the repo is a page nobody can open."""
    deploy = _deploy_dir() / "edito"
    pages = {p.name for p in (APP_JS.parent).glob("*.html")}
    for script in ("apply.sh", "publish-viewer.sh"):
        text = (deploy / script).read_text()
        for page in pages:
            assert page in text, f"{script} does not publish {page}"


def test_the_viewer_and_the_renderer_read_the_same_corrected_depths():
    """One file, so a lake cannot be two colours on two maps.

    HydroLAKES models mean depth rather than measuring it and is out by an
    order of magnitude on some lakes -- Kara-Bogaz-Gol is recorded at 101 m
    against a real 10, which drew an evaporation basin darker than the deep
    lakes beside it. The correction has to reach the vector map and the flat
    tile tree alike, or they disagree about the same lake.
    """
    from basemap.config import LAKE_DEPTHS_FILE, lake_depth_overrides

    assert LAKE_DEPTHS_FILE.is_file(), "lake-depths.json is missing"
    overrides = lake_depth_overrides()
    assert overrides, "the file carries no corrections"

    app = (Path(__file__).resolve().parent.parent
           / "src" / "basemap" / "viewer" / "app.js").read_text()
    assert "window.BASEMAP_LAKE_DEPTHS" in app, "the viewer never reads them"
    assert "lakeDepthOf(props)" in app, "the depth readout ignores them"

    # Both viewers, not one. The bucket copy shipped the table and the
    # cluster copy did not, so the same lake read 101 m on one map and 10 m
    # on the other depending on which you opened.
    edito = Path(__file__).resolve().parent.parent / "deploy" / "edito"
    helper = (edito / "lake-depths.sh").read_text()
    assert "lake-depths.json" in helper, "the shared block never reads the file"
    for script in ("apply.sh", "publish-viewer.sh"):
        assert "lake_depths_js" in (edito / script).read_text(), \
            f"{script} does not ship the corrected depths"


def test_a_corrected_lake_renders_at_its_corrected_depth():
    """The renderer has to use the correction, not just carry the file."""
    import inspect

    from basemap.pipeline import xyz

    source = inspect.getsource(xyz._lakes_in)
    assert "lake_depth_overrides" in source
    assert 'corrected.get(props.get("name")' in source


def test_the_lake_bed_has_its_own_opacity_control():
    """The bed and the water over it are judged apart, as sea and seafloor are.

    They shared one slider, so dimming the lake fill to see the land under it
    also dimmed the bathymetry, which is two different judgements on one
    control -- the same reason the coastline was split from the land and the
    lake outline from the lake.
    """
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "viewer" / "app.js").read_text()
    assert "id: 'lake-bathymetry'" in source, "no group for the lake bed"
    assert "'lake-bathymetry': 'Surface'" in source, "the group has no heading"

    # and it is no longer driven by the Lakes group
    lakes = re.search(r"id: 'water-features'.*?\n  \},", source, re.S).group(0)
    assert "lake-relief" not in lakes, "the Lakes slider still moves the bed"

    beds = re.search(r"id: 'lake-bathymetry'.*?\n  \},", source, re.S).group(0)
    assert "'lake-relief': 'raster-opacity'" in beds


def test_lakes_carry_a_depth_label():
    """Only 143 lakes have a bed, so nothing else says how deep a lake is.

    Geneva at 155.7 m and Malawi at 261 render as a flat tone with no
    bathymetry under them, and the map gave a reader no way to tell either from
    a three-metre pan. The sea has had depth labels all along; this is the same
    for inland water, and it reads the corrected depth where lake-depths.json
    has one so the label and the colour cannot disagree.
    """
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "viewer" / "app.js").read_text()
    layer = re.search(r"id: 'lake-depth-labels'.*?\n      \},", source, re.S)
    assert layer, "no depth label layer for lakes"
    body = layer.group(0)
    assert "'text-field': lakeDepthLabelExpr" in body
    assert "symbol-sort-key" in body, "a dropped label should be a small lake's"
    assert "id: 'lake-depths'" in source, "the labels have no control"

    # the corrected depths have to reach the label, not just the fill
    expr = re.search(r"const lakeDepthLabelExpr = .*?\n\}\)\(\);", source, re.S).group(0)
    assert "LAKE_DEPTHS" in expr, "the label ignores corrected depths"


def test_ocean_depth_labels_have_their_own_control_and_reach_the_shelf():
    """Depth figures are a separate judgement from the lines they sit on.

    They rode the contour slider, so softening the contours took the numbers
    with them -- the same reason the coastline was split from the land and the
    lake outline from the lake. And only multiples of 500 m were ever
    labelled, so in shallow water the only depth figures came from Esri's
    raster, which cannot be restyled or switched off on its own, while our own
    200, 150, 100, 75, 50, 40, 30, 20 and 10 m contours were drawn unlabelled.
    """
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "viewer" / "app.js").read_text()
    assert "id: 'ocean-depths'" in source, "no control for the depth figures"
    assert "'ocean-depths': 'Labels'" in source, "the group has no heading"

    contours = re.search(r"id: 'contours'.*?\n  \},", source, re.S).group(0)
    assert "depth-labels" not in contours, "the contour slider still moves the labels"

    shelf = re.search(r"id: 'depth-labels-shelf'.*?\n      \},", source, re.S)
    assert shelf, "the shelf contours are still unlabelled"
    assert "['>=', ['get', 'depth'], -200]" in shelf.group(0)
    assert "minzoom: 8" in shelf.group(0), "shelf labels belong at coast zooms"


def test_an_hr_preview_redirects_only_the_hr_archive():
    """A new HR source is judged on one coast before the whole footprint.

    That leaves an archive holding only the box, which must not go over the
    real one -- it would drop every other HR survey on the map. ?hr= is
    deliberately narrower than ?try=, which redirects three archives at once:
    a preview prefix holding one of them takes the other two down with it,
    which is how a stale box preview came to be showing through for days.
    """
    source = (Path(__file__).resolve().parent.parent
              / "src" / "basemap" / "viewer" / "app.js").read_text()
    assert "const HR_PREVIEW" in source
    assert "HR_PREVIEW && name === 'bathy_hr'" in source, "it must redirect bathy_hr alone"
    block = re.search(r"const HR_PREVIEW = .*?\}\)\(\);", source, re.S).group(0)
    assert "/^[a-z0-9-]+$/" in block, "an unchecked name builds an arbitrary URL"


def test_only_the_capped_archives_fall_back_to_a_parent_tile():
    """A survey grid stops at the zoom its cells earn, and the tile above it is
    what should be enlarged there -- not the coarser layer underneath.

    MapLibre only walks up to a parent for a tile that failed with a 404; the
    transparent tile every other archive gets counts as a tile it has. Measured
    on 2026-10-20 with v10's archive, which dropped two million z15 tiles the
    cells did not earn: off Lisbon the viewer showed z11 stretched, with the
    fallback the survey's z14. The land and lake archives must keep the blank
    tile, or a coastal parent would be stretched over open sea.
    """
    source = APP_JS.read_text()
    pattern = re.compile(re.search(r"const OVERZOOM_ARCHIVES = /(.+)/;", source).group(1))
    base = "pmtiles://https://basemap.oceanstream.io/tiles"
    for name in ("bathy_hr", "bathy_gmrt"):
        for path in (f"{name}.pmtiles", f"v/{name}-0123456789ab.pmtiles", f"hr-v10/{name}.pmtiles"):
            assert pattern.search(f"{base}/{path}/14/8000/6000"), path
    for name in ("bathy_global", "bathy_eu", "land_relief", "land_relief_eu", "lake_relief",
                 "coastal_water", "depth_contours"):
        assert not pattern.search(f"{base}/v/{name}-0123456789ab.pmtiles/9/250/180"), name
    handler = re.search(r"maplibregl\.addProtocol\('pmtiles'.*?\n\}\);", source, re.S).group(0)
    assert "status: 404" in handler and "OVERZOOM_ARCHIVES.test(params.url)" in handler
    assert "BLANK_TILE" in handler, "the other archives still need the transparent tile"


def test_the_plain_tile_page_defaults_to_the_live_endpoint():
    """xyz.html exists to show that the documented /xyz/ path works.

    Pinned to a version instead, it demonstrates a tree nobody is served: it
    sat on v6 after the CDN was moved to v7, so the page advertised one tree
    while the endpoint it documents served another. A stale default here is
    invisible -- the map still draws, just from the wrong tree.
    """
    html = (APP_JS.parent / "xyz.html").read_text()
    wanted = re.search(r"const WANTED = [^;]*\|\|\s*'([^']+)'", html)
    assert wanted, "xyz.html no longer picks a default tree the same way"
    assert wanted.group(1) == "live", (
        f"xyz.html defaults to {wanted.group(1)!r}; it must default to the live "
        "path so the page shows the endpoint it documents")


def test_zoom_expressions_are_top_level_in_every_paint_property():
    """MapLibre refuses ["zoom"] nested inside arithmetic, and one such error
    stops the whole style from loading -- which is how the rivers layer took the
    viewer down for an hour on 2026-10-17. Every zoom ramp has to be the
    outermost expression of its property."""
    import re

    js = _app_js()
    for m in re.finditer(r"'(line-width|line-opacity|fill-opacity|raster-opacity|circle-radius|text-size)': \[('\*'|'\+'|'-'|'/')", js):
        raise AssertionError(f"{m.group(1)} starts with arithmetic around a zoom ramp at offset {m.start()}")
    assert ("const RIVER_WIDTH = ['interpolate', ['linear'], ['zoom'],\n"
            "  4, ['*', RIVER_PEN, 0.5]") in js

