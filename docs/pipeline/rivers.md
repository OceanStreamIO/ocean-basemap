# 05g — Rivers (HydroRIVERS)

Stages HydroRIVERS and builds `rivers.pmtiles` from it. The flat tree draws its rivers from the staged file, the viewer from the archive, and both choose their reaches with the one rule in `SELECTION`, so the two maps show the same rivers.

## Command

```bash
oceanbase run rivers
```

## Process

1. **Download** `HydroRIVERS_v10_shp.zip` (544 MB), the global network: 8.5 million reaches, each with a Strahler order and a modelled mean discharge.
2. **Stage** the reaches the deepest zoom draws — order 4 or more, or 20 m³/s or more — as `work/land/hydrorivers.fgb` with two attributes, `order` and `dis`, and a spatial index. The tree's `Water` class queries it by bbox per block of tiles.
3. **Archive.** One tippecanoe band per zoom with its own rule, joined into `rivers.pmtiles` (z4–10; the viewer overzooms beyond).

## Which reaches, at which zoom

A reach is kept when its order **or** its discharge clears the bar. Order alone drops the lower Nile below the Aswan dam, which has few tributaries; discharge alone keeps every braid of a mountain torrent.

| Up to | Order ≥ | or discharge ≥ |
|---|---|---|
| z5 | 7 | 1,000 m³/s |
| z6 | 6 | 400 |
| z7 | 6 | 200 |
| z8 | 5 | 100 |
| z9 | 5 | 50 |
| z10 and beyond | 4 | 20 |

The pen follows the reach too: two pixels for order 7 or 300 m³/s, 1.4 for order 5 or 100, a hairline otherwise.

## Why not Natural Earth

The rivers were Natural Earth's centrelines, a few hundred of the world's largest, chosen for a wall map. At z10 that drew the Rhône through the Camargue and nothing else: no Vidourle, no Petit Rhône, none of the drainage that gives a delta its shape on EMODnet's map. `land_base.pmtiles` still carries the Natural Earth layer; nothing draws it.

## Source

| | |
|---|---|
| **Product** | HydroRIVERS v1.0, HydroSHEDS (Lehner & Grill 2013, doi:10.1002/hyp.9740) |
| **Reaches** | 8.5 million, derived from the 90 m HydroSHEDS drainage |
| **Licence** | CC BY 4.0 |
| **Download** | [hydrosheds.org/products/hydrorivers](https://www.hydrosheds.org/products/hydrorivers) |
