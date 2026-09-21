# Publishing

Where the basemap is published, and what is left to do to put it in the EDITO catalogue.

## What is live

| | URL |
|---|---|
| Viewer | <https://project-oceanstream-basemap.lab.dive.edito.eu> |
| Globe | <https://project-oceanstream-basemap.lab.dive.edito.eu/static/globe.html> |
| Style | `https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/style.json` |
| Style, pinned | `…/basemap/style/v1.json` |
| Documentation | `…/basemap/docs/index.html` |
| Archives | `…/basemap/tiles/<name>.pmtiles` |
| Viewer assets | `…/basemap/viewer/` — what the Helm chart fetches |

All of it is public and needs no credentials. [Hosting and caching](hosting.md) covers
the arrangement; [Using the Viewer](viewer.md) covers the style URL and overlays.

## The EDITO catalogue entry

The catalogue is a set of Helm charts in Mercator Ocean's GitLab, and a service is a
chart in it. The chart is written and tested: `deploy/edito/chart/ocean-basemap`.

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
