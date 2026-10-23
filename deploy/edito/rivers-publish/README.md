# OceanStream river network, traced from MERIT Hydro — 2026-10-22

A global river network: every channel draining at least 10 km², from 90°N to 60°S, cut into 7,261,047 stretches between confluences. Each stretch carries its Strahler order and an estimated long-term mean discharge. It is the network the rivers of the OceanStream Basemap are drawn from.

- File: `merit_rivers.fgb`, FlatGeobuf with a spatial index, EPSG:4326, 2.8 GB. GDAL, QGIS and most GIS tools read it in place over HTTP, a bounding box at a time.
- Licence: [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

## Fields

| Field | Meaning |
|---|---|
| `order` | Strahler order, counted from 1 at every channel head of 10 km² |
| `dis` | estimated long-term mean discharge at the stretch's downstream end, m³/s |
| `upa` | upstream drainage area at the stretch's downstream end, km² |
| `width` | mean of MERIT Hydro's channel widths along the stretch, where it has one, m |
| `wet` | share of the stretch's cells that have a MERIT Hydro channel width, 0–1 |

Each stretch ends on the first cell of the stretch it flows into, so tributaries meet their river. Lines are smoothed once (ends fixed) and simplified to half a 3″ cell.

## How it was made

1. Every MERIT Hydro v1.0.1 cell (3″, about 90 m) draining at least 10 km² is kept, with the cell its D8 flow direction points to.
2. The cells are joined into one network. Strahler order comes from a topological pass over it.
3. In the same pass, each cell adds the runoff of the land draining straight into it: its own drainage area minus that of the river cells flowing into it, times the local mean annual runoff. Everything arriving from upstream is passed on. The runoff is the G-RUN ENSEMBLE mean for 1990–2019, at 0.5°, with gaps along coasts filled from the nearest land within ten cells.
4. The network is cut into stretches between confluences.

## Limits of the discharge

The discharge is a climatological estimate and only grows downstream. It has no losses: evaporation from rivers, lakes and wetlands, irrigation and reservoirs are not in it. Where they matter it runs high: at Cairo it gives about 12,300 m³/s, where HydroRIVERS gives about 2,000. Compared with HydroRIVERS, whose discharge is calibrated to gauges, it is within 5% on the Rhine, the Danube, the Mississippi and the Drammenselva. It is 15% high on the Glomma and 32% high on the Congo. It is 19–43% low on the Amazon, the Ganges and the Yangtze.

It is good for ranking rivers. It is not a substitute for a hydrological model, and not for decisions that safety depends on.

## Credit

Contains information from MERIT Hydro, which is made available here under the Open Database License (ODbL).

> Yamazaki, D., Ikeshima, D., Sosa, J., Bates, P. D., Allen, G. H., Pavelsky, T. M. (2019). MERIT Hydro: a high-resolution global hydrography map based on latest topography datasets. Water Resources Research 55, 5053–5073. https://doi.org/10.1029/2019WR024873

The discharge is estimated from G-RUN ENSEMBLE, under CC BY 4.0:

> Ghiggi, G., Humphrey, V., Seneviratne, S. I., Gudmundsson, L. (2021). G-RUN ENSEMBLE: A multi-forcing observation-based global runoff reanalysis. Water Resources Research 57, e2020WR028787. https://doi.org/10.1029/2020WR028787. Data: https://doi.org/10.6084/m9.figshare.12794075.v1

If you publish a database made from this one, it has to be under the ODbL too. Maps and other works you produce from it can be under any terms, with the notice above. See the licence for the exact terms.

Made by Pine View Software AS for [OceanStream](https://oceanstream.io).
