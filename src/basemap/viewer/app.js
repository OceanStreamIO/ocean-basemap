/**
 * OceanStream Basemap — Advanced Test Map
 *
 * Features:
 *   - Continuous shaded bathymetry and land relief, contours and labels
 *   - Per-layer opacity sliders with visibility toggles
 *   - Depth profile on click (popup with zone classification + color swatch)
 *   - Bathymetric depth legend (color bar + ticks)
 *   - Coordinate display with DD/DMS toggle
 *   - Dynamic graticule overlay (adapts to zoom)
 *   - Keyboard shortcuts: G (graticule), L (layer panel)
 */

/* global maplibregl, pmtiles */

// ═══════════════════════════════════════════════════════════════════════
//  CONSTANTS
// ═══════════════════════════════════════════════════════════════════════

// Tile origins, overridable so one viewer can front either the local
// `oceanbase serve` tree or a published bucket. Precedence:
//   ?base=<url>&pmtiles=<url>  >  window.BASEMAP_* (see config.js)  >  local
const _q = new URLSearchParams(window.location.search);
const _trim = (u) => u.replace(/\/+$/, '');

const LOCAL_DEFAULT = `${window.location.origin}/tiles`;
const TILE_BASE = _trim(_q.get('base') || window.BASEMAP_TILE_BASE || LOCAL_DEFAULT);
// Uploads nest PMTiles under tiles/; the local server keeps them at the root.
const PMTILES_BASE = _trim(_q.get('pmtiles') || window.BASEMAP_PMTILES_BASE || TILE_BASE);

// Local tiles get reprocessed in place, so they must not be cached. Remote ones
// are immutable — busting them would defeat every range-request cache. ?v= is
// the escape hatch for the hour after an archive is republished, when a cached
// header would otherwise be read against a body that no longer matches it.
const CACHE_BUST = _q.get('v') ? `?v=${_q.get('v')}`
  : TILE_BASE === LOCAL_DEFAULT ? `?v=${Date.now()}` : '';

// Published archives carry a digest of their own bytes in the filename, and
// config.js is generated at deploy time with the map of them (see
// deploy/edito/tile-versions.sh). Reading those names rather than the fixed
// ones is what makes an archive genuinely immutable to a browser: a republish
// cannot leave a tab holding a directory page from one build and reading tiles
// out of the next, which draws flat, straight-edged holes across the map and
// survives every cache lifetime because nothing about the URL changed.
//
// It also makes a pinned style pin the data, since the style is exported from
// this map, and it lets a CDN cache the archives for a year with no
// invalidation. Without the map — a local tree, or a deploy whose manifest
// could not be read — it falls back to the unversioned name, which still works.
const TILE_VERSIONS = window.BASEMAP_TILE_VERSIONS || {};

// ?water=a|b|c draws the sea from a candidate palette published beside the live
// archives, so a whole map can be compared rather than a screenshot of one
// coast. The candidates cover bathy_global and bathy_eu only -- the HR levels
// are most of the render cost and add nothing to a judgement about colour -- so
// bathy_hr is hidden while one is selected, rather than left showing the live
// palette inside its survey footprints and putting a colour step at their edge.
const WATER = (() => {
  const asked = new URLSearchParams(location.search).get('water');
  return ['a', 'b', 'c', 'd', 'e', 'f'].includes(asked) ? asked : null;
})();
const WATER_VARIANT_ARCHIVES = ['bathy_global', 'bathy_eu'];

// The candidates, in the order they are worth trying. More than one is kept on
// purpose: which reads best depends on the coast and the zoom, so this is a
// choice a reader makes rather than one the map makes for them.
const WATER_CHOICES = [
  ['',  'As published'],
  ['d', 'D \u2014 gentle blue'],
  ['e', 'E \u2014 clearly blue'],
  ['f', 'F \u2014 as blue as the lakes'],
  ['a', 'A \u2014 darker, same greyness'],
  ['b', 'B \u2014 pale shore, steep shelf'],
  ['c', 'C \u2014 between A and B'],
];

// ?try=1 swaps in preview archives rendered over one coast only, which is how a
// land or lake colour gets judged in minutes rather than in a five-hour render.
// They hold nothing outside their box, so those layers are simply absent
// elsewhere -- that is the trade, and it is why this is never the default.
const TRY_PREVIEW = (() => {
  const asked = new URLSearchParams(location.search).get('try');
  if (asked === '1') return 'try';
  return ['l1', 'l2', 'l3', 'l4'].includes(asked) ? `try-${asked}` : null;
})();
const TRY_ARCHIVES = ['land_relief_eu', 'coastal_water', 'water_polygons'];

// ?edge=vector draws observed water as polygons instead of the raster. The two
// are the same data: the raster is the 30 m source at its own spacing with a
// soft edge across the occurrence band, the polygons are a threshold with a
// crisp one. Which reads better is a judgement, so both ship.
const WATER_EDGE = new URLSearchParams(location.search).get('edge') === 'vector'
  ? 'vector' : 'raster';

const archivePath = (name) => {
  if (WATER && WATER_VARIANT_ARCHIVES.includes(name)) return `water-${WATER}/${name}.pmtiles`;
  if (TRY_PREVIEW && TRY_ARCHIVES.includes(name)) return `${TRY_PREVIEW}/${name}.pmtiles`;
  return TILE_VERSIONS[name] || `${name}.pmtiles`;
};
const pmtilesUrl = (name) => `pmtiles://${PMTILES_BASE}/${archivePath(name)}${CACHE_BUST}`;

const FONT_GLYPHS = 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf';

// GEBCO and Natural Earth are public domain; OSM coastlines are ODbL and the
// credit is a licence condition, not decoration. Do not hide the control.
const ATTR_GEBCO = '<a href="https://www.gebco.net">GEBCO</a>';
const ATTR_LAND = '<a href="https://www.naturalearthdata.com">Natural Earth</a>, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors (ODbL)';
// CC BY 4.0 — the credit is a licence condition. Same layer arco-3d already
// uses on the Cesium globe, so the two views share one imagery provider.
const ATTR_S2 = '<a href="https://s2maps.eu">Sentinel-2 cloudless</a> by <a href="https://eox.at">EOX</a> (modified Copernicus Sentinel data)';
const ATTR_HYDROLAKES = '<a href="https://www.hydrosheds.org/products/hydrolakes">HydroLAKES</a> (HydroSHEDS, CC BY 4.0)';
const ATTR_EMODNET = '<a href="https://emodnet.ec.europa.eu/en/bathymetry">EMODnet Bathymetry</a>';
const ATTR_KARTVERKET = '&copy; <a href="https://www.kartverket.no">Kartverket</a> (CC BY 4.0)';
// Place names are two sources in one archive: Kartverket's N50 for Norway and
// GeoNames for the rest of the world. Both are CC BY 4.0, so naming them is a
// licence condition rather than a courtesy, and crediting only the one that
// happens to come first in the file does not meet it. The archive keeps its
// full credit although the viewer draws only the Norwegian layer: it is still
// fetched, and crediting a dataset one layer too widely costs nothing beside
// crediting one too narrowly.
//
// geoBoundaries was credited here too, for boundaries.pmtiles. That archive
// has no layer left in the style, so nothing fetches it and nothing draws it;
// its credit went with it. Put a boundary layer back and the credit has to
// come back with it.
const ATTR_PLACES = `${ATTR_KARTVERKET}, <a href="https://www.geonames.org">GeoNames</a> (CC BY 4.0)`;
// Undersea feature names come from a published gazetteer rather than from the
// grids, and are cited the way its compilers ask to be.
const ATTR_OCEAN_LABELS =
  '<a href="https://www.naturalearthdata.com">Natural Earth</a>, '
  + '<a href="https://www.ngdc.noaa.gov/gazetteer/">GEBCO Gazetteer of Undersea '
  + 'Feature Names</a> (IHO-IOC SCUFN)';
const ATTR_ESRI_IMAGERY = 'Imagery &copy; <a href="https://www.esri.com">Esri</a>, Maxar, Earthstar Geographics';
const ATTR_COPDEM = 'Copernicus DEM &copy; DLR e.V., &copy; Airbus Defence and Space GmbH';
const ATTR_GSW = '<a href="https://global-surface-water.appspot.com">JRC Global Surface Water</a>';

