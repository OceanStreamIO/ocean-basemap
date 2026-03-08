# 04 — Ocean Labels

Merges Natural Earth marine feature names with the IHO/SCUFN undersea feature gazetteer into a
single label layer with zoom-level assignments based on feature type.

## Command

```bash
oceanbase run ocean-labels
```

## Process

1. **Download** Natural Earth GeoJSON files:
    - `ne_10m_geography_marine_polys` — ocean/sea/gulf names
    - `ne_10m_geography_regions_points` — geographic region labels
2. **Download SCUFN** gazetteer from NOAA ArcGIS REST (optional — gracefully skipped if unreachable)
3. **Merge** all sources, assigning `min_zoom` based on feature type
4. **Generate PMTiles** via tippecanoe

### SCUFN Zoom Mapping

Feature types are mapped to minimum zoom levels so that the largest features appear first:

| Feature Type | Min Zoom | Feature Type | Min Zoom |
|-------------|----------|-------------|----------|
| Ocean | 1 | Fracture Zone | 5 |
| Sea | 2 | Escarpment | 5 |
| Gulf, Bay | 2–3 | Seamount, Guyot | 6 |
| Basin, Ridge, Trench | 3 | Bank, Canyon | 6 |
| Plateau, Rise, Plain | 4 | Sill, Knoll, Reef | 7 |
| | | Saddle, Hill, Pinnacle | 8 |

## Input

| Source | Description |
|--------|-------------|
| Natural Earth | Marine polygon/point GeoJSON files |
| SCUFN | NOAA ArcGIS REST API (optional) |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/labels/ne_10m_geography_marine_polys.geojson` | NE marine polygons |
| `work/labels/ne_10m_geography_regions_points.geojson` | NE region points |
| `work/labels/scufn_features.geojson` | SCUFN gazetteer (if available) |
| `work/labels/ocean_labels_merged.geojson` | Merged labels with zoom mapping |

## Output

| File | Description |
|------|-------------|
| `tiles/ocean_labels.pmtiles` | Vector PMTiles, z1–10 |
