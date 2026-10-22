# Data Sources & Attribution

This project builds on openly available geospatial datasets. We gratefully acknowledge
the work of the organizations and researchers who produce and maintain these data.

---

## GEBCO 2026 Grid

| | |
|---|---|
| **Provider** | General Bathymetric Chart of the Oceans (GEBCO), operated under the joint auspices of the International Hydrographic Organization (IHO) and the Intergovernmental Oceanographic Commission (IOC) of UNESCO |
| **Resolution** | 15 arc-second (~460 m at the equator) |
| **Format** | 8 × quarter-hemisphere GeoTIFF tiles (~933 MB each, ~7.5 GB total) |
| **License** | **Public domain** — The GEBCO Grid is placed in the public domain and may be used free of charge |
| **DOI** | [10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa](https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa) |
| **Download** | [CEDA Archive](https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/) |

**Citation:**

> GEBCO Bathymetric Compilation Group 2026 (2026). The GEBCO_2026 Grid — a continuous terrain
> model for oceans and land at 15 arc-second intervals.
> doi:[10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa](https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)

**Usage in this project:** Source elevation data for hillshade, depth polygons, contours, and
hypsometric land coloring. All raster and vector bathymetric products are derived from this grid.

GEBCO publishes a new grid every year, generally in July. Set `BASEMAP_GEBCO_RELEASE` to pin the
pipeline to a different release (e.g. `2025`) — it drives the download URLs, tile filenames and the
`gebco<year>/` data directory.

---

## EMODnet Bathymetry DTM

