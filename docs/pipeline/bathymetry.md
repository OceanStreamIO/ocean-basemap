# 07 — Continuous Bathymetry

Renders the elevation grids straight to shaded, continuously coloured raster tiles.
This replaces the classed depth-zone polygons as what the viewer draws. The polygons
put a pixel staircase at every class edge and left the relief to a separate hillshade
layer, which the opaque fills hid.

## Command

```bash
oceanbase run bathymetry
```

## Output

| Archive | Zooms | Coverage | Sources, best first |
|---------|-------|----------|---------------------|
| `bathy_global.pmtiles` | z0–8 | global ocean | GEBCO |
| `bathy_eu.pmtiles` | z9–11 | EMODnet extent | EMODnet DTM, GEBCO |
| `bathy_hr.pmtiles` | z12–15 | HR survey footprints only, each to the zoom its cells earn | EMODnet HR, BlueTopo, CUDEM and AusBathyTopo grids, EMODnet, GMRT, GEBCO |

Each tile is completed from the sources beneath it wherever the best source has gaps,
so there is nothing underneath to show through. `bathy_eu` and `bathy_hr` need the
opt-in `emodnet` and `hr-bathymetry` steps; without them only `bathy_global` is built.

## How a tile is rendered

1. **Read.** Each source is warped to the tile at 256 px plus a one-pixel buffer. Every
   source fills only the pixels the ones before it left empty.
2. **Colour.** Continuous interpolation on
   `color-ramps/bathymetry-water.txt`, the EMODnet World Base Layer sample, lifted into
   water and turned towards cyan. The sample itself reads as neutral grey where the sea
   is shallow — rgb(241,242,244) at the surface, a chroma of 3, not recognisably blue
   until about 35 m down — so a fjord came out white beside lakes that looked like
   water, the lake ramp starting at a chroma of 31. The shipped ramp starts at
   rgb(191,228,240): a chroma of 49 at a hue of 195°, where the sample sits at 208°,
   which is a blue leaning to violet and read as lavender against the warm land. The
   viewer's legend and the vector strip it draws along every shore are generated from
   this same ramp, so a change here reaches all three.
3. **Shade.** A hillshade whose vertical exaggeration grows with the pixel size, so relief
   still reads at low zoom. It is baked into the colour. Flat seabed keeps its exact ramp
   colour, which is what the legend shows.

   Shading is a multiply that clips at 255, so **the ramp and the shading ceiling have to
   be chosen together.** With a surface blue of 245 and a ceiling of 1.20, an eighth of
   the water in an inner Oslofjord frame hit 255 on some channel and washed out to white,
   losing the hue and not only the detail. No stop's brightest channel now exceeds 240 and
   the ceiling is 1.12, which leaves the lit slopes somewhere to go. A test in
   `tests/test_viewer.py` keeps the two in step.
4. **Alpha.** OSM land is rasterised at 4× the tile resolution. A pixel is opaque only when
   it and all eight neighbours hold no land; everything else is transparent. MapLibre
   cannot clip a raster to a polygon, so the tile has to carry its own edge. It stops just
   short of the shore because MapLibre blends a stretched tile between pixel centres. An
   antialiased edge on a z8 tile, stretched to z15, spread the sea colour hundreds of
   metres over the land. The strip left along the shore is filled by the vector sea mask
   beneath, in the shoreline colour, with the crisp OSM edge.
5. **Encode** as WebP with alpha.

Before shading, elevations are clamped to at most 0 m. A tile is completed from whatever
its sources hold, and next to an HR survey that is the regional grid's land. Unclamped,
that step at the coast would shade as a dark line.

## Each grid stops at its own depth

Every survey and regional grid carries the deepest zoom its cells earn — the zoom whose pixel is nearest a cell, plus one (`native_zoom` in `processing/raster_tiles.py`). Past it the grid still fills the pixels of a tile some finer grid is drawing, but on its own it no longer earns one. Until 2026-10-18 every HR grid was rendered to z15 whatever its cell, so a 58 m EMODnet survey was its own cells stretched fifteen-fold at z15 and the tree carried millions of detail tiles that said nothing the z12 tile did not. At mid latitudes a 4 m grid now earns z15, a 7 m grid z15, a 14 m grid z14, a 30 m grid z13 and a 58 m grid z12; the archive and the tree's detail zooms stop where the data does, and a client overzooms from there.

## HR grid signs

The HR grids disagree on the depth sign, whatever their metadata says. Each grid's sign
is measured pixel by pixel against the local EMODnet mosaic and cached in
`work/emodnet_hr/grid_signs.json`. Comparing medians over a grid's bounding box goes
wrong on coastal surveys: the box is mostly land, so positive depths look right.

## Performance

About 40 ms per tile on a typical coast, and up to about 0.5 s on the densest (fjords).
Rendering runs in spawned worker processes, in 8×8 metatiles that share one coastline
fetch. Tiles are staged in an MBTiles database next to the archive. A metatile is marked
done only in the same transaction that stores its tiles, so an interrupted run resumes
and never skips a failure.

## Requirements

GEBCO needs external overviews, or low zooms would read the whole grid:

```bash
gdaladdo -ro -r average GEBCO_2026.tif 2 4 8 16 32 64 128
```

## Running on a GPU node

The render is CPU-bound, and a GPU node has twice the Jupyter pod's CPUs. `deploy/edito/relief-build/`
runs the bathymetry and land relief steps as a Job on one, requesting no GPU, so it uses no
GPU quota:

```bash
./deploy/edito/relief-build/run.sh --dry-run   # validate server-side
./deploy/edito/relief-build/run.sh             # build the current commit
kubectl -n project-oceanstream logs -f job/basemap-build
```

The Job has its own 300 Gi volume (`basemap-build`). It syncs its inputs from the private
`s3://oceanstream-bucket/basemap-build/inputs/` prefix, fetches the Copernicus DEM directly
from AWS, and writes finished archives to `basemap-build/out/`. The volume persists, so a
rerun resumes rather than starting over.

Rebuilding one archive, rather than everything the volume is missing, is what `REBUILD` is
for — it clears those archives and their staging so the render does them again:

```bash
REBUILD="land_relief" ./deploy/edito/relief-build/run.sh
./deploy/edito/relief-build/status.sh          # where it has got to
```

Promotion to the public prefix is deliberately separate, so a build can be checked before
anyone sees it:

```bash
./deploy/edito/relief-build/promote.sh --dry-run land_relief
./deploy/edito/relief-build/promote.sh land_relief
```

The copy runs server side — a 3 GB archive is promoted in seconds and never travels through
the pod — and restates the content type and cache control, which S3 would otherwise drop.
