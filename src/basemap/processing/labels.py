"""Ocean label processing — merge marine polygons, region points, and SCUFN data.

Extracts label centroids from Natural Earth polygons, merges with region points
and optionally SCUFN gazetteer features, assigning zoom levels and text styles.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from basemap.config import SCUFN_ZOOM

console = Console()


def _polygon_centroid(rings: list) -> tuple[float, float] | None:
    """Average-coordinate centroid of a polygon's outer ring."""
    if not rings or not rings[0]:
        return None
    ring = rings[0]
    n = len(ring)
    if n == 0:
        return None
    cx = sum(p[0] for p in ring) / n
    cy = sum(p[1] for p in ring) / n
    return (round(cx, 4), round(cy, 4))


def _feature_centroid(geom: dict) -> tuple[float, float] | None:
    """Extract centroid from a Polygon or MultiPolygon geometry."""
    coords = geom.get("coordinates", [])
    if geom["type"] == "Polygon":
        return _polygon_centroid(coords)
    elif geom["type"] == "MultiPolygon":
        largest = max(coords, key=lambda p: len(p[0]) if p else 0)
        return _polygon_centroid(largest)
    return None


def process_marine_polys(path: Path) -> list[dict]:
    """Convert marine polygon features to centroid point labels."""
    with open(path) as f:
        data = json.load(f)

    features = []
    for feat in data["features"]:
        props = feat["properties"]
        name = props.get("name", "")
        if not name:
            continue

        fclass = props.get("featurecla", "")
        ne_min_label = props.get("min_label", 5)
        scalerank = props.get("scalerank", 5)
        min_zoom = max(1, int(round(ne_min_label)))

        if fclass == "ocean":
            text_transform, text_size, label_type = "uppercase", 18, "ocean"
        elif fclass in ("sea", "gulf"):
            text_transform, text_size, label_type = "uppercase", 14, "sea"
        elif fclass in ("bay", "strait", "inlet"):
            text_transform, text_size, label_type = "none", 12, "bay"
        else:
            text_transform, text_size, label_type = "none", 11, fclass or "other"

        centroid = _feature_centroid(feat["geometry"])
        if centroid is None:
            continue

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(centroid)},
            "properties": {
                "name": name,
                "name_en": props.get("name_en", name),
                "label_type": label_type,
                "min_zoom": min_zoom,
                "text_transform": text_transform,
                "text_size": text_size,
                "scalerank": scalerank,
            },
        })

    console.print(f"  {len(features)} marine labels from polygons")
    return features


def process_region_points(path: Path) -> list[dict]:
    """Extract region point labels (capes, ridges, basins, etc.)."""
    with open(path) as f:
        data = json.load(f)

    features = []
    for feat in data["features"]:
        props = feat["properties"]
        name = props.get("name", "")
        if not name:
            continue

        fclass = (props.get("featurecla", "") or "").lower()
        min_zoom = props.get("min_zoom", 7)
        if isinstance(min_zoom, (int, float)):
            min_zoom = max(1, int(round(min_zoom)))
        else:
            min_zoom = 7

        features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "name": name,
                "name_en": props.get("name_en", name),
                "label_type": fclass,
                "min_zoom": min_zoom,
                "text_transform": "none",
                "text_size": 10,
                "scalerank": props.get("scalerank", 7),
            },
        })

    console.print(f"  {len(features)} region point labels")
    return features


def process_scufn(path: Path, existing_names: set[str]) -> list[dict]:
    """Process SCUFN gazetteer features, deduplicating against *existing_names*."""
    if not path.is_file():
        console.print("  [dim]SCUFN data not available, skipping.[/dim]")
        return []

    with open(path) as f:
        data = json.load(f)

    features = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        name = props.get("Name", props.get("name", ""))
        if not name or name.lower() in existing_names:
            continue

        generic = props.get("Generic", props.get("generic_term", "Unknown"))
        zoom = 7
        for key, val in SCUFN_ZOOM.items():
            if key.lower() in (generic or "").lower():
                zoom = val
                break

        features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "name": name.strip(),
                "name_en": name.strip(),
                "label_type": (generic or "unknown").lower(),
                "min_zoom": zoom,
                "text_transform": "uppercase" if zoom <= 3 else "none",
                "text_size": 13 if zoom <= 3 else 11,
                "scalerank": zoom,
            },
        })
        existing_names.add(name.lower())

    console.print(f"  {len(features)} SCUFN features (de-duplicated)")
    return features


def merge_ocean_labels(
    marine_polys_path: Path,
    region_points_path: Path,
    scufn_path: Path | None,
    output_path: Path,
) -> Path:
    """Merge all label sources into a single GeoJSON file."""
    all_features: list[dict] = []

    all_features.extend(process_marine_polys(marine_polys_path))
    all_features.extend(process_region_points(region_points_path))

    if scufn_path:
        existing_names = {f["properties"]["name"].lower() for f in all_features}
        all_features.extend(process_scufn(scufn_path, existing_names))

    geojson = {"type": "FeatureCollection", "features": all_features}
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    # Zoom distribution summary
    zoom_dist: dict[int, int] = {}
    for feat in all_features:
        z = feat["properties"]["min_zoom"]
        zoom_dist[z] = zoom_dist.get(z, 0) + 1
    for z in sorted(zoom_dist):
        console.print(f"    z{z}: {zoom_dist[z]} features")

    console.print(f"  [green]Total[/green]: {len(all_features)} labels → {output_path.name}")
    return output_path
