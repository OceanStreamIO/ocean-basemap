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

## Three different things get called a city's shape

| | what it is | where it is |
|---|---|---|
| **Cities** | the urban centre itself, delineated from built-up density and population — Oslo as one shape of 242 km² | `urban.pmtiles`, layer `cities` |
| City limits | the administrative unit the city is governed within — a Norwegian kommune reaches far past the town | `boundaries.pmtiles`, layer `municipalities` |
| Built-up areas | land cover: where the houses happen to reach | `urban.pmtiles`, layers `urban`, `urban_detail` |

The first is what a reader means by "the city": 12,262 named urban centres worldwide from
the [GHS Urban Centre Database](https://ghsl.jrc.ec.europa.eu) (JRC, CC BY 4.0).

Being polygons is also what keeps the name on the map close in. A point label has one
anchor, and once the zoom passes the point where that anchor leaves the screen, the
city's name goes with it — Oslo was in the data at z14 and nowhere on the map. MapLibre
gives a polygon a label in **every tile it covers**, so the name stays while you are
inside the city. A Norwegian name is dropped where a city polygon of the same name
already covers it, or Oslo is printed twice: once across the city, once where Kartverket
puts the word, which for Oslo is out in the fjord.

## Built-up areas are not city limits

Land cover shows where the houses reach — a blotch that starts and stops with the
suburbs and belongs to nobody. A city's limits are administrative, and they live with
the other boundaries in [`boundaries.pmtiles`](../pipeline/index.md): 48,925 second-level
units worldwide from geoBoundaries (CC BY 4.0), and Norway's 365 `Kommune` from N50,
because geoBoundaries' Norwegian units come from Kartverket but from 2013, before the
2020 mergers and the 2024 splits.

What "second level" means varies by country: municipalities in Norway, *powiaty* in
Poland, and in a few countries where no second level is published, the first. The
built-up areas remain in the archive but are off by default.

Kept to z10 and overzoomed beyond. At full detail to z12, 49,000 administrative polygons
made a **1 GB** archive against the 36 MB the country and region lines take, and the z11
and z12 tiles were nearly all of it — for a dashed line whose shape is already settled.

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

## Bands are cumulative and must not overlap

Each band holds every place at or above its threshold, not only its own class, and the
zoom ranges do not overlap — the same shape the lakes step uses. Written the other way,
with each band holding one class and all of them running to the deepest zoom, the bands
collide on every tile they share and one of them wins.

The half of this that demonstrably bit: the world bands stopped at z12 while the archive
ran to z14, so **every place name outside Norway disappeared above z12**. A layer that
ends before its archive does is absent from the deeper tiles rather than overzoomed.

Density past that is the source's, not the pipeline's. GeoNames holds four places around
Faro at z12 and forty-three around Weyerbusch, because one is a Portuguese coastal
lagoon and the other is the German countryside.

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