// Depth bands for the legend and the click readout. The depth breaks must stay
// in step with DEPTH_BREAKS in config.py, which the (now invisible) depth-zone
// polygons are classified against. The colours are the bathymetry tiles' ramp,
// color-ramps/bathymetry-water.txt, sampled at each break so the legend and
// the popup swatches match what is on the map. They are generated from it, not
// typed: hand-copying left a stop at -40 m lighter than its neighbours either
// side, so the legend showed a pale band the map never had.
const DEPTH_STOPS = [
  { depth: -11000, color: '#165974', label: 'Hadal zone' },
  { depth:  -8000, color: '#3c97bc', label: 'Abyssal trench' },
  { depth:  -6000, color: '#4ea7cd', label: 'Deep abyssal' },
  { depth:  -5000, color: '#5eb2da', label: 'Lower abyssal' },
  { depth:  -4000, color: '#70bce4', label: 'Upper abyssal' },
  { depth:  -3000, color: '#78c4ea', label: 'Bathypelagic' },
  { depth:  -2000, color: '#7ec8ee', label: 'Mesopelagic' },
  { depth:  -1500, color: '#80caf0', label: 'Upper mesopelagic' },
  { depth:  -1000, color: '#89cef0', label: 'Twilight zone' },
  { depth:   -500, color: '#99d6f0', label: 'Continental slope' },
  { depth:   -200, color: '#a3d4ed', label: 'Outer shelf' },
  { depth:   -100, color: '#b6d6f2', label: 'Mid shelf' },
  { depth:    -75, color: '#bedaf4', label: 'Inner shelf' },
  { depth:    -50, color: '#c7dff4', label: '50–40 m' },
  { depth:    -40, color: '#cae1f5', label: '40–30 m' },
  { depth:    -30, color: '#cee2f5', label: '30–20 m' },
  { depth:    -20, color: '#d1e4f5', label: '20–15 m' },
  { depth:    -15, color: '#d3e6f5', label: '15–10 m' },
  { depth:    -10, color: '#d4e6f5', label: '10–5 m' },
  { depth:     -5, color: '#d6e7f5', label: '5–2 m' },
  { depth:     -2, color: '#d7e7f5', label: '0–2 m' },
];

// MapLibre step expression for depth zone fill-color
const depthColorExpr = (() => {
  const expr = ['step', ['to-number', ['get', 'min_depth']], DEPTH_STOPS[0].color];
  for (let i = 1; i < DEPTH_STOPS.length; i++) {
    expr.push(DEPTH_STOPS[i].depth, DEPTH_STOPS[i].color);
  }
  return expr;
})();

// Lakes carry one modelled mean depth each (HydroLAKES), in metres below the
// surface. They get their own ramp rather than the sea's: on the sea ramp a
// shallow lake lands in the near-white shallows and vanishes against the land.
// Keep in step with color-ramps/lakes.txt. Within a lake it is still one tone:
// a mean, not bathymetry.
const LAKE_STOPS = [
  { depth: 0, color: '#cee1ec' },
  { depth: 2, color: '#c4ddec' },
  { depth: 5, color: '#b9d8ec' },
  { depth: 10, color: '#a9d2ec' },
  { depth: 20, color: '#95c6e6' },
  { depth: 50, color: '#7fb7de' },
  { depth: 100, color: '#6aa8d6' },
  { depth: 200, color: '#5697cb' },
  { depth: 500, color: '#3f7fb8' },
  { depth: 1000, color: '#306ca2' },
];

// A lake at sea level sits against the sea, and should read like it: the
// limans by Odesa are the same water as the shelf outside them. These take the
// sea's ramp at their mean depth instead of the lake ramp.
// Lakes at or below this surface elevation count as coastal.
const COASTAL_LAKE_ELEV = 5;

// Coastal water's flat tone, which must equal COASTAL_WATER_RGB in
// pipeline/xyz.py: both draw the same water, and a test checks they agree.
// rgb(176, 208, 238): luminance 202, saturation 26%. Against land at about 215
// the old tone was three luminance levels away and told apart by saturation
// alone -- too thin a cue, and the one a red-green reader has least of.
const COASTAL_WATER = '#d8e8f5';

const luminance = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return (((n >> 16) & 255) * 54 + ((n >> 8) & 255) * 183 + (n & 255) * 18) >> 8;
};

// A lake at sea level takes the sea's ramp at its own depth -- floored at the
// coastal tone, exactly as the tile renderer floors it.
//
// Without the floor the ramp's shallow end is near-white, and the enclosed
// basins of a lagoon are a metre deep: the fish farms inside the Venice lagoon
// came out as white holes punched through water that is otherwise continuous.
// The floor is applied to the stops here rather than in an expression, because
// they are constants and MapLibre cannot compare two colours at runtime.
// The colour of water we have no soundings for. It fills the strip between the
// crisp OSM shoreline and where the rendered tiles begin, and -- far more
// visibly -- whole bodies of water the bathymetry does not reach: the Tagus
// estuary is transparent inside its own tile, and the Danube delta has no tile
// in any archive at all. Those read as water or they read as nothing, and no
// amount of tuning the depth ramp touches them, because the ramp is not what
// is drawing them.
//
// It is exactly the ramp's surface stop, because the pixel beside it is that
// colour and any difference shows as a seam along the whole coast. A test
// asserts the two agree, since they are edited in different files.
// The strip between the OSM shoreline and where the bathymetry raster begins.
// It has to match the sea ramp's 0 m stop, so it follows the chosen palette:
// hardcoded to the published ramp it stayed near-white under a blue candidate
// and put a white halo around every coast, which reads as a deliberate glow and
// is not one.
const SHORE_BY_PALETTE = {
  '':  '#d8e8f5', a: '#e0e4e7', b: '#ecf0f3', c: '#e6eaed',
  d:  '#d8e8f5', e: '#cce2f6', f: '#c0dcf6',
};
const SHORE_WATER = SHORE_BY_PALETTE[WATER || ''] ?? '#ecf0f3';

const lakeColorExpr = (() => {
  const expr = ['step', ['to-number', ['get', 'depth_avg']], LAKE_STOPS[0].color];
  for (let i = 1; i < LAKE_STOPS.length; i++) {
    expr.push(LAKE_STOPS[i].depth, LAKE_STOPS[i].color);
  }
  return expr;
})();

// Earth constants
const EARTH_RADIUS_KM = 6371.0088;
const EARTH_RADIUS_NM = 3440.065;

// ═══════════════════════════════════════════════════════════════════════
//  LAYER DEFINITIONS
// ═══════════════════════════════════════════════════════════════════════

/**
 * Each group: { id, name, layerIds[], defaultOpacity, opacityProp }
 * opacityProp is the paint property to control (e.g. 'raster-opacity', 'fill-opacity')
 */
const LAYER_GROUPS = [
  {
    id: 'bathymetry', name: 'Bathymetry',
    layerIds: ['bathy-global', 'bathy-eu', 'bathy-hr'],
    defaultOpacity: 1.0,
    opacityProps: {
      'bathy-global': 'raster-opacity',
      'bathy-eu': 'raster-opacity',
      'bathy-hr': 'raster-opacity',
    },
  },
  {
    id: 'satellite', name: 'Satellite (land)',
    layerIds: ['satellite-land'],
    defaultVisible: false,
    defaultOpacity: 1.0,
    opacityProps: { 'satellite-land': 'raster-opacity' },
  },
  {
    id: 'satellite-esri', name: 'Satellite (Esri)',
    layerIds: ['satellite-esri'],
    defaultVisible: false,
    defaultOpacity: 1.0,
    opacityProps: { 'satellite-esri': 'raster-opacity' },
  },
  {
    id: 'place-names-no', name: 'Place Names (Norway)',
    layerIds: ['place-names-no'],
    defaultOpacity: 1.0,
    opacityProps: { 'place-names-no': 'text-opacity' },
  },
  {
    id: 'land', name: 'Land',
    layerIds: ['land-fill', 'land-relief', 'land-relief-eu'],
    // Opaque, and that is the point. 60% was chosen against the old saturated
    // khaki, then raised to 85% when the paler EMODnet palette replaced it --
    // but the layer underneath the land is the shore-water background at
    // luminance 238, so any transparency at all mixes near-white into every
    // land pixel. At 85% the relief lost 15% of its range and the surface came
    // out flat and faintly blue: the same hillshade, drawn opaque in the tile
    // tree, shows dunes and slopes that vanish here. Contrast on land is what
    // the relief is for, and it is also what a colour-blind reader has instead
    // of hue. The panel slider still moves it either way.
    defaultOpacity: 1.0,
    opacityProps: {
      'land-fill': 'fill-opacity',
      'land-relief': 'raster-opacity',
      'land-relief-eu': 'raster-opacity',
    },
  },
  {
    // Its own group, because the line is the thing a reader adjusts most. It
    // used to ride the Land slider, so softening the outline also dimmed the
    // relief under it -- two different judgements on one control.
    id: 'coastline', name: 'Coastline',
    layerIds: ['coastline-line'],
    defaultOpacity: 0.9,
    opacityProps: { 'coastline-line': 'line-opacity' },
  },
  {
    // The ring around a lake, separated from the lake's fill for the same
    // reason the coastline was separated from the land: the outline and the
    // body it encloses are judged apart.
    id: 'lake-outline', name: 'Lake outline',
    layerIds: ['lake-outline'],
    defaultOpacity: 0.8,
    opacityProps: { 'lake-outline': 'line-opacity' },
  },
  {
    id: 'water-features', name: 'Lakes',
    layerIds: ['lake-fill', 'lake-relief',
               'coastal-water', 'water-polygons'],
    defaultOpacity: 1.0,
    opacityProps: {
      'lake-fill': 'fill-opacity',
      'lake-relief': 'raster-opacity',
      'coastal-water': 'raster-opacity',
      'water-polygons': 'fill-opacity',
      'lake-outline': 'line-opacity',
    },
  },
  {
    id: 'place-labels', name: 'Place Labels (ours)',
    layerIds: ['ocean-labels', 'lake-labels', 'city-labels'],
    defaultVisible: false,
    defaultOpacity: 1.0,
    opacityProps: {
      'ocean-labels': 'text-opacity',
      'lake-labels': 'text-opacity',
      'city-labels': 'text-opacity',
    },
  },
  {
    id: 'contours', name: 'Depth Contours',
    layerIds: ['depth-contours-major', 'depth-contours-minor', 'depth-contours-shelf', 'depth-contours-shallow', 'depth-labels'],
    defaultOpacity: 1.0,
    // Contours use line-color with embedded alpha, so we control via line-opacity
    opacityProps: {
      'depth-contours-major': 'line-opacity',
      'depth-contours-minor': 'line-opacity',
      'depth-contours-shelf': 'line-opacity',
      'depth-contours-shallow': 'line-opacity',
      'depth-labels': 'text-opacity',
    },
  },
  {
    id: 'reference-labels', name: 'ESRI Labels + Undersea Names',
    layerIds: ['esri-ocean-labels'],
    defaultOpacity: 0.85,
    opacityProps: { 'esri-ocean-labels': 'raster-opacity' },
  },
  {
    id: 'graticule', name: 'Graticule',
    layerIds: ['graticule-lines', 'graticule-labels'],
    defaultOpacity: 1.0,
    opacityProps: {
      'graticule-lines': 'line-opacity',
      'graticule-labels': 'text-opacity',
    },
    defaultVisible: false,
  },
];

