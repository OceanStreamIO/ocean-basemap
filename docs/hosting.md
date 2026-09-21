# Hosting and caching

Everything the basemap serves is a file in object storage. There is no tile server, no
database and no application behind it: the browser reads byte ranges out of PMTiles
archives, and the viewer is three static files. What follows is the arrangement as it
stands, measured rather than assumed, and what would need thinking about before it
carries much more traffic than it does now.

## What is published, and where

| | |
|---|---|
| Store | CloudFerro RGW (Ceph), `s3.waw3-1.cloudferro.com`, region `waw3-1` |
| Bucket | `oceanstream-bucket`, prefix `basemap/` |
| Addressing | virtual-host (`https://oceanstream-bucket.s3.waw3-1.cloudferro.com/…`) |
| Published | **20 archives, 16.9 GB**, plus the style, the docs and the viewer |
| Viewer | `basemap-viewer` Deployment, stock nginx, assets from a ConfigMap |
| Private | `basemap-build/`, 58 GB: build inputs, outputs awaiting promotion, and backups |

The whole `basemap/` prefix is public-read. Nothing else in the bucket is.

## Why this works without a server

A PMTiles archive is a single file with its own index, so a client fetches the header,
then the directory pages it needs, then the tiles — all as HTTP range requests. Three
things have to hold for that, and all three are checked above:

```
HTTP/1.1 206 Partial Content
content-range: bytes 0-15/200540053
access-control-allow-origin: *
access-control-expose-headers: Accept-Ranges,Content-Range,Content-Length,ETag
```

Without `Content-Range` exposed, the browser hides it from JavaScript and pmtiles.js
cannot read the archive at all — the failure is silent and looks like an empty map. The
bucket's CORS rules are kept at `~/work/deploy/s3-basemap/`, **with the pre-change backup
beside them**: EDITO tooling depends on the first rule, so rules are appended, never
replaced.

## Cache lifetimes

| What | Lifetime | Why |
|---|---|---|
| `tiles/*.pmtiles` | 5 minutes | the archives change in place under a fixed name |
| `style.json` | 5 minutes | follows the viewer |
| `style/v1.json` | 1 day | pinned; a breaking change becomes `v2.json` |
| `docs/*` | 10 minutes | |
| rendered tile directories | 1 week | regenerated wholesale, never edited |

Five minutes is deliberately short and deliberately temporary. An archive republished
under the same name is invisible to anyone holding the old one until their copy expires,
and an hour of that is long enough to conclude that work which was published and verified
never landed — which is exactly what happened here. While the map is still changing
weekly, five minutes is the right trade; the cost is conditional requests that come back
304 unless the archive really changed.

When it settles, the better shape is the one the style already uses: publish archives at
a versioned path, cache those for a year, and let the unversioned name be a short-lived
pointer.

## Is it as fast as it can be?

No, and only one of the three reasons is about Poland. Measured from inside CloudFerro's
own datacentre, so none of this is distance:

| | |
|---|---|
| Protocol | **HTTP/1.1**. The endpoint refuses HTTP/2 even when asked for it. |
| One cold map view | **122 requests, 1.7 MB** — 14 KB a request |
| A warm range read | ~4 ms |
| A cold connection | 30 ms, of which 22 ms is the TLS handshake |

The shape of the problem is request count, not bandwidth. Under HTTP/1.1 a browser opens
at most six connections to one origin, so 122 requests queue about twenty deep. In
Warsaw that is milliseconds; from Oslo, at ~30 ms of round trip, it is most of a second
before the map settles, and every pan asks for more.

**A CDN is the fix, and the reason is multiplexing more than proximity.** HTTP/2 puts
all 122 requests on one connection with no six-at-a-time cap, and terminating TLS at an
edge turns a 3-round-trip handshake into a local one. Caching at the edge is the smaller
win, though it is real for a second visitor to the same water.

Choosing one, the constraint that rules most of them out: **eight archives are larger
than 512 MB**, up to 5.7 GB, and a CDN has to cache *ranges* of them rather than the
whole object.

| | Large objects | Notes |
|---|---|---|
| CloudFront | 30 GB | range caching, HTTP/3, ~$0.085/GB out of Europe |
| bunny.net | multi-GB | cheapest per GB; range support |
| Cloudflare free/pro | **512 MB cap** | the eight big archives would bypass the cache; still gets HTTP/2 and edge TLS |

Any of them needs a domain we control in front of the bucket, and the five-minute cache
becomes a purge problem — which is the third argument for versioned archive paths, since
an immutable URL never needs purging and can be cached for a year.

### What is already done

The viewer's own files are stored gzipped, because the endpoint compresses nothing and
cannot be told to: **92 KB of JavaScript, CSS and HTML is 27 KB on the wire**, a 71% cut
on every cold load. Only those files — the archives are compressed internally, and
`style.json` is read by programs as well as browsers, which may not ask for gzip.

There is a trap in doing that against this endpoint. Uploaded the obvious way, the AWS
CLI streams the object and appends its own encoding, so it comes back as
`content-encoding: gzip,aws-chunked`; no browser can decode that, and the symptom is a
page that loads with a script that never runs and no error to say why. Setting
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required` is what stops it.

## What would need attention before heavy use

**There is no CDN.** Every range request goes to Warsaw. For a European audience that is
fine — the archives are read a few kilobytes at a time and the browser caches aggressively
— but a reader in Sydney pays the round trip on every directory page. A CDN in front of
the bucket would fix it, at the cost of having to purge on republish; that is another
argument for versioned paths, which never need purging.

**Egress is unmetered here but not free everywhere.** A single session panning across
Europe at z12 pulls a few megabytes. The archives are 16.9 GB in total but nobody
downloads them whole; the shape of the cost is per-tile, not per-archive.

**One region, one bucket.** There is no replica. If `waw3-1` is unavailable the map is
unavailable. The archives are reproducible from the pipeline, but that is hours, not
minutes.

**Nothing pins the tiles.** A reader can pin the *style* to `v1.json`, but its sources
point at unversioned archives, so the data underneath them can still change. Anyone who
needs a fixed map should take a copy of the archives they care about.

**The private prefix is 58 GB**, most of it pre-change backups kept while verifying each
rebuild. They cost storage and nothing else; delete them once a release is accepted.
