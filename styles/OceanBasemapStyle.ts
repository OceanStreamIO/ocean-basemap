/**
 * OceanStream Custom Basemap — MapLibre Style Specification
 *
 * Hybrid approach: raster hillshade (3D texture) + vector depth polygons (color)
 * + vector contours/labels. Same technique as Mapbox Bathymetry v2.
 *
 * Designed to match ESRI Ocean Basemap quality using:
 *   - Multi-directional hillshade with gamma correction
 *   - Vector depth zone polygons for crisp, zoom-independent coloring
 *   - Italic serif typography for water features (cartographic convention)
 *   - Non-linear depth color ramp with extra detail in continental shelf
 *   - Subtle contours with blur and zoom-dependent opacity
 *
 * Usage:
 *   import { createOceanBasemapStyle } from './OceanBasemapStyle';
 *   const style = createOceanBasemapStyle();
 *   new maplibregl.Map({ container, style });
 */
import type { StyleSpecification, LayerSpecification } from 'maplibre-gl';

// Azure Blob Storage base URL for custom basemap tiles (legacy host)
const BLOB_BASE = 'https://ne1osvmdevtest.blob.core.windows.net/basemap';

// CloudFerro/EDITO S3 base URL — current tile host. Virtual-host style, since
// PMTiles relies on HTTP range requests against this origin.
const S3_BASE = 'https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap';

// Host the style points at unless overridden via createOceanBasemapStyle().
const DEFAULT_TILE_BASE = S3_BASE;

// Protomaps free font glyphs (Noto Sans + Serif, excellent Unicode coverage)
const FONT_GLYPHS = 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf';

// --- Depth Zone Color Ramp ---
// Non-linear: more stops in 0-500m continental shelf (where visual interest is highest)
// Colors designed for semi-transparent overlay on grayscale hillshade
const DEPTH_COLORS: Record<number, string> = {
  [-11000]: '#06122e',  // Hadal zone — deep dark navy
  [-8000]:  '#0a1e4a',  // Abyssal trench
  [-6000]:  '#0e2c68',  // Deep abyssal
  [-5000]:  '#133a82',  // Lower abyssal plain
  [-4000]:  '#1a4c9c',  // Upper abyssal
  [-3000]:  '#2462b8',  // Bathypelagic
  [-2000]:  '#3078d0',  // Mesopelagic
  [-1500]:  '#4090e0',  // Upper mesopelagic
  [-1000]:  '#52aaec',  // Twilight zone
  [-500]:   '#68c2f4',  // Continental slope
  [-200]:   '#84d8fa',  // Outer shelf
  [-100]:   '#a2e6fc',  // Mid shelf
  [-50]:    '#c0eefd',  // Inner shelf
  [-10]:    '#daf6fe',  // Nearshore
};

/**
 * Build a MapLibre fill-color expression for depth zone polygons.
 * Uses step expression to assign colors based on min_depth property.
 */
function depthFillColorExpression(): unknown[] {
  const depths = Object.keys(DEPTH_COLORS)
    .map(Number)
    .sort((a, b) => a - b);

  // step expression: value, default, stop1, color1, stop2, color2, ...
  const expr: unknown[] = ['step', ['to-number', ['get', 'min_depth']], DEPTH_COLORS[depths[0]]];
  for (let i = 1; i < depths.length; i++) {
    expr.push(depths[i], DEPTH_COLORS[depths[i]]);
  }
  return expr;
}

// --- Layer Definitions ---

const hillshadeLayer: LayerSpecification = {
  id: 'hillshade-raster',
  type: 'raster',
  source: 'hillshade',
  paint: {
    'raster-opacity': 0.6,  // Dimmed when depth polygons provide color
    'raster-resampling': 'linear',
  },
};

const depthZonesLayer: LayerSpecification = {
  id: 'depth-zones',
  type: 'fill',
  source: 'depth_zones',
  'source-layer': 'depth_zones',
  paint: {
    'fill-color': depthFillColorExpression() as never,
    'fill-opacity': 1.0,  // Full opacity — vivid depth coloring
  },
};

// EMODnet DTM (~115 m) — European seas only, replaces GEBCO detail at high zoom
const depthZonesEuLayer: LayerSpecification = {
  id: 'depth-zones-eu',
  type: 'fill',
  source: 'depth_zones_eu',
  'source-layer': 'depth_zones_eu',
  minzoom: 6,
  paint: {
    'fill-color': depthFillColorExpression() as never,
    'fill-opacity': 1.0,
  },
};