// ═══════════════════════════════════════════════════════════════════════
//  MAP STYLE
// ═══════════════════════════════════════════════════════════════════════

const mapStyle = {
  version: 8,
  name: 'OceanStream Basemap',
  glyphs: FONT_GLYPHS,
  sources: {
    // Continuous shaded relief. Each archive declares its own zoom range and
    // bounds, which MapLibre reads from the PMTiles header, so a source
    // overzooms past its last level instead of going blank.
    bathy_global: { type: 'raster', url: pmtilesUrl('bathy_global'), tileSize: 256, attribution: ATTR_GEBCO },
    bathy_eu: { type: 'raster', url: pmtilesUrl('bathy_eu'), tileSize: 256, attribution: ATTR_EMODNET },
    bathy_hr: { type: 'raster', url: pmtilesUrl('bathy_hr'), tileSize: 256, attribution: ATTR_EMODNET },
    land_relief: { type: 'raster', url: pmtilesUrl('land_relief'), tileSize: 256, attribution: ATTR_COPDEM },
    land_relief_eu: { type: 'raster', url: pmtilesUrl('land_relief_eu'), tileSize: 256, attribution: ATTR_COPDEM },
    depth_zones: { type: 'vector', url: pmtilesUrl('depth_zones'), attribution: ATTR_GEBCO },
    depth_zones_eu: { type: 'vector', url: pmtilesUrl('depth_zones_eu'), attribution: ATTR_EMODNET },
    depth_zones_hr: { type: 'vector', url: pmtilesUrl('depth_zones_hr'), attribution: ATTR_EMODNET },
    depth_contours: { type: 'vector', url: pmtilesUrl('depth_contours'), attribution: ATTR_GEBCO },
    coastal_water: { type: 'raster', url: pmtilesUrl('coastal_water'), tileSize: 256,
                     attribution: ATTR_GSW },
    // GeoJSON while this is a prototype: the preview covers one coast and is a
    // few hundred kilobytes, so there is nothing to tile yet. A global version
    // would be a vector archive like every other.
    water_polygons: {
      type: 'geojson', attribution: ATTR_GSW,
      data: `${PMTILES_BASE}/${TRY_PREVIEW ? `${TRY_PREVIEW}/` : ''}water_polygons.geojson${CACHE_BUST}`,
    },
    land_base: { type: 'vector', url: pmtilesUrl('land_base'), attribution: ATTR_LAND },
    sea_mask: { type: 'vector', url: pmtilesUrl('sea_mask'), attribution: ATTR_LAND },
    lakes: { type: 'vector', url: pmtilesUrl('lakes'), attribution: ATTR_HYDROLAKES },
    places: { type: 'vector', url: pmtilesUrl('places'), attribution: ATTR_PLACES },
    lake_relief: { type: 'raster', url: pmtilesUrl('lake_relief'), tileSize: 256, attribution: ATTR_GEBCO },
    ocean_labels: { type: 'vector', url: pmtilesUrl('ocean_labels'), attribution: ATTR_OCEAN_LABELS },
    esri_ocean_ref: {
      type: 'raster',
      tiles: ['https://services.arcgisonline.com/arcgis/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 13,
      attribution: 'Labels &copy; <a href="https://www.esri.com">Esri</a>',
    },
    // Esri's imagery basemap: sub-metre in many places, and the deepest of the
    // three. Their terms of use apply, as they already do for the ocean labels.
    esri_imagery: {
      type: 'raster',
      tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 19,
      attribution: ATTR_ESRI_IMAGERY,
    },
    // Native resolution is 10 m, i.e. about z14. The service will serve
    // deeper levels, but they are upsampled - overzoom locally instead.
    satellite: {
      type: 'raster',
      tiles: ['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg'],
      tileSize: 256,
      maxzoom: 14,
      attribution: ATTR_S2,
    },
  },
  layers: [
    // 1. Ocean base: the fallback beneath everything, in the shoreline colour.
    {
      id: 'ocean-base', type: 'background',
      paint: { 'background-color': SHORE_WATER },
    },
    // 2. Land. The relief tiles are opaque rectangles with their hillshade
    //    baked in; they carry no coastline of their own, because the sea mask
    //    above hides their sea part along the exact OSM edge.
    {
      id: 'land-fill', type: 'fill', source: 'land_base', 'source-layer': 'land',
      paint: { 'fill-color': '#d2d8d5', 'fill-opacity': 1.0 },
    },
    { id: 'land-relief', type: 'raster', source: 'land_relief', paint: { 'raster-opacity': 1.0 } },
    { id: 'land-relief-eu', type: 'raster', source: 'land_relief_eu', paint: { 'raster-opacity': 1.0 } },
    // 3. Satellite imagery, in place of the relief when it is switched on.
    {
      id: 'satellite-land', type: 'raster', source: 'satellite',
      layout: { visibility: 'none' },
      paint: { 'raster-opacity': 1.0, 'raster-saturation': -0.1 },
    },
    {
      id: 'satellite-esri', type: 'raster', source: 'esri_imagery',
      layout: { visibility: 'none' },
      paint: { 'raster-opacity': 1.0 },
    },
    // 3b. Observed coastal water. OSM's coastline runs along the outer shore of
    //     a barrier island, so every mask cut from the land polygons calls the
    //     water behind it land: the Venice lagoon, the Wadden Sea, the limans.
    //     The flat tile tree paints these from the same raster while it renders,
    //     which is why it shows a lagoon the vector map did not. Drawn over the
    //     land and the imagery and under the sea mask, so it only ever adds
    //     water where the coastline wrongly claimed land.
    {
      id: 'coastal-water', type: 'raster', source: 'coastal_water',
      layout: { visibility: WATER_EDGE === 'vector' ? 'none' : 'visible' },
      paint: { 'raster-opacity': 1.0 },
    },
    // The same water as polygons. Sharp at any zoom, because the edge is a
    // vector rather than a 30 m cell magnified: the raster above is the source
    // at its own spacing, and nothing deeper resolves more of it.
    {
      // No source-layer: this source is GeoJSON, which has no layers inside it,
      // and naming one makes MapLibre reject the layer silently.
      id: 'water-polygons', type: 'fill', source: 'water_polygons',
      layout: { visibility: WATER_EDGE === 'vector' ? 'visible' : 'none' },
      paint: { 'fill-color': COASTAL_WATER, 'fill-opacity': 1.0 },
    },
    // 4. Sea mask — the complement of the OSM land polygons. MapLibre cannot
    //    clip a raster to a polygon, so this is what hides the land relief and
    //    the imagery beneath it at sea, with the crisp vector edge.
    {
      id: 'sea-mask', type: 'fill', source: 'sea_mask', 'source-layer': 'sea',
      paint: {
        'fill-color': SHORE_WATER,
        'fill-opacity': 1.0,
      },
    },
    // 5. Bathymetry: continuous colour with its hillshade baked in, carrying
    //    the sea as its alpha channel -- inset a pixel from the coast, so even
    //    a coarse archive stretched to high zoom never tints the land. The sea
    //    mask beneath fills that strip up to the crisp OSM edge. Each finer
    //    archive draws over the coarser ones where it exists.
    { id: 'bathy-global', type: 'raster', source: 'bathy_global', paint: { 'raster-opacity': 1.0 } },
    { id: 'bathy-eu', type: 'raster', source: 'bathy_eu', paint: { 'raster-opacity': 1.0 } },
    {
      id: 'bathy-hr', type: 'raster', source: 'bathy_hr',
      layout: { visibility: WATER ? 'none' : 'visible' },
      paint: { 'raster-opacity': 1.0 },
    },
    // 6. Depth-zone polygons, invisible. The rasters carry no attributes, so
    //    these stay in the style for the click readout to query.
    {
      id: 'depth-zones', type: 'fill', source: 'depth_zones', 'source-layer': 'depth_zones',
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 0 },
    },
    {
      id: 'depth-zones-eu', type: 'fill', source: 'depth_zones_eu', 'source-layer': 'depth_zones_eu',
      minzoom: 6,
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 0 },
    },
    {
      id: 'depth-zones-hr', type: 'fill', source: 'depth_zones_hr', 'source-layer': 'depth_zones_hr',
      minzoom: 10,
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 0 },
    },
    // 7. Coastline stroke over everything, so the shoreline reads as one line.
    {
      id: 'coastline-line', type: 'line', source: 'land_base', 'source-layer': 'coastline',
      paint: {
        'line-color': '#8fa89a',
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.3, 6, 0.6, 10, 1.0, 14, 1.4],
        'line-opacity': 0.9,
      },
    },
    // 8. Lakes: HydroLAKES outlines, 1.4 million of them, each toned by its
    //    mean depth on the sea's ramp. They sit above the land relief, which
    //    holds no lakes of its own -- the Copernicus DEM flattens their
    //    surfaces, so without these a lake is just level ground.
    //    One ramp for all of them. Lakes near sea level took the sea's ramp
    //    until it became clear that nothing here says whether a lake meets
    //    the sea: Ladoga went pale beside Onega, a depth test then threw out
    //    the Etang de Berre, and an area test threw in Sarygamysh, 400 km
    //    into the Karakum. The two ramps are 44 luminance levels apart at the
    //    same depth, so each miss showed as a different kind of water beside
    //    its neighbour. Water genuinely continuous with the sea that OSM
    //    calls land is drawn by the observed-water raster instead.
    {
      id: 'lake-fill', type: 'fill', source: 'lakes', 'source-layer': 'lakes',
      paint: { 'fill-color': lakeColorExpr, 'fill-opacity': 1.0 },
    },
    // 8a. Lake beds, where GEBCO has soundings rather than a flat surface:
    //     2,472 lakes, drawn over their flat tone. Inset from the shoreline
    //     like the bathymetry, so the vector fill and outline keep the edge.
    {
      id: 'lake-relief', type: 'raster', source: 'lake_relief',
      paint: { 'raster-opacity': 1.0 },
    },
    {
      id: 'lake-outline', type: 'line', source: 'lakes', 'source-layer': 'lakes',
      paint: {
        'line-color': '#7aa7c7',
        'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.3, 8, 0.6, 12, 1.0],
        'line-opacity': 0.8,
      },
    },
    // 6. Major contours (1000m)
    {
      id: 'depth-contours-major', type: 'line', source: 'depth_contours', 'source-layer': 'depth_contours',
      filter: ['all', ['<', ['get', 'depth'], 0], ['==', ['%', ['get', 'depth'], 1000], 0]],
      minzoom: 2,
      paint: {
        'line-color': '#4878a8',
        'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.4, 4, 0.7, 8, 1.2, 10, 1.6],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 2, 0.20, 4, 0.35, 6, 0.45, 8, 0.50],
      },
    },
    // 7. Minor contours (500m)
    {
      id: 'depth-contours-minor', type: 'line', source: 'depth_contours', 'source-layer': 'depth_contours',
      filter: ['all', ['<', ['get', 'depth'], 0], ['==', ['%', ['get', 'depth'], 500], 0], ['!=', ['%', ['get', 'depth'], 1000], 0]],
      minzoom: 4,
      paint: {
        'line-color': '#4878a8',
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.3, 6, 0.6, 10, 1.0],
        'line-dasharray': [6, 3],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.12, 5, 0.25, 7, 0.35, 9, 0.42],
      },
    },
    // 8. Shelf contours (200m + fixed -50/-100m)
    {
      id: 'depth-contours-shelf', type: 'line', source: 'depth_contours', 'source-layer': 'depth_contours',
      filter: ['all',
        ['<', ['get', 'depth'], 0],
        ['>', ['get', 'depth'], -2000],
        ['any',
          ['all', ['==', ['%', ['get', 'depth'], 200], 0], ['!=', ['%', ['get', 'depth'], 500], 0]],
          ['==', ['get', 'depth'], -50],
          ['==', ['get', 'depth'], -100],
        ],
      ],
      minzoom: 5,
      paint: {
        'line-color': '#4878a8',
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.2, 5, 0.4, 7, 0.7, 10, 1.0],
        'line-dasharray': [3, 3],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.10, 5, 0.20, 7, 0.30, 9, 0.40],
      },
    },
    // 8b. Shallow contours
    {
      id: 'depth-contours-shallow', type: 'line', source: 'depth_contours', 'source-layer': 'depth_contours',
      filter: ['all',
        ['<', ['get', 'depth'], 0],
        ['>', ['get', 'depth'], -200],
        ['!=', ['%', ['get', 'depth'], 200], 0],
        ['!=', ['%', ['get', 'depth'], 500], 0],
        ['!=', ['%', ['get', 'depth'], 1000], 0],
      ],
      minzoom: 6,
      paint: {
        'line-color': '#4878a8',
        'line-width': ['interpolate', ['linear'], ['zoom'], 6, 0.2, 7, 0.4, 9, 0.6, 10, 0.8],
        'line-dasharray': [2, 2],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 6, 0.10, 7, 0.20, 9, 0.30],
      },
    },
    // 9. Depth contour labels
    {
      id: 'depth-labels', type: 'symbol', source: 'depth_contours', 'source-layer': 'depth_contours',
      filter: ['all', ['<', ['get', 'depth'], 0], ['==', ['%', ['get', 'depth'], 500], 0]],
      minzoom: 3,
      layout: {
        'symbol-placement': 'line',
        'text-field': ['concat', ['to-string', ['abs', ['get', 'depth']]], ' m'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 3, 9, 5, 11, 8, 13, 10, 15],
        'text-font': ['Noto Sans Medium'],
        'text-max-angle': 30,
        'text-padding': 40,
      },
      paint: {
        'text-color': '#1a3a6c',
        'text-halo-color': 'rgba(180, 210, 240, 0.8)',
        'text-halo-width': 1.5,
      },
    },
    // 12. ESRI Ocean Reference — labels for oceans, seas, countries, cities
    {
      // 10. Our own labels. Esri's reference raster carries the same names as
      //     pixels, which cannot be restyled and mixes boundaries in; these
      //     are text, dark with a light halo, and each kind can be toggled.
      id: 'ocean-labels', type: 'symbol', source: 'ocean_labels', 'source-layer': 'ocean_labels',
      minzoom: 1,
      layout: {
        visibility: 'none',
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Italic'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 2, 11, 6, 13, 10, 15],
        'text-max-width': 8,
      },
      paint: {
        'text-color': '#2c5c80',
        'text-halo-color': 'rgba(255, 255, 255, 0.85)',
        'text-halo-width': 1.2,
      },
    },
    {
      id: 'lake-labels', type: 'symbol', source: 'land_base', 'source-layer': 'lakes',
      minzoom: 4,
      layout: {
        visibility: 'none',
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Italic'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 4, 10, 8, 12, 11, 14],
        'text-max-width': 8,
      },
      paint: {
        'text-color': '#2c5c80',
        'text-halo-color': 'rgba(255, 255, 255, 0.85)',
        'text-halo-width': 1.2,
      },
    },
    {
      id: 'city-labels', type: 'symbol', source: 'land_base', 'source-layer': 'cities',
      minzoom: 4,
      // Natural Earth ranks places; at low zoom keep to the ones that carry.
      filter: ['<=', ['to-number', ['get', 'scalerank']], 7],
      layout: {
        visibility: 'none',
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Regular'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 4, 10, 8, 12, 11, 13],
        'text-offset': [0, 0.6],
        'text-anchor': 'top',
      },
      paint: {
        'text-color': '#4a5560',
        'text-halo-color': 'rgba(255, 255, 255, 0.85)',
        'text-halo-width': 1.2,
      },
    },
    {
      id: 'place-names-no', type: 'symbol', source: 'places', 'source-layer': 'places',
      minzoom: 6,
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Regular'],
        // Kartverket's type sizes, scaled to the screen: 6.0 is a farm and
        // 12.7 a city. The first pass mapped these almost flat, 10.5 to 15,
        // which drew a city barely larger than a farm and both too small.
        'text-size': ['interpolate', ['linear'], ['get', 'rank'], 6, 12, 8.5, 14, 12.7, 21],
        'text-padding': 5,
        'symbol-sort-key': ['-', 20, ['get', 'rank']],
      },
      paint: {
        'text-color': '#2f3944',
        'text-halo-color': 'rgba(255, 255, 255, 0.92)',
        'text-halo-width': 1.5,
      },
    },
    {
      id: 'esri-ocean-labels', type: 'raster', source: 'esri_ocean_ref',
      paint: {
        // Esri's text and its boundary lines are the same pixels, so anything
        // that darkens the labels darkens the borders with them. This is the
        // middle: dark enough to read over a pale lake, held back a little so
        // the boundaries do not dominate. Our own lighter vector boundaries
        // are drawn underneath, and can be turned off if these suffice.
        'raster-opacity': 0.85,
        // These labels are pixels, not text, so they cannot be recoloured --
        // but the layer can be darkened, which comes to the same thing since
        // the tiles are transparent apart from the lettering. Esri's ink is a
        // light blue (70, 162, 212): against a pale lake it measured 1.6:1,
        // and darkening it to about (40, 105, 140) brings that to ~3.5:1.
        // Lower still reads better but drags Esri's boundary lines to black.
        'raster-brightness-max': 0.75,
      },
    },
  ],
};


