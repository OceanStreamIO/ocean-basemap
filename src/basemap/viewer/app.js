/**
 * OceanStream Basemap — Advanced Test Map
 *
 * Features:
 *   - Full GEBCO-based basemap (hillshade + depth zones + contours + labels)
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
// are immutable — busting them would defeat every range-request cache.
const CACHE_BUST = TILE_BASE === LOCAL_DEFAULT ? `?v=${Date.now()}` : '';
const pmtilesUrl = (name) => `pmtiles://${PMTILES_BASE}/${name}.pmtiles${CACHE_BUST}`;

const FONT_GLYPHS = 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf';

// GEBCO and Natural Earth are public domain; OSM coastlines are ODbL and the
// credit is a licence condition, not decoration. Do not hide the control.
const ATTR_GEBCO = '<a href="https://www.gebco.net">GEBCO</a>';
const ATTR_LAND = '<a href="https://www.naturalearthdata.com">Natural Earth</a>, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors (ODbL)';

// Depth color ramp — 14 stops, non-linear (more detail on continental shelf)
// Light palette: soft blues, clearly distinct from land
const DEPTH_STOPS = [
  { depth: -11000, color: '#3a6898', label: 'Hadal zone' },
  { depth:  -8000, color: '#4878a8', label: 'Abyssal trench' },
  { depth:  -6000, color: '#5688b6', label: 'Deep abyssal' },
  { depth:  -5000, color: '#6496c2', label: 'Lower abyssal' },
  { depth:  -4000, color: '#72a4cc', label: 'Upper abyssal' },
  { depth:  -3000, color: '#80b2d6', label: 'Bathypelagic' },
  { depth:  -2000, color: '#8ec0de', label: 'Mesopelagic' },
  { depth:  -1500, color: '#9acae4', label: 'Upper mesopelagic' },
  { depth:  -1000, color: '#a8d4ea', label: 'Twilight zone' },
  { depth:   -500, color: '#b6ddf0', label: 'Continental slope' },
  { depth:   -200, color: '#c4e6f5', label: 'Outer shelf' },
  { depth:   -100, color: '#d2eef9', label: 'Mid shelf' },
  { depth:    -50, color: '#e0f4fc', label: 'Inner shelf' },
  { depth:    -10, color: '#ecf8fe', label: 'Nearshore' },
];

// MapLibre step expression for depth zone fill-color
const depthColorExpr = (() => {
  const expr = ['step', ['to-number', ['get', 'min_depth']], DEPTH_STOPS[0].color];
  for (let i = 1; i < DEPTH_STOPS.length; i++) {
    expr.push(DEPTH_STOPS[i].depth, DEPTH_STOPS[i].color);
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
    id: 'hillshade', name: 'Hillshade', shortcut: '',
    layerIds: ['hillshade-raster'],
    defaultOpacity: 0.8,
    opacityProps: { 'hillshade-raster': 'raster-opacity' },
  },
  {
    id: 'depth-zones', name: 'Depth Zones',
    layerIds: ['depth-zones'],
    defaultOpacity: 1.0,
    opacityProps: { 'depth-zones': 'fill-opacity' },
  },
  {
    id: 'depth-zones-eu', name: 'Depth Zones (EMODnet)',
    layerIds: ['depth-zones-eu'],
    defaultOpacity: 1.0,
    opacityProps: { 'depth-zones-eu': 'fill-opacity' },
  },
  {
    id: 'depth-zones-hr', name: 'Depth Zones (HR surveys)',
    layerIds: ['depth-zones-hr'],
    defaultOpacity: 1.0,
    opacityProps: { 'depth-zones-hr': 'fill-opacity' },
  },
  {
    id: 'land', name: 'Land',
    layerIds: ['land-fill', 'hypsometric-land', 'coastline-line'],
    defaultOpacity: 1.0,
    opacityProps: {
      'land-fill': 'fill-opacity',
      'hypsometric-land': 'raster-opacity',
      'coastline-line': 'line-opacity',
    },
  },
  {
    id: 'water-features', name: 'Lakes',
    layerIds: ['lake-fill'],
    defaultOpacity: 1.0,
    opacityProps: { 'lake-fill': 'fill-opacity' },
  },
  {
    id: 'borders', name: 'Country Borders',
    layerIds: ['country-borders'],
    defaultOpacity: 0.5,
    opacityProps: { 'country-borders': 'line-opacity' },
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
    id: 'reference-labels', name: 'ESRI Reference Labels',
    layerIds: ['esri-ocean-labels'],
    defaultOpacity: 1.0,
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
  name: 'OceanStream Basemap (Local Test)',
  glyphs: FONT_GLYPHS,
  sources: {
    hillshade: {
      type: 'raster',
      tiles: [TILE_BASE + '/hillshade/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 10,
      attribution: ATTR_GEBCO,
    },
    depth_zones: { type: 'vector', url: pmtilesUrl('depth_zones'), attribution: ATTR_GEBCO },
    depth_zones_eu: { type: 'vector', url: pmtilesUrl('depth_zones_eu') },
    depth_zones_hr: { type: 'vector', url: pmtilesUrl('depth_zones_hr') },
    depth_contours: { type: 'vector', url: pmtilesUrl('depth_contours') },
    land_base: { type: 'vector', url: pmtilesUrl('land_base'), attribution: ATTR_LAND },
    seamounts: { type: 'vector', url: pmtilesUrl('seamounts') },
    ocean_labels: { type: 'vector', url: pmtilesUrl('ocean_labels') },
    esri_ocean_ref: {
      type: 'raster',
      tiles: ['https://services.arcgisonline.com/arcgis/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 13,
      attribution: 'Labels &copy; <a href="https://www.esri.com">Esri</a>',
    },
    hypsometric: {
      type: 'raster',
      tiles: [TILE_BASE + '/hypsometric/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 10,
      attribution: ATTR_GEBCO,
    },
  },
  layers: [
    // 0. Ocean base. Every bathymetry source ends at its own land/sea boundary,
    //    and none of them agree with the OSM coastline. Without a base fill the
    //    mismatch band along the shore paints nothing and the page background
    //    shows through as near-black.
    {
      id: 'ocean-base', type: 'background',
      paint: { 'background-color': DEPTH_STOPS[DEPTH_STOPS.length - 1].color },
    },
    // 1. Hillshade raster base
    {
      id: 'hillshade-raster', type: 'raster', source: 'hillshade',
      paint: {
        'raster-opacity': 1.0,
        'raster-contrast': 0.35,
        'raster-brightness-min': 0.0,
        'raster-brightness-max': 0.75,
      },
    },
    // 2. Depth zone polygons — vivid blues, all ocean depths
    {
      id: 'depth-zones', type: 'fill', source: 'depth_zones', 'source-layer': 'depth_zones',
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 1.0 },
    },
    // 2b. EMODnet depth zones — ~115 m regional detail, European seas only
    {
      id: 'depth-zones-eu', type: 'fill', source: 'depth_zones_eu', 'source-layer': 'depth_zones_eu',
      minzoom: 6,
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 1.0 },
    },
    // 2c. EMODnet HR coastal surveys — scattered footprints, up to ~2 m
    {
      id: 'depth-zones-hr', type: 'fill', source: 'depth_zones_hr', 'source-layer': 'depth_zones_hr',
      minzoom: 10,
      filter: ['<', ['to-number', ['get', 'min_depth']], 0],
      paint: { 'fill-color': depthColorExpr, 'fill-opacity': 1.0 },
    },
    // 3. Land base — OSM coastline polygons. This vector edge is the shoreline;
    //    it stays sharp at every zoom because the GPU antialiases it.
    {
      id: 'land-fill', type: 'fill', source: 'land_base', 'source-layer': 'land',
      paint: { 'fill-color': '#bed2c3', 'fill-opacity': 1.0 },
    },
    // 3a. Shaded relief, clipped to the same coastline. Carries its own
    //     hillshade, so it does not need the one under the ocean fills.
    //     GEBCO cells are 463 m, so past ~z11 they read as blocks rather than
    //     terrain; fade out and let the vector land carry the shoreline.
    {
      id: 'hypsometric-land', type: 'raster', source: 'hypsometric',
      maxzoom: 13,
      paint: {
        'raster-opacity': ['interpolate', ['linear'], ['zoom'], 10, 1.0, 11, 0.9, 12.5, 0],
        'raster-saturation': -0.15,
      },
    },
    // 3b. Coastline drawn over the raster, so the shoreline reads as the crisp
    //     vector edge rather than the GEBCO pixel staircase underneath it.
    {
      id: 'coastline-line', type: 'line', source: 'land_base', 'source-layer': 'coastline',
      paint: {
        'line-color': '#8fa89a',
        'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.3, 6, 0.6, 10, 1.0, 14, 1.4],
        'line-opacity': 0.9,
      },
    },
    // 3b. Lakes — subtle vector reinforcement
    {
      id: 'lake-fill', type: 'fill', source: 'land_base', 'source-layer': 'lakes',
      minzoom: 4,
      paint: {
        'fill-color': '#b8dce8',
        'fill-opacity': ['interpolate', ['linear'], ['zoom'], 4, 0.3, 6, 0.5, 10, 0.6],
      },
    },
    // 4. Country borders
    {
      id: 'country-borders', type: 'line', source: 'land_base', 'source-layer': 'countries',
      minzoom: 3,
      paint: { 'line-color': '#b8c4d0', 'line-width': 0.5, 'line-dasharray': [3, 2], 'line-opacity': 0.5 },
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
    // 12. ESRI Ocean Reference — labels for oceans, seas, countries, cities
    {
      id: 'esri-ocean-labels', type: 'raster', source: 'esri_ocean_ref',
      paint: {
        'raster-opacity': 1.0,
      },
    },
  ],
};


// ═══════════════════════════════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════════════════════════════

// Register PMTiles protocol
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);

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
      group.layerIds.forEach(lid => {
        if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', vis);
      });
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
    'depth-zones', 'depth-zones-eu', 'depth-zones-hr',
    'depth-contours-major', 'depth-contours-minor', 'depth-contours-shelf',
  ];
  const existing = queryLayers.filter(id => map.getLayer(id));
  const features = map.queryRenderedFeatures(e.point, { layers: existing });
  if (!features.length) return;

  let html = '';
  let gotDepthZone = false;
  let gotContour = false;

  for (const f of features) {
    const lid = f.layer.id;
    const props = f.properties;

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

  // ── Build UI ──
  buildLayerPanel();
  buildDepthLegend();
  updateInfoBar();

  // Set initial visibility for graticule (default hidden)
  map.setLayoutProperty('graticule-lines', 'visibility', 'none');
  map.setLayoutProperty('graticule-labels', 'visibility', 'none');

  console.log('OceanStream Basemap loaded. Keyboard shortcuts: G=graticule, L=layer panel, Esc=cancel measure');
});