// EMODnet HR coastal surveys — scattered footprints at 1/32 to 1/1024 arc-minute
const depthZonesHrLayer: LayerSpecification = {
  id: 'depth-zones-hr',
  type: 'fill',
  source: 'depth_zones_hr',
  'source-layer': 'depth_zones_hr',
  minzoom: 10,
  paint: {
    'fill-color': depthFillColorExpression() as never,
    'fill-opacity': 1.0,
  },
};

const landFillLayer: LayerSpecification = {
  id: 'land-fill',
  type: 'fill',
  source: 'land_base',
  'source-layer': 'land',
  paint: {
    'fill-color': '#ddddd5',  // Cool neutral gray — doesn't compete with ocean
    'fill-opacity': 1.0,
  },
};

const coastlineLayer: LayerSpecification = {
  id: 'coastline',
  type: 'line',
  source: 'land_base',
  'source-layer': 'coastline',
  paint: {
    'line-color': '#8ea4b8',
    'line-width': ['interpolate', ['linear'], ['zoom'], 0, 0.3, 5, 0.8, 10, 1.2],
    'line-opacity': 0.9,
  },
};

const countryBordersLayer: LayerSpecification = {
  id: 'country-borders',
  type: 'line',
  source: 'land_base',
  'source-layer': 'countries',
  minzoom: 3,
  paint: {
    'line-color': '#b8c4d0',
    'line-width': 0.5,
    'line-dasharray': [3, 2],
    'line-opacity': 0.5,
  },
};

// Major depth contours: 1000m intervals, visible from z2
const depthContoursMajorLayer: LayerSpecification = {
  id: 'depth-contours-major',
  type: 'line',
  source: 'depth_contours',
  'source-layer': 'depth_contours',
  filter: ['all',
    ['<', ['get', 'depth'], 0],
    ['==', ['%', ['get', 'depth'], 1000], 0],
  ],
  minzoom: 2,
  paint: {
    'line-color': 'rgba(80, 120, 170, 0.35)',
    'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.4, 5, 0.8, 10, 1.2],
    'line-blur': 0.5,  // Subtle anti-aliasing — prevents harsh/dated look
  },
};

// Minor depth contours: 500m intervals, visible from z4
const depthContoursMinorLayer: LayerSpecification = {
  id: 'depth-contours-minor',
  type: 'line',
  source: 'depth_contours',
  'source-layer': 'depth_contours',
  filter: ['all',
    ['<', ['get', 'depth'], 0],
    ['==', ['%', ['get', 'depth'], 500], 0],
    ['!=', ['%', ['get', 'depth'], 1000], 0],  // Exclude major contours
  ],
  minzoom: 5,
  paint: {
    'line-color': 'rgba(80, 120, 170, 0.2)',
    'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.3, 8, 0.6],
    'line-dasharray': [4, 3],
    'line-blur': 0.5,
  },
};

// Shelf contours: 200m intervals, visible from z5
const depthContoursShelfLayer: LayerSpecification = {
  id: 'depth-contours-shelf',
  type: 'line',
  source: 'depth_contours',
  'source-layer': 'depth_contours',
  filter: ['all',
    ['<', ['get', 'depth'], 0],
    ['>', ['get', 'depth'], -2000],
    ['==', ['%', ['get', 'depth'], 200], 0],
    ['!=', ['%', ['get', 'depth'], 500], 0],  // Exclude minor/major
  ],
  minzoom: 6,
  paint: {
    'line-color': 'rgba(80, 120, 170, 0.15)',
    'line-width': 0.4,
    'line-dasharray': [2, 3],
    'line-blur': 0.5,
  },
};

// Seamount polygon fill — subtle highlight on the ocean floor
const seamountFillLayer: LayerSpecification = {
  id: 'seamount-fill',
  type: 'fill',
  source: 'seamounts',
  'source-layer': 'seamounts',
  minzoom: 3,
  paint: {
    'fill-color': '#c2785a',
    'fill-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.08, 6, 0.15, 10, 0.20],
  },
};

// Seamount polygon outline — dashed to distinguish from contours
const seamountOutlineLayer: LayerSpecification = {
  id: 'seamount-outline',
  type: 'line',
  source: 'seamounts',
  'source-layer': 'seamounts',
  minzoom: 3,
  paint: {
    'line-color': '#c2785a',
    'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.4, 6, 0.8, 10, 1.2],
    'line-dasharray': [4, 3],
    'line-opacity': ['interpolate', ['linear'], ['zoom'], 3, 0.3, 6, 0.5, 10, 0.6],
  },
};

