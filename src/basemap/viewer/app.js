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
 *   - Distance measurement (nautical miles) and area measurement (km²)
 *   - Keyboard shortcuts: G (graticule), L (layer panel), Escape (cancel measurement)
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
const archivePath = (name) => TILE_VERSIONS[name] || `${name}.pmtiles`;
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
// GeoNames for the rest of the world. Boundaries likewise -- geoBoundaries for
// the world's second-level units, N50's kommuner for Norway. Both GeoNames and
// geoBoundaries are CC BY 4.0, so naming them is a licence condition, not a
// courtesy, and crediting only the source that happens to be first in the file
// does not meet it.
//
// Both keep their full credit although the viewer no longer draws the world
// names or the municipal lines: the archives still carry those layers, they
// are still fetched and still queryable, and the cost of crediting a dataset
// one layer too widely is nothing beside the cost of crediting one too
// narrowly.
const ATTR_PLACES = `${ATTR_KARTVERKET}, <a href="https://www.geonames.org">GeoNames</a> (CC BY 4.0)`;
const ATTR_BOUNDARIES =
  '<a href="https://www.naturalearthdata.com">Natural Earth</a>, '
  + '<a href="https://www.geoboundaries.org">geoBoundaries</a> (CC BY 4.0), '
  + '&copy; <a href="https://www.kartverket.no">Kartverket</a> (CC BY 4.0)';
// Seamounts and undersea feature names come from published gazetteers rather
// than from the grids, and are cited the way their authors ask to be.
const ATTR_SEAMOUNTS =
  '<a href="https://bluehabitats.org">Global Seafloor Geomorphic Features Map</a> '
  + '(Harris et al. 2014)';
const ATTR_OCEAN_LABELS =
  '<a href="https://www.naturalearthdata.com">Natural Earth</a>, '
  + '<a href="https://www.ngdc.noaa.gov/gazetteer/">GEBCO Gazetteer of Undersea '
  + 'Feature Names</a> (IHO-IOC SCUFN)';
const ATTR_ESRI_IMAGERY = 'Imagery &copy; <a href="https://www.esri.com">Esri</a>, Maxar, Earthstar Geographics';
const ATTR_COPDEM = 'Copernicus DEM &copy; DLR e.V., &copy; Airbus Defence and Space GmbH';

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
  { depth:   -200, color: '#a1daf0', label: 'Outer shelf' },
  { depth:   -100, color: '#b1dff0', label: 'Mid shelf' },
  { depth:    -75, color: '#b7e0f0', label: 'Inner shelf' },
  { depth:    -50, color: '#b8e0f0', label: '50–40 m' },
  { depth:    -40, color: '#b9e1f0', label: '40–30 m' },
  { depth:    -30, color: '#bae1f0', label: '30–20 m' },
  { depth:    -20, color: '#bce2f0', label: '20–15 m' },
  { depth:    -15, color: '#bce2f0', label: '15–10 m' },
  { depth:    -10, color: '#bde3f0', label: '10–5 m' },
  { depth:     -5, color: '#bee3f0', label: '5–2 m' },
  { depth:     -2, color: '#bee3f0', label: '0–2 m' },
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
  { depth: 0, color: '#d8ecf7' },
  { depth: 2, color: '#cbe4f4' },
  { depth: 5, color: '#bcdcf0' },
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

