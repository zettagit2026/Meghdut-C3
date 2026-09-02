# Offline geographic basemap slot (sovereign / air-gapped)

The tactical map (`frontend/src/pages/Map.jsx`) runs on an **isolated field
network with no internet route**, so it must never call an external tile/CDN
host. By default it renders a fully self-contained **dark tactical grid**
(a MapLibre `background` layer + a coordinate graticule drawn in-process) that
needs **no assets and no network** — that grid is the guaranteed, demo-safe
basemap and always renders.

This directory is an **optional, best-effort** slot for dropping in a *local*
geographic raster basemap (satellite / OSM-style tiles you have pre-exported)
so the map shows real terrain **under** the grid. It is served same-origin by
the frontend's own nginx, so enabling it still makes **zero external requests**.

## How the map decides

On load, `Map.jsx` does a single same-origin probe:

```
GET /basemap/tiles.json
```

- **File present** → it reads the metadata and adds a raster layer beneath the
  grid using the `tiles` template below.
- **File absent (404) or unreadable** → it silently stays on the grid. No error
  is shown. This is the shipped default.

## To add a real offline tileset later

1. Pre-export an **XYZ raster tile pyramid** for your area of interest (e.g. with
   `gdal2tiles.py`, TileMill/MBUtil, or QGIS QMetaTiles) as PNG files laid out
   as `{z}/{x}/{y}.png`.
2. Copy that pyramid into this directory so tiles resolve at:

   ```
   frontend/public/basemap/{z}/{x}/{y}.png
   ```

3. Create `frontend/public/basemap/tiles.json` describing it, e.g.:

   ```json
   {
     "tiles": ["/basemap/{z}/{x}/{y}.png"],
     "tileSize": 256,
     "minzoom": 8,
     "maxzoom": 16,
     "attribution": "Local offline tileset — <your source>"
   }
   ```

   All fields except `tiles` are optional (sensible defaults are applied). The
   `tiles` URL(s) **must** stay same-origin (a root-relative `/basemap/...`
   path), never an `http(s)://` host — an external URL would break sovereignty.

4. Rebuild the frontend container. Because the tiles are baked into
   `public/`, they ship inside the image and are served offline by nginx.

## TODO / future

- If a single-file bundle is preferred over a directory pyramid, add the
  `pmtiles` package and register its protocol, then point `tiles.json` at a
  `.pmtiles` archive. This was intentionally **not** added here to avoid any
  build risk in the isolated lab — the directory-pyramid path above needs no
  new dependency.
