# 02e — GMRT Swath Bathymetry

Stages GMRT's masked grid — the Global Multi-Resolution Topography synthesis (Ryan et al. 2009) — as local grids for the bathymetry stack. GMRT is the compilation of every ship multibeam swath its curators can gather, gridded at about 100 m, and it is what the Esri ocean basemap shows wherever that map is sharp. The step is **opt-in** and is driven by `BASEMAP_GMRT_REGIONS`:

```bash
BASEMAP_GMRT_REGIONS="us-west gulf-of-mexico us-northeast" oceanbase run gmrt
```

Nothing is staged when the variable is unset. Region names are in `REGIONS` in `pipeline/gmrt.py`; the first set covers the Americas' margins and Hawaii, with Alaska left for later as it is for BlueTopo.

## Why the masked layer

GMRT publishes two grids. `topo` is filled with GEBCO wherever no high-resolution data exist; `topo-mask` holds only the swath-sourced cells and NaN everywhere else. The masked one is the point: it says where the survey ends and the fill begins, which is exactly what BlueTopo's attribute table could not, and which no rule over that table could recover — at Monterey the canyon detail sat in BlueTopo's grade-3 fill, inseparable from the triangulated facets beside it. Masked GMRT has the canyon and no facets, because where it has no swath it has nothing, and the stack falls through to GEBCO.

Measured at Monterey Bay the masked grid matched Esri's Ocean Base tiles one for one, and an open-Pacific control came back 94% NaN.

## Process

1. **Fetch** — one HTTP request per named box to the GridServer, `layer=topo-mask`, GeoTIFF, `mresolution=100`
2. **Tag** — the service's GeoTIFF carries NaN but no nodata tag, so the tag is set on the download in place; read as it comes, the warper would take the NaNs for elevation
3. **Write** — a COG with deflate, 512-pixel blocks and averaged overviews, like every other staged grid, under `work/gmrt/grids/<region>.tif`

A box may not exceed 20 by 20 degrees, which is the service's 2 GB request limit at 100 m; the step refuses one that does before asking. The 3 by 3.5 degree California box is 165 MB in 52 s; a full 20 by 20 box is about 1.5 GB. Requests go one at a time with a pause between, since it is one shared research service.

## Where it sits

| Archive | Zoom | Sources, first wins |
|---|---|---|
| `bathy_gmrt` | z9–11 | GMRT boxes, then GEBCO |
| `bathy_hr` | z12–15 | surveys, EMODnet, **GMRT**, GEBCO |

`bathy_gmrt` is `bathy_eu`'s band with its own bounds. The two are told apart by their headers — the reader skips an archive whose declared bounds exclude the tile — so Europe comes from EMODnet and the Americas from GMRT with no overlap to resolve. A tile with no swath in it is not written, so the archive holds only what it improves.

In `bathy_hr` the swath grid goes under the surveys and over GEBCO, so a z12 tile a survey only half covers shows swath in the rest rather than a 460 m grid. That needs `bathy_hr` re-rendered to take effect; the tree and the viewer get the z9–11 archive without it.

## Coverage, measured

Share of sea with swath data in 2 by 2 degree boxes, from the service's fine levels:

| Place | Swath | Place | Swath |
|---|---|---|---|
| Monterey / SF | 93% | Texas shelf | 95% |
| Oregon coast | 73% | Mississippi delta | 82% |
| N. California (Eureka) | 64% | Florida Straits | 85% |
| S. California Bight | 53% | Cape Hatteras | 77% |
| WA coast | 50% | Hudson Canyon | 89% |
| SF north (Pt Reyes) | 40% | Georges Bank | 95% |
| Baja (La Paz) | 42% | Puerto Rico | 99% |
| Mexico Pacific (Manzanillo) | 52% | Cuba north / Bahamas | 70% |
| Hawaii | 97% | Lesser Antilles | 69% |
| Hispaniola south | 19% | Jamaica / Caymans | 7% |
| Yucatán, Honduras–Nicaragua, Panama–Colombia | **0%** | central Pacific / Atlantic | 15–28% |

The US margins and the Caribbean's US territories are well covered; the western Caribbean is not, and will keep looking like GEBCO.

**A measurement trap.** The service snaps a request to the levels of its pyramid, and the mask is not the same at every level: at about 2 km per node the Monterey box reports 2% swath where the 390 m and finer levels report 93–95%, and land comes back as NaN. Coverage must be measured at 400 m or finer, which is what the step stages at.

## Licence

CC BY 4.0, with the credit *GMRT: Ryan et al. (2009), Global Multi-Resolution Topography synthesis, doi:10.1594/IEDA.100001*. The data are not for navigation, as nothing here is.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `BASEMAP_GMRT_REGIONS` | *unset* | region names to stage, space-separated; unset stages none |
| `BASEMAP_GMRT_RESOLUTION_M` | 100 | metres per node asked of the service |
