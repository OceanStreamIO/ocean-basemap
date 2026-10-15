# Choosing the coastal palette

The sea's colour is baked into the bathymetry archives when they are rendered,
not applied by the viewer. Comparing two palettes therefore means rendering two
sets of archives and loading one or the other — which is what this is for.

**H is adopted** (2026-10-15), replacing E. `bathymetry-water.txt` carries its stops. H and E are identical below 125 m and differ across the shelf: E ran from 174 at 125 m to 204 at the surface, H from 174 to 196, so the shelf keeps 17–20% saturation to the shore instead of fading toward white in the last 35 m. Judged on the Camargue, the Venice lagoon, Ria Formosa, the Danube delta, the Oslofjord and the IJsselmeer against EMODnet's tiles: E's near-white shallows were what made the lagoons and the shelf beside them read as one pale wash. The coastal-water tone now sits twenty luminance levels under this surface stop rather than on it; see [09 — Flat Tile Tree](pipeline/xyz.md).

Before that, **E was adopted** (2026-10-11), replacing D, with every channel capped at 245 so the brightest sea colour still had somewhere to go under the bathymetry's 1.12 shading ceiling — E's own surface stop reaches 246 and would clip.

D and E are identical below 50 m and differ only across the shelf: at 1 m D is
at luminance 227 and 12% saturation, E at 221 and 17%. On a wide shallow bank
that is the whole difference. The Great Bahama Bank is 1–10 m across its entire
extent, and under D it read as almost white, as did the Central American
shelves; E keeps them as water.

The chooser stays and the candidate archives remain published, so `?water=e`
now renders the same palette as no parameter at all, one level brighter at the
surface where the cap bites.

## Using it

The full viewer has a **Coastal palette** chooser in the layer panel, under
Seafloor. Picking one reloads the page with `?water=<letter>`, so a view is
shareable:

```
https://basemap.oceanstream.io/viewer/index.html?water=d
```

More than one candidate is kept deliberately. Which reads best depends on the
coast and the zoom — a tidal shelf, a fjord and a steep-to volcanic island do
not agree — so this is a choice a reader makes rather than one the map makes for
them.

The candidates are rendered to z11 (`bathy_global` and `bathy_eu`) and not at
survey resolution. While one is selected the viewer hides `bathy_hr`, because a
candidate shelf meeting live HR tiles inside a survey footprint puts a colour
step at its edge that looks like a palette fault and is not one. Choose **As
published** to get the survey levels back.

## The candidates

Measured at the Frisian shore, where the published ramp is at its palest:

| | colour | luminance | saturation |
|---|---|---|---|
| As published | (237,241,244) | 239 | 2.9 % |
| A | (224,228,231) | 226 | 3.0 % |
| B | (237,241,244) | 239 | 2.9 % |
| C | (230,234,237) | 232 | 3.0 % |
| D | (217,233,246) | 229 | 11.8 % |
| E | (205,227,247) | 222 | 17.0 % |
| F | (192,220,244) | 214 | 21.3 % |

A, B and C vary **luminance** and were the first attempt. They are kept because
they are cheap to keep and because B's steep shelf is liked, but the table shows
why they disappointed: the published ramp is 2.9 % saturated at the waterline,
which is grey, and scaling a grey only produces a darker grey. Land there is
about 240, so A and C also land close to it.

D, E and F vary **saturation** instead, meeting the published ramp again at
87 m so nothing below the shelf changes. F is deliberately the tone coastal
lakes are floored at — 17.6 % — so a lagoon stops standing out against the sea
it opens into. The IJsselmeer against the Wadden Sea is the case to look at.

Saturation is also the more useful axis for a red-green colour-blind reader: the
blue-yellow channel is unaffected, so a blue shore is legible where a
luminance-only difference is not.

## Rendering a candidate

A ramp lives in `color-ramps/bathymetry-water-<name>.txt`.
`BASEMAP_WATER_RAMP` selects it, and `BASEMAP_BATHY_ARCHIVES` keeps the render
to the archives that matter for judging colour:

```bash
BASEMAP_WATER_RAMP=d \
BASEMAP_BATHY_ARCHIVES="bathy_global bathy_eu" \
BUILD_STEPS=bathymetry \
REBUILD="bathy_global bathy_eu" \
  ./deploy/edito/relief-build/run.sh
```

`REBUILD` matters: without it the step skips an archive that already exists and
a palette change renders nothing. Publish the result beside the live archives:

```bash
aws --endpoint-url "$ENDPOINT" s3 cp \
  "s3://$BUCKET/basemap-build/out/tiles/bathy_eu.pmtiles" \
  "s3://$BUCKET/basemap/tiles/water-d/bathy_eu.pmtiles" \
  --content-type application/vnd.pmtiles \
  --cache-control "public, max-age=604800" --metadata-directive REPLACE
```

Then add the letter to `WATER_CHOICES` in `viewer/app.js`.

## Adopting one

A candidate becomes the default by rendering **all three** sea archives with its
ramp — `bathy_hr` included, or every survey footprint keeps the old palette and
shows a step at its boundary — promoting them, and running
`publish-versions.sh` so the viewer's manifest points at the new bytes.

That last step is easy to forget and silent when missed: the archives are served
under digest-named copies in `tiles/v/`, and promoting the plain name alone
leaves the viewer loading the previous render.

The flat `{z}/{x}/{y}` tree bakes the same palette into its pixels, so adopting
a candidate also means re-rendering it — about three hours on the cluster and
most of a day to publish. Decide the palette before starting that render.

`COASTAL_WATER_RGB` — the flat tone for lagoons, limans and the observed-water
raster — is derived from the ramp's 0 m stop, so it moved with E to
(204, 226, 245). Tests assert three things about it: that it equals the ramp's
surface, that the viewer's `COASTAL_WATER` and `SHORE_BY_PALETTE` agree with the
renderer, and that it stands at least ten luminance levels clear of the lowland
land colour. The third of those changed when E was adopted. It had been a
luminance gap of ten levels, which D met at 14 and E misses at 7 — while
separating better on the blue-yellow axis, 54 against D's 42. For a red-green
colour-blind reader blue-yellow is the axis that survives, so that is what the
test now measures, with a weak luminance floor beneath it.