// Seamount labels — name property from GSFM
const seamountLabelsLayer: LayerSpecification = {
  id: 'seamount-labels',
  type: 'symbol',
  source: 'seamounts',
  'source-layer': 'seamounts',
  minzoom: 6,
  filter: ['!=', ['get', 'name'], ''],
  layout: {
    'text-field': ['get', 'name'],
    'text-font': ['Noto Sans Italic'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 6, 9, 10, 12],
    'text-allow-overlap': false,
    'text-optional': true,
    'text-padding': 8,
  },
  paint: {
    'text-color': 'rgba(168, 100, 70, 0.8)',
    'text-halo-color': 'rgba(220, 235, 250, 0.5)',
    'text-halo-width': 1.2,
  },
};

// Depth contour labels (major only, along the line)
const depthLabelsLayer: LayerSpecification = {
  id: 'depth-labels',
  type: 'symbol',
  source: 'depth_contours',
  'source-layer': 'depth_contours',
  filter: ['all',
    ['<', ['get', 'depth'], 0],
    ['==', ['%', ['get', 'depth'], 1000], 0],
  ],
  minzoom: 4,
  layout: {
    'symbol-placement': 'line',
    'text-field': ['concat', ['to-string', ['abs', ['get', 'depth']]], 'm'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 4, 9, 10, 11],
    'text-font': ['Noto Sans Italic'],
    'text-max-angle': 30,
    'text-padding': 50,
  },
  paint: {
    'text-color': 'rgba(140, 185, 230, 0.8)',
    'text-halo-color': 'rgba(8, 16, 40, 0.6)',
    'text-halo-width': 1.5,
  },
};

// Ocean feature labels — major features (oceans, seas, ridges, trenches)
const oceanLabelsMajorLayer: LayerSpecification = {
  id: 'ocean-labels-major',
  type: 'symbol',
  source: 'ocean_labels',
  'source-layer': 'ocean_labels',
  filter: ['<=', ['get', 'min_zoom'], 4],
  minzoom: 1,
  layout: {
    'text-field': ['get', 'name'],
    'text-font': ['Noto Sans Italic'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 1, 12, 4, 18, 8, 22],
    'text-letter-spacing': ['interpolate', ['linear'], ['zoom'], 1, 0.1, 4, 0.25],
    'text-transform': 'uppercase',
    'text-allow-overlap': false,
    'text-optional': false,
    'text-padding': 20,
  },
  paint: {
    'text-color': 'rgba(160, 200, 240, 0.85)',
    'text-halo-color': 'rgba(8, 16, 40, 0.7)',
    'text-halo-width': 2,
  },
};

// Ocean feature labels — medium features (basins, plateaus, fracture zones)
const oceanLabelsMediumLayer: LayerSpecification = {
  id: 'ocean-labels-medium',
  type: 'symbol',
  source: 'ocean_labels',
  'source-layer': 'ocean_labels',
  filter: ['all',
    ['>', ['get', 'min_zoom'], 4],
    ['<=', ['get', 'min_zoom'], 6],
  ],
  minzoom: 4,
  layout: {
    'text-field': ['get', 'name'],
    'text-font': ['Noto Sans Italic'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 4, 10, 8, 14],
    'text-letter-spacing': 0.1,
    'text-transform': 'uppercase',
    'text-allow-overlap': false,
    'text-optional': true,  // Can be hidden if overlapping more important labels
    'text-padding': 15,
  },
  paint: {
    'text-color': 'rgba(140, 185, 230, 0.8)',
    'text-halo-color': 'rgba(8, 16, 40, 0.6)',
    'text-halo-width': 1.5,
  },
};

// Ocean feature labels — minor features (seamounts, guyots, canyons)
const oceanLabelsMinorLayer: LayerSpecification = {
  id: 'ocean-labels-minor',
  type: 'symbol',
  source: 'ocean_labels',
  'source-layer': 'ocean_labels',
  filter: ['>', ['get', 'min_zoom'], 6],
  minzoom: 6,
  layout: {
    'text-field': ['get', 'name'],
    'text-font': ['Noto Sans Italic'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 6, 9, 10, 12],
    'text-letter-spacing': 0.05,
    'text-allow-overlap': false,
    'text-optional': true,
    'text-padding': 10,
  },
  paint: {
    'text-color': 'rgba(120, 170, 220, 0.75)',
    'text-halo-color': 'rgba(8, 16, 40, 0.5)',
    'text-halo-width': 1,
  },
};

