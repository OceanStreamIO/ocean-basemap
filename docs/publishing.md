# Publishing

Where the basemap is published, and what is left to do to put it in the EDITO catalogue.

## What is live

| | URL |
|---|---|
| Viewer, on the CDN | <https://d3r4ksf8ere900.cloudfront.net/basemap/viewer/index.html> |
| Viewer, on EDITO | <https://project-oceanstream-basemap.lab.dive.edito.eu> |
| Globe | <https://project-oceanstream-basemap.lab.dive.edito.eu/static/globe.html> |
| Style | `https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/style.json` |
| Style, pinned | `…/basemap/style/v1.json` |
| Documentation | `…/basemap/docs/index.html` |
| Archives | `…/basemap/tiles/<name>.pmtiles`, current build |
| Archives, pinned | `…/basemap/tiles/v/<name>-<digest>.pmtiles`, never reused |
| Which digest is current | `…/basemap/tiles/versions.json` |
| Static viewer | `…/basemap/viewer/index.html` — no server involved |
| CDN | `https://d3r4ksf8ere900.cloudfront.net/basemap` — same paths, HTTP/2 |

The viewer and the published style read the **versioned** archive names through the CDN,
so a style saved today keeps drawing today's data however often the map is rebuilt. The
unversioned names still work and still track the current build; see
[Hosting and caching](hosting.md#cache-lifetimes).

All of it is public and needs no credentials. [Hosting and caching](hosting.md) covers
the arrangement; [Using the Viewer](viewer.md) covers the style URL and overlays.

## Using it anonymously

Nothing here asks who you are. The bucket is public-read with `Access-Control-Allow-Origin: *`,
and the viewer's ingress carries no authentication, so all of this works from any
browser, any script, any other site, with no account and no key:

| Want | Use | Depends on |
|---|---|---|
| The map in your own application | the [style URL](viewer.md#using-the-basemap-in-your-own-map) | the CDN |
| A map to look at, nothing to install | `https://d3r4ksf8ere900.cloudfront.net/basemap/viewer/index.html` | the CDN |
| The same, straight from the bucket | `…/basemap/viewer/index.html` | the bucket |
| The same, on EDITO | <https://project-oceanstream-basemap.lab.dive.edito.eu> | our pod |
| Raw data | `…/basemap/tiles/<name>.pmtiles` | the bucket |

**Prefer the CDN copy.** CloudFront fronts the whole `basemap/` prefix, so the viewer's
own five files come through it as well as the archives: the page and the tiles arrive
from the same edge, over HTTP/2, with nothing of ours in the path. The bucket URL is the
same files one hop further back, and is the fallback if the distribution is ever removed.

**Prefer the bucket to the pod.** The viewer on `lab.dive.edito.eu` runs in this
project's namespace: it survives as long as the namespace does, and EDITO warns that
services can be removed during platform maintenance. The copy on the bucket is five
static files next to the archives they read — there is no server to stop, and it fails
only if object storage does.

That copy exists because the viewer's asset paths are relative rather than absolute. A
page that asks for `/static/app.js` can only be served from a site's root; asking for
`static/app.js` works from the root, from a prefix, from a subdirectory, from anywhere.
The same five files are therefore the deployment, the standalone copy and what the Helm
chart fetches, and a test keeps them that way. `deploy/edito/publish-viewer.sh` puts
them there.

## The EDITO catalogue entry

The catalogue is a set of Helm charts in Mercator Ocean's GitLab, and a service is a
chart in it. The chart is written and tested: `deploy/edito/chart/ocean-basemap`.

### What launching it actually gives someone

Worth being clear, because for most readers the answer is *nothing they need*. The map
is already public and anonymous; the tiles come from the same bucket either way. What an
instance adds is narrow:

- **Their own copy of the archives.** `basemap.tileBase` points the viewer somewhere
  else, so a user can render their own PMTiles — their survey, their region, their
  edit of ours — in a viewer that already knows how to draw them.
- **A URL in their own space**, which does not change when we redeploy ours.
- **Discoverability.** A tile in the catalogue is how people on EDITO find things at
  all, which is the honest reason to submit one.

Anyone who only wants to *look at* the basemap, or to use it in their own page, should
use the style URL or the static viewer and launch nothing.

It has no image of its own. The viewer is static, so the chart runs stock unprivileged
nginx and an init container fetches the five files from the public bucket at start-up.
That keeps a container registry out of the picture — nothing of ours to build, publish,
scan or keep patched — and a change to the map reaches every instance without a chart
release. `values.yaml` carries the two bases, so anyone can point an instance at their
own copy of the archives.

Tested by installing it into `project-oceanstream`, loading the result in a browser (46
layers, 19 panel groups, no errors), and uninstalling it again.

One thing the chart gets right that is easy to get wrong here: **the init container
declares `requests.cpu` and `requests.memory` too.** The namespace quota counts init
containers, and EDITO has no LimitRange to supply defaults, so without them the
ReplicaSet is refused — `must specify requests.cpu,requests.memory` — while the
Deployment sits at 0 of 1 and reports nothing. That is what the first install of this
chart did.

### What remains, and who has to do it

Submission needs write access to the contributing repositories, which is granted by
request, not by anything in this repository:

1. **Ask for access.** Mail <support@edito.eu> for write access to
   `gitlab.mercator-ocean.fr/pub/edito-infra/service-playground` (and, if wanted, to be
   added to the support channel).
2. **Clone it** and copy `deploy/edito/chart/ocean-basemap` in as a top-level folder.
   The directory name must match the chart name.
3. **Add an icon.** `Chart.yaml` points at `…/basemap/viewer/icon.png`, which is not
   there yet — the catalogue requires one, "even a generic one". Put it in the bucket
   beside the viewer, or point the field at any stable image.
4. **Open a merge request.** The tile appears in the catalogue once it is merged and the
   chart repository is re-indexed.

Steps 2 and 4 take minutes. Step 1 is the long pole, and it is a mail nobody here can
send on the project's behalf.

### Keeping it truthful afterwards

The chart pins nothing but the nginx image, so the map it shows is whatever the bucket
holds. That is the right default for a basemap that improves weekly, and the wrong one
for anybody who needs a fixed map. If the catalogue tile should show a fixed version,
point `basemap.tileBase` at a versioned prefix and publish archives there — the same
shape the style already uses with `style/v1.json`.