const lakeSeaToneExpr = (() => {
  const depth = ['*', -1, ['to-number', ['get', 'depth_avg']]];
  const expr = ['step', depth, DEPTH_STOPS[0].color];
  for (let i = 1; i < DEPTH_STOPS.length; i++) {
    expr.push(DEPTH_STOPS[i].depth, DEPTH_STOPS[i].color);
  }
  return expr;
})();

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
const SHORE_WATER = '#bfe4f0';

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
    layerIds: ['land-fill', 'land-relief', 'land-relief-eu', 'coastline-line'],
    defaultOpacity: 1.0,
    opacityProps: {
      'land-fill': 'fill-opacity',
      'land-relief': 'raster-opacity',
      'land-relief-eu': 'raster-opacity',
      'coastline-line': 'line-opacity',
    },
  },
  {
    id: 'water-features', name: 'Lakes',
    layerIds: ['lake-fill', 'lake-fill-coastal', 'lake-relief', 'lake-relief-coastal', 'lake-outline'],
    defaultOpacity: 1.0,
    opacityProps: {
      'lake-fill': 'fill-opacity',
      'lake-fill-coastal': 'fill-opacity',
      'lake-relief': 'raster-opacity',
      'lake-relief-coastal': 'raster-opacity',
      'lake-outline': 'line-opacity',
    },
  },
  {
    id: 'borders', name: 'Country Borders',
    layerIds: ['country-borders'],
    defaultOpacity: 0.7,
    opacityProps: { 'country-borders': 'line-opacity' },
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
    id: 'seamounts', name: 'Seamounts',
    layerIds: ['seamount-fill', 'seamount-outline', 'seamount-labels'],
    defaultOpacity: 1.0,
    opacityProps: {
      'seamount-fill': 'fill-opacity',
      'seamount-outline': 'line-opacity',
      'seamount-labels': 'text-opacity',
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
    id: 'reference-labels-plain', name: 'ESRI Labels, No Borders',
    layerIds: ['esri-place-labels'],
    defaultOpacity: 1.0,
    defaultVisible: false,
    opacityProps: { 'esri-place-labels': 'raster-opacity' },
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
    land_base: { type: 'vector', url: pmtilesUrl('land_base'), attribution: ATTR_LAND },
    sea_mask: { type: 'vector', url: pmtilesUrl('sea_mask'), attribution: ATTR_LAND },
    lakes: { type: 'vector', url: pmtilesUrl('lakes'), attribution: ATTR_HYDROLAKES },
    boundaries: { type: 'vector', url: pmtilesUrl('boundaries'), attribution: ATTR_BOUNDARIES },
    places: { type: 'vector', url: pmtilesUrl('places'), attribution: ATTR_PLACES },
    lake_relief: { type: 'raster', url: pmtilesUrl('lake_relief'), tileSize: 256, attribution: ATTR_GEBCO },
    lake_relief_coastal: { type: 'raster', url: pmtilesUrl('lake_relief_coastal'), tileSize: 256, attribution: ATTR_GEBCO },
    seamounts: { type: 'vector', url: pmtilesUrl('seamounts'), attribution: ATTR_SEAMOUNTS },
    ocean_labels: { type: 'vector', url: pmtilesUrl('ocean_labels'), attribution: ATTR_OCEAN_LABELS },
    esri_ocean_ref: {
      type: 'raster',
      tiles: ['https://services.arcgisonline.com/arcgis/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 13,
      attribution: 'Labels &copy; <a href="https://www.esri.com">Esri</a>',
    },
    // The same idea without the boundary lines. Esri's Ocean reference bakes
    // country and province borders into the same pixels as its text, so no
    // style can lighten one without the other; this canvas reference ships
    // place names on their own, and leaves the borders to our vector layers.
    // What it costs is the undersea names -- basins, ridges and soundings --
    // which only the Ocean reference carries. Hence both, as separate options.
    esri_place_ref: {
      type: 'raster',
      tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 16,
      attribution: 'Labels &copy; <a href="https://www.esri.com">Esri</a>, HERE, Garmin, &copy; OpenStreetMap contributors',
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
    { id: 'bathy-hr', type: 'raster', source: 'bathy_hr', paint: { 'raster-opacity': 1.0 } },
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
    {
      id: 'lake-fill', type: 'fill', source: 'lakes', 'source-layer': 'lakes',
      filter: ['>', ['to-number', ['get', 'elev_m']], COASTAL_LAKE_ELEV],
      paint: { 'fill-color': lakeColorExpr, 'fill-opacity': 1.0 },
    },
    {
      id: 'lake-fill-coastal', type: 'fill', source: 'lakes', 'source-layer': 'lakes',
      filter: ['<=', ['to-number', ['get', 'elev_m']], COASTAL_LAKE_ELEV],
      paint: { 'fill-color': lakeSeaToneExpr, 'fill-opacity': 1.0 },
    },
    // 8a. Lake beds, where GEBCO has soundings rather than a flat surface:
    //     2,472 lakes, drawn over their flat tone. Inset from the shoreline
    //     like the bathymetry, so the vector fill and outline keep the edge.
    {
      id: 'lake-relief', type: 'raster', source: 'lake_relief',
      paint: { 'raster-opacity': 1.0 },
    },
    {
      // Coastal lake beds, on the sea's ramp: a liman reads as the water it is
      // continuous with, not as an inland lake.
      id: 'lake-relief-coastal', type: 'raster', source: 'lake_relief_coastal',
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
    // 9. Boundaries, as lines rather than country outlines, so a coast is not
    //    drawn as a border. Esri's reference raster bakes the boundaries into
    //    the same pixels as its labels, which is why they could not be told
    //    apart, lightened, or switched off. Internal and municipal lines were
    //    here too; they are gone, and boundaries.pmtiles still carries the
    //    'regions' and 'municipalities' layers if they are ever wanted back.
    {
      id: 'country-borders', type: 'line', source: 'boundaries', 'source-layer': 'countries',
      minzoom: 2,
      paint: {
        'line-color': '#95a3b3',
        'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.4, 6, 0.7, 11, 1.1],
        'line-opacity': 0.7,
      },
    },
    // 5b. Seamount polygons
    {
      id: 'seamount-fill', type: 'fill', source: 'seamounts', 'source-layer': 'seamounts',
      minzoom: 3,
      paint: {
        'fill-color': '#c2785a',
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.08, 6, 0.15, 10, 0.20],
      },
    },
    {
      id: 'seamount-outline', type: 'line', source: 'seamounts', 'source-layer': 'seamounts',
      minzoom: 3,
      paint: {
        'line-color': '#c2785a',
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.4, 6, 0.8, 10, 1.2],
        'line-dasharray': [4, 3],
        'line-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.3, 6, 0.5, 10, 0.6],
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
    // 9b. Seamount labels
    {
      id: 'seamount-labels', type: 'symbol', source: 'seamounts', 'source-layer': 'seamounts',
      minzoom: 4,
      filter: ['!=', ['get', 'name'], ''],
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Italic'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 4, 9, 6, 11, 10, 14],
        'text-allow-overlap': false,
        'text-optional': true,
        'text-padding': 8,
      },
      paint: {
        'text-color': '#6a3820',
        'text-halo-color': 'rgba(180, 210, 240, 0.7)',
        'text-halo-width': 1.2,
      },
    },
    {
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
      paint: {
        'text-color': '#2f3944',
        'text-halo-color': 'rgba(255, 255, 255, 0.92)',
        'text-halo-width': 1.5,
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
    {
      id: 'esri-place-labels', type: 'raster', source: 'esri_place_ref',
      layout: { visibility: 'none' },
      paint: {
        // No borders in these tiles, so the labels can be darkened as far as
        // they read well without anything else coming with them.
        'raster-opacity': 1.0,
        'raster-brightness-max': 0.7,
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
  attributionControl: true,
});

map.addControl(new maplibregl.NavigationControl(), 'top-right');
map.addControl(new maplibregl.ScaleControl({ maxWidth: 200, unit: 'nautical' }), 'bottom-right');

// Keep the info bar clear of the attribution rather than on top of it.
//
// The credits run to about 1,500 px now that every source names itself, so on
// most screens they wrap to two lines -- and the info bar, anchored to the
// bottom, covered the second one. Several of these datasets are CC BY, where
// being credited is a condition of use, so an obscured line is a real fault
// and not a cosmetic one. The height is measured rather than assumed, because
// how many lines it wraps to depends on the window.
//
// What is measured is how far the attribution's top sits above the bottom of
// the window, not the element's own height: MapLibre's control container adds
// a 10 px margin under it, and taking the height alone left the two boxes
// overlapping by exactly that much less the gap -- 2 px, enough to clip the
// descenders on the last line. One number covers height and margin together.
(() => {
  const attrib = document.querySelector('.maplibregl-ctrl-attrib');
  if (!attrib || typeof ResizeObserver === 'undefined') return;
  const fit = () => {
    const clear = Math.max(0, window.innerHeight - attrib.getBoundingClientRect().top);
    document.documentElement.style.setProperty('--attrib-height', `${clear}px`);
  };
  new ResizeObserver(fit).observe(attrib);
  // A narrower window rewraps the credits without necessarily changing their
  // height, and innerHeight moves on its own, so the observer is not enough.
  window.addEventListener('resize', fit);
  fit();
})();

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

function buildLayerPanel() {
  const list = document.getElementById('layer-list');
  list.innerHTML = '';

  LAYER_GROUPS.forEach(group => {
    const div = document.createElement('div');
    div.className = 'layer-group';
    div.dataset.groupId = group.id;

    const visible = group.defaultVisible !== false;
    const opacity = Math.round(group.defaultOpacity * 100);

    div.innerHTML = `
      <div class="layer-group-header">
        <span class="eye-btn ${visible ? '' : 'off'}" data-group="${group.id}" title="Toggle visibility">${visible ? eyeOpenSVG : eyeClosedSVG}</span>
        <span class="layer-name">${group.name}</span>
      </div>
      <div class="opacity-row">
        <input type="range" class="opacity-slider" min="0" max="100" value="${opacity}" data-group="${group.id}" />
        <label class="opacity-val">${opacity}%</label>
      </div>
    `;
    list.appendChild(div);
  });

  // Eye toggle handlers
  list.querySelectorAll('.eye-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const gid = btn.dataset.group;
      const group = LAYER_GROUPS.find(g => g.id === gid);
      const isOff = btn.classList.toggle('off');
      btn.innerHTML = isOff ? eyeClosedSVG : eyeOpenSVG;
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
      const frac = val / 100;
      slider.nextElementSibling.textContent = val + '%';
      for (const [lid, prop] of Object.entries(group.opacityProps)) {
        const maxOp = group.maxOpacity?.[lid] ?? 1.0;
        if (map.getLayer(lid)) map.setPaintProperty(lid, prop, frac * maxOp);
      }
    });
  });
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
  const canvas = document.getElementById('legend-canvas');
  const ctx = canvas.getContext('2d');
  const h = canvas.height;
  const w = canvas.width;

  // Draw gradient from shallowest (top) to deepest (bottom)
  const reversed = [...DEPTH_STOPS].reverse(); // shallowest first
  for (let y = 0; y < h; y++) {
    const t = y / h; // 0 = top (shallow), 1 = bottom (deep)
    const idx = Math.min(Math.floor(t * (reversed.length - 1)), reversed.length - 2);
    const localT = (t * (reversed.length - 1)) - idx;
    ctx.fillStyle = lerpColor(reversed[idx].color, reversed[idx + 1].color, localT);
    ctx.fillRect(0, y, w, 1);
  }

  // Tick marks
  const tickDepths = [0, -50, -200, -500, -1000, -2000, -4000, -6000, -8000, -11000];
  const ticks = document.getElementById('legend-ticks');
  ticks.innerHTML = '';
  const minD = -11000, maxD = -10; // range of our ramp
  tickDepths.forEach(d => {
    const frac = (maxD - d) / (maxD - minD); // 0 = shallow/top, 1 = deep/bottom
    const el = document.createElement('span');
    el.textContent = d === 0 ? '0' : String(d);
    el.style.position = 'absolute';
    el.style.top = (frac * 100) + '%';
    el.style.transform = 'translateY(-50%)';
    ticks.appendChild(el);
  });
  ticks.style.position = 'relative';
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
const coordFmtBtn = document.getElementById('coord-fmt-btn');

coordFmtBtn.addEventListener('click', () => {
  coordFormat = coordFormat === 'DD' ? 'DMS' : 'DD';
  coordFmtBtn.textContent = coordFormat;
});

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
  // Don't fire if a measurement is in progress
  if (measureMode) return;

  const queryLayers = [
    'lake-fill', 'lake-fill-coastal',
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

    if ((lid === 'lake-fill' || lid === 'lake-fill-coastal') && !gotLake) {
      gotLake = true;
      // HydroLAKES gives one modelled mean depth per lake, not a sounding at
      // this point, so it is labelled as the lake's mean.
      const name = props.name || 'Unnamed lake';
      const depth = Number(props.depth_avg);
      const area = Number(props.area_km2);
      html += `<div class="popup-title"><span class="popup-swatch" style="background:${
        lid === 'lake-fill-coastal' ? '#cfe3f2' : '#95c6e6'}"></span>${name}</div>`;
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
//  MEASUREMENT TOOLS
// ═══════════════════════════════════════════════════════════════════════

let measureMode = null; // null | 'distance' | 'area'
let measureCoords = [];
let measurePopups = [];

const btnDist = document.getElementById('btn-measure-dist');
const btnArea = document.getElementById('btn-measure-area');
const btnClear = document.getElementById('btn-measure-clear');
const readout = document.getElementById('measure-readout');

function haversine(c1, c2) {
  const toRad = v => v * Math.PI / 180;
  const [lng1, lat1] = c1;
  const [lng2, lat2] = c2;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat/2)**2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng/2)**2;
  return 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function totalDistanceNM(coords) {
  let total = 0;
  for (let i = 1; i < coords.length; i++) {
    total += haversine(coords[i-1], coords[i]) * EARTH_RADIUS_NM;
  }
  return total;
}

function totalDistanceKM(coords) {
  let total = 0;
  for (let i = 1; i < coords.length; i++) {
    total += haversine(coords[i-1], coords[i]) * EARTH_RADIUS_KM;
  }
  return total;
}

/** Spherical excess formula for polygon area on a sphere */
function sphericalPolygonAreaKM2(coords) {
  if (coords.length < 3) return 0;
  const toRad = v => v * Math.PI / 180;
  // Close the polygon
  const ring = [...coords, coords[0]];
  let sum = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    const [lng1, lat1] = ring[i];
    const [lng2, lat2] = ring[i + 1];
    sum += toRad(lng2 - lng1) * (2 + Math.sin(toRad(lat1)) + Math.sin(toRad(lat2)));
  }
  const area = Math.abs(sum / 2) * EARTH_RADIUS_KM * EARTH_RADIUS_KM;
  return area;
}

function activateMeasure(mode) {
  if (measureMode === mode) {
    deactivateMeasure();
    return;
  }
  deactivateMeasure();
  measureMode = mode;
  measureCoords = [];
  map.getCanvas().style.cursor = 'crosshair';

  btnDist.classList.toggle('active', mode === 'distance');
  btnArea.classList.toggle('active', mode === 'area');

  map.on('click', onMeasureClick);
  map.on('dblclick', onMeasureDoubleClick);
  map.on('mousemove', onMeasureMouseMove);
}

function deactivateMeasure() {
  measureMode = null;
  measureCoords = [];
  map.getCanvas().style.cursor = '';
  btnDist.classList.remove('active');
  btnArea.classList.remove('active');
  readout.classList.add('hidden');

  map.off('click', onMeasureClick);
  map.off('dblclick', onMeasureDoubleClick);
  map.off('mousemove', onMeasureMouseMove);
}

function clearMeasurements() {
  deactivateMeasure();
  // Remove drawn features
  const src = map.getSource('measure');
  if (src) src.setData({ type: 'FeatureCollection', features: [] });
  const srcPts = map.getSource('measure-points');
  if (srcPts) srcPts.setData({ type: 'FeatureCollection', features: [] });
  const srcLabel = map.getSource('measure-labels');
  if (srcLabel) srcLabel.setData({ type: 'FeatureCollection', features: [] });
  // Remove popups
  measurePopups.forEach(p => p.remove());
  measurePopups = [];
}

function onMeasureClick(e) {
  e.preventDefault();
  const coord = [e.lngLat.lng, e.lngLat.lat];
  measureCoords.push(coord);
  updateMeasureDisplay();
}

function onMeasureDoubleClick(e) {
  e.preventDefault();
  // Finish measurement
  if (measureMode === 'distance' && measureCoords.length >= 2) {
    finishMeasurement();
  } else if (measureMode === 'area' && measureCoords.length >= 3) {
    finishMeasurement();
  }
}

function onMeasureMouseMove(e) {
  if (measureCoords.length === 0) return;

  const preview = [...measureCoords, [e.lngLat.lng, e.lngLat.lat]];

  // Update live line
  const src = map.getSource('measure');
  if (src) {
    const geom = measureMode === 'area' && preview.length >= 3
      ? { type: 'Polygon', coordinates: [[...preview, preview[0]]] }
      : { type: 'LineString', coordinates: preview };
    src.setData({ type: 'Feature', properties: {}, geometry: geom });
  }

  // Update readout
  if (measureMode === 'distance') {
    const nm = totalDistanceNM(preview);
    const km = totalDistanceKM(preview);
    readout.textContent = `${nm.toFixed(2)} NM (${km.toFixed(2)} km)`;
  } else {
    const area = sphericalPolygonAreaKM2(preview);
    readout.textContent = `${area.toFixed(2)} km²`;
  }
  readout.classList.remove('hidden');
  readout.style.left = (e.point.x + 16) + 'px';
  readout.style.top = (e.point.y - 10) + 'px';

  // Update point markers
  const srcPts = map.getSource('measure-points');
  if (srcPts) {
    srcPts.setData({
      type: 'FeatureCollection',
      features: measureCoords.map(c => ({
        type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: c },
      })),
    });
  }
}

function updateMeasureDisplay() {
  const src = map.getSource('measure');
  if (!src) return;

  if (measureCoords.length < 2) {
    src.setData({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: measureCoords } });
    return;
  }

  const geom = measureMode === 'area' && measureCoords.length >= 3
    ? { type: 'Polygon', coordinates: [[...measureCoords, measureCoords[0]]] }
    : { type: 'LineString', coordinates: measureCoords };
  src.setData({ type: 'Feature', properties: {}, geometry: geom });
}

function finishMeasurement() {
  let text = '';
  if (measureMode === 'distance') {
    const nm = totalDistanceNM(measureCoords);
    const km = totalDistanceKM(measureCoords);
    text = `${nm.toFixed(2)} NM (${km.toFixed(2)} km)`;
  } else if (measureMode === 'area') {
    const area = sphericalPolygonAreaKM2(measureCoords);
    text = `${area.toFixed(2)} km²`;
    // Close polygon visually
    const src = map.getSource('measure');
    if (src) {
      src.setData({
        type: 'Feature', properties: {},
        geometry: { type: 'Polygon', coordinates: [[...measureCoords, measureCoords[0]]] },
      });
    }
  }

  // Add final label as a popup pinned to last coord
  if (text) {
    const lastCoord = measureCoords[measureCoords.length - 1];
    // Place result as a label feature
    const srcLabel = map.getSource('measure-labels');
    if (srcLabel) {
      const existing = srcLabel._data || { type: 'FeatureCollection', features: [] };
      existing.features.push({
        type: 'Feature',
        properties: { label: text },
        geometry: { type: 'Point', coordinates: lastCoord },
      });
      srcLabel.setData(existing);
      srcLabel._data = existing;
    }
  }

  readout.classList.add('hidden');
  deactivateMeasure();
}

btnDist.addEventListener('click', () => activateMeasure('distance'));
btnArea.addEventListener('click', () => activateMeasure('area'));
btnClear.addEventListener('click', clearMeasurements);


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

  if (key === 'escape') {
    if (measureMode) deactivateMeasure();
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

  // ── Measurement source + layers ──
  map.addSource('measure', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });

  map.addSource('measure-points', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });

  map.addSource('measure-labels', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });
  // Store reference to data for appending
  map.getSource('measure-labels')._data = { type: 'FeatureCollection', features: [] };

  map.addLayer({
    id: 'measure-fill',
    type: 'fill',
    source: 'measure',
    filter: ['==', ['geometry-type'], 'Polygon'],
    paint: {
      'fill-color': 'rgba(100, 180, 240, 0.12)',
    },
  });

  map.addLayer({
    id: 'measure-line',
    type: 'line',
    source: 'measure',
    paint: {
      'line-color': '#68b8e8',
      'line-width': 2,
      'line-dasharray': [3, 2],
    },
  });

  map.addLayer({
    id: 'measure-vertices',
    type: 'circle',
    source: 'measure-points',
    paint: {
      'circle-radius': 5,
      'circle-color': '#68b8e8',
      'circle-stroke-color': '#0e1117',
      'circle-stroke-width': 2,
    },
  });

  map.addLayer({
    id: 'measure-result-labels',
    type: 'symbol',
    source: 'measure-labels',
    layout: {
      'text-field': ['get', 'label'],
      'text-font': ['Noto Sans Medium'],
      'text-size': 13,
      'text-anchor': 'left',
      'text-offset': [1, 0],
      'text-allow-overlap': true,
    },
    paint: {
      'text-color': '#90d0f8',
      'text-halo-color': 'rgba(14, 17, 23, 0.85)',
      'text-halo-width': 2,
    },
  });

  // ── User overlays, then the UI that lists them ──
  addOverlays().finally(() => {
    buildLayerPanel();
    buildDepthLegend();
  });

  // ── Build UI ──
  buildLayerPanel();
  buildDepthLegend();
  updateInfoBar();

  // Set initial visibility for graticule (default hidden)
  map.setLayoutProperty('graticule-lines', 'visibility', 'none');
  map.setLayoutProperty('graticule-labels', 'visibility', 'none');

  console.log('OceanStream Basemap loaded. Keyboard shortcuts: G=graticule, L=layer panel, Esc=cancel measure');
});
