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
| Published | **20 archives, 18.2 GB**, each also under an immutable versioned name |
| Read through | CloudFront, `d3r4ksf8ere900.cloudfront.net` (HTTP/2 and HTTP/3) |
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
| `tiles/v/<name>-<digest>.pmtiles` | **1 year, immutable** | the name contains the bytes' digest, so it is never reused |
| `tiles/<name>.pmtiles` | 5 minutes | a pointer at the current build, kept so older links work |
| `tiles/versions.json` | 5 minutes | the map from name to digest |
| `style.json` | 5 minutes | follows the viewer |
| `style/v1.json` | 1 day | pinned; a breaking change becomes `v2.json` |
| `docs/*` | 10 minutes | |
| rendered tile directories | 1 week | regenerated wholesale, never edited |

**Every archive is published twice.** Once under its fixed name, and once under a name
containing a digest of its own bytes, which is what the viewer and the style actually
read.

That is not tidiness. An archive republished in place keeps its URL, so a browser holding
a PMTiles *directory page* from the old file goes on reading byte ranges out of the new
one, and what it draws is neither: flat, straight-edged holes across the map. No cache
lifetime ends it, because nothing about the URL changed — a tab can sit like that
indefinitely. It happened here, and it looked convincingly like a data defect: the flat
colour turned out to be the *previous* build's shallow-water colour, in a session whose
archives had already been replaced underneath it.

Immutable names also pay for themselves twice over. They never need a CDN invalidation,
which is CloudFront's one metered operation on this workload. And a style holding one is
genuinely pinned: before this, `style/v1.json` pinned the *style* while its sources
pointed at archives that could change under it.

`deploy/edito/publish-versions.sh` makes the copies and writes `versions.json`; both
viewer deploys bake that map into `config.js`. The digest comes from the object's ETag
and size rather than from reading the archive — RGW computes the ETag on upload and it
changes when the content does, and hashing 18 GB back through the pod to learn the same
thing would cost minutes and egress. It is a cache key, not a checksum anyone verifies.

The cost is storage: the `basemap/tiles` prefix roughly doubles, 18 GB to 36 GB, and each
superseded version kept adds its own size again. `--prune N` keeps the newest N
superseded versions per archive and deletes the rest; it is never the default, because
anyone who pinned an older one loses it.

## Is it as fast as it can be?

**It is now behind CloudFront** — `https://d3r4ksf8ere900.cloudfront.net/basemap`, which
is where the viewer and the published style read from. What follows is why, measured
against the bucket directly from inside CloudFerro's own datacentre, so none of it is
distance:

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

### What a CDN would cost

Measured, not guessed. One session — load at z9, pan twice, zoom to 12, pan, zoom to 14,
zoom back out to 7 — is **454 requests and 9.6 MB** from the bucket. The cold load alone
is 126 requests and 1.8 MB; zooming out is the expensive move, because coarse raster
tiles cover a lot of ground each.

At Europe list prices, with CloudFront's always-free tier (1 TB out, 10 M requests a
month) and bunny.net's standard network:

| Sessions a month | GB | Requests | CloudFront | bunny.net |
|---|---|---|---|---|
| 1,000 | 9 | 0.45 M | $0 | $0 |
| 10,000 | 94 | 4.5 M | $0 | $1 |
| 50,000 | 468 | 23 M | $15 | $5 |
| 100,000 | 937 | 45 M | $42 | $9 |
| 500,000 | 4,683 | 227 M | $571 | $47 |
| 1,000,000 | 9,365 | 454 M | $1,242 | $94 |

**CloudFront is free up to about 22,000 sessions a month**, and what runs out first is
requests, not bytes: the free tier's 1 TB would carry 109,000 sessions, its 10 M requests
only 22,000. Past that a session costs about a seventh of a cent, **41% of which is the
per-request charge** rather than the data.

That split is the whole story of which CDN to pick. This workload is a great many small
reads — 9.6 MB in 454 requests, 21 KB each — because that is how PMTiles works: header,
directory, tile. A CDN that charges per request is being paid for the shape of the file
format. bunny.net charges for bytes only, which is why it comes out roughly ten times
cheaper at every size above the free tier, and it handles multi-GB objects.

