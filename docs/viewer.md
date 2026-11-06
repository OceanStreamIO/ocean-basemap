# Using the viewer

The viewer at
[project-oceanstream-basemap.lab.dive.edito.eu](https://project-oceanstream-basemap.lab.dive.edito.eu)
is a static page: no account, no server-side state, and everything it draws
comes from PMTiles archives read over HTTPS range requests. That means it can
be pointed at other data, and linked to at an exact view, purely through the
URL.

## Linking to a view

The map position lives in the hash, in the usual MapLibre form:

```
https://project-oceanstream-basemap.lab.dive.edito.eu/#12/38.44/-9.21
```

That is `#zoom/latitude/longitude`. The hash updates as you pan, so any view
can be copied out of the address bar and shared.

## Adding your own layers

Add `?overlay=` with the URL of your data to draw it on top of the basemap.
Repeat the parameter for several layers:

```
?overlay=https://example.org/my-survey.pmtiles
?overlay=https://example.org/track.geojson&overlay=https://example.org/stations.geojson
```

| Format | What happens |
|---|---|
| Vector PMTiles | one toggle per layer in the archive, drawn as fill, line and circle |
| Raster PMTiles | drawn as it is, above the basemap |
| GeoJSON | split by geometry type, so points, lines and polygons all show |

Each overlay gets its own entry at the top of the layer panel, with a
visibility toggle and an opacity slider, and is drawn in a single colour that
stands apart from everything in the basemap.

The file has to be reachable by the browser, which means HTTPS and
**CORS enabled on the host** — for a PMTiles archive, range requests too
(`Access-Control-Allow-Headers: Range` and
`Access-Control-Expose-Headers: Content-Range`). A file served without those
headers fails silently in the browser's network layer; the overlay simply does
not appear. Nothing is uploaded to us: the URL is read by your browser, and
anyone you send the link to needs access to that URL as well.

This is also the route for lake bathymetry we do not hold. If you would rather
have a lake built into the basemap itself, see
[the lakes step](pipeline/lakes.md) — a survey added there is rendered on the
same ramp as everything else, instead of sitting on top in overlay colours.

## Three ways in, none of them needing an account

| | |
|---|---|
| `…/basemap/style.json` | the style, for your own MapLibre map |
| `…/basemap/viewer/index.html` | the whole viewer, served straight from object storage |
| <https://project-oceanstream-basemap.lab.dive.edito.eu> | the same viewer, running on EDITO |

The first two depend on nothing but the bucket. The third depends on this project's
namespace, which EDITO may remove during maintenance — so link the bucket copy if the
link has to keep working.

## Using the basemap in your own map

There is a style URL. Point MapLibre at it and you get the whole basemap — the same
layers the viewer draws, in the same order, with the attribution already in place:

```
https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/style.json
```

```html
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/pmtiles@3.2.0/dist/pmtiles.js"></script>
<script>
  // The archives are PMTiles, so register the protocol before the map reads them.
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', async (params, ac) => {
    const r = await protocol.tile(params, ac);
    // Our raster archives are sparse: a missing tile is not an error.
    if (params.type !== 'json' && !r?.data) return { data: new Uint8Array() };
    return r;
  });

  const map = new maplibregl.Map({
    container: 'map',
    style: 'https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/style.json',
    center: [-9.2, 38.7], zoom: 8,
  });
</script>
```

Two URLs, for two different needs:

| URL | changes | cached |
|---|---|---|
| `basemap/style.json` | follows the viewer | 5 minutes |
| `basemap/style/v1.json` | layer ids pinned; a breaking change becomes `v2.json`; archives follow the viewer | 1 day |

Pin `v1.json` for anything you will not be watching. The layer ids and source-layer names
are the contract: `bathy-global`, `land-relief`, `sea-mask`, `place-names-no` and the
rest, as listed in the layer panel.

Layers are removed from time to time when they earn their keep no longer. `style.json`
follows; `v1.json` is what protects anyone who cannot absorb that.

The style is exported from the running viewer (`scripts/export-style.py`) rather than
maintained beside it, so the two cannot drift: what it contains is literally what the map
is holding after it loads, minus the viewer's own measuring and graticule layers.

### Attribution

Every source in the style carries its own `attribution`, so a map that loads `style.json`
renders the credits without being told to. They are not decoration on our viewer: several
of the datasets are **CC BY 4.0**, where naming the source is a condition of use.

| Dataset | Used for | Terms |
|---|---|---|
| GEBCO | global bathymetry, contours, lake beds | free to use, credit requested |
| EMODnet Bathymetry | European and HR bathymetry | free to use, credit requested |
| Copernicus DEM (© DLR e.V., © Airbus DS) | land relief | free to use with the notice |
| OpenStreetMap contributors | coastline, land, sea mask | ODbL |
| Natural Earth | low-zoom land, boundaries, ocean labels | public domain |
| HydroLAKES (HydroSHEDS) | lakes | CC BY 4.0 |
| Kartverket | Norwegian lakes, place names, kommuner | CC BY 4.0 |
| GeoNames | world place names | CC BY 4.0 |
| geoBoundaries | second-level administrative units | CC BY 4.0 |
| GSFM (Harris et al. 2014) | seamounts | published dataset, cite the authors |
| GEBCO Gazetteer (IHO-IOC SCUFN) | undersea feature names | free to use, credit requested |

Two archives merge sources and have to credit both halves: `places` is Kartverket for
Norway and GeoNames for everywhere else, and `boundaries` is geoBoundaries plus
Kartverket's kommuner. Each was once credited only to whichever came first in the file,
which names one CC BY dataset and silently omits another; a test now rejects that, and
another rejects any source with no attribution at all.

The optional overlays are third-party services on their own terms — Esri imagery and
labels, NASA GIBS, EOX Sentinel-2 cloudless, Kartverket's own tiles. They are off by
default and carry their own attribution when switched on.

## Pointing the viewer at other tiles

`?pmtiles=` replaces the archive base URL, so a full local or staged build can
be checked in the deployed viewer:

```
?pmtiles=https://my-bucket.example.org/basemap/tiles
```

Our own archives are public and can be used directly, in this viewer or in any
MapLibre application:

```
https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/tiles/<name>.pmtiles
```

## The globe

The map above is Web Mercator, which stops at 85.05° and therefore has no South
Pole, no North Pole and no centre to the Arctic basin. [`/static/globe.html`](https://project-oceanstream-basemap.lab.dive.edito.eu/static/globe.html)
is the same bathymetry and relief on a CesiumJS globe, drawn from the geographic
archives, where Antarctica and the Arctic are whole. It is linked from the info
bar at the bottom of the map.

The globe shows the sea floor and the land relief only — the vector layers
(coastline, labels, contours, boundaries) are not on it, because Cesium does not
render vector tiles natively. `?pmtiles=` works there too.

To put these archives in your own Cesium app, use
`src/basemap/viewer/cesium-provider.js`; see
[the globe grid](pipeline/globe.md) for what makes those archives different.

## Labels

Two label sets come from Esri, as separate options in the panel:

- **ESRI Labels + Undersea Names** — place names together with undersea
  features: basins, ridges, banks and soundings. Its country and province
  boundaries are baked into the same pixels as the text, so they cannot be
  restyled or switched off on their own; they are drawn on top of our own
  vector boundaries.
- **ESRI Labels, No Borders** — place names only, and more legible, but without
  the undersea names. With this one the only boundaries on the map are ours,
  which have separate country and internal-border toggles and opacity.

**Place Labels (ours)** is a third set, drawn from Natural Earth and our own
ocean labels. It is off by default and needs no external service.
