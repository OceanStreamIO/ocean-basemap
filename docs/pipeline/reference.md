# Reference Layer (names) — parked

The names, on their own transparent tile tree. **Not part of a full run.**

Built and tested, but not shipped. The typography was not judged good enough
beside Esri's, and the decision for now is to stack Esri's
`World_Ocean_Reference` over our tiles instead. `oceanbase run reference`
still builds and publishes it; `oceanbase run all` does not.

What ours can do that Esri's cannot: Esri's reference is a **single-layer
raster** service, so its country borders, country names and city labels come
with the names and cannot be switched off. Ours carries only what we put in
it. That is the reason to finish it in a later release.

## Command

```bash
oceanbase run reference
```

`BASEMAP_LABELS_STAGE_ONLY=1` stops after the render, before publishing.

## Output

```
basemap/labels/{z}/{x}/{y}.png      z0–12, PNG32, transparent
```

About 40,000 tiles. A tile exists only where a name falls; everywhere else
returns 404, which every client treats as empty.

## Why a second tree

This is how Esri publish their ocean map, and the split is the point.
`World_Ocean_Base` is JPEG — bathymetry, land cover, no text anywhere.
`World_Ocean_Reference` is PNG32 and carries the marine water body names and
the undersea feature names. A client stacks the second over the first. Nothing
is ever baked into the base.

| Ours | |
|---|---|
| `basemap/xyz/{z}/{x}/{y}.webp` | the map, opaque |
| `basemap/labels/{z}/{x}/{y}.png` | the names, transparent |

Three things follow, and they are the reasons for it.

**Restyling is cheap.** The base tree is twenty million tiles and most of a
day; this one is forty thousand and a few minutes. Changing a typeface, a
size, or a rule about what appears when costs a re-render of this tree alone.

**The names are optional at the point of use.** Nobody has to accept our
labels to use our bathymetry, and nobody has to take our bathymetry to use the
names — which matters for anyone who needs them in another language.

**The text can be right at every zoom.** Baked into the base, a name drawn at
z6 is stretched to nonsense by z9. Here each level is drawn at its own scale,
with its own decision about what is worth showing.

## Sources

| | Features | What |
|---|---|---|
| GEBCO gazetteer | 5,412 | undersea feature names — the register Esri's own undersea names come from |
| Natural Earth | 530 | seas, bays, straits, capes, as the `ocean-labels` step already merges them |

The gazetteer is the IHO-IOC Sub-Committee on Undersea Feature Names, served
as a feature service: 2,632 points, 1,128 lines, 1,652 polygons, with a `NAME`
and a `TYPE`. It is cached to `work/labels/gebco_undersea.geojson` — about
1.9 MB — and changes a few times a year.

## When a name appears

Type alone is not enough: the Mid-Atlantic Ridge and a ridge twenty kilometres
long are both `RIDGE`, and only one of them belongs on a world view. So where
a feature has a geometry, its **size** decides — the first zoom at which it
covers 48 px — and type shifts that by a level, so a knoll waits a little
longer than a basin of the same width. Points have no size to read and fall
back on a table of types.

## Placement is global, then cut

Labels are laid out once per zoom in world pixel coordinates, and only then
cut into tiles. A name straddling a tile boundary is therefore drawn
identically in both tiles, and two names either side of a boundary still
collide with each other. Placing per tile is the classic way to get labels
that repeat, jump, or overprint at the seams.

Collisions are resolved greedily, by tier and then by the zoom the feature
earned: where two names cannot both fit, the one that has been on the map
since further out keeps its place. The order does not depend on how the
sources happened to arrive, so the same input gives the same tiles every
time — which matters when only changed tiles are re-uploaded.

## Typography

| Tier | Size | Weight | Set |
|---|---|---|---|
| Water bodies | 17 px | regular | capitals, tracked |
| Major undersea | 16 px | **bold** | as written |
| Minor undersea | 14 px | regular | as written |

Tier is carried by size and weight, never by colour alone, and the text is
larger than a web map's usual 11–12 px with a near-opaque white halo. Lato
ships in `assets/fonts/`, so the render does not depend on what fonts the
build image happens to have.

## Antimeridian

Longitude is averaged as an angle, not as a number. A feature crossing the
dateline arrives split into parts sitting at +180 and −180, whose arithmetic
mean is zero: the Emperor Trough, which is in the north-west Pacific, came out
over central Spain. Averaging unit vectors puts it back where it belongs, and
the same unwrapping keeps a two-degree feature from measuring 358 degrees
wide.
