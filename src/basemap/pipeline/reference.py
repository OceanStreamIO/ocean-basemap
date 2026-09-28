"""The reference layer — names, on their own transparent tree. **Parked.**

Not in FULL_ORDER and not in the build job: `oceanbase run reference` builds
and publishes it, a full run does not. The mechanism works and is tested; the
typography was not judged good enough beside Esri's, and the decision for now
is to stack Esri's World_Ocean_Reference over our tiles instead. Kept because
shipping our own names is wanted in a later release, and because it is the
only version that can leave out what Esri's cannot — see below.

Esri publish their ocean map as two services, and the split is the whole
point of this step. `World_Ocean_Base` is JPEG: bathymetry, land cover, no
text anywhere. `World_Ocean_Reference` is PNG32 — transparent — and carries
the marine water body names and the undersea feature names. A client stacks
the second over the first. Nothing is ever baked into the base.

We publish the same pair:

    basemap/xyz/{z}/{x}/{y}.webp        the map          opaque
    basemap/labels/{z}/{x}/{y}.png      the names        transparent

Three things follow from the split, and they are the reasons for it.

Restyling is cheap. The base tree is twenty million tiles and most of a day;
this one is about a hundred and sixty thousand and a few minutes, because a
tile is only written where a name actually falls. Changing a typeface, a size
or a rule about what appears when costs a re-render of this tree alone.

The names are optional at the point of use. A plain {z}/{x}/{y} consumer adds
one more layer or does not. Nobody has to accept our labels to use our
bathymetry, and nobody has to take our bathymetry to use the names.

And it is the only way the text can be right at every zoom. Baked into the
base, a name drawn at z6 is stretched to nonsense by z9. Here each level is
drawn at its own scale, with its own decision about what is worth showing.

**Placement is global, not per tile.** Labels are laid out once per zoom in
world pixel coordinates and only then cut into tiles, so a name that straddles
a tile boundary is drawn identically in both tiles and two names either side
of a boundary still collide with each other. Doing it per tile is the classic
way to get labels that repeat, jump, or overprint at the seams.

Sources: the GEBCO gazetteer of undersea feature names (the IHO-IOC
Sub-Committee on Undersea Feature Names, which is where Esri's own undersea
names come from), plus the Natural Earth marine names the ocean-labels step
already merges.
"""

from __future__ import annotations

import io
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.progress import Progress

from basemap.config import get_data_dir, get_tile_dir, get_work_dir
from basemap.utils.paths import ensure_dir

console = Console()

TILE = 256
MAX_ZOOM = 12

# The GEBCO gazetteer, as served by the Sub-Committee's own ArcGIS feature
# service: 2,632 points, 1,128 lines, 1,652 polygons. Esri's ocean reference
# draws its undersea names from the same register.
GAZETTEER = ("https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/"
             "Undersea_Features/FeatureServer")
GAZETTEER_LAYERS = (0, 1, 2)
PAGE = 1000

FONT_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"

# Tiers carry meaning, so they are distinguished by size and weight rather
# than by colour: the one reader this map is built for first is colour blind,
# and a hue difference would tell them nothing. The same reason the text is
# larger than a web map's usual 11-12 px and the halo is close to opaque.
WATER_RGB = (22, 52, 84)
UNDERSEA_RGB = (28, 62, 96)
HALO_RGBA = (255, 255, 255, 236)
HALO_WIDTH = 3

TIER_WATER, TIER_MAJOR, TIER_MINOR = 0, 1, 2
TIER_STYLE = {
    #        size  bold   tracking  upper
    TIER_WATER: (17, False, 2.4, True),
    TIER_MAJOR: (16, True, 0.0, False),
    TIER_MINOR: (14, False, 0.0, False),
}

# When each kind of undersea feature earns its name. A basin spans an ocean
# and is worth showing as soon as the ocean is; a knoll is a few kilometres
# across and would be a dot with a caption until you are close.
FEATURE_ZOOM: dict[str, tuple[int, int]] = {
    # TYPE (upper-cased)   (min zoom, tier)
    "ABYSSAL PLAIN": (3, TIER_MAJOR),
    "BASIN": (3, TIER_MAJOR),
    "TRENCH": (3, TIER_MAJOR),
    "RIDGE": (3, TIER_MAJOR),
    "PLATEAU": (4, TIER_MAJOR),
    "RISE": (4, TIER_MAJOR),
    "FRACTURE ZONE": (4, TIER_MAJOR),
    "ESCARPMENT": (5, TIER_MAJOR),
    "SEAMOUNTS": (5, TIER_MAJOR),
    "SEACHANNEL": (5, TIER_MAJOR),
    "PROMONTORY": (5, TIER_MAJOR),
    "SHELF": (5, TIER_MAJOR),
    "SILL": (7, TIER_MINOR),
    "GAP": (7, TIER_MINOR),
    "SADDLE": (7, TIER_MINOR),
    "SEAMOUNT": (7, TIER_MINOR),
    "GUYOT": (7, TIER_MINOR),
    "BANK": (7, TIER_MINOR),
    "CANYON": (7, TIER_MINOR),
    "VALLEY": (7, TIER_MINOR),
    "TERRACE": (7, TIER_MINOR),
    "SPUR": (8, TIER_MINOR),
    "KNOLL": (8, TIER_MINOR),
    "HILLS": (8, TIER_MINOR),
    "REEF": (8, TIER_MINOR),
    "SHOAL": (8, TIER_MINOR),
}
FEATURE_ZOOM_DEFAULT = (7, TIER_MINOR)