// ═══════════════════════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════════════════════

// Register PMTiles protocol
const protocol = new pmtiles.Protocol();

// A 1x1 fully transparent PNG, for tiles an archive does not contain.
// Check any replacement actually decodes to (0, 0, 0, 0): the widely copied
// "blank" PNG is a half-opaque red pixel, which paints every missing tile red.
const BLANK_TILE = Uint8Array.from(
  atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNgYGBgAAAABQABeqhXUAAAAABJRU5ErkJggg=='),
  c => c.charCodeAt(0),
);

// The raster archives are sparse: the bathymetry holds no tile where there is
// no sea, the land relief none where there is no land, the lake beds none away
// from a lake. For those the protocol resolves with no data, and MapLibre's
// raster loader leaves the tile "loading" for ever -- which keeps the map from
// ever reaching its load event, so nothing that is built there (the layer
// panel, the depth legend, the info bar) ever appears. Hand it a transparent
// tile instead. Vector sources handle an empty response themselves.
maplibregl.addProtocol('pmtiles', async (params, abortController) => {
  const result = await protocol.tile(params, abortController);
  if (params.type !== 'json' && !result?.data) {
    return { data: BLANK_TILE };
  }
  return result;
});

const map = new maplibregl.Map({
  container: 'map',
  style: mapStyle,
  center: [0, 20],
  zoom: 2.5,
  hash: true,
  // Our own credit line instead, in #credits. MapLibre's control lists every
  // source's attribution and wraps: ten credits came to about 1,500 px, two
  // lines on most screens, and it grew and shrank as sources dropped in and
  // out of view, so it appeared to vanish on zooming. The per-source
  // attributions stay in the style, which is what reaches anyone loading
  // style.json; this is only how this page chooses to show them.
  attributionControl: false,
});

