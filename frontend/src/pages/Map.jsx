import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { MapPin, AlertTriangle, ShieldAlert } from "lucide-react";
import { getThreatHex } from "@/lib/threatLevels";
import { useTheme } from "@/context/ThemeContext";

// ---------------------------------------------------------------------------
// BASEMAP STRATEGY (offline-first, graceful, never a blank/"API key" tile)
// ---------------------------------------------------------------------------
// The map picks a basemap in strict precedence, always beneath the tactical
// graticule grid and all overlays (sensor marker, range rings, contact
// markers, popups):
//
//   1. OFFLINE bundled tiles (SOVEREIGN / air-gapped path): if the operator
//      has dropped a local raster tileset under the frontend's public/basemap/
//      directory (described same-origin by /basemap/tiles.json), it is used and
//      the map makes ZERO external requests. This is what the FIELDED
//      AIR-GAPPED APPLIANCE must use. See public/basemap/README.md.
//   2. OSM online tiles (DEMO ONLY): on a networked demo box with no bundled
//      tiles, the map falls back to OpenStreetMap raster so it shows real
//      geography instead of a bare grid. This path reaches the public internet
//      and MUST NOT be relied on by the sovereign appliance (which is why (1)
//      takes precedence and is tried first).
//   3. GRID fallback (guaranteed): a MapLibre `background` fill + GeoJSON
//      graticule, drawn with ZERO assets and ZERO network. If BOTH raster
//      paths are unavailable (air-gapped with no bundled tiles, or OSM blocked/
//      offline) the map simply degrades to this grid -- it NEVER shows an "API
//      key Required" error or a blank bland tile.
//
// Legibility: OSM standard tiles are light and busy, so in DARK mode a
// semi-transparent dark scrim is layered above the raster (below the grid +
// overlays) to keep tactical markers/rings/threat colors readable; in LIGHT
// mode the scrim is transparent and the tiles show through.
// ---------------------------------------------------------------------------

// Tactical basemap fill per theme (from index.css --bg tokens; maplibre paint
// props can't read CSS vars, so the hex is mirrored here per theme).
const BASEMAP_BG = { dark: "#060B14", light: "#DFE4EC" };
// Graticule / grid line stroke per theme.
const GRID_COLOR = { dark: "#17263D", light: "#9FADBF" };
// Same-origin metadata file an operator drops in to enable the offline
// geographic raster basemap (see public/basemap/README.md). Same-origin only.
const OFFLINE_BASEMAP_META_URL = "/basemap/tiles.json";

// DEMO ONLINE basemap: OpenStreetMap raster tiles. Used ONLY when no bundled
// offline tiles are present (see precedence note in the header comment). The
// fielded air-gapped appliance must ship /basemap tiles so this is never hit.
const OSM_TILES = ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"];
const OSM_ATTRIBUTION = "© OpenStreetMap contributors";
// Dark-mode scrim opacity over the (light, busy) raster so tactical overlays
// stay legible; light mode lets the tiles show through.
const SCRIM_OPACITY = { dark: 0.55, light: 0.0 };
const SCRIM_COLOR = "#050810";

// Pick a "nice" graticule spacing (degrees) for the current zoom so the grid
// stays legible (a handful of lines) at any scale from world view to site view.
function niceGridStep(zoom) {
  if (zoom >= 13) return 0.005;
  if (zoom >= 11) return 0.01;
  if (zoom >= 9) return 0.05;
  if (zoom >= 7) return 0.1;
  if (zoom >= 5) return 0.5;
  if (zoom >= 3) return 1;
  return 5;
}

