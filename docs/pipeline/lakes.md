# 05c — Lakes

Lake outlines, a depth tone for every lake, and rendered beds for the lakes
whose bathymetry we hold.

## Command

```bash
oceanbase run lakes
```

## Output

| Archive | What it is |
|---------|------------|
| `lakes.pmtiles` | HydroLAKES outlines, 1.4M lakes, with name, area, mean depth and surface elevation |
| `lake_relief.pmtiles` | shaded beds of inland lakes, on the lake ramp |
| `lake_relief_coastal.pmtiles` | shaded beds of lakes at or below 5 m elevation, on the sea ramp |

Lakes are tiled in bands by area, so a zoomed-out view carries the few hundred
big enough to see rather than every pond: z2–4 above 1,000 km², z5–6 above 100,
z7–8 above 10, z9–10 above 1, z11–12 everything.

A lake at or below `COASTAL_LAKE_ELEV` (5 m) is drawn on the sea's ramp, fill
and bed alike, so a liman reads as the water it is continuous with. The same
threshold is used by the viewer; a test keeps the two in step.

## Norway: N50 on top of HydroLAKES

HydroLAKES stops at about 10 ha, and in Norway that omits most of the water. In one
z12 frame over Sør-Aurdal, Kartverket draw **202 water bodies to HydroLAKES' 45**;
across Innlandet county, N50 has 64,014 lakes of which only 1,768 clear HydroLAKES'
threshold.

So Kartverket's [N50 Kartdata](https://www.geonorge.no) (CC BY 4.0) is used for the
lakes HydroLAKES lacks, and only those. HydroLAKES is kept wherever it reaches, because
it carries a modelled mean depth and N50 carries none: swapping Mjøsa's outline for the
N50 one would trade a depth-coloured lake for a flat one. A lake counts as new when no
HydroLAKES polygon covers a third of it — on Innlandet, 62,067 added and 1,947
recognised as already present.

The added lakes have authoritative outlines and, for four in five, a surface elevation,
which is what the coastal split and the depth datum need. They have no depth, so they
take the shallowest tone; the viewer reads a missing `depth_avg` as zero.

**Their coastline is deliberately not used.** OSM's Norwegian coastline came from this
same N50 data, and the two agree to 99.97% overlap with vertex counts within 1% along
the outer coast north-west of Bergen — there is nothing there to gain.

## Where a lake's depth comes from

1. **Its own bathymetry**, if we have it — see below.
2. **GEBCO**, where it holds a surveyed bed. GEBCO stores a lake bed as height
   above sea level, so the depth is the lake's surface elevation (from
   HydroLAKES) minus the bed. 2,472 lakes qualify, including Caspian, Baikal,
   most of the Great Lakes, Geneva, Constance and Vänern's neighbours.
3. **HydroLAKES' modelled mean depth** otherwise: one flat tone for the lake.

A lake GEBCO has no soundings for sits at one flat value — its own surface — so
step 2 recognises it by how far the bed falls below the surface and by whether
it has any relief at all.

## Adding a lake's bathymetry

Ladoga, Onega, Vänern, Vättern, Mälaren, Victoria, Tanganyika, Malawi, Erie,
Great Slave, Great Bear, Winnipeg, Titicaca and Issyk-Kul have no bed in GEBCO.
To give one its own:

1. **Put the raster** in `<data>/gebco2026/work/lakes/sources/`. Any format and
   CRS GDAL can read. It may hold depths (positive down) or bed elevations.
2. **Add an entry** to `lake-sources.json` at the repository root:

   ```json
   {
     "lakes": [
       {
         "name": "Ladoga",
         "file": "ladoga.tif",
         "depths": true,
         "surface_m": -10,
         "credit": "…dataset and licence…"
       }
     ]
   }
   ```

   `name` matches HydroLAKES' `Lake_name` (or use `hylak_id`). `depths: true`
   converts depth below the surface into bed elevation, against `surface_m` —
   HydroLAKES' figure for that lake, which `oceanbase status` and the lake
   readout both show. Add `"sign": -1` for a grid stored the other way up, and
   `"crs"` for a file that declares none.

3. **Rebuild**:

   ```bash
   rm <data>/gebco2026/tiles/lake_relief*.pmtiles
   oceanbase run lakes
   ```

The raster is normalised once into bed elevation — what GEBCO stores — and put
ahead of GEBCO in the priority stack, so it is used where it has data and GEBCO
fills in behind it. Lakes named in the manifest are rendered whatever GEBCO
says about them.

Licences differ per dataset. `credit` is shown in the build log; add the
attribution to the viewer too if the licence asks for it.