map.addControl(new maplibregl.NavigationControl(), 'top-right');
map.addControl(new maplibregl.ScaleControl({ maxWidth: 200, unit: 'nautical' }), 'bottom-right');

// ═══════════════════════════════════════════════════════════════════════
//  CREDITS
// ═══════════════════════════════════════════════════════════════════════

// What has to be named, and nothing else. Four of these are CC BY 4.0, where
// naming the source is a condition of use; OpenStreetMap is ODbL; GEBCO,
// EMODnet and Copernicus ask to be acknowledged and Copernicus specifies its
// wording. Natural Earth is public domain and the two gazetteers ask for a
// citation rather than a credit, so they are carried in the style -- which is
// what a reuser gets -- and left off the page, which is what "only what is
// required" means here.
const CREDIT_LINK = (href, text) =>
  `<a href="${href}" target="_blank" rel="noopener">${text}</a>`;

const CREDITS_ALWAYS = [
  CREDIT_LINK('https://www.gebco.net', 'GEBCO'),
  CREDIT_LINK('https://emodnet.ec.europa.eu/en/bathymetry', 'EMODnet'),
  'Copernicus DEM &copy; DLR e.V., &copy; Airbus DS',
  `&copy; ${CREDIT_LINK('https://www.openstreetmap.org/copyright', 'OpenStreetMap')} contributors`,
  CREDIT_LINK('https://www.hydrosheds.org/products/hydrolakes', 'HydroLAKES'),
  CREDIT_LINK('https://www.kartverket.no', 'Kartverket'),
  CREDIT_LINK('https://www.geonames.org', 'GeoNames'),
];

// Only while the layer that uses them is switched on.
const CREDITS_WHEN_ON = {
  'reference-labels': `Labels &copy; ${CREDIT_LINK('https://www.esri.com', 'Esri')}`,
  satellite: CREDIT_LINK('https://s2maps.eu', 'Sentinel-2 cloudless by EOX'),
  'satellite-esri': `Imagery &copy; ${CREDIT_LINK('https://www.esri.com', 'Esri')}, Maxar`,
};

function updateCredits() {
  const target = document.getElementById('credits-list');
  if (!target) return;
  const on = new Set(
    [...document.querySelectorAll('.layer-group.visible')].map(el => el.dataset.groupId),
  );
  const extra = Object.entries(CREDITS_WHEN_ON)
    .filter(([id]) => on.has(id))
    .map(([, html]) => html);
  target.innerHTML = [...CREDITS_ALWAYS, ...new Set(extra)].join('<span class="dot">&middot;</span>');
}



// ═══════════════════════════════════════════════════════════════════════
//  DIAGNOSTIC LOGGING
// ═══════════════════════════════════════════════════════════════════════
console.log('%c[DIAG] Map instance created', 'color: cyan; font-weight: bold');
console.log('[DIAG] Style sources:', Object.keys(mapStyle.sources));
console.log('[DIAG] Style layers:', mapStyle.layers.map(l => l.id));
console.log('[DIAG] CACHE_BUST value:', CACHE_BUST);

// Log tile request errors
map.on('error', (e) => {
  console.error('[DIAG] Map error:', e.error?.message || e.message || e);
});

map.on('data', (e) => {
  if (e.dataType === 'source' && e.isSourceLoaded) {
    console.log(`[DIAG] Source loaded: ${e.sourceId}`);
  }
});

map.on('load', () => {
  console.log('%c[DIAG] Map fully loaded!', 'color: lime; font-weight: bold');
  console.log(`[DIAG] Tile base: ${TILE_BASE}  PMTiles base: ${PMTILES_BASE}`);
  // Log all sources and their status
  const style = map.getStyle();
  for (const [name, src] of Object.entries(style.sources)) {
    console.log(`[DIAG] Source "${name}": type=${src.type}`, src.url || src.tiles || '');
  }
  // Log all layers with PAINT values
  for (const layer of style.layers) {
    const vis = map.getLayoutProperty(layer.id, 'visibility') || 'visible';
    const paint = {};
    if (layer.type === 'fill') {
      paint.opacity = map.getPaintProperty(layer.id, 'fill-opacity');
      paint.antialias = map.getPaintProperty(layer.id, 'fill-antialias');
    } else if (layer.type === 'raster') {
      paint.opacity = map.getPaintProperty(layer.id, 'raster-opacity');
      paint.contrast = map.getPaintProperty(layer.id, 'raster-contrast');
      paint.brightnessMin = map.getPaintProperty(layer.id, 'raster-brightness-min');
      paint.brightnessMax = map.getPaintProperty(layer.id, 'raster-brightness-max');
    } else if (layer.type === 'line') {
      paint.opacity = map.getPaintProperty(layer.id, 'line-opacity');
    }
    console.log(`[DIAG] Layer "${layer.id}": type=${layer.type}, vis=${vis}, paint=`, paint);
  }
});

// ═══════════════════════════════════════════════════════════════════════
//  LAYER PANEL — visibility toggles + opacity sliders
// ═══════════════════════════════════════════════════════════════════════

const eyeOpenSVG = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><ellipse cx="10" cy="10" rx="8" ry="5"/><circle cx="10" cy="10" r="2.5"/></svg>`;
const eyeClosedSVG = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"><ellipse cx="10" cy="10" rx="8" ry="5"/><line x1="4" y1="16" x2="16" y2="4"/></svg>`;

// Satellite imagery and the flat land fill occupy the same place in the stack,
// so only one may paint at a time. The coastline stroke is unaffected either
// way - it is the vector edge that keeps the shoreline crisp in both modes.
const GROUP_VISIBLE = {
  land: true, satellite: false, 'satellite-esri': false,
};
const IMAGERY = ['satellite', 'satellite-esri'];
const LAND_SURFACE = ['land-fill', 'land-relief', 'land-relief-eu'];

function syncLandSurface() {
  // Any imagery layer replaces the land relief; the sea mask covers them all.
  const imagery = IMAGERY.some(id => GROUP_VISIBLE[id]);
  const vis = GROUP_VISIBLE.land && !imagery ? 'visible' : 'none';
  LAND_SURFACE.forEach(lid => {
    if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', vis);
  });
}

// arco-3d groups its layer list under small uppercase captions with an accent
// icon. Thirteen rows in one column is a list; under four headings it is a
// panel someone can scan.
const SECTION_ICONS = {
  Seafloor: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M1 6c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 3 2"/><path d="M1 11c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 3 2"/></svg>',
  Surface: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 12l4-6 3 4 2-3 3 5z"/></svg>',
  Reference: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2C6 4 6 12 8 14"/></svg>',
  Labels: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 4h12M2 8h8M2 12h10"/></svg>',
};

// Which heading each group sits under, in panel order.
const GROUP_SECTION = {
  bathymetry: 'Seafloor',
  contours: 'Seafloor',
  'water-features': 'Surface',
  land: 'Surface',
  coastline: 'Surface',
  'lake-outline': 'Surface',
  satellite: 'Surface',
  'satellite-esri': 'Surface',
  'place-names-no': 'Labels',
  'place-labels': 'Labels',
  'reference-labels': 'Labels',
  graticule: 'Reference',
};

