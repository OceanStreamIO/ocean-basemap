"""Central configuration — paths, URLs, constants.

The data directory is resolved from (in order):
  1. BASEMAP_DATA_DIR environment variable
  2. Default: /Volumes/RP60/oceanstream/basemap
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = Path("/media/andrei/RP60/oceanstream/basemap")


def get_data_dir(override: str | Path | None = None) -> Path:
    """Return the base data directory, from *override* > env > default."""
    if override:
        return Path(override)
    return Path(os.environ.get("BASEMAP_DATA_DIR", str(DEFAULT_DATA_DIR)))


def get_work_dir(data_dir: Path) -> Path:
    return data_dir / "gebco2025" / "work"


def get_tile_dir(data_dir: Path) -> Path:
    return data_dir / "gebco2025" / "tiles"


# ---------------------------------------------------------------------------
# GEBCO download
# ---------------------------------------------------------------------------

CEDA_BASE = "https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2025/ice_surface_elevation/geotiff"

GEBCO_TILES: list[tuple[str, str]] = [
    # (filename, md5)
    ("gebco_2025_n90.0_s0.0_w-180.0_e-90.0.tif", "5339d30e2a4f4ae766a436d33ab185af"),
    ("gebco_2025_n90.0_s0.0_w-90.0_e0.0.tif", "add2bd85c08106e47d67a1802ae8355a"),
    ("gebco_2025_n90.0_s0.0_w0.0_e90.0.tif", "e93523f35c40d65e14e15724960cbe2d"),
    ("gebco_2025_n90.0_s0.0_w90.0_e180.0.tif", "5ef662c016a17d6eaf03841f840d629c"),
    ("gebco_2025_n0.0_s-90.0_w-180.0_e-90.0.tif", "4fc4ce0052dc91463a9ab43521cbc7d8"),
    ("gebco_2025_n0.0_s-90.0_w-90.0_e0.0.tif", "39b88b46c36e999021d6bf38e21a794c"),
    ("gebco_2025_n0.0_s-90.0_w0.0_e90.0.tif", "cc7820f063dc547c8711c367b9dbb06c"),
    ("gebco_2025_n0.0_s-90.0_w90.0_e180.0.tif", "da7235029d598c53c509e56e7472239d"),
]

# ---------------------------------------------------------------------------
# Depth classification
# ---------------------------------------------------------------------------

DEPTH_BREAKS: list[int] = [
    -11000, -8000, -6000, -5000, -4000, -3000, -2000,
    -1500, -1000, -500, -200, -100, -50, -10, 0,
]

DEPTH_COLORS: dict[int, str] = {
    -11000: "#080c2e",
    -8000: "#0a1944",
    -6000: "#0e285a",
    -5000: "#12346c",
    -4000: "#19427e",
    -3000: "#245494",
    -2000: "#346cac",
    -1500: "#4480bc",
    -1000: "#5898cc",
    -500: "#70b0da",
    -200: "#8cc4e6",
    -100: "#a8d6f0",
    -50: "#c0e4f8",
    -10: "#d2eefb",
}

# ---------------------------------------------------------------------------
# External data URLs
# ---------------------------------------------------------------------------

NE_GEOJSON_BASE = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
)

NE_SHAPEFILES: dict[str, str] = {
    "ne_10m_land": "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip",
    "ne_10m_coastline": "https://naciscdn.org/naturalearth/10m/physical/ne_10m_coastline.zip",
    "ne_10m_admin_0_countries": "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip",
    "ne_10m_populated_places_simple": "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places_simple.zip",
    "ne_50m_land": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip",
    "ne_50m_coastline": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_coastline.zip",
    "ne_110m_land": "https://naciscdn.org/naturalearth/110m/physical/ne_110m_land.zip",
}

OSM_URLS: dict[str, str] = {
    "land": "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip",
    "coastline": "https://osmdata.openstreetmap.de/download/coastlines-split-4326.zip",
}

SCUFN_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/IHO/undersea_features/MapServer/0/query"
)

GSFM_URL = (
    "https://www.dropbox.com/scl/fi/mm01uhaa0ohyfitckmru7/"
    "global-seafloor-geomorphic-features-map.zip"
    "?rlkey=so36tzk2mfq6344xioq4v92y0&dl=1"
)

# ---------------------------------------------------------------------------
# Azure upload
# ---------------------------------------------------------------------------

AZURE_STORAGE_ACCOUNT = "ne1osvmdevtest"
AZURE_CONTAINER = "basemap"
BLOB_BASE = f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{AZURE_CONTAINER}"

# ---------------------------------------------------------------------------
# Hypsometric color ramp
# ---------------------------------------------------------------------------

HYPSOMETRIC_RAMP = """\
# EMODnet-style hypsometric color ramp
# Format: elevation  R  G  B
-1      170 210 220
0       196 218 158
10      200 220 160
25      205 222 150
50      210 215 145
100     215 215 140
150     218 210 135
200     220 205 130
300     222 200 125
500     218 190 120
750     210 175 115
1000    200 160 110
1500    190 145 105
2000    180 140 105
2500    175 150 135
3000    185 170 150
4000    200 195 185
5000    218 215 210
6000    235 232 228
7000    245 243 240
8000    252 250 248
9000    255 254 252
nv     0  0  0  0
"""

# ---------------------------------------------------------------------------
# SCUFN zoom mapping
# ---------------------------------------------------------------------------

SCUFN_ZOOM: dict[str, int] = {
    "Ocean": 1, "Sea": 2, "Gulf": 2, "Bay": 3,
    "Basin": 3, "Ridge": 3, "Trench": 3, "Trough": 3,
    "Plateau": 4, "Rise": 4, "Plain": 4,
    "Fracture zone": 5, "Fracture Zone": 5, "Escarpment": 5,
    "Seamount": 6, "Seamounts": 6, "Guyot": 6,
    "Bank": 6, "Canyon": 6,
    "Sill": 7, "Knoll": 7, "Shoal": 7, "Reef": 7,
    "Fan": 7, "Channel": 7, "Valley": 7,
    "Saddle": 8, "Hill": 8, "Pinnacle": 8,
}
