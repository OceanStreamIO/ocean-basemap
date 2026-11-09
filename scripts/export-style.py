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

Add --verify to refuse a style naming an archive the server does not have.
The archives are published under digest names that are never reused, so a
style exported before a rebuild names files that a later prune deletes, and
a style like that is a map with no bathymetry in it and nothing to say why:
MapLibre draws nothing for a source that 404s. Every publish path should
pass it.

--channel chrome (or msedge, chromium) runs the export in an installed
browser instead of the one Playwright downloads, for a machine that has one
and not the other.

--repoint takes an already published style instead of a running map and moves
its archives to the versions a manifest names, changing nothing else. That is
how style/v1.json stays alive: its layer ids are a promise to whoever pinned
it, so it must never be overwritten with an export of today's viewer, which
drops whatever layers the viewer has dropped. It needs no browser.

    python scripts/export-style.py --out dist/v1.json --verify \
        --repoint https://.../basemap/style/v1.json \
        --manifest https://.../tiles/versions.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

VIEWER = "https://project-oceanstream-basemap.lab.dive.edito.eu/"

# Layers the viewer draws for itself, which mean nothing to anyone else: the
# measuring tools' scratch geometry and the graticule it generates at runtime.
RUNTIME_LAYERS = ("measure-", "graticule-", "overlay-")


def export(url: str, timeout_ms: int = 120_000, channel: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel,
                                    args=["--no-sandbox", "--enable-unsafe-swiftshader"])
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


def archive_urls(style: dict) -> list[str]:
    """The PMTiles archives the style reads, as plain https URLs.

    Only the archives: the Esri and EOX raster services in the style are not
    ours to verify, and they are addressed by template, not by file.
    """
    urls: list[str] = []
    for source in style.get("sources", {}).values():
        url = source.get("url")
        if isinstance(url, str) and url.startswith("pmtiles://"):
            urls.append(url[len("pmtiles://"):].split("?", 1)[0])
        elif isinstance(url, str) and url.endswith(".pmtiles"):
            urls.append(url.split("?", 1)[0])          # after --plain
    return urls


def archive_name(url: str) -> str:
    """The archive a URL reads, without its scheme, query or digest suffix."""
    leaf = url.split("?", 1)[0].rsplit("/", 1)[-1].removesuffix(".pmtiles")
    return re.sub(r"-[0-9a-f]{8,}$", "", leaf)


def repoint(style: dict, manifest: dict, base: str) -> dict:
    """*style* with each archive at the version *manifest* names, under *base*.

    Layers, source-layers and metadata are left exactly as they are. An archive
    the manifest does not know is refused rather than kept: its old URL may
    still resolve today, but nothing would stop a prune from deleting it.
    """
    archives = manifest["archives"]
    unknown = []
    for source in style.get("sources", {}).values():
        url = source.get("url")
        if not (isinstance(url, str) and url.startswith("pmtiles://")):
            continue
        name = archive_name(url)
        if name in archives:
            source["url"] = f"pmtiles://{base.rstrip('/')}/{archives[name]['path']}"
        else:
            unknown.append(name)
    if unknown:
        raise SystemExit(f"not in the manifest, so not re-pointed: {sorted(unknown)}")
    return style


def _read_json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _head(url: str) -> int:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def verify_sources(style: dict, head: Callable[[str], int] = _head) -> list[tuple[str, int]]:
    """Every archive the style names that the server does not have, with its status.

    200 and 206 both mean the file is there: a CDN may answer a HEAD with
    either, depending on whether it holds the object whole or in ranges.
    """
    return [(url, status) for url in archive_urls(style)
            if (status := head(url)) not in (200, 206)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=VIEWER, help="viewer to read the style from")
    parser.add_argument("--out", type=Path, required=True, help="where to write it")
    parser.add_argument("--plain", action="store_true",
                        help="rewrite pmtiles:// URLs as plain https")
    parser.add_argument("--verify", action="store_true",
                        help="refuse to write a style naming an archive the server lacks")
    parser.add_argument("--channel", default=None,
                        help="Playwright browser channel, e.g. chrome, to use an installed browser")
    parser.add_argument("--repoint", default=None, metavar="STYLE_URL",
                        help="re-point this published style instead of exporting the viewer's")
    parser.add_argument("--manifest", default=None, metavar="URL",
                        help="the versions.json --repoint reads; archives resolve beside it")
    args = parser.parse_args()
    if bool(args.repoint) != bool(args.manifest):
        parser.error("--repoint and --manifest go together")

    if args.repoint:
        style = repoint(_read_json(args.repoint), _read_json(args.manifest),
                        args.manifest.rsplit("/", 1)[0])
    else:
        style = trim(export(args.url, channel=args.channel), args.plain)
    if args.verify:
        archives = archive_urls(style)
        missing = verify_sources(style)
        if missing:
            for url, status in missing:
                print(f"  {status}  {url}", file=sys.stderr)
            raise SystemExit(f"{len(missing)} of {len(archives)} archives are not on the "
                             f"server; the style was not written")
        print(f"{len(archives)} archives verified on the server")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(style['layers'])} layers, {len(style['sources'])} sources "
          f"→ {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