function applyGroupOpacity(group, frac) {
  for (const [lid, prop] of Object.entries(group.opacityProps)) {
    const maxOp = group.maxOpacity?.[lid] ?? 1.0;
    if (map.getLayer(lid)) map.setPaintProperty(lid, prop, frac * maxOp);
  }
}

// The sea palette is baked into the archives, so choosing one means loading
// different archives rather than restyling a layer. Reloading with ?water= is
// the honest way to do that: it swaps the sources cleanly and leaves the choice
// in the URL, so a view worth arguing about can be sent to someone else.
// ---------------------------------------------------------------------------
// Panel state in the URL
// ---------------------------------------------------------------------------
// Everything the panel can change goes in the query string, so a view can be
// sent to someone and arrive looking the same. Only departures from the
// defaults are written: a URL that says nothing means the map as published,
// which keeps a shared link short and keeps the defaults in one place.

const PANEL_STATE = (() => {
  const params = new URLSearchParams(location.search);
  const hidden = new Set((params.get('hide') || '').split(',').filter(Boolean));
  const opacity = {};
  for (const pair of (params.get('op') || '').split(',').filter(Boolean)) {
    const [id, value] = pair.split(':');
    const percent = Number(value);
    if (id && Number.isFinite(percent)) opacity[id] = Math.min(Math.max(percent, 0), 100);
  }
  return { hidden, opacity };
})();

function panelSettings() {
  const hidden = [...PANEL_STATE.hidden];
  const opacity = {};
  for (const group of LAYER_GROUPS) {
    const percent = PANEL_STATE.opacity[group.id];
    if (percent !== undefined && percent !== Math.round(group.defaultOpacity * 100)) {
      opacity[group.id] = percent;
    }
  }
  return { water: WATER || null, preview: TRY_PREVIEW || null, hidden, opacity };
}

function writePanelState() {
  const { hidden, opacity } = panelSettings();
  const url = new URL(location.href);
  if (hidden.length) url.searchParams.set('hide', hidden.join(','));
  else url.searchParams.delete('hide');
  const pairs = Object.entries(opacity).map(([id, v]) => `${id}:${v}`);
  if (pairs.length) url.searchParams.set('op', pairs.join(','));
  else url.searchParams.delete('op');
  history.replaceState(null, '', url);
}

// A button rather than just the URL: a reader pasting settings into an issue
// wants them named, not positional.
function settingsRow() {
  const div = document.createElement('div');
  div.className = 'layer-group settings-row';
  div.innerHTML = `
    <button type="button" class="settings-copy">Copy settings as JSON</button>
    <div class="settings-note" role="status"></div>
  `;
  const note = div.querySelector('.settings-note');
  div.querySelector('button').addEventListener('click', async () => {
    const text = JSON.stringify(panelSettings(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      note.textContent = 'Copied.';
    } catch {
      // Clipboard access is refused outside a secure context and in some
      // embeddings; showing the JSON is more use than an error.
      note.textContent = text;
    }
    setTimeout(() => { if (note.textContent === 'Copied.') note.textContent = ''; }, 2500);
  });
  return div;
}

// The same water drawn two ways, so they can be put side by side. The raster
// is the 30 m source at its own spacing with a soft edge across the occurrence
// band; the polygons are a threshold with a crisp one. Neither is obviously
// right, which is why both ship and this is a control rather than a constant.
function edgeRow() {
  const div = document.createElement('div');
  div.className = 'layer-group palette-row';
  const options = [['raster', 'Raster (soft, 30 m source)'],
                   ['vector', 'Polygons (sharp, thresholded)']]
    .map(([value, label]) =>
      `<option value="${value}"${WATER_EDGE === value ? ' selected' : ''}>${label}</option>`).join('');
  div.innerHTML = `
    <div class="layer-group-header"><span class="layer-name">Observed water</span></div>
    <select class="palette-select" aria-label="How observed water is drawn">${options}</select>
  `;
  div.querySelector('select').addEventListener('change', (event) => {
    const url = new URL(location.href);
    if (event.target.value === 'vector') url.searchParams.set('edge', 'vector');
    else url.searchParams.delete('edge');
    location.href = url.toString();
  });
  return div;
}

function paletteRow() {
  const div = document.createElement('div');
  div.className = 'layer-group palette-row';
  const options = WATER_CHOICES.map(([value, label]) =>
    `<option value="${value}"${(WATER || '') === value ? ' selected' : ''}>${label}</option>`).join('');
  div.innerHTML = `
    <div class="layer-group-header"><span class="layer-name">Coastal palette</span></div>
    <select class="palette-select" aria-label="Coastal colour palette">${options}</select>
    <div class="palette-note">Candidates are rendered to z11; survey detail returns on
      \u201cAs published\u201d.</div>
  `;
  div.querySelector('select').addEventListener('change', (event) => {
    const url = new URL(location.href);
    if (event.target.value) url.searchParams.set('water', event.target.value);
    else url.searchParams.delete('water');
    location.href = url.toString();
  });
  return div;
}

function buildLayerPanel() {
  const list = document.getElementById('layer-list');
  list.innerHTML = '';

  let section = null;
  const ordered = [...LAYER_GROUPS].sort((a, b) => {
    const order = ['Seafloor', 'Surface', 'Labels', 'Reference'];
    return order.indexOf(GROUP_SECTION[a.id] ?? 'Reference')
         - order.indexOf(GROUP_SECTION[b.id] ?? 'Reference');
  });

  ordered.forEach(group => {
    const heading = GROUP_SECTION[group.id] ?? 'Reference';
    if (heading !== section) {
      section = heading;
      const cap = document.createElement('div');
      cap.className = 'layer-section';
      cap.innerHTML = `${SECTION_ICONS[heading] ?? ''}<span>${heading}</span>`;
      list.appendChild(cap);
    }
    const visible = PANEL_STATE.hidden.has(group.id)
      ? false
      : group.defaultVisible !== false;
    const opacity = PANEL_STATE.opacity[group.id] ?? Math.round(group.defaultOpacity * 100);

    const div = document.createElement('div');
    // arco-3d tints a row that is on rather than marking it; the class is what
    // the stylesheet reads for that, and for the heavier label weight.
    div.className = visible ? 'layer-group visible' : 'layer-group';
    div.dataset.groupId = group.id;

    div.innerHTML = `
      <div class="layer-group-header">
        <span class="layer-name">${group.name}</span>
        <span class="eye-btn ${visible ? '' : 'off'}" data-group="${group.id}" title="Toggle visibility">${visible ? eyeOpenSVG : eyeClosedSVG}</span>
      </div>
      <div class="opacity-row">
        <input type="range" class="opacity-slider" min="0" max="100" value="${opacity}" data-group="${group.id}" />
        <label class="opacity-val">${opacity}%</label>
      </div>
    `;
    list.appendChild(div);
    if (group.id === 'bathymetry') list.appendChild(paletteRow());
    if (group.id === 'water-features') list.appendChild(edgeRow());
  });
  list.appendChild(settingsRow());

  // Eye toggle handlers
  list.querySelectorAll('.eye-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const gid = btn.dataset.group;
      const group = LAYER_GROUPS.find(g => g.id === gid);
      const isOff = btn.classList.toggle('off');
      if (isOff) PANEL_STATE.hidden.add(gid); else PANEL_STATE.hidden.delete(gid);
      writePanelState();
      btn.innerHTML = isOff ? eyeClosedSVG : eyeOpenSVG;
      btn.closest('.layer-group')?.classList.toggle('visible', !isOff);
      updateCredits();
      const vis = isOff ? 'none' : 'visible';
      if (gid in GROUP_VISIBLE) GROUP_VISIBLE[gid] = !isOff;
      group.layerIds.forEach(lid => {
        if (LAND_SURFACE.includes(lid)) return;
        if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', vis);
      });
      syncLandSurface();
    });
  });

  // Opacity slider handlers
  list.querySelectorAll('.opacity-slider').forEach(slider => {
    slider.addEventListener('input', () => {
      const gid = slider.dataset.group;
      const group = LAYER_GROUPS.find(g => g.id === gid);
      const val = parseInt(slider.value, 10);
      slider.nextElementSibling.textContent = val + '%';
      applyGroupOpacity(group, val / 100);
      PANEL_STATE.opacity[gid] = val;
      writePanelState();
    });
  });

  // And apply each group's default once, so the map agrees with the slider it
  // is showing. Without this the two are only in step while every default is
  // 1.0, and a group set to 60% reads 60% in the panel and draws at 100%.
  LAYER_GROUPS.forEach(group => applyGroupOpacity(group, group.defaultOpacity));
}

// Panel collapse/expand
document.getElementById('panel-collapse').addEventListener('click', () => {
  const panel = document.getElementById('layer-panel');
  panel.classList.toggle('collapsed');
  document.getElementById('panel-collapse').textContent = panel.classList.contains('collapsed') ? '▶' : '◀';
});