| | |
|---|---|
| **Provider** | EMODnet Bathymetry Consortium, supported by the EU's integrated maritime policy (DG MARE) |
| **Resolution** | 1/16 arc-minute (~115 m) |
| **Coverage** | European seas (36° W – 43° E, 15° N – 90° N) plus a Caribbean tile |
| **Format** | OGC WCS coverage `emodnet__mean`, served as GeoTIFF |
| **License** | Free to use, including commercially, with attribution |
| **Vertical datum** | Lowest Astronomical Tide (LAT) — *not* mean sea level |
| **Access** | [ows.emodnet-bathymetry.eu/wcs](https://ows.emodnet-bathymetry.eu/wcs) |

**Citation:**

> EMODnet Digital Bathymetry (DTM 2024). EMODnet Bathymetry Consortium.
> doi:[10.12770/cf51df64-56f9-4a99-b1aa-36b8d7b743a1](https://doi.org/10.12770/cf51df64-56f9-4a99-b1aa-36b8d7b743a1)

**Usage in this project:** Regional high-resolution depth zone polygons (`depth_zones_eu.pmtiles`),
drawn above the global GEBCO depth zones from zoom 6. Updated by EMODnet every two years. Areas
without survey coverage are filled from GEBCO by EMODnet itself, so the extra detail is confined to
surveyed shelf areas.

### High-Resolution DTM collection

The same consortium publishes ~1600 individual coastal survey and LiDAR grids at 1/32 to 1/1024
arc-minute (~58 m to ~2 m), indexed by the `emodnet:hr_bathymetry_area` WFS layer and used for
`depth_zones_hr.pmtiles`. Each grid retains the attribution of its originating hydrographic office
or survey provider, linked from the `metadata_url` of its index entry.

---

## GMRT — Global Multi-Resolution Topography

**Source:** Lamont-Doherty Earth Observatory, [gmrt.org](https://www.gmrt.org)
**Licence:** CC BY 4.0 — credit *Ryan et al. (2009), doi:10.1594/IEDA.100001*; not for navigation

The synthesis of ship multibeam swaths at about 100 m. Its masked grid (`topo-mask`) carries only swath-sourced cells, which is what lets it sit over GEBCO without bringing fill with it. Staged by region through the GridServer API — see [02e — GMRT Swath Bathymetry](pipeline/gmrt.md).

## NOAA NCEI CUDEM

**Source:** NOAA National Centers for Environmental Information, AWS open data `s3://noaa-nos-coastal-lidar-pds`
**Licence:** US public domain — *doi:10.25921/ds9v-ky35*

Ninth-arc-second (about 3 m) topobathy tiles that carry the research multibeam BlueTopo leaves out, among them MBARI's Monterey Canyon. See [02f — NCEI CUDEM](pipeline/cudem.md).

## Geoscience Australia AusBathyTopo

**Source:** Geoscience Australia, [files.ausseabed.gov.au](https://files.ausseabed.gov.au/survey/)
**Licence:** CC BY 4.0 — credit *© Commonwealth of Australia (Geoscience Australia)*; two regions carry an additional Australian Hydrographic Service notice

30 m regional depth models for northern Australia, the Torres Strait, the Great Barrier Reef and Bass Strait. See [02g — AusBathyTopo](pipeline/ausbathytopo.md).

## ESA WorldCover 2021

**Source:** ESA, [esa-worldcover.org](https://esa-worldcover.org); AWS open data `s3://esa-worldcover`
**Licence:** CC BY 4.0 — credit *© ESA WorldCover project 2022, produced by the WorldCover consortium*

A global 10 m land-cover map in eleven classes, read at 74 m to tint the land relief by what grows on each pixel — see [05f — Land Cover](pipeline/worldcover.md).

## HydroRIVERS

**Source:** HydroSHEDS, [hydrosheds.org/products/hydrorivers](https://www.hydrosheds.org/products/hydrorivers)
**Licence:** the HydroSHEDS version 1 licence agreement with WWF, **not CC BY** (HydroLAKES is CC BY 4.0; HydroRIVERS is not). It allows scientific, educational and commercial use, but data incorporated in a product may reach end users only under a licence "at least as protective" as WWF's, never as a stand-alone product, and WWF may end the licence at its discretion. See Appendix A of the [HydroSHEDS Technical Documentation v1.4](https://data.hydrosheds.org/file/technical-documentation/HydroSHEDS_TechDoc_v1_4.pdf). Cite *Lehner & Grill (2013), doi:10.1002/hyp.9740*.

The licence requires this statement with the documentation or metadata of any product that uses the data:

> This product OceanStream Basemap incorporates data from the HydroSHEDS version 1 database which is © World Wildlife Fund, Inc. (2006-2022) and has been used herein under license. WWF has not evaluated the data as altered and incorporated within OceanStream Basemap, and therefore gives no warranty regarding its accuracy, completeness, currency or suitability for any particular purpose. Portions of the HydroSHEDS v1 database incorporate data which are the intellectual property rights of © USGS (2006-2008), NASA (2000-2005), ESRI (1992-1998), CIAT (2004-2006), UNEP-WCMC (1993), WWF (2004), Commonwealth of Australia (2007), and Her Royal Majesty and the British Crown and are used under license. The HydroSHEDS v1 database and more information are available at https://www.hydrosheds.org.

The global river network derived from the 90 m HydroSHEDS drainage, 8.5 million reaches with Strahler order and mean discharge. It drew the rivers in the flat tree and the viewer until 2026-10 (tree v10 and earlier); the network traced from MERIT Hydro replaces it — see [05g — Rivers](pipeline/rivers.md).

## MERIT Hydro

**Source:** Global Hydrodynamics Lab, University of Tokyo, [global-hydrodynamics.github.io/MERIT_Hydro](https://global-hydrodynamics.github.io/MERIT_Hydro/), v1.0.1 (10 June 2019); download after registration.
**Licence:** dual, CC BY-NC 4.0 or ODbL 1.0; used here under **ODbL 1.0**, which permits commercial use: "Commercial use is permitted; however, derived data based on MERIT Hydro must be made publicly available under the same ODbL license." The terms "do not apply to produced works", so tiles drawn from it are licensed as the project decides. Cite *Yamazaki et al. (2019), doi:10.1029/2019WR024873*.

3 arc-second flow direction, drainage area and channel width over all land from 90°N to 60°S. The river network is traced from it ([05h — River network from MERIT Hydro](pipeline/merit-rivers.md)) to replace HydroRIVERS, and that network is published under ODbL 1.0.

## G-RUN ENSEMBLE

**Source:** Ghiggi, Humphrey, Gudmundsson & Seneviratne, ETH Zürich, [figshare, doi:10.6084/m9.figshare.12794075](https://doi.org/10.6084/m9.figshare.12794075.v1), version 1 (3 May 2021); the ensemble mean, `G-RUN_ENSEMBLE_MMM.nc`.
**Licence:** CC BY 4.0. Cite *Ghiggi et al. (2021), doi:10.1029/2020WR028787*.

Monthly runoff from 1901 at 0.5°, reconstructed from gauged small basins. Its 1990–2019 mean is routed down the MERIT network to estimate each river's long-term mean discharge ([05h — Discharge](pipeline/merit-rivers.md#discharge)).

## Natural Earth

| | |
|---|---|
| **Provider** | [Natural Earth](https://www.naturalearthdata.com/) — a collaborative project of the North American Cartographic Information Society (NACIS) |
| **Resolutions** | 1:10 m, 1:50 m, 1:110 m |
| **License** | **Public domain** — free for any use without restriction |
| **Download** | [naturalearthdata.com](https://www.naturalearthdata.com/downloads/) |

**Datasets used:**

| Dataset | Scale | Layer |
|---------|-------|-------|
| `ne_110m_land` | 1:110 m | Land polygons (z0–2) |
| `ne_50m_land` | 1:50 m | — |
| `ne_50m_coastline` | 1:50 m | Coastline (z0–2) |
| `ne_10m_land` | 1:10 m | Land polygons (z3–4) |
| `ne_10m_coastline` | 1:10 m | Coastline (z3–4) |
| `ne_10m_admin_0_countries` | 1:10 m | Country borders |
| `ne_10m_populated_places_simple` | 1:10 m | City labels |
| `ne_10m_geography_marine_polys` | 1:10 m | Ocean feature polygons |
| `ne_10m_geography_regions_points` | 1:10 m | Geographic region labels |

**Citation:**

> Made with Natural Earth. Free vector and raster map data @ [naturalearthdata.com](https://www.naturalearthdata.com/).

---

## SCUFN Gazetteer (IHO–IOC GEBCO)

| | |
|---|---|
| **Provider** | Sub-Committee on Undersea Feature Names (SCUFN), maintained by the International Hydrographic Organization (IHO) and served via NOAA National Centers for Environmental Information (NCEI) |
| **Format** | ArcGIS REST API → GeoJSON |
| **License** | **Free for research and non-commercial use** |
| **Endpoint** | [NOAA ArcGIS REST](https://gis.ngdc.noaa.gov/arcgis/rest/services/IHO/undersea_features/MapServer/0/) |

The gazetteer provides standardized names for undersea features (oceans, seas, ridges, trenches,
seamounts, etc.) with feature type classifications used for zoom-level assignment.

**Citation:**

> IHO–IOC GEBCO Gazetteer of Undersea Feature Names. International Hydrographic Organization /
> Intergovernmental Oceanographic Commission. Available from NOAA NCEI.

**Usage in this project:** Ocean feature labels with zoom mapping based on feature type (Ocean → z1,
Sea → z2, Ridge → z3, Seamount → z6, etc.).

---

## Global Seafloor Geomorphic Features Map (GSFM)

| | |
|---|---|
| **Provider** | Geoscience Australia / bluehabitats.org |
| **Format** | Shapefile archive (~389 MB) |
| **License** | **CC-BY 4.0** |
| **Download** | [bluehabitats.org](https://www.bluehabitats.org/) |

**Citation:**

> Harris, P.T., Macmillan-Lawler, M., Rupp, J., and Baker, E.K. (2014).
> "Geomorphology of the oceans." *Marine Geology*, 352: 4–24.
> [doi:10.1016/j.margeo.2014.01.011](https://doi.org/10.1016/j.margeo.2014.01.011)

**Usage in this project:** Seamount polygon features extracted from the GSFM shapefile archive.
Properties are cleaned and simplified to name + geometry for vector tile generation.

---

## OpenStreetMap Coastlines

| | |
|---|---|
| **Provider** | [OpenStreetMap](https://www.openstreetmap.org/) contributors |
| **Format** | Pre-processed coastline and land polygon shapefiles |
| **License** | **Open Data Commons Open Database License (ODbL)** |
| **Download** | [osmdata.openstreetmap.de](https://osmdata.openstreetmap.de/data/) |

**Datasets used:**

| Dataset | Description |
|---------|-------------|
| `land-polygons-split-4326` | Land polygons (~620 MB), used at z5+ |
| `coastlines-split-4326` | Coastline linestrings (~70 MB), used at z5+ |

**Citation:**

> © OpenStreetMap contributors. Data available under the Open Database License.
> [openstreetmap.org/copyright](https://www.openstreetmap.org/copyright)

**Usage in this project:** High-resolution land and coastline geometry for zoom levels 5 and above,
supplementing Natural Earth at lower zoom levels.

---

## Protomaps Font Glyphs

| | |
|---|---|
| **Provider** | [Protomaps](https://protomaps.com/) / Google Fonts |
| **Font families** | Noto Sans, Noto Serif |
| **License** | **SIL Open Font License (OFL)** |
| **Source** | [protomaps.github.io/basemaps-assets](https://protomaps.github.io/basemaps-assets/) |

**Citation:**

> Noto Sans and Noto Serif font families by Google. Distributed under the SIL Open Font License.
> Glyph PBFs served via Protomaps.

**Usage in this project:** Font glyphs for all text label layers (ocean features, cities, contour
labels, seamount names) in the MapLibre GL style.

---

## Summary

| Dataset | License | Commercial Use |
|---------|---------|----------------|
| GEBCO 2026 | Public domain | ✓ |
| EMODnet Bathymetry DTM | Free use with attribution | ✓ with attribution |
| GMRT | CC-BY 4.0 | ✓ with attribution |
| Natural Earth | Public domain | ✓ |
| SCUFN Gazetteer | Free for research | Check with IHO |
| GSFM Seamounts | CC-BY 4.0 | ✓ with attribution |
| OSM Coastlines | ODbL | ✓ with attribution |
| MERIT Hydro | ODbL 1.0 (of a CC BY-NC / ODbL dual licence) | ✓ with attribution; derived data under ODbL |
| G-RUN ENSEMBLE | CC-BY 4.0 | ✓ with attribution |
| Protomaps Fonts | OFL | ✓ |
