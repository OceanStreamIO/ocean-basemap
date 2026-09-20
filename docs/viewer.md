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
Pole, no North Pole and no centre to the Arctic basin. [`/globe.html`](https://project-oceanstream-basemap.lab.dive.edito.eu/globe.html)
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