// Build a graticule (FeatureCollection of lat/lon lines) covering the current
// map bounds, padded by one step so lines still fill the viewport after a pan.
// Every 5th line is flagged `major` for a heavier tactical stroke. The step is
// widened if the viewport would otherwise need too many lines (keeps it cheap).
function buildGraticule(bounds, zoom) {
  let step = niceGridStep(zoom);
  const west = bounds.getWest();
  const east = bounds.getEast();
  const south = bounds.getSouth();
  const north = bounds.getNorth();
  // Clamp total line count for very wide viewports.
  let guard = 0;
  while (((east - west) / step + (north - south) / step) > 160 && guard < 12) {
    step *= 2;
    guard += 1;
  }
  const w = Math.floor(west / step) * step - step;
  const e = Math.ceil(east / step) * step + step;
  const s = Math.max(Math.floor(south / step) * step - step, -85);
  const n = Math.min(Math.ceil(north / step) * step + step, 85);
  const eps = step / 1e6;
  const features = [];
  for (let x = w; x <= e + eps; x += step) {
    const idx = Math.round(x / step);
    features.push({
      type: "Feature",
      properties: { major: idx % 5 === 0 },
      geometry: { type: "LineString", coordinates: [[x, s], [x, n]] },
    });
  }
  for (let y = s; y <= n + eps; y += step) {
    const idx = Math.round(y / step);
    features.push({
      type: "Feature",
      properties: { major: idx % 5 === 0 },
      geometry: { type: "LineString", coordinates: [[w, y], [e, y]] },
    });
  }
  return { type: "FeatureCollection", features };
}

// Context ring stroke (the non-threat reference rings) per theme.
const CTX_RING_COLOR = { dark: "#1E2A3F", light: "#94A3B8" };

// ---------------------------------------------------------------------------
// HONESTY NOTE (read before touching this file):
//
// This map CANNOT plot real drone/contact positions. Absolute lat/lon for a
// detection would require bearing (direction from the sensor) + range, but
// backend/server.py's DetectionIngestBody.bearing_deg is ALWAYS a 0.0
// placeholder -- field-bridge/hackrf_rx.py has no direction-finding antenna
// array, so every detection dict it emits hardcodes "bearing_deg": 0.0.
// distance_m is at best a coarse RSSI path-loss ESTIMATE (distance_estimated
// flag), never a calibrated range.
//
// So instead of fabricating a pin position (which would be actively
// misleading to an operator), this view shows:
//   1. The sensor's own fixed, configured position (SENSOR_LAT/SENSOR_LON
//      env vars on the backend) -- or an explicit "not configured" banner
//      if those aren't set.
//   2. Range-only rings around the sensor for each ACTIVE contact, sized by
//      its measured/estimated distance_m, with NO angular placement implying
//      a direction that isn't known. Each contact also gets a labeled marker
//      placed at a fixed reference angle purely so multiple simultaneous
//      contacts don't overlap -- this angle is a UI layout convenience, not
//      a bearing measurement, and the popup says so explicitly.
//
// If real bearing/DF hardware is ever added, replace the ring-only renderer
// below with true polar-to-cartesian pin placement (bearing+distance from
// SENSOR_LAT/SENSOR_LON), gated on `bearing_available` from
// GET /sensor/position and a per-detection "this bearing is real" flag.
// ---------------------------------------------------------------------------

// Metres -> approximate degrees offset at a given latitude (WGS84 sphere
// approximation, plenty accurate for a UI ring at few-km scale).
function metersToLatLonOffset(lat, meters) {
  const dLat = meters / 111320;
  const dLon = meters / (111320 * Math.cos((lat * Math.PI) / 180));
  return { dLat, dLon };
}

function ringGeoJSON(centerLat, centerLon, radiusM, points = 72) {
  const coords = [];
  for (let i = 0; i <= points; i++) {
    const theta = (i / points) * 2 * Math.PI;
    const { dLat, dLon } = metersToLatLonOffset(centerLat, radiusM);
    coords.push([centerLon + dLon * Math.cos(theta), centerLat + dLat * Math.sin(theta)]);
  }
  return { type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} };
}

