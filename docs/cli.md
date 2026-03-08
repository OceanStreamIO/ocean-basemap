# CLI Reference

The `basemap` CLI is built with [Typer](https://typer.tiangolo.com/) and provides three commands
for running the pipeline, checking status, and serving tiles locally.

## Global Options

| Option | Description |
|--------|-------------|
| `--data-dir`, `-d` | Override the data directory (default: `$BASEMAP_DATA_DIR` or `/Volumes/RP60/oceanstream/basemap`) |
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
| `download` | `basemap.pipeline.download` | Download & merge GEBCO 2025 tiles |
| `hillshade` | `basemap.pipeline.hillshade` | Generate hillshade raster tiles |
| `depth-polygons` | `basemap.pipeline.depth_polygons` | Generate depth zone vector PMTiles |
| `contours` | `basemap.pipeline.contours` | Extract depth contour PMTiles |
| `ocean-labels` | `basemap.pipeline.ocean_labels` | Merge ocean feature labels → PMTiles |
| `seamounts` | `basemap.pipeline.seamounts` | GSFM seamount polygons → PMTiles |
| `land-tiles` | `basemap.pipeline.land_tiles` | NE + OSM → land base PMTiles |
| `hypsometric` | `basemap.pipeline.hypsometric` | Hypsometric color relief tiles |
| `upload` | `basemap.pipeline.upload` | Upload to Azure Blob Storage |

### Examples

```bash
# Run a single step
oceanbase run download
oceanbase run hillshade

# Run with custom data directory
oceanbase run depth-polygons --data-dir /mnt/data/basemap

# Run the full pipeline
oceanbase run all
```

### Execution Order

When using `all`, steps execute in this order:

1. download → 2. hillshade → 3. depth-polygons → 4. contours → 5. ocean-labels → 6. seamounts → 7. land-tiles → 8. hypsometric → 9. upload

---

## `oceanbase status`

Show the status of generated tiles and intermediate files.

```bash
oceanbase status
```

Displays a table with:

- **GEBCO 2025** — whether the merged DEM exists
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
│ GEBCO 2025             │ ✓      │ 3.2 GB  │
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
