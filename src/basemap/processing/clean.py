"""Seamount property normalization for GSFM data.

Cleans and standardizes property names from the GSFM shapefile, which vary
between GSFM versions.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

console = Console()

# Field name variants across GSFM versions
_NAME_KEYS = ("Name", "NAME", "name", "FEATNAME", "Feature_Na")


def clean_seamounts(src: Path, dst: Path) -> Path:
    """Normalize seamount GeoJSON properties to a clean schema.

    Output properties: ``name``, ``type`` ("seamount").
    """
    with open(src) as f:
        data = json.load(f)

    clean_features = []
    for feat in data.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue

        props = feat.get("properties", {})
        name = ""
        for key in _NAME_KEYS:
            if props.get(key):
                name = str(props[key]).strip()
                break

        clean_features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "name": name,
                "type": "seamount",
            },
        })

    output = {"type": "FeatureCollection", "features": clean_features}
    with open(dst, "w") as f:
        json.dump(output, f)

    console.print(f"  [green]Cleaned[/green]: {len(clean_features)} seamount features → {dst.name}")
    return dst