@dataclass(frozen=True)
class Label:
    text: str
    lon: float
    lat: float
    tier: int
    min_zoom: int


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _fetch_gazetteer(dest: Path) -> dict:
    """The whole gazetteer, cached on disk — it changes a few times a year."""
    if dest.is_file():
        return json.loads(dest.read_text())

    features = []
    with httpx.Client(timeout=120) as client:
        for layer in GAZETTEER_LAYERS:
            offset = 0
            while True:
                r = client.get(f"{GAZETTEER}/{layer}/query", params={
                    "where": "1=1", "outFields": "NAME,TYPE", "f": "geojson",
                    "outSR": "4326", "resultOffset": offset, "resultRecordCount": PAGE,
                })
                r.raise_for_status()
                page = r.json().get("features", [])
                features += page
                if len(page) < PAGE:
                    break
                offset += len(page)
            console.print(f"  gazetteer layer {layer}: {len(features):,} so far")

    data = {"type": "FeatureCollection", "features": features}
    dest.write_text(json.dumps(data))
    return data


def _vertices(coords) -> list[list[float]]:
    out: list[list[float]] = []

    def walk(node) -> None:
        if node and isinstance(node[0], (int, float)):
            out.append(node)
        else:
            for child in node:
                walk(child)

    walk(coords)
    return out


def _anchor(geometry: dict) -> tuple[float, float] | None:
    """Where a feature's name goes: the point, or the centre of the geometry.

    Longitude is averaged as an angle, not as a number. A feature crossing the
    antimeridian arrives split into parts sitting at +180 and -180, whose
    arithmetic mean is zero: the Emperor Trough, which is in the north-west
    Pacific, came out over central Spain. Averaging unit vectors puts it back
    on the dateline where it belongs.

    A line or polygon gets the mean of its vertices rather than a true
    centroid. For the shapes in this register the two are within a label's
    width of each other, and the mean cannot fall outside a ring the way a
    bounding-box centre can.
    """
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if geometry.get("type") == "Point":
        return float(coords[0]), float(coords[1])

    points = _vertices(coords)
    if not points:
        return None
    east = sum(math.cos(math.radians(p[0])) for p in points)
    north = sum(math.sin(math.radians(p[0])) for p in points)
    lon = math.degrees(math.atan2(north, east)) if (east or north) else points[0][0]
    return lon, sum(p[1] for p in points) / len(points)


# A feature earns its name once it is wide enough on screen to be a thing
# rather than a dot with a caption. 48 px is a fifth of a tile.
MIN_FEATURE_PX = 48
EQUATOR_M = 40075016.686


def _span_m(geometry: dict) -> float | None:
    """How wide a feature is on the ground, or None for a bare point.

    The register gives no size attribute, so type is all there is to rank by,
    and by type alone the Mid-Atlantic Ridge and a ridge twenty kilometres
    long appear at the same zoom. The geometry knows the difference.
    """
    if geometry.get("type", "").endswith("Point"):
        return None
    points = _vertices(geometry.get("coordinates") or [])
    if len(points) < 2:
        return None
    lats = [p[1] for p in points]
    lat = math.radians(sum(lats) / len(lats))
    # Unwrap longitudes around the first vertex, so a dateline crossing does
    # not read as a feature wrapping the whole planet.
    base = points[0][0]
    lons = [p[0] - 360 * round((p[0] - base) / 360) for p in points]
    dx = (max(lons) - min(lons)) / 360 * EQUATOR_M * math.cos(lat)
    dy = (max(lats) - min(lats)) / 360 * EQUATOR_M
    return math.hypot(dx, dy)


def _zoom_for_span(span_m: float, lat: float) -> int:
    """The first zoom at which *span_m* covers MIN_FEATURE_PX."""
    metres_per_px_z0 = EQUATOR_M * math.cos(math.radians(max(min(lat, 85.0), -85.0))) / TILE
    for z in range(MAX_ZOOM + 1):
        if span_m / (metres_per_px_z0 / (1 << z)) >= MIN_FEATURE_PX:
            return z
    return MAX_ZOOM


