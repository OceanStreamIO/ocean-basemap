/**
 * Deploy-time overrides for the viewer.
 *
 * Shipped empty so `oceanbase serve` falls back to same-origin /tiles. Hosted
 * deployments replace this file (see deploy/edito/apply.sh) with, e.g.:
 *
 *   window.BASEMAP_TILE_BASE    = 'https://<bucket>.s3.<region>.example.com/basemap';
 *   window.BASEMAP_PMTILES_BASE = 'https://<bucket>.s3.<region>.example.com/basemap/tiles';
 *
 * Both are also settable per-request via ?base= and ?pmtiles=.
 */
