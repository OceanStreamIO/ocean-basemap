# Upload to Azure

Uploads all generated tile outputs to Azure Blob Storage for serving via the MapLibre GL style.

## Command

```bash
oceanbase run upload
```

## Prerequisites

- **Azure CLI** installed (`brew install azure-cli`)
- Authenticated session (`az login`)

## Process

1. **Check authentication** — runs `az account show`; prompts `az login` if needed
2. **Create container** — ensures the `basemap` container exists with public blob access
3. **Upload hillshade** — batch upload of PNG tile tree as `image/png`
4. **Upload PMTiles** — individual upload of each `.pmtiles` file as `application/vnd.pmtiles`

## Uploaded Assets

| Blob Path | Source | Content-Type |
|-----------|--------|-------------|
| `hillshade/{z}/{x}/{y}.png` | `tiles/hillshade/` | `image/png` |
| `tiles/depth_zones.pmtiles` | `tiles/depth_zones.pmtiles` | `application/vnd.pmtiles` |
| `tiles/depth_contours.pmtiles` | `tiles/depth_contours.pmtiles` | `application/vnd.pmtiles` |
| `tiles/ocean_labels.pmtiles` | `tiles/ocean_labels.pmtiles` | `application/vnd.pmtiles` |
| `tiles/seamounts.pmtiles` | `tiles/seamounts.pmtiles` | `application/vnd.pmtiles` |
| `tiles/land_base.pmtiles` | `tiles/land_base.pmtiles` | `application/vnd.pmtiles` |

## Azure Configuration

| Setting | Value |
|---------|-------|
| Storage account | `ne1osvmdevtest` |
| Container | `basemap` |
| Base URL | `https://ne1osvmdevtest.blob.core.windows.net/basemap` |

!!! note
    Hypsometric tiles are **not uploaded** by default — only hillshade raster tiles and PMTiles
    are deployed to Azure.
