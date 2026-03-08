# 04b — Seamounts

Extracts seamount polygon features from the Global Seafloor Geomorphic Features Map (GSFM) archive
and generates vector PMTiles.

## Command

```bash
oceanbase run seamounts
```

## Process

1. **Download** the GSFM shapefile archive from Dropbox (~389 MB)
2. **Extract** and locate the seamount shapefile (`*Seamount*.shp`)
3. **Convert** to GeoJSON with EPSG:4326 projection
4. **Clean properties** — normalize field names to a minimal set (name + geometry)
5. **Generate PMTiles** via tippecanoe with border detection and simplification

## Input

| Source | Description |
|--------|-------------|
| GSFM | Shapefile archive from [bluehabitats.org](https://www.bluehabitats.org/) |

## Intermediate Files

| File | Description |
|------|-------------|
| `work/seamounts/gsfm_shapefiles.zip` | Downloaded archive |
| `work/seamounts/gsfm_extracted/` | Extracted shapefiles |
| `work/seamounts/seamounts.geojson` | Raw GeoJSON conversion |
| `work/seamounts/seamounts_clean.geojson` | Cleaned properties |

## Output

| File | Description |
|------|-------------|
| `tiles/seamounts.pmtiles` | Vector PMTiles, z0–10 |

## Tippecanoe Options

```
--simplification=4
--detect-shared-borders
--no-feature-limit
--no-tile-size-limit
```

## Attribution

> Harris, P.T., Macmillan-Lawler, M., Rupp, J., and Baker, E.K. (2014).
> "Geomorphology of the oceans." *Marine Geology*, 352: 4–24. CC-BY 4.0.