So: **CloudFront if traffic stays in five figures** — it is free there, and it is the
easier thing to justify inside an AWS account that already exists. **bunny.net if this
becomes popular**, where the difference is $94 against $1,242 a month at a million
sessions.

Two things the table does not include. Origin egress from CloudFerro to the CDN is
charged by CloudFerro, not by the CDN, and a good cache hit ratio is what keeps it small.
And invalidations: CloudFront gives 1,000 paths a month free and charges beyond that,
which versioned archive paths avoid entirely, since an immutable URL is never
invalidated.

These are list prices as of early 2026 and they move. Check them before committing.

### Putting CloudFront in front of it

Two scripts, in `deploy/cdn/`:

```sh
./deploy/cdn/create-distribution.sh          # or --dry-run to see the config
./deploy/cdn/switch-origin.sh https://dxxxxxxxx.cloudfront.net/basemap
```

The first creates the distribution against CloudFerro as a custom origin and refuses to
finish unless the result speaks HTTP/2 or HTTP/3, answers range requests, and returns
both `Access-Control-Allow-Origin` and `Content-Range` in
`Access-Control-Expose-Headers` when asked with an `Origin` header — the things that have
to hold or the map goes blank. It asks the way a browser does, which the first version of
the check did not: a `curl -I` with no `Origin` gets no CORS header from RGW even when
everything is configured correctly. The second moves
the published map onto it: the viewer on EDITO, the static viewer on the bucket, and the
style, which is re-exported from the running map rather than edited, so it cannot
disagree with what the viewer is actually doing. `--back` returns everything to the
bucket, which is the rollback.

Nothing in the AWS account is created but the distribution. The origin is CloudFerro, so
there is no bucket to copy into, no role, and no certificate unless a custom domain is
wanted — that needs one in ACM, in `us-east-1`, and a DNS record.

Three settings are worth knowing about, since they are easy to get wrong:

- **The bucket stays in the origin hostname.** RGW resolves the bucket from the Host
  header, and CloudFront sends the origin's own domain, so it works without a custom
  header.
- **`Origin` is deliberately not in the cache key**, and that is exactly why **CORS has
  to be added at the edge**. RGW answers `Access-Control-Allow-Origin` only when the
  request carries an `Origin` header; CloudFront does not forward one, because the answer
  is `*` for everybody and forwarding it would split the cache by requesting site for
  nothing. Neither forwarded nor added, no cached response carries CORS at all and every
  browser refuses the archive — which is what the first distribution did. A response
  headers policy (`deploy/cdn/response-headers-policy.json`) adds it, and exposes
  `Content-Range`: without that the browser hides the header from JavaScript, pmtiles.js
  cannot read an archive, and the map is simply blank with nothing in the console.
- **Range requests need no configuration at all.** CloudFront fetches and caches the
  object in chunks and serves ranges from them, which is exactly how PMTiles reads.
- **The AWS credentials are not the ones in the environment.** This pod exports
  CloudFerro's `AWS_ACCESS_KEY_ID`, `AWS_ENDPOINT_URL` and `AWS_DEFAULT_REGION` into every
  shell, and those outrank `~/.aws/credentials`. Left alone, every CloudFront call goes to
  Warsaw with a Ceph key and fails as `Unknown`. The script strips that environment for
  the AWS-proper calls and only those.

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

**Egress is unmetered here but not free everywhere.** A single session panning across
Europe at z12 pulls a few megabytes. The archives are 18.2 GB in total but nobody
downloads them whole; the shape of the cost is per-tile, not per-archive. Now that
CloudFront is in front, the origin only pays for cache misses.

**The AWS account is being used as root.** The credentials configured here are the
account root, which is more than `cloudfront:*` needs and cannot be rotated
independently. An IAM user scoped to CloudFront would be the right thing before anyone
else uses this.

**One region, one bucket.** There is no replica. If `waw3-1` is unavailable the map is
unavailable. The archives are reproducible from the pipeline, but that is hours, not
minutes.

**The private prefix is 58 GB**, most of it pre-change backups kept while verifying each
rebuild. They cost storage and nothing else; delete them once a release is accepted.
