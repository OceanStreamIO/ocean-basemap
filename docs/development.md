# Development

## Setup

```bash
# Clone the repository
git clone https://github.com/OceanStreamIO/ocean-basemap.git
cd ocean-basemap

# Install with dev dependencies
uv sync

# Verify installation
oceanbase --help
```

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | ≥ 3.11 | — |
| uv | latest | `pip install uv` |
| GDAL | ≥ 3.12 | `brew install gdal` |
| Tippecanoe | 2.x | macOS: `brew install tippecanoe` · Ubuntu: build from [source](https://github.com/felt/tippecanoe) |
| Azure CLI | latest | `brew install azure-cli` (upload step only) |

## Testing

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_config.py
```

The test suite covers:

- **Config** — Data directory resolution, path helpers, constant integrity
- **Reclassify** — Depth zone classification with rasterio
- **Labels** — Ocean label merging and zoom assignment
- **Clean** — Seamount property normalization
- **Utils** — GDAL wrapper argument construction, tippecanoe invocations

## Linting

```bash
# Check for issues
uv run ruff check src/ tests/

# Auto-fix
uv run ruff check --fix src/ tests/

# Format
uv run ruff format src/ tests/
```

## Documentation

```bash
# Live preview with hot reload
uv run mkdocs serve

# Build static site
uv run mkdocs build

# Build output goes to site/
```

## Project Structure

```
src/basemap/
├── cli.py                 # Typer CLI entry point
├── config.py              # URLs, checksums, constants
├── pipeline/              # Pipeline step modules
│   ├── download.py        # 00 — Download & merge GEBCO
│   ├── hillshade.py       # 01 — Hillshade raster tiles
│   ├── depth_polygons.py  # 02 — Depth zone PMTiles
│   ├── contours.py        # 03 — Contour PMTiles
│   ├── ocean_labels.py    # 04 — Ocean label PMTiles
│   ├── seamounts.py       # 04b — Seamount PMTiles
│   ├── land_tiles.py      # 05 — Land base PMTiles
│   ├── hypsometric.py     # 06 — Hypsometric raster tiles
│   └── upload.py          # Upload to Azure
├── processing/            # Data processing modules
│   ├── reclassify.py      # Rasterio depth zone classification
│   ├── labels.py          # NE + SCUFN label merge
│   └── clean.py           # Seamount property cleaning
├── utils/                 # Shared utilities
│   ├── paths.py           # Path resolution, mount checks
│   ├── download.py        # HTTP downloads with progress
│   ├── gdal.py            # GDAL subprocess wrappers
│   └── tippecanoe.py      # Tippecanoe PMTiles generation
├── viewer/                # Interactive tile viewer
│   ├── index.html         # Page shell (MapLibre + PMTiles CDN)
│   ├── style.css          # Dark theme, panel styling
│   └── app.js             # Map init, layer controls, tools
└── server/
    └── serve.py           # Local HTTP tile server
```

## Adding a Pipeline Step

1. Create a new module in `src/basemap/pipeline/` with a `run(data_dir=None)` function
2. Register it in `cli.py` by adding an entry to the `STEPS` dict and `RUN_ORDER` list
3. Add a status check in the `status` command
4. Add a documentation page in `docs/pipeline/` and update `mkdocs.yml` nav
5. Write tests in `tests/`

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BASEMAP_DATA_DIR` | Base data directory | `/media/andrei/RP60/oceanstream/basemap` |
| `BASEMAP_GEBCO_RELEASE` | GEBCO grid year — drives download URLs and the `gebco<year>/` directory | `2026` |
