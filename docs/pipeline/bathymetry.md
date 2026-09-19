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
| `bathy_hr.pmtiles` | z12–15 | HR survey footprints only | HR grids, EMODnet, GEBCO |

Each tile is completed from the sources beneath it wherever the best source has gaps,
so there is nothing underneath to show through. `bathy_eu` and `bathy_hr` need the
opt-in `emodnet` and `hr-bathymetry` steps; without them only `bathy_global` is built.

## How a tile is rendered

1. **Read.** Each source is warped to the tile at 256 px plus a one-pixel buffer. Every
   source fills only the pixels the ones before it left empty.
2. **Colour.** Continuous interpolation on
   `color-ramps/bathymetry-emodnet.txt`, a ramp sampled from the EMODnet World Base Layer.
3. **Shade.** A hillshade whose vertical exaggeration grows with the pixel size, so relief
   still reads at low zoom. It is baked into the colour. Flat seabed keeps its exact ramp
   colour, which is what the legend shows.
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
from AWS, and writes finished archives to `basemap-build/out/`. Promoting them to the public
`basemap/` prefix is a separate step. The volume persists, so a rerun resumes rather than
starting over.
