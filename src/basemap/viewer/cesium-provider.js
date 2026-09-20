// A CesiumJS imagery provider for our geographic PMTiles archives.
//
// Cesium's GeographicTilingScheme is exactly the grid `oceanbase run globe`
// renders: two square tiles at level 0, four at level 1, reaching 90 degrees
// at every level. So the archives drop straight onto a globe, poles included,
// with no tile server in between -- the browser reads byte ranges from the
// object store the same way the 2D viewer does.
//
// The one thing a caller cannot guess is the zoom offset. PMTiles orders tiles
// on a Hilbert curve over a square grid and refuses any x past 2**z, so a
// geographic pyramid, twice as wide as it is tall, is stored one zoom down --
// see docs/pipeline/globe.md. The archive records the offset in its metadata
// and this provider applies it, so callers work in Cesium's levels throughout.
//
//   import { Viewer } from 'cesium';
//   import { OceanBasemapProvider } from './cesium-provider.js';
//
//   const viewer = new Viewer('globe', { baseLayer: false });
//   for (const name of ['land_geo', 'bathy_geo']) {
//     const provider = await OceanBasemapProvider.fromUrl(
//       `https://oceanstream-bucket.s3.waw3-1.cloudferro.com/basemap/tiles/${name}.pmtiles`
//     );
//     viewer.imageryLayers.addImageryProvider(provider);
//   }
//
// Add land_geo first: the sea tiles carry an alpha channel that stops at the
// coastline, so they compose over the land rather than replacing it.

import { Credit, Event, GeographicTilingScheme } from 'cesium';
import { PMTiles } from 'pmtiles';

export class OceanBasemapProvider {
  /**
   * Open an archive and read its header before constructing the provider.
   * Cesium's own providers use this `fromUrl` shape, because a constructor
   * cannot await and Cesium no longer supports the old `ready` dance.
   */
  static async fromUrl(url, options = {}) {
    const archive = new PMTiles(url);
    const [header, metadata] = await Promise.all([archive.getHeader(), archive.getMetadata()]);
    return new OceanBasemapProvider(archive, header, metadata ?? {}, options);
  }

  constructor(archive, header, metadata, options = {}) {
    const scheme = metadata.scheme ?? 'mercator';
    if (scheme !== 'geographic') {
      // Loading a Mercator archive here would place every tile wrongly, and
      // silently: the tiles decode, they just belong somewhere else.
      throw new Error(
        `${scheme} archive cannot be drawn on a geographic tiling scheme; ` +
        'build it with `oceanbase run globe`'
      );
    }
    this._archive = archive;
    // The rendered level is the stored level minus this (see the file header).
    this._offset = Number(metadata.zoom_offset ?? 0);
    this._cache = new Map();

    this._tilingScheme = new GeographicTilingScheme();
    this._rectangle = this._tilingScheme.rectangle;
    this._errorEvent = new Event();
    this._credit = options.credit
      ? new Credit(options.credit, true)
      : metadata.attribution
        ? new Credit(metadata.attribution, true)
        : undefined;
    this._minimumLevel = Math.max(0, header.minZoom - this._offset);
    this._maximumLevel = Math.max(this._minimumLevel, header.maxZoom - this._offset);
  }

  get tileWidth() { return 256; }
  get tileHeight() { return 256; }
  get minimumLevel() { return this._minimumLevel; }
  get maximumLevel() { return this._maximumLevel; }
  get tilingScheme() { return this._tilingScheme; }
  get rectangle() { return this._rectangle; }
  get tileDiscardPolicy() { return undefined; }
  get errorEvent() { return this._errorEvent; }
  get credit() { return this._credit; }
  get hasAlphaChannel() { return true; }
  get proxy() { return undefined; }

  getTileCredits() { return []; }

  /** No vector features in a rendered raster, so nothing to pick. */
  pickFeatures() { return undefined; }

  /** One transparent pixel, made once and reused for every gap. */
  _blank() {
    this._blankTile ??= this._decode(
      new Uint8Array([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f,
        0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0x60,
        0x00, 0x02, 0x00, 0x00, 0x05, 0x00, 0x01, 0xe2, 0x26, 0x05, 0x9b, 0x00, 0x00, 0x00, 0x00,
        0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
      ]),
      'image/png',
    );
    return this._blankTile;
  }

  /**
   * Decode tile bytes into something Cesium can upload as a texture.
   *
   * An HTMLImageElement rather than an ImageBitmap, deliberately. Cesium
   * builds its own bitmaps with `imageOrientation: "flipY"` and then uploads
   * them without flipping again; a bitmap made the ordinary way arrives the
   * other way up, and every tile is drawn upside down -- which on a globe
   * reads as data in the wrong place rather than as an obvious mistake.
   */
  async _decode(bytes, type) {
    const url = URL.createObjectURL(new Blob([bytes], { type }));
    try {
      const image = new Image();
      image.src = url;
      await image.decode();
      return image;
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  async requestImage(x, y, level) {
    if (level > this._maximumLevel) return undefined;
    const key = `${level}/${x}/${y}`;
    if (this._cache.has(key)) return this._cache.get(key);

    const tile = await this._archive.getZxy(level + this._offset, x, y);
    // Both archives are sparse: the sea one holds no tile where there is only
    // land, the land one none where there is only sea. A missing tile has to
    // come back transparent rather than undefined, because Cesium reads
    // undefined as "not available at this level" and upsamples the nearest
    // ancestor to fill the hole. Over Antarctica the nearest ancestor with any
    // sea in it is three levels up, so the whole interior was painted with
    // stretched ocean, radiating from the pole.
    if (!tile?.data) return this._blank();

    const image = await this._decode(tile.data, 'image/webp');
    // Cesium asks for the same tile repeatedly while a camera settles.
    if (this._cache.size > 256) this._cache.clear();
    this._cache.set(key, image);
    return image;
  }
}
