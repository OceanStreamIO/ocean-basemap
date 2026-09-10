# OceanStream Basemap Pipeline

GEBCO-based ocean basemap for the OceanStream web app. Hybrid approach: **raster hillshade** (3D texture) + **vector depth polygons** (crisp color) + **vector contours/labels**.

## Architecture

See [README.md](../README.md) for the full layer stack and pipeline overview.

Key components:
- `scripts/` — Numbered shell scripts (`00-` to `06-`), run sequentially. Each is idempotent.
- `styles/OceanBasemapStyle.ts` — MapLibre GL style definition (TypeScript). Constants → helpers → layers → exported factory.
- `test/` — Local dev server (`serve.py`) + browser app (`app.js`) for visual tile inspection.
- `color-ramps/` — GDAL color relief ramp files for hillshade and depth coloring.

## Build & Test

**Prerequisites:** `gdal` (3.12+), `tippecanoe` (2.x), `az` CLI, Python 3 stdlib.

```bash
# Run pipeline scripts in numbered order
bash scripts/00-download-gebco2025.sh
bash scripts/01-generate-hillshade.sh
# ...etc through 06

# Local tile inspection
python3 test/serve.py                        # serves at http://localhost:9191
python3 test/serve.py /path/to/tiles --port 8080  # custom path/port
```

## Conventions

### Shell scripts (`scripts/`)
- Always start with `set -euo pipefail`
- Use step-wise console output (`echo "=== Step N: ..."`)
- Check prerequisites early (external drive mount, input files) with `exit 1` on failure
- Idempotency: skip steps if output already exists (`if [[ -f ... ]]`)
- End with "Next: run ..." guidance pointing to the next script
- All data lives on external drive at `/media/andrei/RP60/oceanstream/basemap/`
- Use variables: `BASEMAP_DIR`, `WORK_DIR`, `OUTPUT_DIR`, `TILE_DIR`

### TypeScript style (`styles/`)
- Top-level constants (`BLOB_BASE`, `DEPTH_COLORS`, `FONT_GLYPHS`)
- Helper functions build MapLibre expressions programmatically — don't hardcode long arrays
- Each layer is a typed `const` (`LayerSpecification`) in render order
- Export a factory function (`createOceanBasemapStyle`) plus named accessors
- Azure blob base URL: `https://ne1osvmdevtest.blob.core.windows.net/basemap`

### Test app (`test/`)
- `serve.py` handles Range requests (for PMTiles), CORS, and serves tiles + static assets
- `app.js` registers PMTiles protocol, builds style, provides layer toggle / opacity / legend UI
- Test against tiles at `/Volumes/RP60/oceanstream/basemap/gebco2025/tiles/`

## Known Pitfalls

- **External drive dependency**: All scripts assume `/media/andrei/RP60/...`. Mount must be present.
- **Hypsometric tiles not uploaded**: `06-upload-azure.sh` skips hypsometric tile tree — only hillshade + PMTiles are uploaded.
- **PMTiles protocol**: `OceanBasemapStyle.ts` assumes the consumer registers the PMTiles protocol externally before using the style.
- **Hillshade max zoom**: Style caps hillshade at `maxzoom: 8` — generation scripts can build higher, but style won't request them.
