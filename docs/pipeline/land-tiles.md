# 05 — Land Tiles

Combines Natural Earth and OpenStreetMap data into a multi-resolution, multi-layer vector PMTiles
file containing land polygons, coastline, country borders, and city labels.

## Command

```bash
oceanbase run land-tiles
```

## Process

1. **Download Natural Earth** shapefiles (7 datasets across 3 scales)
2. **Download OSM** pre-processed coastline and land polygon shapefiles
3. **Convert** all sources to GeoJSON (small files) or FlatGeobuf (large OSM files)
4. **Generate PMTiles** — multi-layer with per-source zoom ranges

### Multi-Resolution Strategy

Different sources are used at different zoom levels for optimal detail vs. file size:

| Layer | z0–2 | z3–4 | z5+ |
|-------|------|------|-----|
| Land | NE 110m | NE 10m | OSM |
| Coastline | NE 50m | NE 10m | OSM |
| Countries | — | NE 10m | NE 10m |
| Cities | — | NE 10m | NE 10m |

## Input

| Source | Datasets |
|--------|----------|
| Natural Earth | `ne_110m_land`, `ne_10m_land`, `ne_50m_coastline`, `ne_10m_coastline`, `ne_10m_admin_0_countries`, `ne_10m_populated_places_simple` |
| OpenStreetMap | `land-polygons-split-4326` (~620 MB), `coastlines-split-4326` (~70 MB) |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/land/natural_earth/` | Extracted NE shapefiles |
| `work/land/osm/` | Extracted OSM shapefiles |
| `work/land/land_110m.geojson` | NE 110m land |
| `work/land/land_10m.geojson` | NE 10m land |
| `work/land/coastline_50m.geojson` | NE 50m coastline |
| `work/land/coastline_10m.geojson` | NE 10m coastline |
| `work/land/countries_10m.geojson` | NE 10m countries |
| `work/land/cities_10m.geojson` | NE 10m cities |
| `work/land/osm_land.fgb` | OSM land (FlatGeobuf) |
| `work/land/osm_coastline.fgb` | OSM coastline (FlatGeobuf) |

## Output

| File | Layers | Description |
|------|--------|-------------|
| `tiles/land_base.pmtiles` | `land`, `coastline`, `countries`, `cities` | Multi-layer PMTiles, z0–10 |

## Tippecanoe Options

```
--no-feature-limit
--no-tile-size-limit
--detect-shared-borders
--no-simplification-of-shared-nodes
--no-line-simplification
```
