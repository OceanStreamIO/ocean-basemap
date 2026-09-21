#!/usr/bin/env python
"""Export the viewer's MapLibre style as a file anyone can point a map at.

The style is not written down anywhere to copy: it is built in app.js, layer
by layer, with expressions that depend on constants next to them. Copying it
into a style.json by hand would be a second copy to keep in step, and it would
be wrong within a week.

So the style is taken from a running map instead. The viewer is loaded in a
headless browser and asked for `map.getStyle()`, which is the style MapLibre
actually holds after everything is added -- the same object, resolved. What
comes back is trimmed of the things that only make sense inside the viewer:
any layer added from `?overlay=`, and the diagnostic graticule and measuring
layers, which have no data behind them until someone draws one.

    python scripts/export-style.py --out dist/style.json

The tile URLs in it are `pmtiles://...`, so whoever loads the style needs the
pmtiles protocol registered (pmtiles.js does this in three lines; see
docs/viewer.md). Add --plain to rewrite them as https, for a client that
reads PMTiles natively.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VIEWER = "https://project-oceanstream-basemap.lab.dive.edito.eu/"

# Layers the viewer draws for itself, which mean nothing to anyone else: the
# measuring tools' scratch geometry and the graticule it generates at runtime.
RUNTIME_LAYERS = ("measure-", "graticule-", "overlay-")


def export(url: str, timeout_ms: int = 120_000) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url, wait_until="load", timeout=timeout_ms)
        page.wait_for_function("() => typeof map !== 'undefined' && map.isStyleLoaded()",
                               timeout=timeout_ms)
        style = page.evaluate("() => JSON.parse(JSON.stringify(map.getStyle()))")
        browser.close()
    if errors:
        raise SystemExit(f"the viewer reported errors: {errors[:3]}")
    return style


def trim(style: dict, plain: bool) -> dict:
    style["layers"] = [
        layer for layer in style.get("layers", [])
        if not layer["id"].startswith(RUNTIME_LAYERS)
    ]
    used = {layer.get("source") for layer in style["layers"]}
    style["sources"] = {k: v for k, v in style.get("sources", {}).items() if k in used}
    if plain:
        for source in style["sources"].values():
            if isinstance(source.get("url"), str):
                source["url"] = source["url"].replace("pmtiles://", "")
    style["metadata"] = {
        "oceanstream:generated-by": "scripts/export-style.py",
        "oceanstream:viewer": VIEWER,
    }
    return style


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=VIEWER, help="viewer to read the style from")
    parser.add_argument("--out", type=Path, required=True, help="where to write it")
    parser.add_argument("--plain", action="store_true",
                        help="rewrite pmtiles:// URLs as plain https")
    args = parser.parse_args()

    style = trim(export(args.url), args.plain)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(style['layers'])} layers, {len(style['sources'])} sources "
          f"→ {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