def _undersea_labels(data: dict) -> list[Label]:
    out = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = (props.get("NAME") or "").strip()
        if not name:
            continue
        where = _anchor(feature.get("geometry") or {})
        if where is None:
            continue
        kind = (props.get("TYPE") or "").strip()
        min_zoom, tier = FEATURE_ZOOM.get(kind.upper(), FEATURE_ZOOM_DEFAULT)
        span = _span_m(feature.get("geometry") or {})
        if span:
            # Where the geometry gives a size, that decides: a ridge twenty
            # kilometres long and the Mid-Atlantic Ridge are both "RIDGE" to
            # the table, and only one of them belongs on a world view. Type
            # still shifts the answer by a level, so a knoll waits a little
            # longer than a basin of the same width. Points keep the table:
            # they have no size to read.
            min_zoom = _zoom_for_span(span, where[1]) + (1 if tier == TIER_MINOR else 0)
        # "Charcot" plus "Seamounts" reads as a name; the register splits them.
        text = name
        if kind and not name.upper().endswith(kind.upper()):
            text = f"{name} {kind}"
        out.append(Label(text, where[0], where[1], tier, min_zoom))
    return out


def _water_labels(merged: Path) -> list[Label]:
    """Sea and bay names, from what the ocean-labels step already merged."""
    if not merged.is_file():
        console.print("  [yellow]No merged ocean labels; undersea names only.[/yellow]")
        return []
    data = json.loads(merged.read_text())
    out = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = (props.get("name") or props.get("name_en") or "").strip()
        where = _anchor(feature.get("geometry") or {})
        if not name or where is None:
            continue
        out.append(Label(name, where[0], where[1], TIER_WATER,
                         int(props.get("min_zoom") or 2)))
    return out


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    name = "Lato-Bold.ttf" if bold else "Lato-Regular.ttf"
    path = FONT_DIR / name
    if not path.is_file():
        raise SystemExit(f"missing bundled font {path}")
    return ImageFont.truetype(str(path), size)


def _display(label: Label) -> tuple[str, ImageFont.FreeTypeFont, float]:
    size, bold, tracking, upper = TIER_STYLE[label.tier]
    return (label.text.upper() if upper else label.text), _font(bold, size), tracking


def _text_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: float) -> float:
    return draw.textlength(text, font=font) + tracking * max(0, len(text) - 1)


def _world_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    world = TILE << z
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * world
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * world
    return x, y


@dataclass(frozen=True)
class Placed:
    text: str
    font: ImageFont.FreeTypeFont
    tracking: float
    rgb: tuple[int, int, int]
    x: float
    y: float
    half_w: float
    half_h: float


