# Upload to S3

Uploads all generated tile outputs to the S3 bucket the MapLibre style and the
viewer read from.

## Command

```bash
oceanbase run upload
```

## Prerequisites

- **AWS CLI** installed (`uv tool install awscli`, or `brew install awscli`)
- Credentials in the environment as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
  (the `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` names are accepted too)

On the EDITO datalab both are handled by `deploy/s3-basemap/basemap-env.sh`,
which also points `BASEMAP_DATA_DIR` at the pod's volume.

## Process

1. **Sync raster tiles** — `hillshade` and `hypsometric` PNG trees, in bulk
2. **Upload PMTiles** — each `.pmtiles` archive individually

Raster trees go up with `s3 sync`, so an interrupted upload resumes rather than
starting over. The PMTiles archives are single large files and are copied
outright.

## Uploaded Assets

| Key | Source | Content-Type |
|-----|--------|-------------|
| `basemap/hillshade/{z}/{x}/{y}.png` | `tiles/hillshade/` | `image/png` |
| `basemap/hypsometric/{z}/{x}/{y}.png` | `tiles/hypsometric/` | `image/png` |
| `basemap/tiles/*.pmtiles` | `tiles/*.pmtiles` | `application/vnd.pmtiles` |

## S3 Configuration

| Setting | Environment variable | Default |
|---------|---------------------|---------|
| Endpoint | `BASEMAP_S3_ENDPOINT` | `https://s3.waw3-1.cloudferro.com` |
| Bucket | `BASEMAP_S3_BUCKET` | `oceanstream-bucket` |
| Prefix | `BASEMAP_S3_PREFIX` | `basemap` |
| Region | `BASEMAP_S3_REGION` | `waw3-1` |

Public base URL: `https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap`

!!! note
    PMTiles are served with `max-age=3600` and raster tiles with `max-age=604800`.
    Replacing an archive in place therefore takes up to an hour to reach a browser
    that has already cached it; the viewer accepts a `?v=` parameter to force it
    sooner.