// City labels for geographic context
const cityLabelsLayer: LayerSpecification = {
  id: 'city-labels',
  type: 'symbol',
  source: 'land_base',
  'source-layer': 'cities',
  minzoom: 3,
  layout: {
    'text-field': ['get', 'name'],
    'text-font': ['Noto Sans Regular'],
    'text-size': ['interpolate', ['linear'], ['zoom'], 3, 10, 5, 12, 8, 14, 10, 15],
    'text-anchor': 'left',
    'text-offset': [0.5, 0],
    'text-allow-overlap': false,
    'text-optional': true,
  },
  paint: {
    'text-color': '#3a3f48',
    'text-halo-color': '#ddddd5',
    'text-halo-width': 1.8,
  },
};

// All basemap layers in render order (bottom to top)
const BASEMAP_LAYERS: LayerSpecification[] = [
  hillshadeLayer,
  depthZonesLayer,
  depthZonesEuLayer,
  depthZonesHrLayer,
  landFillLayer,
  coastlineLayer,
  countryBordersLayer,
  seamountFillLayer,
  seamountOutlineLayer,
  depthContoursMajorLayer,
  depthContoursMinorLayer,
  depthContoursShelfLayer,
  depthLabelsLayer,
  seamountLabelsLayer,
  oceanLabelsMajorLayer,
  oceanLabelsMediumLayer,
  oceanLabelsMinorLayer,
  cityLabelsLayer,
];

/**
 * Create the full MapLibre style for the OceanStream custom basemap.
 *
 * @param options - Override the tile base URL or add additional sources/layers
 */
export function createOceanBasemapStyle(options?: {
  blobBase?: string;
}): StyleSpecification {
  const base = options?.blobBase ?? DEFAULT_TILE_BASE;

  return {
    version: 8,
    name: 'OceanStream Basemap',
    glyphs: FONT_GLYPHS,
    sources: {
      // Grayscale hillshade raster — only provides 3D relief texture
      hillshade: {
        type: 'raster',
        tiles: [`${base}/hillshade/{z}/{x}/{y}.png`],
        tileSize: 256,
        maxzoom: 8,
        attribution: '© GEBCO',
      },
      // Vector depth zone polygons — provides crisp depth coloring
      depth_zones: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/depth_zones.pmtiles`,
      },
      // Higher-resolution depth zones for European seas (EMODnet DTM)
      depth_zones_eu: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/depth_zones_eu.pmtiles`,
        attribution: '© EMODnet Bathymetry',
      },
      // Coastal survey and LiDAR grids (EMODnet HR-DTM collection)
      depth_zones_hr: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/depth_zones_hr.pmtiles`,
        attribution: '© EMODnet Bathymetry',
      },
      // Vector depth contour lines
      depth_contours: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/depth_contours.pmtiles`,
      },
      // Vector ocean feature labels (SCUFN gazetteer)
      ocean_labels: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/ocean_labels.pmtiles`,
      },
      // Vector seamount polygons (GSFM — Harris et al. 2014)
      seamounts: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/seamounts.pmtiles`,
      },
      // Vector land, coastlines, countries, cities (Natural Earth)
      land_base: {
        type: 'vector',
        url: `pmtiles://${base}/tiles/land_base.pmtiles`,
      },
    },
    layers: BASEMAP_LAYERS,
  };
}

/**
 * Get only the basemap layers (for inserting into an existing style).
 * Useful when adding campaign data layers on top.
 */
export function getBasemapLayers(): LayerSpecification[] {
  return [...BASEMAP_LAYERS];
}

/**
 * Get the depth color for a given depth value.
 * Useful for legends and tooltips.
 */
export function getDepthColor(depth: number): string {
  const depths = Object.keys(DEPTH_COLORS)
    .map(Number)
    .sort((a, b) => b - a);  // Descending (shallowest first)

  for (const d of depths) {
    if (depth <= d) {
      return DEPTH_COLORS[d];
    }
  }
  return DEPTH_COLORS[depths[depths.length - 1]];
}

export { DEPTH_COLORS, FONT_GLYPHS, BLOB_BASE, S3_BASE, DEFAULT_TILE_BASE };