def place(labels: list[Label], z: int, probe: ImageDraw.ImageDraw) -> list[Placed]:
    """Lay the labels out for one zoom, in world pixels, resolving collisions.

    Greedy, in tier order then by name so the result does not depend on the
    order the sources happened to arrive in: the same input gives the same map
    every time, which matters when only the changed tiles are re-uploaded.
    """
    world = TILE << z
    # Tier first, then the zoom the feature earned: where two names cannot
    # both fit, the one that has been on the map since further out keeps its
    # place, rather than whichever happens to sort first by name.
    candidates = sorted((lb for lb in labels if z >= lb.min_zoom),
                        key=lambda lb: (lb.tier, lb.min_zoom, lb.text))

    placed: list[Placed] = []
    # Coarse grid so collision stays linear in the number of labels.
    cell = 256
    grid: dict[tuple[int, int], list[Placed]] = {}

    for label in candidates:
        text, font, tracking = _display(label)
        width = _text_width(probe, text, font, tracking)
        height = font.size
        x, y = _world_xy(label.lon, label.lat, z)
        half_w, half_h = width / 2 + 5, height / 2 + 3
        if y - half_h < 0 or y + half_h > world:
            continue

        clash = False
        for gx in range(int((x - half_w) // cell), int((x + half_w) // cell) + 1):
            for gy in range(int((y - half_h) // cell), int((y + half_h) // cell) + 1):
                for other in grid.get((gx, gy), ()):
                    if (abs(x - other.x) < half_w + other.half_w
                            and abs(y - other.y) < half_h + other.half_h):
                        clash = True
                        break
                if clash:
                    break
            if clash:
                break
        if clash:
            continue

        item = Placed(text, font, tracking,
                      WATER_RGB if label.tier == TIER_WATER else UNDERSEA_RGB,
                      x, y, half_w, half_h)
        placed.append(item)
        for gx in range(int((x - half_w) // cell), int((x + half_w) // cell) + 1):
            for gy in range(int((y - half_h) // cell), int((y + half_h) // cell) + 1):
                grid.setdefault((gx, gy), []).append(item)
    return placed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _draw_label(pen: ImageDraw.ImageDraw, item: Placed, x: float, y: float) -> None:
    """One label, haloed, centred on (x, y) in this tile's pixel space."""
    if not item.tracking:
        pen.text((x, y), item.text, font=item.font, fill=(*item.rgb, 255), anchor="mm",
                 stroke_width=HALO_WIDTH, stroke_fill=HALO_RGBA)
        return
    # PIL has no letter-spacing, so tracked text is set a glyph at a time.
    width = _text_width(pen, item.text, item.font, item.tracking)
    cursor = x - width / 2
    for char in item.text:
        pen.text((cursor, y), char, font=item.font, fill=(*item.rgb, 255), anchor="lm",
                 stroke_width=HALO_WIDTH, stroke_fill=HALO_RGBA)
        cursor += pen.textlength(char, font=item.font) + item.tracking


def render_tiles(placed: list[Placed], z: int) -> dict[tuple[int, int], bytes]:
    """Cut the laid-out zoom into tiles, writing only the ones with text."""
    buckets: dict[tuple[int, int], list[Placed]] = {}
    side = 1 << z
    for item in placed:
        for tx in range(int((item.x - item.half_w) // TILE),
                        int((item.x + item.half_w) // TILE) + 1):
            for ty in range(int((item.y - item.half_h) // TILE),
                            int((item.y + item.half_h) // TILE) + 1):
                if 0 <= tx < side and 0 <= ty < side:
                    buckets.setdefault((tx, ty), []).append(item)

    out = {}
    for (tx, ty), items in buckets.items():
        tile = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
        pen = ImageDraw.Draw(tile)
        for item in items:
            _draw_label(pen, item, item.x - tx * TILE, item.y - ty * TILE)
        if not tile.getbbox():
            continue
        buf = io.BytesIO()
        tile.save(buf, "PNG", optimize=True)
        out[(tx, ty)] = buf.getvalue()
    return out


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------

def _stage(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE IF NOT EXISTS tiles "
               "(zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB, "
               " PRIMARY KEY (zoom_level, tile_column, tile_row))")
    return db


def load_labels(base: Path) -> list[Label]:
    work = ensure_dir(get_work_dir(base) / "labels")
    gazetteer = _undersea_labels(_fetch_gazetteer(work / "gebco_undersea.geojson"))
    water = _water_labels(work / "ocean_labels_merged.geojson")
    console.print(f"  {len(gazetteer):,} undersea names, {len(water):,} water body names")
    return gazetteer + water


def run(data_dir: Path | None = None, max_zoom: int = MAX_ZOOM) -> None:
    console.print("\n[bold]Step 09: Reference layer (names)[/bold]\n")

    base = get_data_dir(data_dir)
    stage_path = ensure_dir(get_tile_dir(base)) / "labels.stage.mbtiles"
    labels = load_labels(base)

    db = _stage(stage_path)
    db.execute("DELETE FROM tiles")          # minutes to rebuild; never resumed
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    written = 0
    with Progress(console=console) as progress:
        task = progress.add_task("Zooms", total=max_zoom + 1)
        for z in range(max_zoom + 1):
            tiles = render_tiles(place(labels, z, probe), z)
            db.executemany(
                "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                [(z, tx, (1 << z) - 1 - ty, data) for (tx, ty), data in tiles.items()],
            )
            written += len(tiles)
            progress.advance(task)
    db.close()
    console.print(f"[green]Staged:[/green] {written:,} label tiles in {stage_path}")

    if os.environ.get("BASEMAP_LABELS_STAGE_ONLY"):
        console.print("  [dim]BASEMAP_LABELS_STAGE_ONLY set; not publishing.[/dim]")
        return
    publish(data_dir)


def publish(data_dir: Path | None = None, workers: int | None = None) -> None:
    """Send the staged tree to basemap/labels/{z}/{x}/{y}.png.

    Shares pipeline/publish.py with the flat tree, which is what gives this one
    resume marks: it used to read every row of the staging database into memory
    at once and start again from nothing if it was interrupted.
    """
    from basemap.pipeline.publish import publish_stages

    base = get_data_dir(data_dir)
    stage_path = get_tile_dir(base) / "labels.stage.mbtiles"
    prefix = "basemap/labels"
    publish_stages(
        [stage_path],
        prefix=prefix,
        marks_path=stage_path.with_suffix(".sent.sqlite"),
        extension="png",
        content_type="image/png",
        cache_control="public, max-age=86400",
        workers=workers,
    )
