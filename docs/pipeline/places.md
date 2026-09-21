# 05e — Places

Built-up areas and place names: where people are, drawn on top of the relief.

## Command

```bash
oceanbase run places
```

## Output

| Archive | Layer | What it is | Zooms |
|---------|-------|------------|-------|
| `urban.pmtiles` | `urban` | Natural Earth urban areas, worldwide | z4–9 |
| | `urban_detail` | N50 built-up areas, Norway | z10–14 |
| `places.pmtiles` | `world` | GeoNames populated places, worldwide | z3–12 |
| | `places` | N50 place names, Norway | z6–14 |

## Why these are vectors, always

Labels cannot be baked into the rendered tiles. They have to stay upright as the map
turns, grow and shrink with the zoom, step aside from one another, and be capable of
being switched off, restyled or searched. Pixels can do none of that — which is exactly
why Esri's raster labels cannot have their boundary lines softened.

## Thinning names the way a paper map does

Natural Earth's populated places, which the land tiles already carry, name 7,342
settlements worldwide — **two of them around the whole Oslofjord**. It is a 1:10M
product and was never meant to name a village.

Kartverket's N50 has **113,781 settlement names for Norway alone**, and ranks them the
way its own maps do: by the size of type each is drawn in. That ranking decides the zoom
a name appears at, so the map thins out the way a printed map does rather than by a rule
invented for it.

| Type size | What it is | From |
|---|---|---|
| ≥ 12.0 | cities (`by`) — Oslo, Trondheim, Tromsø | z6 |
| ≥ 10.0 | large villages | z8 |
| ≥ 8.5 | towns and villages | z10 |
| ≥ 7.5 | hamlets, housing areas | z11 |
| below | farms, summer farms, single holdings | z12 |

The label text also scales with the rank, so the hierarchy survives being zoomed through.

## The world outside Norway

GeoNames' `cities500` (CC BY 4.0) carries every populated place above 500 people, with
its population — which plays the part Kartverket's type size plays in Norway:

| Population | appears from | places |
|---|---|---|
| ≥ 1,000,000, and every national capital | z3 | 717 |
| ≥ 200,000 | z5 | 2,465 |
| ≥ 50,000 | z7 | 9,249 |
| ≥ 10,000 | z9 | 32,726 |
| ≥ 1,000 | z11 | 102,740 |
| the rest | z12 | 87,325 |

Norway is excluded from it: N50 names the country far better, down to the farms, and two
sources over the same ground print every town twice. Label size follows the *logarithm*
of the population, since it spans six orders of magnitude and a linear reading draws
everything below a million at village size.

## Tippecanoe thins points unless told not to

Its drop rate removes a share of the points at each zoom below the maximum — the right
default when every point is equal. These are not: each band already carries the zoom its
places earned. Leaving the default on left **eleven place names across the whole of
Europe** at z5 while the bands held thousands, and nothing reported a problem. `-r1`
turns it off.

## Two traps in the source

The annotation carries the whole name in `fulltekst`; `TextString` is only the line it is
broken across, so reading that gives hyphenated names in pieces — `Lambeto-` and `nuten`
as separate labels. Its geometry is the box the text occupies rather than the place, so
the point used here is that box's centre, which is where Kartverket puts the label. Names
repeat across map sheets, so `stedsnummer` keeps each place once.

And the geodatabase is UTM 33N. A derived point left in it is metres from a false origin,
which tippecanoe drops as an impossible longitude **without saying anything** — the only
symptom is an archive with no tiles in it. A test now asserts the written points are
Norwegian longitudes and latitudes.
