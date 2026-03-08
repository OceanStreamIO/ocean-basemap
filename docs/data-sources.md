# Data Sources & Attribution

This project builds on openly available geospatial datasets. We gratefully acknowledge
the work of the organizations and researchers who produce and maintain these data.

---

## GEBCO 2025 Grid

| | |
|---|---|
| **Provider** | General Bathymetric Chart of the Oceans (GEBCO), operated under the joint auspices of the International Hydrographic Organization (IHO) and the Intergovernmental Oceanographic Commission (IOC) of UNESCO |
| **Resolution** | 15 arc-second (~460 m at the equator) |
| **Format** | 8 × quarter-hemisphere GeoTIFF tiles |
| **License** | **Public domain** — The GEBCO Grid is placed in the public domain and may be used free of charge |
| **DOI** | [10.5285/37c52e96-24ea-67ce-e063-7086abc05f29](https://doi.org/10.5285/37c52e96-24ea-67ce-e063-7086abc05f29) |
| **Download** | [CEDA Archive](https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2025/) |

**Citation:**

> GEBCO Compilation Group (2025). GEBCO 2025 Grid.
> doi:[10.5285/37c52e96-24ea-67ce-e063-7086abc05f29](https://doi.org/10.5285/37c52e96-24ea-67ce-e063-7086abc05f29)

**Usage in this project:** Source elevation data for hillshade, depth polygons, contours, and
hypsometric land coloring. All raster and vector bathymetric products are derived from this grid.

---

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
| GEBCO 2025 | Public domain | ✓ |
| Natural Earth | Public domain | ✓ |
| SCUFN Gazetteer | Free for research | Check with IHO |
| GSFM Seamounts | CC-BY 4.0 | ✓ with attribution |
| OSM Coastlines | ODbL | ✓ with attribution |
| Protomaps Fonts | OFL | ✓ |
