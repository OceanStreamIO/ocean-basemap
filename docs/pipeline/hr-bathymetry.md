# 02c — EMODnet High-Resolution Coastal DTMs

Adds the EMODnet HR-DTM collection — ~1600 coastal survey and LiDAR grids at 1/32 to 1/1024
arc-minute (~58 m down to ~2 m) — as a depth-zone overlay for the highest zoom levels.

## Command

This step is **opt-in** — `oceanbase run all` skips it unless asked:

```bash
oceanbase run hr-bathymetry
oceanbase run all --with hr-bathymetry
```

## Process

1. **Index** — fetch the `emodnet:hr_bathymetry_area` WFS layer and cache it as GeoJSON
2. **Select** — keep grids intersecting the configured extent, one entry per download
3. **Download** — fetch each `.emo.zip` and extract its SeaDataNet netCDF grid
4. **Orient** — detect the grid's sign convention against the EMODnet DTM (see below)
5. **Classify & polygonize** — per grid, into the shared depth zones
6. **Tile** — merge every grid into `depth_zones_hr.pmtiles` at z10–14

Grids are processed individually rather than mosaicked. Their footprints are scattered across
Europe, so a common raster grid would be almost entirely empty.

## Input

| Source | Value |
|--------|-------|
| WFS endpoint | `https://ows.emodnet-bathymetry.eu/wfs` |
| Index layer | `emodnet:hr_bathymetry_area` (~1600 footprints) |
| Downloads | `https://downloads.emodnet-bathymetry.eu/high_resolution/*.emo.zip` |
| Grid format | SeaDataNet netCDF, `elevation` subdataset, Float32, NaN nodata |

Available resolutions, as `1/N` arc-minute:

| N | Grids | Approx. cell size |
|---|-------|-------------------|
| 32 | 380 | ~58 m |
| 64 | 775 | ~29 m |
| 128 | 216 | ~15 m |
| 256 | 151 | ~7 m |
| 512 | 83 | ~3.6 m |
| 1024 | 11 | ~1.8 m |

Coverage is dense around Ireland and the Irish Sea, the Bay of Biscay and French Atlantic canyons,
the Iberian and Portuguese coasts (including the Algarve, Centro and Norte LiDAR surveys and the
Sesimbra grid), the Danish Wadden Sea, the German Elbe and Ems estuaries, Belgian coastal waters,
and the Scottish shelf.

!!! warning "Sign conventions are inconsistent"
    Some HR grids follow the DTM convention (elevation, negative below sea level) while others
    store depth positive-down — with *identical* netCDF metadata in both cases. `HR_Sesimbra` is
    positive-down; `HR_Lidar_Algarve` is not. The step therefore compares each grid's median
    against the EMODnet DTM over the same footprint and flips it when that is the better fit.
    Grids where the reference is unavailable or the comparison is ambiguous keep the documented
    elevation convention.

!!! note "Vertical datum"
    Like the regional DTM, these grids are referenced to Lowest Astronomical Tide.

## Output

| File | Description |
|------|-------------|
| `gebco2026/tiles/depth_zones_hr.pmtiles` | Layer `depth_zones_hr`, z10–14 |

Each polygon carries `min_depth`, plus `source` (the dataset identifier) and `res` (the resolution
denominator) so the origin of any patch can be identified in the viewer.

Intermediates are kept per dataset under `gebco2026/work/emodnet_hr/` — `archives/`, `grids/`,
`rasters/` and `vectors/` — so an interrupted run resumes where it stopped, and a single failing
grid never aborts the step.

## Configuration

| Constant | Default | Description |
|----------|---------|-------------|
| `EMODNET_HR_EXTENT` | `EMODNET_EXTENT` | Footprints must intersect this box |
| `EMODNET_HR_MIN_DENOM` | `32` | Skip grids coarser than 1/N arc-minute |
| `EMODNET_HR_MIN_ZOOM` / `EMODNET_HR_MAX_ZOOM` | `10` / `14` | PMTiles zoom range |

Narrow `EMODNET_HR_EXTENT` to a single region to build a smaller overlay — for example
`(-11.0, 51.0, -5.0, 56.0)` for Ireland.

## Safety

The index is third-party input, so the step validates it: download URLs must be HTTPS on
`downloads.emodnet-bathymetry.eu`, dataset identifiers are stripped to `[A-Za-z0-9_.-]` before
being used in filenames or OGR SQL, and archive members are extracted by name only — never by the
path recorded in the zip.

## Tools

- **httpx** — WFS index and archive downloads
- **rasterio** — netCDF reads, sign detection, depth-zone classification
- **gdal_polygonize.py / ogr2ogr** — Raster to attributed polygons
- **tippecanoe** — PMTiles output

## Attribution

> EMODnet Bathymetry Consortium — High Resolution Digital Terrain Models. Individual grids retain
> the attribution of their originating hydrographic office or survey provider, linked from each
> entry's `metadata_url` in the index.