export default function MapView() {
  const { theme } = useTheme();
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const [sensor, setSensor] = useState(null);
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const markersRef = useRef([]);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const load = async (cancelled) => {
    try {
      const [{ data: sensorData }, { data: detData }] = await Promise.all([
        api.get("/sensor/position"),
        api.get("/detections"),
      ]);
      if (cancelled?.current) return;
      setSensor(sensorData);
      setDetections(detData || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch (e) {
      if (cancelled?.current) return;
      setConsecutiveFailures((n) => n + 1);
      toast.error("Failed to load map data", { description: formatApiError(e) });
    } finally {
      if (!cancelled?.current) setLoading(false);
    }
  };

  useEffect(() => {
    const cancelled = { current: false };
    load(cancelled);
    const id = setInterval(() => load(cancelled), 10000);
    return () => { cancelled.current = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const POLL_INTERVAL_MS = 10000;
  const MAX_CONSECUTIVE_FAILURES = 3;
  const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;
  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const monitoringDegraded = staleByAge || staleByFailures || neverSucceeded;

  const activeContacts = useMemo(
    () => detections.filter((d) => d.status === "ACTIVE"),
    [detections]
  );

  const hasSensor = sensor?.configured;

  // Init map once we know whether/where the sensor is.
  useEffect(() => {
    if (!mapContainer.current || mapRef.current || sensor === null) return;

    const center = hasSensor ? [sensor.lon, sensor.lat] : [78.9629, 20.5937]; // India centroid fallback
    const zoom = hasSensor ? 13 : 3.5;

    const map = new maplibregl.Map({
      container: mapContainer.current,
      // Fully self-contained style: no external tile/glyph/sprite/style URL.
      // Just a tactical background fill; the grid + optional offline raster are
      // added on load below. This is what kills the "API key Required" tiles.
      style: {
        version: 8,
        sources: {},
        layers: [
          {
            id: "tactical-bg",
            type: "background",
            paint: { "background-color": BASEMAP_BG[theme] || BASEMAP_BG.dark },
          },
        ],
      },
      center,
      zoom,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    // Draw / refresh the coordinate graticule to cover the current viewport.
    const updateGrid = () => {
      if (!map.getSource("grid")) return;
      map.getSource("grid").setData(buildGraticule(map.getBounds(), map.getZoom()));
    };

    // Insert a raster basemap BENEATH the given beforeId, offline-first then
    // OSM (demo). Returns nothing; on total failure the grid remains.
    const setupBasemap = async () => {
      const beforeRaster = () =>
        map.getLayer("basemap-scrim") ? "basemap-scrim" : (map.getLayer("grid") ? "grid" : undefined);

      // (1) OFFLINE bundled tiles (sovereign / air-gapped). Same-origin probe;
      // if present we use them and NEVER touch the network.
      try {
        const res = await fetch(OFFLINE_BASEMAP_META_URL, { cache: "no-store" });
        if (res.ok && mapRef.current) {
          const meta = await res.json();
          if (!map.getSource("offline-basemap")) {
            map.addSource("offline-basemap", {
              type: "raster",
              tiles: meta.tiles || ["/basemap/{z}/{x}/{y}.png"],
              tileSize: meta.tileSize || 256,
              minzoom: meta.minzoom ?? 0,
              maxzoom: meta.maxzoom ?? 19,
              attribution: meta.attribution || "",
            });
            map.addLayer(
              { id: "offline-basemap", type: "raster", source: "offline-basemap", paint: { "raster-opacity": 1 } },
              beforeRaster()
            );
          }
          return; // offline tiles win -> do NOT fall through to online OSM
        }
      } catch {
        /* no offline tiles present -> try OSM online below */
      }

      if (!mapRef.current) return;

      // (2) OSM online tiles (DEMO ONLY). If these fail to load (offline /
      // blocked) maplibre just renders no raster and the grid + scrim remain --
      // it never shows an "API key Required" error or a blank bland tile.
      try {
        if (!map.getSource("osm-basemap")) {
          map.addSource("osm-basemap", {
            type: "raster",
            tiles: OSM_TILES,
            tileSize: 256,
            minzoom: 0,
            maxzoom: 19,
            attribution: OSM_ATTRIBUTION, // rendered by the default AttributionControl
          });
          map.addLayer(
            { id: "osm-basemap", type: "raster", source: "osm-basemap", paint: { "raster-opacity": 1 } },
            beforeRaster()
          );
        }
      } catch {
        /* stay on the grid */
      }
    };

    const onLoad = () => {
      map.addSource("grid", { type: "geojson", data: buildGraticule(map.getBounds(), map.getZoom()) });
      map.addLayer({
        id: "grid",
        type: "line",
        source: "grid",
        paint: {
          "line-color": GRID_COLOR[theme] || GRID_COLOR.dark,
          "line-width": ["case", ["get", "major"], 1.1, 0.5],
          "line-opacity": ["case", ["get", "major"], 0.9, 0.45],
        },
      });
      // Dark-mode legibility scrim: sits directly beneath the grid and above
      // whatever raster loads, so a busy light basemap doesn't wash out the
      // tactical overlays. Transparent in light mode (toggled on theme flip).
      map.addLayer(
        {
          id: "basemap-scrim",
          type: "background",
          paint: {
            "background-color": SCRIM_COLOR,
            "background-opacity": SCRIM_OPACITY[theme] ?? SCRIM_OPACITY.dark,
          },
        },
        "grid"
      );
      setupBasemap();
    };
    map.on("load", onLoad);
    map.on("moveend", updateGrid);

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sensor === null]);

  // Recolor the tactical basemap (background fill + graticule + optional
  // offline-raster dimming + reference-ring stroke) in place when the theme
  // flips, without tearing down the map (keeps zoom/pan). No network involved.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (map.getLayer("tactical-bg")) {
        map.setPaintProperty("tactical-bg", "background-color", BASEMAP_BG[theme] || BASEMAP_BG.dark);
      }
      if (map.getLayer("grid")) {
        map.setPaintProperty("grid", "line-color", GRID_COLOR[theme] || GRID_COLOR.dark);
      }
      // Legibility scrim: opaque-ish in dark to tame the busy OSM raster,
      // transparent in light so the tiles show through.
      if (map.getLayer("basemap-scrim")) {
        map.setPaintProperty("basemap-scrim", "background-opacity", SCRIM_OPACITY[theme] ?? SCRIM_OPACITY.dark);
      }
      (map.getStyle()?.layers || []).forEach((l) => {
        if (l.id.startsWith("ring-ctx-") && map.getLayer(l.id)) {
          map.setPaintProperty(l.id, "line-color", CTX_RING_COLOR[theme] || CTX_RING_COLOR.dark);
        }
      });
    };
    if (map.isStyleLoaded()) apply();
    else map.once("load", apply);
  }, [theme]);

  // Draw sensor marker + range rings + range-only contact indicators.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !hasSensor) return;
    const THREAT_COLOR = getThreatHex(theme);
    const ctxRingColor = CTX_RING_COLOR[theme] || CTX_RING_COLOR.dark;

    const drawLayer = () => {
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = [];

      // Sensor marker
      const sensorEl = document.createElement("div");
      sensorEl.style.cssText =
        "width:16px;height:16px;border-radius:50%;background:#00F0FF;box-shadow:0 0 12px 4px rgba(0,240,255,0.6);border:2px solid #fff;";
      const sensorMarker = new maplibregl.Marker({ element: sensorEl })
        .setLngLat([sensor.lon, sensor.lat])
        .setPopup(
          new maplibregl.Popup({ offset: 12 }).setHTML(
            `<div style="font-family:monospace;font-size:11px;color:#000;">
               <strong>${sensor.label}</strong><br/>SENSOR (fixed, configured)<br/>
               ${sensor.lat.toFixed(5)}, ${sensor.lon.toFixed(5)}
             </div>`
          )
        )
        .addTo(map);
      markersRef.current.push(sensorMarker);

      // Remove previous ring sources/layers on refresh
      const existing = map.getStyle()?.layers || [];
      existing.forEach((l) => {
        if (l.id.startsWith("ring-")) {
          if (map.getLayer(l.id)) map.removeLayer(l.id);
        }
      });
      Object.keys(map.getStyle()?.sources || {}).forEach((s) => {
        if (s.startsWith("ring-")) map.removeSource(s);
      });

      // Fixed reference rings (context only)
      [500, 1000, 2000].forEach((r) => {
        const id = `ring-ctx-${r}`;
        const gj = ringGeoJSON(sensor.lat, sensor.lon, r);
        if (map.getSource(id)) {
          map.getSource(id).setData(gj);
        } else {
          map.addSource(id, { type: "geojson", data: gj });
          map.addLayer({
            id,
            type: "line",
            source: id,
            paint: { "line-color": ctxRingColor, "line-width": 1, "line-dasharray": [2, 2] },
          });
        }
      });

      // Range-only indicator per active contact: placed at a fixed layout
      // angle (golden-angle spacing) around the sensor purely to avoid
      // overlap -- NOT a bearing measurement. Distance is real (or a labeled
      // RSSI estimate); direction is explicitly "unknown" in the popup.
      activeContacts.forEach((d, idx) => {
        const angle = (idx * 137.5 * Math.PI) / 180; // golden angle, layout only
        const dist = Math.max(d.distance_m || 0, 50);
        const { dLat, dLon } = metersToLatLonOffset(sensor.lat, dist);
        const lon = sensor.lon + dLon * Math.cos(angle);
        const lat = sensor.lat + dLat * Math.sin(angle);

        const ringId = `ring-contact-${d.id}`;
        const gj = ringGeoJSON(sensor.lat, sensor.lon, dist, 96);
        if (map.getSource(ringId)) {
          map.getSource(ringId).setData(gj);
        } else {
          map.addSource(ringId, { type: "geojson", data: gj });
          map.addLayer({
            id: ringId,
            type: "line",
            source: ringId,
            paint: {
              "line-color": THREAT_COLOR[d.threat_level] || "#00F0FF",
              "line-width": 1.5,
              "line-opacity": 0.55,
            },
          });
        }

        const el = document.createElement("div");
        const color = THREAT_COLOR[d.threat_level] || "#00F0FF";
        el.style.cssText = `width:12px;height:12px;border-radius:2px;transform:rotate(45deg);background:${color};box-shadow:0 0 8px 2px ${color}aa;border:1px solid #000;`;
        const distLabel = d.distance_estimated ? `~${d.distance_m}m (est.)` : `${d.distance_m}m`;
        const marker = new maplibregl.Marker({ element: el })
          .setLngLat([lon, lat])
          .setPopup(
            new maplibregl.Popup({ offset: 10 }).setHTML(
              `<div style="font-family:monospace;font-size:11px;color:#000;max-width:220px;">
                 <strong>${d.callsign || d.model}</strong><br/>
                 THREAT: ${d.threat_level}<br/>
                 RANGE: ${distLabel}<br/>
                 <span style="color:#a00;font-weight:bold;">DIRECTION: NOT MEASURED</span><br/>
                 <span style="font-size:10px;color:#555;">Marker angle is layout-only, not a bearing.
                 Ring shows true range from sensor.</span>
               </div>`
            )
          )
          .addTo(map);
        markersRef.current.push(marker);
      });
    };

    if (map.isStyleLoaded()) drawLayer();
    else map.once("load", drawLayer);
  }, [hasSensor, sensor, activeContacts, theme]);

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <MapPin size={12} className="inline mr-2" strokeWidth={1.5} /> Geospatial
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">Tactical Map</h1>
      </div>

      {!loading && !hasSensor && (
        <div
          className="tactical-border px-4 py-3 flex items-center gap-3"
          style={{ background: "rgba(255,214,10,0.06)", borderColor: "var(--accent-warning)" }}
          data-testid="sensor-not-configured-banner"
        >
          <AlertTriangle size={16} style={{ color: "var(--accent-warning)" }} />
          <div className="font-mono text-[11px]" style={{ color: "var(--accent-warning)" }}>
            SENSOR POSITION NOT CONFIGURED — set SENSOR_LAT / SENSOR_LON on the backend to place
            the RX site on the map. Until then, no reliable geospatial reference point exists, so
            no contacts can be shown here.
          </div>
        </div>
      )}

      {!loading && hasSensor && (
        <div
          className="tactical-border px-4 py-2 flex items-center gap-3 flex-wrap"
          style={{ background: monitoringDegraded ? "rgba(255,149,0,0.08)" : "rgba(0,240,255,0.05)" }}
        >
          <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--accent-info)" }}>
            ● SENSOR: {sensor.label} @ {sensor.lat.toFixed(5)}, {sensor.lon.toFixed(5)}
          </span>
          <span className="font-mono text-[10px] text-slate-500">|</span>
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
            Bearing/DF hardware: not present — contacts shown as RANGE RINGS only (direction unknown),
            not as position pins
          </span>
          <span
            data-testid="map-monitoring-degraded"
            role="alert"
            className="ml-auto items-center gap-1 font-mono text-[10px] font-bold uppercase tracking-widest"
            style={{ display: monitoringDegraded ? "flex" : "none", color: "var(--accent-warning)" }}
          >
            <ShieldAlert size={12} strokeWidth={2} />
            MONITORING DEGRADED — contact positions may be stale
          </span>
        </div>
      )}

      <div className="tactical-border overflow-hidden" style={{ background: "var(--bg-surface)" }}>
        <div
          ref={mapContainer}
          data-testid="tactical-map-canvas"
          style={{ width: "100%", height: "70vh" }}
        />
      </div>

      <div className="font-mono text-[10px] text-slate-500 uppercase tracking-widest px-1">
        {activeContacts.length} active contact{activeContacts.length === 1 ? "" : "s"} shown as range-only indicators.
        Square markers mark a fixed display angle for readability — NOT a measured bearing.
      </div>
    </div>
  );
}
