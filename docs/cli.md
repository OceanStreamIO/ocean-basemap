# CLI Reference

The `basemap` CLI is built with [Typer](https://typer.tiangolo.com/) and provides three commands
for running the pipeline, checking status, and serving tiles locally.

## Global Options

| Option | Description |
|--------|-------------|
| `--data-dir`, `-d` | Override the data directory (default: `$BASEMAP_DATA_DIR`, or `~/basemap-data`, which `run` makes on first use) |
| `--help` | Show help and exit |

---

## `oceanbase run`

Run a pipeline step or the full pipeline.

```bash
oceanbase run <step>
```

### Arguments

| Argument | Description |
|----------|-------------|
| `step` | Step name or `all` to run the full pipeline in order |

### Available Steps

| Step | Module | Description |
|------|--------|-------------|
| `download` | `basemap.pipeline.download` | Download & merge GEBCO 2026 tiles |
| `hillshade` | `basemap.pipeline.hillshade` | Generate hillshade raster tiles |
| `depth-polygons` | `basemap.pipeline.depth_polygons` | Generate depth zone vector PMTiles |
| `emodnet` | `basemap.pipeline.emodnet` | EMODnet regional depth zones (European seas) — *opt-in* |
| `hr-bathymetry` | `basemap.pipeline.hr_bathymetry` | EMODnet high-resolution coastal DTMs — *opt-in* |
| `contours` | `basemap.pipeline.contours` | Extract depth contour PMTiles |
| `ocean-labels` | `basemap.pipeline.ocean_labels` | Merge ocean feature labels → PMTiles |
| `seamounts` | `basemap.pipeline.seamounts` | GSFM seamount polygons → PMTiles |
| `land-tiles` | `basemap.pipeline.land_tiles` | NE + OSM → land base PMTiles |
| `sea-mask` | `basemap.pipeline.sea_mask` | Complement of OSM land → sea mask PMTiles |
| `lakes` | `basemap.pipeline.lakes` | HydroLAKES outlines + shaded lake beds |
| `boundaries` | `basemap.pipeline.boundaries` | Country and internal boundary lines |
| `hypsometric` | `basemap.pipeline.hypsometric` | Copernicus DEM → shaded land relief PMTiles |
| `bathymetry` | `basemap.pipeline.bathymetry` | Continuous shaded bathymetry PMTiles |
| `upload` | `basemap.pipeline.upload_s3` | Upload to S3 object storage |

### Examples

```bash
# Run a single step
oceanbase run download
oceanbase run hillshade

# Run with custom data directory
oceanbase run depth-polygons --data-dir /mnt/data/basemap

# Run the full pipeline
oceanbase run all

# Add the opt-in regional steps
oceanbase run all --with emodnet --with hr-bathymetry
oceanbase run all --with all
```

### Options

| Option | Description |
|--------|-------------|
| `--skip-upload` | Publish nothing: leave out the `upload` step, and keep the `xyz` tree on disk (it sets `BASEMAP_XYZ_STAGE_ONLY=1` for the run). Works with a single step too |
| `--with <step>` | Add an opt-in step to `all` — `emodnet`, `hr-bathymetry`, or `all`. Repeatable |

### Execution Order

When using `all`, steps execute in this order:

download → hillshade → depth-polygons → contours → ocean-labels → seamounts → land-tiles →
sea-mask → lakes → rivers → surface-water → coastal-water → water-polygons → places →
boundaries → worldcover → hypsometric → bathymetry → globe → xyz → upload

`rivers` downloads the published river network when none has been traced here, and
`worldcover` stages the land cover that `hypsometric` and `globe` are tinted by. The
staging steps for the regional and survey grids (`gmrt`, `cudem`, `ausbathytopo`,
`bluetopo`), `merit-rivers` and the parked `reference` run only by name.

Opt-in steps keep their place in that order: `emodnet` and `hr-bathymetry` run after
`depth-polygons` and before `contours` when requested.

---

## `oceanbase status`

Show the status of generated tiles and intermediate files.

```bash
oceanbase status
```

Displays a table with:

- **GEBCO 2026** — whether the merged DEM exists
- **Hillshade tiles** — whether the z0 tile directory exists
- **PMTiles** — existence and file size for each `.pmtiles` output:
    - `depth_zones.pmtiles`
    - `depth_contours.pmtiles`
    - `ocean_labels.pmtiles`
    - `seamounts.pmtiles`
    - `land_base.pmtiles`
- **Hypsometric tiles** — whether the z0 tile directory exists

### Example Output

```
              Basemap Pipeline Status
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ Asset                  ┃ Status ┃ Size    ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ GEBCO 2026             │ ✓      │ 3.2 GB  │
│ Hillshade tiles        │ ✓      │ —       │
│ depth_zones.pmtiles    │ ✓      │ 412 MB  │
│ depth_contours.pmtiles │ ✓      │ 89 MB   │
│ ocean_labels.pmtiles   │ ✓      │ 1.2 MB  │
│ seamounts.pmtiles      │ ✓      │ 24 MB   │
│ land_base.pmtiles      │ ✓      │ 340 MB  │
│ Hypsometric tiles      │ ✗      │ —       │
└────────────────────────┴────────┴─────────┘
```

---

## `oceanbase serve`

Start a local tile server for visual inspection in the browser.

```bash
oceanbase serve [--port PORT]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--port`, `-p` | `9191` | Port to listen on |

The server:

- Serves tile files from the data directory
- Handles HTTP Range requests (required for PMTiles)
- Sets CORS headers for cross-origin access
- Supports both raster PNG tiles and PMTiles

### Example

```bash
# Default port
oceanbase serve

# Custom port
oceanbase serve --port 8080
```

Then open `http://localhost:9191` in a browser.

---

## `oceanbase viewer`

Launch the interactive tile viewer — starts the server and opens the browser.

```bash
oceanbase viewer [--port PORT] [--no-open]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--port`, `-p` | `9191` | Port to listen on |
| `--no-open` | `false` | Don't auto-open the browser |

### Example

```bash
# Launch viewer and open browser
oceanbase viewer

# Custom port, no auto-open
oceanbase viewer --port 8080 --no-open
```
