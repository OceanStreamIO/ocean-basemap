# OceanStream Basemap

GEBCO-based ocean basemap pipeline for the [OceanStream](https://oceanstream.io) web app.
Raster hillshade for 3D texture, vector polygons for crisp depth color, vector contours and labels — all in one.

## Why Hybrid?

Traditional ocean basemaps bake color into the hillshade raster, which looks washed out at most
zoom levels. OceanStream uses a **hybrid approach** (similar to Mapbox Bathymetry v2):

- **Hillshade raster** — grayscale 3D relief texture only (no color data)
- **Vector depth polygons** — crisp, zoom-independent depth zone coloring
- **Vector contours & labels** — sharp at any resolution

The result: pixel-perfect color at any zoom, with hillshade providing subtle relief underneath.

## Layer Stack

Rendered bottom-to-top in MapLibre GL:

| # | Layer | Source |
|---|-------|--------|
| 1 | Hillshade raster (z0–10) | GEBCO 2026 → `gdal2tiles` → Azure Blob |
| 2 | Depth zone polygons | GEBCO 2026 → reclassify → `tippecanoe` → PMTiles |
| 2b | Depth zones (Europe, z6+) | *Optional.* EMODnet DTM → reclassify → `tippecanoe` → PMTiles |
| 2c | Depth zones (HR coastal, z10+) | *Optional.* EMODnet HR-DTMs → reclassify → `tippecanoe` → PMTiles |
| 3 | Land fill | Natural Earth + OSM → PMTiles |
| 4 | Coastline | Natural Earth + OSM → PMTiles |
| 5 | Country borders | Natural Earth → PMTiles |
| 6 | Seamount polygons | GSFM (Harris et al. 2014) → PMTiles |
| 7 | Depth contour lines | GEBCO 2026 → `gdal_contour` → PMTiles |
| 8 | Depth contour labels | Along-line symbol placement |
| 9 | Seamount labels | GSFM name property |
| 10 | Ocean feature labels | Natural Earth + SCUFN Gazetteer → PMTiles |
| 11 | City labels | Natural Earth populated places |

## Quick Start

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | ≥ 3.11 | — |
| uv | latest | `pip install uv` |
| GDAL | ≥ 3.12 | `brew install gdal` |
| Tippecanoe | 2.x | macOS: `brew install tippecanoe` · Ubuntu: build from [source](https://github.com/felt/tippecanoe) |
| Azure CLI | latest | `brew install azure-cli` (upload step only) |

### Install

```bash
git clone https://github.com/OceanStreamIO/ocean-basemap.git
cd ocean-basemap
uv sync
```

### CLI

```bash
# Check pipeline status
oceanbase status

# Run a single step
oceanbase run download
oceanbase run hillshade

# Run the full pipeline
oceanbase run all

# Include the opt-in regional layers (European seas, large downloads)
oceanbase run all --with emodnet --with hr-bathymetry

# Start local tile viewer
oceanbase serve
```

### Pipeline Steps

| # | Step | Command | Description |
|---|------|---------|-------------|
| 00 | Download | `oceanbase run download` | Download & merge 8 GEBCO 2026 tiles from CEDA |
| 01 | Hillshade | `oceanbase run hillshade` | Multi-directional hillshade → gamma correction → PNG tiles z0–10 |
| 02 | Depth Polygons | `oceanbase run depth-polygons` | Reclassify into 14 depth zones → polygonize → PMTiles |
| 02b | EMODnet | `oceanbase run emodnet` | *Opt-in.* EMODnet DTM (~115 m) → regional depth zones for European seas |
| 02c | HR Coastal DTMs | `oceanbase run hr-bathymetry` | *Opt-in.* EMODnet HR survey & LiDAR grids (to ~2 m) → coastal depth zones |
| 03 | Contours | `oceanbase run contours` | Extract depth contours at multiple intervals → PMTiles |
| 04 | Ocean Labels | `oceanbase run ocean-labels` | Merge Natural Earth + SCUFN gazetteer → PMTiles |
| 04b | Seamounts | `oceanbase run seamounts` | GSFM seamount polygons → PMTiles |
| 05 | Land Tiles | `oceanbase run land-tiles` | Natural Earth + OSM → multi-layer PMTiles |
| 06 | Hypsometric | `oceanbase run hypsometric` | EMODnet-style land color relief → PNG tiles z0–8 |
| — | Upload | `oceanbase run upload` | Upload all outputs to Azure Blob Storage |

## Project Structure

```
basemap/
├── src/basemap/              # Python package
│   ├── cli.py                # Typer CLI (run, status, serve, viewer)
│   ├── config.py             # URLs, checksums, depth breaks, colors
│   ├── pipeline/             # 9 pipeline step modules
│   ├── processing/           # Reclassify, labels, seamount cleaning
│   ├── utils/                # GDAL wrappers, downloads, paths
│   ├── viewer/               # Interactive tile viewer (HTML/JS/CSS)
│   └── server/               # Local tile server
├── scripts/                  # Original bash scripts (reference, pinned to GEBCO 2025)
├── styles/                   # MapLibre GL style (TypeScript)
├── docs/                     # MkDocs documentation site
├── tests/                    # pytest test suite
├── color-ramps/              # GDAL color relief ramp files
├── mkdocs.yml                # Docs site configuration
└── pyproject.toml            # Project metadata & dependencies
```

## Data Directory

All heavy data lives on an external drive (configurable via `$BASEMAP_DATA_DIR`):

```
/media/andrei/RP60/oceanstream/basemap/
└── gebco2026/
    ├── GEBCO_2026.tif          # Merged source DEM (~3 GB)
    ├── work/                   # Intermediate processing files
    └── tiles/                  # Generated tile outputs
        ├── hillshade/          # PNG tile tree (z0–10)
        ├── hypsometric/        # PNG tile tree (z0–8)
        ├── depth_zones.pmtiles
        ├── depth_contours.pmtiles
        ├── ocean_labels.pmtiles
        ├── seamounts.pmtiles
        └── land_base.pmtiles
```

## Development

```bash
# Install with dev dependencies
uv sync

# Run tests
uv run pytest

# Lint & format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Docs — live preview
uv run mkdocs serve              # http://localhost:9292

# Docs — build static site
uv run mkdocs build              # output in site/
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BASEMAP_DATA_DIR` | Base data directory | `/media/andrei/RP60/oceanstream/basemap` |
| `BASEMAP_GEBCO_RELEASE` | GEBCO grid year — drives download URLs and the `gebco<year>/` directory | `2026` |

### Adding a Pipeline Step

1. Create a module in `src/basemap/pipeline/` with a `run(data_dir=None)` function
2. Register it in `cli.py` — add to `STEPS` dict and `RUN_ORDER` list
3. Add a status check in the `status` command
4. Write tests in `tests/`
5. Add a docs page in `docs/pipeline/` and update `mkdocs.yml`

## Local Tile Viewer

An interactive HTML + MapLibre GL map for visually inspecting all basemap layers.

```bash
# Launch viewer and auto-open browser
oceanbase viewer

# Server only (no auto-open)
oceanbase serve

# Custom port
oceanbase viewer --port 8080
```

Open **http://localhost:9191/** in a browser. The viewer includes layer toggles, opacity sliders,
depth legend, coordinate display, graticule, and measurement tools.

## Data Sources

| Dataset | Provider | License |
|---------|----------|---------|
| [GEBCO 2025 Grid](https://www.gebco.net/data_and_products/gridded_bathymetry_data/) | GEBCO / BODC | Public domain |
| [Natural Earth](https://www.naturalearthdata.com/) | NACIS | Public domain |
| [SCUFN Gazetteer](https://gis.ngdc.noaa.gov/arcgis/rest/services/IHO/undersea_features/MapServer/) | IHO / NOAA NCEI | Free for research |
| [GSFM Seamounts](https://www.bluehabitats.org/) | Harris et al. (2014) | CC-BY 4.0 |
| [OSM Coastlines](https://osmdata.openstreetmap.de/data/) | OpenStreetMap contributors | ODbL |
| [Protomaps Fonts](https://protomaps.github.io/basemaps-assets/) | Google / Protomaps | OFL (Noto) |

See the full [Data Sources & Attribution](docs/data-sources.md) page for citations and DOIs.

## License

MIT License — © 2025 [Pine View Software AS](https://oceanstream.io)
