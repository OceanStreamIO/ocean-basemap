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

Cesium's `GeographicTilingScheme` is this grid. A custom `ImageryProvider`
whose `requestImage(x, y, level)` calls pmtiles.js `getZxy(level + 1, x, y)`
displays them directly, with no tile server in between.
