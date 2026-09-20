"""Central configuration — paths, URLs, constants.

The data directory is resolved from (in order):
  1. BASEMAP_DATA_DIR environment variable
  2. Default: /media/andrei/RP60/oceanstream/basemap

The GEBCO release is resolved from BASEMAP_GEBCO_RELEASE, defaulting to the
latest published grid. Set it to an older year to keep working against data
already on disk.
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


# ---------------------------------------------------------------------------
# GEBCO release
# ---------------------------------------------------------------------------

LATEST_GEBCO_RELEASE = 2026

GEBCO_RELEASE = int(os.environ.get("BASEMAP_GEBCO_RELEASE", LATEST_GEBCO_RELEASE))

# Releases we know how to locate on disk, newest first.
KNOWN_GEBCO_RELEASES: tuple[int, ...] = (2026, 2025)


def get_gebco_dir(data_dir: Path, release: int = GEBCO_RELEASE) -> Path:
    return data_dir / f"gebco{release}"


def get_gebco_tif(data_dir: Path, release: int = GEBCO_RELEASE) -> Path:
    return get_gebco_dir(data_dir, release) / f"GEBCO_{release}.tif"


def get_work_dir(data_dir: Path) -> Path:
    return get_gebco_dir(data_dir) / "work"


def get_tile_dir(data_dir: Path) -> Path:
    return get_gebco_dir(data_dir) / "tiles"


def get_land_dem_dir(data_dir: Path) -> Path:
    """Where the Copernicus DEM tiles for the land relief are downloaded."""
    return get_work_dir(data_dir) / "land_dem"


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

# layer -> (Natural Earth dataset, URL, (min zoom, max zoom)). Country and
# internal boundaries are kept apart so they can be drawn and toggled apart.
BOUNDARY_SHAPEFILES: dict[str, tuple[str, str, tuple[int, int]]] = {
    "countries": (
        "ne_10m_admin_0_boundary_lines_land",
        "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_boundary_lines_land.zip",
        (2, 10),
    ),
    "regions": (
        "ne_10m_admin_1_states_provinces_lines",
        "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces_lines.zip",
        (4, 10),
    ),
}

# ---------------------------------------------------------------------------
# Lakes
# ---------------------------------------------------------------------------

# HydroLAKES (HydroSHEDS), CC BY 4.0: 1.4 million lakes with mean depth and
# surface elevation per lake.
HYDROLAKES_URL = "https://data.hydrosheds.org/file/hydrolakes/HydroLAKES_polys_v10_shp.zip"

# (minimum lake area in km2, (min zoom, max zoom)). Tiling every pond from z2
# would bury the few hundred lakes that are actually legible there.
LAKE_BANDS: tuple[tuple[float, tuple[int, int]], ...] = (
    (1000, (2, 4)),
    (100, (5, 6)),
    (10, (7, 8)),
    (1, (9, 10)),
    (0, (11, 12)),
)

# Lakes rendered as raster relief: big enough for GEBCO's 463 m grid to say
# something (10 km2 is about 6 cells across), and deep enough that the bed is
# a real sounding rather than the flattened surface.
LAKE_RELIEF_AREA = 10.0
LAKE_RELIEF_DEPTH = 5.0
LAKE_RELIEF_ZOOMS = (4, 10)

# A lake at or below this surface elevation counts as coastal: it sits against
# the sea and is drawn on the sea's ramp rather than the lake ramp. Keep in
# step with COASTAL_LAKE_ELEV in the viewer.
COASTAL_LAKE_ELEV = 5.0

# Per-lake bathymetry for lakes GEBCO has no soundings for. The file's own
# comment says how to add one.
LAKE_SOURCES_FILE = Path(__file__).resolve().parents[2] / "lake-sources.json"

# Colour ramps shipped with the repository.
RAMP_DIR = Path(__file__).resolve().parents[2] / "color-ramps"

# Worker processes for the raster renderer. Measured per worker: about 1 GiB
# for the GEBCO and EMODnet archives, levelling off at about 3.5 GiB through the
# HR surveys (see MAX_OPEN_DATASETS). Size memory for the HR pass: 8 workers on
# this pod, 14 on a GPU node's 16 cores with the build Job's 52 GiB.
RENDER_WORKERS = int(os.environ.get("BASEMAP_RENDER_WORKERS", "8"))


# ---------------------------------------------------------------------------
# Tile geometry precision
# ---------------------------------------------------------------------------

# Tippecanoe quantises each tile's geometry onto a 2**detail grid, and defaults
# to 12 (4096). At the maximum zoom of 10 that grid is ~7 m across in European
# latitudes, which is invisible at z10 and stays invisible while the viewer is
# showing z10 tiles at z10. The viewer sets no maxZoom, so MapLibre overzooms
# them instead, and the step scales with the zoom: 1 px at z14, 4 px at z16,
# 16 px at z18. A shoreline is exactly where that staircase is legible, so
# every layer whose edge reads as the coast is built at 15 (32768) instead --
# ~0.9 m, subpixel until z19. A dense coastal tile roughly doubles, but most
# tiles carry little or no coastline, so whole archives barely grow: land_base
# went from 880 MB to 976 MB, and depth_zones stayed at 1.1 GB.
SHORELINE_DETAIL = 15

# ---------------------------------------------------------------------------
# GEBCO download
# ---------------------------------------------------------------------------

CEDA_BASE = (
    f"https://dap.ceda.ac.uk/bodc/gebco/global/gebco_{GEBCO_RELEASE}"
    "/ice_surface_elevation/geotiff"
)

# 8 quarter-hemisphere tiles covering the globe (90° x 90° each).
GEBCO_TILE_BBOXES: list[str] = [
    "n90.0_s0.0_w-180.0_e-90.0",
    "n90.0_s0.0_w-90.0_e0.0",
    "n90.0_s0.0_w0.0_e90.0",
    "n90.0_s0.0_w90.0_e180.0",
    "n0.0_s-90.0_w-180.0_e-90.0",
    "n0.0_s-90.0_w-90.0_e0.0",
    "n0.0_s-90.0_w0.0_e90.0",
    "n0.0_s-90.0_w90.0_e180.0",
]

# CEDA appended a "_geotiff" suffix to per-tile filenames from the 2026 release on.
_TILE_SUFFIX = "" if GEBCO_RELEASE <= 2025 else "_geotiff"

# MD5s are only published for some releases; CEDA has no manifest for 2026.
# Tiles without a known MD5 are verified against the server's content-length.
_TILE_MD5: dict[int, dict[str, str]] = {
    2025: {
        "n90.0_s0.0_w-180.0_e-90.0": "5339d30e2a4f4ae766a436d33ab185af",
        "n90.0_s0.0_w-90.0_e0.0": "add2bd85c08106e47d67a1802ae8355a",
        "n90.0_s0.0_w0.0_e90.0": "e93523f35c40d65e14e15724960cbe2d",
        "n90.0_s0.0_w90.0_e180.0": "5ef662c016a17d6eaf03841f840d629c",
        "n0.0_s-90.0_w-180.0_e-90.0": "4fc4ce0052dc91463a9ab43521cbc7d8",
        "n0.0_s-90.0_w-90.0_e0.0": "39b88b46c36e999021d6bf38e21a794c",
        "n0.0_s-90.0_w0.0_e90.0": "cc7820f063dc547c8711c367b9dbb06c",
        "n0.0_s-90.0_w90.0_e180.0": "da7235029d598c53c509e56e7472239d",
    },
}

GEBCO_TILES: list[tuple[str, str | None]] = [
    # (filename, md5 or None)
    (
        f"gebco_{GEBCO_RELEASE}_{bbox}{_TILE_SUFFIX}.tif",
        _TILE_MD5.get(GEBCO_RELEASE, {}).get(bbox),
    )
    for bbox in GEBCO_TILE_BBOXES
]

# ---------------------------------------------------------------------------
# Depth classification
# ---------------------------------------------------------------------------

# The shelf breaks below 100 m are deliberately fine: 5-30 m is the working
# range for kelp and seagrass survey work, and a single -50..-10 band hid it.
DEPTH_BREAKS: list[int] = [
    -11000, -8000, -6000, -5000, -4000, -3000, -2000,
    -1500, -1000, -500, -200, -100, -75, -50, -40, -30,
    -20, -15, -10, -5, -2, 0,
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
    -75: "#b4dcf4",
    -50: "#c0e4f8",
    -40: "#c8e8f9",
    -30: "#d0ecfa",
    -20: "#d8f0fb",
    -15: "#def2fb",
    -10: "#e4f5fc",
    -5: "#ecf8fd",
    -2: "#f4fbfe",
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
    "ne_10m_rivers_lake_centerlines": "https://naciscdn.org/naturalearth/10m/physical/ne_10m_rivers_lake_centerlines.zip",
    "ne_10m_lakes": "https://naciscdn.org/naturalearth/10m/physical/ne_10m_lakes.zip",
    "ne_10m_admin_0_countries": "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip",
    "ne_10m_populated_places_simple": "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places_simple.zip",
    "ne_50m_land": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip",
    "ne_50m_coastline": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_coastline.zip",
    "ne_50m_rivers_lake_centerlines": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_rivers_lake_centerlines.zip",
    "ne_50m_lakes": "https://naciscdn.org/naturalearth/50m/physical/ne_50m_lakes.zip",
    "ne_110m_land": "https://naciscdn.org/naturalearth/110m/physical/ne_110m_land.zip",
    "ne_110m_lakes": "https://naciscdn.org/naturalearth/110m/physical/ne_110m_lakes.zip",
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
# EMODnet Bathymetry — regional high-resolution overlay
# ---------------------------------------------------------------------------

EMODNET_WCS = "https://ows.emodnet-bathymetry.eu/wcs"

# Latest harmonised DTM. Historical releases are exposed as emodnet__mean_<year>.
EMODNET_COVERAGE = "emodnet__mean"

# Request extent (west, south, east, north). The coverage envelope spans
# -70.5..43 lon / 11..90 lat; this default covers the European seas and stops
# below the Web Mercator limit.
EMODNET_EXTENT: tuple[float, float, float, float] = (-36.0, 15.0, 43.0, 84.0)

# WCS is chunked into square requests. The service rejects large ones with a 500:
# measured 2026-09, 3° returns 37 MB but 4° and 5° fail in ~0.2 s, i.e. an upfront
# size limit rather than a timeout. 2° is ~2048 × 2048 px / 16 MB per response.
EMODNET_CHUNK_DEG = 2.0

# Native grid is 1/16 arc-minute (~115 m). Depths are referenced to Lowest
# Astronomical Tide, not mean sea level.
EMODNET_NATIVE_RES = 1 / 960

# Resolution used for polygonizing the classified raster.
EMODNET_POLYGONIZE_RES = 0.001

EMODNET_MIN_ZOOM = 6
EMODNET_MAX_ZOOM = 12

# ---------------------------------------------------------------------------
# EMODnet high-resolution DTMs (coastal surveys, 1/32 to 1/1024 arc-minute)
# ---------------------------------------------------------------------------

EMODNET_HR_WFS = "https://ows.emodnet-bathymetry.eu/wfs"
EMODNET_HR_LAYER = "emodnet:hr_bathymetry_area"

# Every download_url in the index must live on this host.
EMODNET_HR_DOWNLOAD_HOST = "downloads.emodnet-bathymetry.eu"

# Footprints are selected by intersection with this box (west, south, east, north).
EMODNET_HR_EXTENT: tuple[float, float, float, float] = EMODNET_EXTENT

# Only keep grids at 1/N arc-minute or finer.
EMODNET_HR_MIN_DENOM = 32

EMODNET_HR_MIN_ZOOM = 10
EMODNET_HR_MAX_ZOOM = 14

# ---------------------------------------------------------------------------
# S3 upload (CloudFerro / EDITO, or any S3-compatible endpoint)
# ---------------------------------------------------------------------------

S3_ENDPOINT = os.environ.get("BASEMAP_S3_ENDPOINT", "https://s3.waw3-1.cloudferro.com")
S3_BUCKET = os.environ.get("BASEMAP_S3_BUCKET", "oceanstream-bucket")
S3_PREFIX = os.environ.get("BASEMAP_S3_PREFIX", "basemap").strip("/")
S3_REGION = os.environ.get("BASEMAP_S3_REGION", "waw3-1")

# Public base URL for the uploaded tiles. Virtual-host style keeps the bucket in
# the hostname, which is what MapLibre/PMTiles range requests expect.
_S3_HOST = S3_ENDPOINT.split("://", 1)[-1]
S3_PUBLIC_BASE = os.environ.get(
    "BASEMAP_S3_PUBLIC_BASE",
    f"https://{S3_BUCKET}.{_S3_HOST}/{S3_PREFIX}" if S3_PREFIX else f"https://{S3_BUCKET}.{_S3_HOST}",
)

# Tiles are content-addressed by z/x/y and regenerated wholesale, so they can be
# cached hard. PMTiles archives change in place, so they get a shorter TTL.
S3_CACHE_CONTROL_TILES = "public, max-age=604800"
S3_CACHE_CONTROL_PMTILES = "public, max-age=3600"

# ---------------------------------------------------------------------------
# Parallelism
# ---------------------------------------------------------------------------


def get_tile_processes() -> int:
    """Worker count for gdal2tiles.

    Takes the lesser of the cgroup CPU quota and the machine's online cores.
    The quota alone is not enough: it can legally exceed what the node has, and
    ``os.cpu_count()`` alone reports the host rather than the pod's share.
    """
    override = os.environ.get("BASEMAP_TILE_PROCESSES")
    if override:
        return max(1, int(override))
    online = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return max(1, min(int(quota) // int(period), online))
    except (OSError, ValueError):
        pass
    return max(1, online)

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
