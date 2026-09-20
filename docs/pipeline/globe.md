# 08 — Geographic Grid for Globes

The same relief on a second tile grid, so a 3D globe can show the poles.

## Command

```bash
oceanbase run globe
```

## Why a second grid

Every other archive here is Web Mercator, because that is the only projection
MapLibre draws. Mercator cannot represent a pole — it stops at 85.05° — and the
cost is not cosmetic:

| Missing from any Mercator tileset | Area |
|---|---|
| South of 85.05°S: the South Pole and the interior of Antarctica | 0.95 M km², **6.7% of the continent** |
| North of 85.05°N: the North Pole and the central Arctic basin | 0.95 M km², **6.1% of the Arctic Ocean** |

No styling recovers that. The tiles do not exist.

A globe has no such limit, and CesiumJS tiles natively in the geographic grid:
two square tiles at z0, four at z1, reaching 90° at every zoom.

## Output

| Archive | Zooms | Coverage | Sources |
|---------|-------|----------|---------|
| `bathy_geo.pmtiles` | z0–6 | ocean, poles included | GEBCO |
| `land_geo.pmtiles` | z0–6 | land, poles included | GEBCO relief with the ice ramp |

These are deliberately shallow. A globe is viewed from far enough out that z6 —
about 2 km per pixel at the equator — covers it, and the Mercator archives stay
the better source wherever a viewer zooms in away from the poles.

## The stored zoom is one higher than the rendered zoom

PMTiles orders tiles along a Hilbert curve over a **square** grid, and rejects
any `x` or `y` at or past 2^z. A geographic pyramid is twice as wide as it is
tall, so it cannot be written at its own zoom.

It fits exactly one zoom down. At storage zoom z+1 the square grid is 2^(z+1)
wide — precisely the geographic grid's width at z — and its 2^z rows fill the
top half:

```
geographic (z, x, y)  ⟷  archive (z + 1, x, y)
```

So `bathy_geo.pmtiles` reports z1–7 for content rendered at z0–6, and a reader
subtracts one. The archive says so itself: the metadata carries `crs`
(`EPSG:4326`), `scheme` (`geographic`) and `zoom_offset` (`1`). PMTiles has no
field for a projection — the format assumes Mercator, as does every off-the-shelf
reader — so a client has to know to look. **Do not load these archives in
MapLibre**: it will place them as though they were Mercator.

## Reading them from CesiumJS

Cesium's `GeographicTilingScheme` is this grid. `src/basemap/viewer/cesium-provider.js`
is an `ImageryProvider` that reads the archives over byte ranges, with no tile server in
between, and `globe.html` is a working page built on it.

The provider reads both grids, because a globe wants both. These archives are the only
ones that reach the poles, but they stop at z6 and look soft close up; the Mercator
archives carry the detail — to z15 in European waters — everywhere between 85° north and
south. Stacked, with the geographic pair underneath, each covers what the other cannot,
and no extra tiles have to be rendered for it. Each archive's metadata declares the zooms
and the region it covers, so Cesium requests nothing outside them.

Two traps, both found in a browser rather than in a test:

- **Cesium uploads tile bitmaps without flipping them**, because its own loader builds
  them with `imageOrientation: "flipY"`. An `ImageBitmap` made the ordinary way arrives
  upside down — which on a globe does not look like a flipped image, it looks like the
  wrong data in the right place. The provider hands Cesium an `HTMLImageElement`.
- **A missing tile must come back transparent, not `undefined`.** `undefined` means "not
  available at this level", and Cesium fills the gap by upsampling the nearest ancestor
  that does have one.