// ═══════════════════════════════════════════════════════════════════════
//  USER OVERLAYS
// ═══════════════════════════════════════════════════════════════════════
// Anyone can put their own data on the basemap without deploying anything:
//
//   ?overlay=https://host/lake-survey.pmtiles
//   ?overlay=https://host/track.geojson
//
// Repeat the parameter for several. A vector archive gets a layer per source
// layer, a raster archive is drawn as it is, and GeoJSON is split by geometry
// type. Each overlay appears in the panel with its own toggle and opacity.

const OVERLAY_COLOR = '#d97b2b';   // stands apart from everything in the basemap

function overlayLayers(id, source, sourceLayer) {
  const of = sourceLayer ? { source, 'source-layer': sourceLayer } : { source };
  return [
    { id: `${id}-fill`, type: 'fill', ...of,
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': OVERLAY_COLOR, 'fill-opacity': 0.35 } },
    { id: `${id}-line`, type: 'line', ...of,
      filter: ['!=', ['geometry-type'], 'Point'],
      paint: { 'line-color': OVERLAY_COLOR, 'line-width': 1.4 } },
    { id: `${id}-point`, type: 'circle', ...of,
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-color': OVERLAY_COLOR, 'circle-radius': 4,
               'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1 } },
  ];
}

async function addOverlay(url, index) {
  const id = `overlay-${index}`;
  const name = decodeURIComponent(url.split('/').pop().split('?')[0]) || `Overlay ${index + 1}`;
  const added = [];

  if (/\.(geojson|json)(\?|$)/i.test(url)) {
    map.addSource(id, { type: 'geojson', data: url });
    for (const layer of overlayLayers(id, id, null)) {
      map.addLayer(layer);
      added.push(layer.id);
    }
  } else {
    // Ask the archive what it holds rather than guessing from the name.
    const archive = new pmtiles.PMTiles(url);
    const header = await archive.getHeader();
    protocol.add(archive);
    if (header.tileType === 1) {           // 1 = Mapbox Vector Tile
      const meta = await archive.getMetadata();
      const sourceLayers = (meta.vector_layers || []).map(l => l.id);
      map.addSource(id, { type: 'vector', url: `pmtiles://${url}` });
      sourceLayers.forEach((sl, i) => {
        for (const layer of overlayLayers(`${id}-${i}`, id, sl)) {
          map.addLayer(layer);
          added.push(layer.id);
        }
      });
    } else {
      map.addSource(id, { type: 'raster', url: `pmtiles://${url}`, tileSize: 256 });
      map.addLayer({ id: `${id}-raster`, type: 'raster', source: id,
                     paint: { 'raster-opacity': 0.9 } });
      added.push(`${id}-raster`);
    }
  }

  if (!added.length) return;
  const opacityProps = {};
  for (const lid of added) {
    const type = map.getLayer(lid).type;
    opacityProps[lid] = `${type}-opacity`;
  }
  // Put overlays at the top of the panel: they are why the viewer was opened.
  LAYER_GROUPS.unshift({ id, name, layerIds: added, defaultOpacity: 1.0, opacityProps });
}

async function addOverlays() {
  const urls = _q.getAll('overlay').filter(Boolean);
  for (const [i, url] of urls.entries()) {
    try {
      await addOverlay(url, i);
      console.log(`[overlay] added ${url}`);
    } catch (err) {
      console.error(`[overlay] could not add ${url}:`, err);
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════
//  DEPTH LEGEND
// ═══════════════════════════════════════════════════════════════════════

function buildDepthLegend() {
  // Horizontal, the way arco-3d draws a colour scale: a short strip with the
  // range at its ends, rather than a 240 px column with eleven numbers down
  // it. Deep on the left, the surface on the right, which is how a depth
  // scale is read.
  const canvas = document.getElementById('legend-canvas');
  const ctx = canvas.getContext('2d');
  const { width: w, height: h } = canvas;

  const deepFirst = [...DEPTH_STOPS];               // DEPTH_STOPS runs deep to shallow
  for (let x = 0; x < w; x++) {
    const t = x / (w - 1);
    const idx = Math.min(Math.floor(t * (deepFirst.length - 1)), deepFirst.length - 2);
    const localT = (t * (deepFirst.length - 1)) - idx;
    ctx.fillStyle = lerpColor(deepFirst[idx].color, deepFirst[idx + 1].color, localT);
    ctx.fillRect(x, 0, 1, h);
  }

  // Four numbers, not eleven: the ends, and two the eye can anchor on.
  const ticks = document.getElementById('legend-ticks');
  ticks.innerHTML = '';
  const minD = DEPTH_STOPS[0].depth;
  for (const d of [minD, -4000, -1000, 0]) {
    const el = document.createElement('span');
    el.textContent = d === 0 ? '0' : (d <= -1000 ? `${d / 1000}k` : String(d));
    ticks.appendChild(el);
  }
}

/** Linearly interpolate between two hex colors */
function lerpColor(a, b, t) {
  const ar = parseInt(a.slice(1, 3), 16), ag = parseInt(a.slice(3, 5), 16), ab = parseInt(a.slice(5, 7), 16);
  const br = parseInt(b.slice(1, 3), 16), bg = parseInt(b.slice(3, 5), 16), bb = parseInt(b.slice(5, 7), 16);
  const r = Math.round(ar + (br - ar) * t);
  const g = Math.round(ag + (bg - ag) * t);
  const bv = Math.round(ab + (bb - ab) * t);
  return `rgb(${r},${g},${bv})`;
}


// ═══════════════════════════════════════════════════════════════════════
//  COORDINATE DISPLAY + DD/DMS TOGGLE
// ═══════════════════════════════════════════════════════════════════════

let coordFormat = 'DD'; // 'DD' | 'DMS'

function formatCoord(lat, lng) {
  if (coordFormat === 'DMS') {
    return toDMS(lat, 'lat') + ', ' + toDMS(lng, 'lng');
  }
  const ns = lat >= 0 ? 'N' : 'S';
  const ew = lng >= 0 ? 'E' : 'W';
  return Math.abs(lat).toFixed(4) + '° ' + ns + ', ' + Math.abs(lng).toFixed(4) + '° ' + ew;
}

function toDMS(val, axis) {
  const abs = Math.abs(val);
  const d = Math.floor(abs);
  const mFull = (abs - d) * 60;
  const m = Math.floor(mFull);
  const s = ((mFull - m) * 60).toFixed(1);
  const dir = axis === 'lat' ? (val >= 0 ? 'N' : 'S') : (val >= 0 ? 'E' : 'W');
  return `${d}° ${String(m).padStart(2, '0')}' ${String(s).padStart(4, '0')}" ${dir}`;
}

const coordDisplay = document.getElementById('coord-display');
const zoomDisplay = document.getElementById('zoom-display');
const scaleDisplay = document.getElementById('scale-display');
const settingsBtn = document.getElementById('settings-btn');
const settingsPanel = document.getElementById('settings-panel');

// The projections the map and the globe are actually drawn in. Stated rather
// than assumed: this page is Web Mercator, which is why it has no poles, and
// the globe draws the geographic archives, which is why it does.
const PROJECTIONS = [
  ['EPSG:3857', 'Web Mercator', 'this map, and every Mercator archive; stops at 85° N/S'],
  ['EPSG:4326', 'Geographic (Plate Carrée)', 'the globe, and the archives that reach the poles'],
];

function buildProjectionList() {
  const target = document.getElementById('projection-list');
  if (!target) return;
  target.innerHTML = PROJECTIONS.map(([code, name, note]) =>
    `<div class="proj-row"><code>${code}</code><span>${name}</span><em>${note}</em></div>`,
  ).join('');
}

function setCoordFormat(fmt) {
  coordFormat = fmt === 'DMS' ? 'DMS' : 'DD';
  settingsBtn.textContent = coordFormat;
  updateInfoBar();
}

function toggleSettings(open) {
  const show = open ?? settingsPanel.classList.contains('hidden');
  settingsPanel.classList.toggle('hidden', !show);
  settingsBtn.setAttribute('aria-expanded', String(show));
}

settingsBtn.addEventListener('click', () => toggleSettings());
document.getElementById('settings-close').addEventListener('click', () => toggleSettings(false));
document.querySelectorAll('input[name="coord-fmt"]').forEach(radio => {
  radio.addEventListener('change', () => radio.checked && setCoordFormat(radio.value));
});
// Clicking the map should put the panel away, but clicking inside it must not.
settingsPanel.addEventListener('click', e => e.stopPropagation());
map.on('click', () => toggleSettings(false));

function updateInfoBar() {
  const z = map.getZoom();
  zoomDisplay.textContent = 'z' + z.toFixed(1);
  // Approximate scale at center latitude
  const lat = map.getCenter().lat;
  const mPerPx = (156543.03 * Math.cos(lat * Math.PI / 180)) / Math.pow(2, z);
  const scaleDenom = Math.round(mPerPx * 96 / 0.0254); // 96 DPI screen
  scaleDisplay.textContent = '1:' + scaleDenom.toLocaleString();
}

map.on('mousemove', e => {
  coordDisplay.textContent = formatCoord(e.lngLat.lat, e.lngLat.lng);
});
map.on('move', updateInfoBar);
map.on('zoom', updateInfoBar);


// ═══════════════════════════════════════════════════════════════════════
//  DEPTH PROFILE ON CLICK (popup)
// ═══════════════════════════════════════════════════════════════════════

function getDepthZoneInfo(depth) {
  // Stops run deepest-first, so the last match walking back is the zone that contains depth.
  for (let i = DEPTH_STOPS.length - 1; i >= 0; i--) {
    if (depth >= DEPTH_STOPS[i].depth) {
      const upper = i < DEPTH_STOPS.length - 1 ? DEPTH_STOPS[i + 1].depth : 0;
      return { color: DEPTH_STOPS[i].color, label: DEPTH_STOPS[i].label, range: `${upper}m to ${DEPTH_STOPS[i].depth}m` };
    }
  }
  return { color: DEPTH_STOPS[0].color, label: DEPTH_STOPS[0].label, range: '' };
}

map.on('click', e => {
  const queryLayers = [
    'lake-fill',
    'depth-zones', 'depth-zones-eu', 'depth-zones-hr',
    'depth-contours-major', 'depth-contours-minor', 'depth-contours-shelf',
  ];
  const existing = queryLayers.filter(id => map.getLayer(id));
  const features = map.queryRenderedFeatures(e.point, { layers: existing });
  if (!features.length) return;

  let html = '';
  let gotDepthZone = false;
  let gotContour = false;
  let gotLake = false;

  for (const f of features) {
    const lid = f.layer.id;
    const props = f.properties;

    if (lid === 'lake-fill' && !gotLake) {
      gotLake = true;
      // HydroLAKES gives one modelled mean depth per lake, not a sounding at
      // this point, so it is labelled as the lake's mean.
      const name = props.name || 'Unnamed lake';
      const depth = Number(props.depth_avg);
      const area = Number(props.area_km2);
      html += `<div class="popup-title"><span class="popup-swatch" style="background:${
        '#95c6e6'}"></span>${name}</div>`;
      if (Number.isFinite(depth)) {
        html += `<div class="popup-row"><span class="popup-label">Mean depth</span><span class="popup-value">${depth.toFixed(1)} m</span></div>`;
      }
      if (Number.isFinite(area)) {
        html += `<div class="popup-row"><span class="popup-label">Area</span><span class="popup-value">${area.toLocaleString(undefined, { maximumFractionDigits: 0 })} km²</span></div>`;
      }
      html += `<div class="popup-row"><span class="popup-label">Surface</span><span class="popup-value">${Number(props.elev_m).toFixed(0)} m</span></div>`;
    }

    if (lid === 'depth-zones-hr') {
      html += `<div class="popup-row"><span class="popup-label">HR survey</span><span class="popup-value">${props.source} (1/${props.res}′)</span></div>`;
    }

    if (lid === 'depth-zones' && !gotDepthZone) {
      gotDepthZone = true;
      const minDepth = parseFloat(props.min_depth);
      const maxDepth = props.max_depth !== undefined ? parseFloat(props.max_depth) : null;
      const info = getDepthZoneInfo(minDepth);
      html += `<div class="popup-title"><span class="popup-swatch" style="background:${info.color}"></span>${info.label}</div>`;
      html += `<div class="popup-row"><span class="popup-label">Depth range</span><span class="popup-value">${info.range}</span></div>`;
      if (maxDepth !== null) {
        html += `<div class="popup-row"><span class="popup-label">Zone bounds</span><span class="popup-value">${maxDepth}m — ${minDepth}m</span></div>`;
      }
    }

    if ((lid.startsWith('depth-contours')) && !gotContour) {
      gotContour = true;
      const depth = props.depth;
      html += `<div class="popup-row"><span class="popup-label">Contour</span><span class="popup-value">${depth}m</span></div>`;
    }
  }

  // Coordinate of click
  const { lat, lng } = e.lngLat;
  html += `<div class="popup-row" style="margin-top:4px;border-top:1px solid rgba(255,255,255,0.06);padding-top:4px">`;
  html += `<span class="popup-label">Position</span><span class="popup-value">${formatCoord(lat, lng)}</span></div>`;

  if (html) {
    new maplibregl.Popup({ closeButton: true, maxWidth: '320px' })
      .setLngLat(e.lngLat)
      .setHTML(html)
      .addTo(map);
  }
});


// ═══════════════════════════════════════════════════════════════════════
//  GRATICULE (dynamic lat/lon grid)
// ═══════════════════════════════════════════════════════════════════════

function getGraticuleInterval(zoom) {
  if (zoom < 2)  return 30;
  if (zoom < 4)  return 10;
  if (zoom < 6)  return 5;
  if (zoom < 8)  return 1;
  if (zoom < 10) return 0.5;
  return 0.25;
}

function buildGraticuleGeoJSON(interval) {
  const features = [];

  // Meridians (longitude lines)
  for (let lng = -180; lng <= 180; lng += interval) {
    const coords = [];
    for (let lat = -90; lat <= 90; lat += 2) {
      coords.push([lng, lat]);
    }
    features.push({
      type: 'Feature',
      properties: { value: lng, axis: 'lng' },
      geometry: { type: 'LineString', coordinates: coords },
    });
  }

  // Parallels (latitude lines)
  for (let lat = -90; lat <= 90; lat += interval) {
    const coords = [];
    for (let lng = -180; lng <= 180; lng += 2) {
      coords.push([lng, lat]);
    }
    features.push({
      type: 'Feature',
      properties: { value: lat, axis: 'lat' },
      geometry: { type: 'LineString', coordinates: coords },
    });
  }

  // Label points at intersections (sparse: only every other line)
  const labelInterval = interval < 1 ? interval * 2 : interval;
  for (let lat = -90; lat <= 90; lat += labelInterval) {
    for (let lng = -180; lng < 180; lng += labelInterval) {
      if (lat === 0 && lng === 0) continue; // skip origin for cleanliness
      features.push({
        type: 'Feature',
        properties: { label: `${Math.abs(lat)}°${lat >= 0 ? 'N' : 'S'} ${Math.abs(lng)}°${lng >= 0 ? 'E' : 'W'}` },
        geometry: { type: 'Point', coordinates: [lng, lat] },
      });
    }
  }

  return { type: 'FeatureCollection', features };
}

let currentGraticuleInterval = null;

function updateGraticule() {
  const interval = getGraticuleInterval(map.getZoom());
  if (interval === currentGraticuleInterval) return;
  currentGraticuleInterval = interval;

  const src = map.getSource('graticule');
  if (src) {
    src.setData(buildGraticuleGeoJSON(interval));
  }
}


// ═══════════════════════════════════════════════════════════════════════
//  KEYBOARD SHORTCUTS
// ═══════════════════════════════════════════════════════════════════════

document.addEventListener('keydown', e => {
  // Ignore if typing in an input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  const key = e.key.toLowerCase();

  if (key === 'g') {
    // Toggle graticule visibility
    const group = LAYER_GROUPS.find(g => g.id === 'graticule');
    const eyeBtn = document.querySelector(`.eye-btn[data-group="graticule"]`);
    if (eyeBtn) eyeBtn.click();
  }

  if (key === 'l') {
    document.getElementById('panel-collapse').click();
  }
});


// ═══════════════════════════════════════════════════════════════════════
//  MAP LOAD — add dynamic sources + layers, build UI
// ═══════════════════════════════════════════════════════════════════════

map.on('load', () => {
  // ── Graticule source + layers ──
  const initInterval = getGraticuleInterval(map.getZoom());
  currentGraticuleInterval = initInterval;

  map.addSource('graticule', {
    type: 'geojson',
    data: buildGraticuleGeoJSON(initInterval),
  });

  map.addLayer({
    id: 'graticule-lines',
    type: 'line',
    source: 'graticule',
    filter: ['!=', ['geometry-type'], 'Point'],
    layout: { visibility: 'none' },
    paint: {
      'line-color': 'rgba(180, 200, 220, 0.25)',
      'line-width': 0.6,
      'line-dasharray': [4, 4],
    },
  });

  map.addLayer({
    id: 'graticule-labels',
    type: 'symbol',
    source: 'graticule',
    filter: ['has', 'label'],
    layout: {
      visibility: 'none',
      'text-field': ['get', 'label'],
      'text-font': ['Noto Sans Regular'],
      'text-size': 10,
      'text-allow-overlap': false,
      'text-optional': true,
      'text-padding': 20,
    },
    paint: {
      'text-color': 'rgba(200, 215, 235, 0.85)',
      'text-halo-color': 'rgba(10, 14, 20, 0.85)',
      'text-halo-width': 1.5,
    },
  });

  // Update graticule on zoom
  map.on('zoomend', updateGraticule);

  // ── User overlays, then the UI that lists them ──
  addOverlays().finally(() => {
    buildLayerPanel();
    buildDepthLegend();
    updateCredits();
  });

  // ── Build UI ──
  buildLayerPanel();
  buildDepthLegend();
  buildProjectionList();
  updateCredits();
  updateInfoBar();

  // Set initial visibility for graticule (default hidden)
  map.setLayoutProperty('graticule-lines', 'visibility', 'none');
  map.setLayoutProperty('graticule-labels', 'visibility', 'none');

  console.log('OceanStream Basemap loaded. Keyboard shortcuts: G=graticule, L=layer panel');
});
