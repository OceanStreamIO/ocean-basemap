# 02f — NCEI CUDEM

Stages NOAA NCEI's Continuously Updated DEM ninth-arc-second topobathy tiles as survey grids, for the US coasts where BlueTopo's charting surveys leave the research multibeam out. Opt-in by region, the bucket's own folder names:

```bash
BASEMAP_CUDEM_REGIONS="CA" oceanbase run cudem
```

## Why it exists

Monterey Canyon stayed soft after BlueTopo went in, and the reason is recorded in [BlueTopo](bluetopo.md): the canyon in BlueTopo sits in a generalised grade its filter rightly rejects elsewhere. CUDEM is NCEI's other national compilation, built for inundation modelling, and it takes the research surveys: the California tiles carry MBARI's multibeam at 1/9 arc-second, about 3 m, from the shoreline to below 900 m. Rendered at Monterey beside the live tree, the canyon walls and their tributary gullies appear where the tree showed a stretched z11 tile, and z13, where the tree had no tile at all, carries full detail.

## Process

1. **List** the region's folder in the public bucket, `noaa-nos-coastal-lidar-pds/dem/NCEI_ninth_Topobathy_2014_8483/<region>/`, following continuation pages.
2. **Download** each tile as it is published: a quarter-degree cloud-optimised GeoTIFF with internal overviews, named by its north-west corner (`ncei19_n36x75_w122x00_2023v1.tif`). California is 40 tiles, 7.5 GB, Monterey Bay to the Golden Gate.
3. **Register** each as a source placed by its name, read as plain NAD83 so the warper does a horizontal transform and nothing else, and capped at the zoom its 3 m cells earn.

Elevations are metres on NAVD88, positive up; bathymetry is negative like GEBCO and the render clamps the land away. The grids feed both `bathy_gmrt` (z9–12) and `bathy_hr` (z12–15).

## Source

| | |
|---|---|
| **Product** | NOAA NCEI Continuously Updated DEM (CUDEM), 1/9 arc-second topobathy |
| **DOI** | [10.25921/ds9v-ky35](https://doi.org/10.25921/ds9v-ky35) |
| **Licence** | US public domain; credited as a courtesy |
| **Access** | AWS open data, `s3://noaa-nos-coastal-lidar-pds/dem/NCEI_ninth_Topobathy_2014_8483/` |
