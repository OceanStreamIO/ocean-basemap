# 02g — AusBathyTopo

Stages Geoscience Australia's 30 m regional depth models as survey and regional grids. Opt-in by name:

```bash
BASEMAP_AUSBATHYTOPO="north torres" oceanbase run ausbathytopo
```

## Why it exists

Around Australia the map was GEBCO's 460 m grid from z0 to z12. Geoscience Australia compiles every survey it holds into 30 m regional models and publishes them openly. Rendered off Cairns beside the live tree, the reef outlines and the shelf-edge canyons appear where GEBCO had smooth blobs and the tile seams of a z8 tile stretched to z11.

## The regions

| Name | Model | Extent | Zip |
|---|---|---|---|
| `north` | Northern Australia 2018 | 121–133° E, 8–18° S | 4.4 GB |
| `torres` | Torres Strait 2023 | 139–146° E, 8–13° S | 1.4 GB |
| `gbr` | Great Barrier Reef 2020 | 142–156° E, 10–29° S | 3.8 GB |
| `bass` | Bass Strait 2022 | 143–150° E, 37–42° S | 4.5 GB |

Each zip holds cloud-optimised GeoTIFFs, WGS84, float32, metres about mean sea level, negative down. The step downloads the zip, extracts the GeoTIFFs and deletes the zip. A 30 m cell is native at z12, so the grids feed `bathy_gmrt` (z9–12) and `bathy_hr`, where they stop at z13.

## Licence

All four are published under CC BY 4.0, credited as *© Commonwealth of Australia (Geoscience Australia)*, and none is for navigation. Two of the records add a second notice in their lineage, from the Australian Hydrographic Service: material in the product is reproduced under its licence and "may not be copied, reproduced, translated, or reduced to any electronic medium or machine readable form, in whole or part, without the prior written consent of the Australian Hydrographic Service". Those are `gbr` and `bass` (`NOTICE` in `pipeline/ausbathytopo.py`). Whether a rendered basemap falls under that notice is a decision for the project, not for the code, so those two are staged only when named.
