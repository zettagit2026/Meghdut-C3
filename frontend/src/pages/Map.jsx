import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { MapPin, AlertTriangle, ShieldAlert, Pencil, Hexagon, X, Info } from "lucide-react";
import { getThreatHex } from "@/lib/threatLevels";
import { useTheme } from "@/context/ThemeContext";
import { useAuth } from "@/context/AuthContext";

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
// ZONE PALETTE (Phase D1 — zone/SOP engine).
//
// Five distinct tactical colors, one per zone_type. maplibre-gl paint props
// cannot read CSS custom properties, so — like THREAT_COLOR_HEX_* in
// lib/threatLevels.js — the hex is mirrored here per theme. The dark values
// track the console's --accent-*/--threat-* tokens; the light values are the
// darker AA-safe counterparts used elsewhere in the app.
// ---------------------------------------------------------------------------
const ZONE_TYPES = ["DETECTION", "TRACKING", "ALERT", "MITIGATION", "CLUTTER"];
const ZONE_COLOR_HEX = {
  dark: {
    DETECTION: "#38BDF8", // info cyan
    TRACKING: "#A78BFA",  // violet
    ALERT: "#EAB308",     // amber
    MITIGATION: "#EF4444",// critical red
    CLUTTER: "#64748B",   // slate (low-signal)
  },
  light: {
    DETECTION: "#155E75",
    TRACKING: "#6D28D9",
    ALERT: "#92600A",
    MITIGATION: "#B91C1C",
    CLUTTER: "#475569",
  },
};

// Data-driven maplibre `match` expression: zone_type -> color (CLUTTER as the
// safe fallback for any unknown type).
function zoneColorExpr(pal) {
  return [
    "match", ["get", "zone_type"],
    "DETECTION", pal.DETECTION,
    "TRACKING", pal.TRACKING,
    "ALERT", pal.ALERT,
    "MITIGATION", pal.MITIGATION,
    "CLUTTER", pal.CLUTTER,
    pal.CLUTTER,
  ];
}

// Id of the first contact/context range-ring layer, if any, so zone fill/line
// layers can be inserted BENEATH the rings (and thus beneath the DOM contact
// markers, which always paint above every canvas layer) yet above the
// graticule/scrim. Returns undefined when no ring layer exists yet.
function firstRingLayerId(map) {
  const layers = map.getStyle()?.layers || [];
  const ring = layers.find((l) => l.id.startsWith("ring-"));
  return ring ? ring.id : undefined;
}

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
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const mapContainer = useRef(null);
  const mapRef = useRef(null);
  const [sensor, setSensor] = useState(null);
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const markersRef = useRef([]);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  // ----- Zone state (Phase D1) -------------------------------------------
  const [zones, setZones] = useState([]);
  const [drawMode, setDrawMode] = useState(false);
  const [ringCoords, setRingCoords] = useState([]); // in-progress ring: [lon,lat][]
  const [ringClosed, setRingClosed] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [zoneName, setZoneName] = useState("");
  const [zoneType, setZoneType] = useState("DETECTION");
  const [zoneNotes, setZoneNotes] = useState("");
  const [confirmPhrase, setConfirmPhrase] = useState("");
  const [saving, setSaving] = useState(false);
  // Refs mirror the mutable draw state so the (once-attached) map click
  // handler always reads current values without re-binding on every point.
  const ringCoordsRef = useRef([]);
  const ringClosedRef = useRef(false);
  const ZONE_CONFIRM_PHRASE = "CREATE ZONE";

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

  // Zone list loads on its own lane so a not-yet-live endpoint (404 while
  // Phase A lands) or an empty result degrades cleanly to "no zones" and
  // NEVER fails the sensor/detection load. No fabricated zones.
  const loadZones = async (cancelled) => {
    try {
      const { data } = await api.get("/zones");
      if (cancelled?.current) return;
      const list = Array.isArray(data) ? data : (Array.isArray(data?.zones) ? data.zones : []);
      setZones(list);
    } catch {
      if (cancelled?.current) return;
      setZones([]); // 404 / not-live / empty -> render clean empty case
    }
  };

  useEffect(() => {
    const cancelled = { current: false };
    load(cancelled);
    loadZones(cancelled);
    const id = setInterval(() => load(cancelled), 10000);
    return () => { cancelled.current = true; clearInterval(id); };
  }, []);

  // Keep the click-handler refs in sync with draw state.
  useEffect(() => { ringCoordsRef.current = ringCoords; }, [ringCoords]);
  useEffect(() => { ringClosedRef.current = ringClosed; }, [ringClosed]);

  const resetDraw = useCallback(() => {
    setDrawMode(false);
    setRingCoords([]);
    setRingClosed(false);
    setShowForm(false);
    setZoneName("");
    setZoneType("DETECTION");
    setZoneNotes("");
    setConfirmPhrase("");
  }, []);

  const startDraw = useCallback(() => {
    if (!isCommander) return;
    setRingCoords([]);
    setRingClosed(false);
    setShowForm(false);
    setZoneName("");
    setZoneNotes("");
    setConfirmPhrase("");
    setDrawMode(true);
  }, [isCommander]);

  // Close the in-progress ring: >=3 vertices required (honest hard gate),
  // otherwise surface a clear message and keep drawing.
  const closeRing = useCallback(() => {
    if (ringCoordsRef.current.length < 3) {
      toast.error("A zone needs at least 3 vertices", {
        description: "Click at least three points on the map before closing the ring.",
      });
      return;
    }
    setRingClosed(true);
    setShowForm(true);
  }, []);

  const phraseOk = confirmPhrase.trim() === ZONE_CONFIRM_PHRASE;
  const canSave =
    isCommander && ringClosed && ringCoords.length >= 3 && zoneName.trim().length > 0 && phraseOk && !saving;

  const saveZone = async () => {
    const coords = ringCoordsRef.current;
    if (!isCommander) return;
    if (coords.length < 3) {
      toast.error("A zone needs at least 3 vertices");
      return;
    }
    if (!zoneName.trim()) {
      toast.error("Zone name is required");
      return;
    }
    if (!phraseOk) {
      toast.error(`Type the exact phrase "${ZONE_CONFIRM_PHRASE}" to confirm`);
      return;
    }
    // Build a closed GeoJSON linear ring (first == last vertex).
    const ring = coords.map((c) => [c[0], c[1]]);
    const [fx, fy] = ring[0];
    const [lx, ly] = ring[ring.length - 1];
    if (fx !== lx || fy !== ly) ring.push([fx, fy]);
    setSaving(true);
    try {
      await api.post("/zones", {
        name: zoneName.trim(),
        zone_type: zoneType,
        polygon: { type: "Polygon", coordinates: [ring] },
        notes: zoneNotes.trim() || null,
      });
      toast.success("ZONE CREATED", { description: `${zoneName.trim()} — ${zoneType}` });
      resetDraw();
      loadZones();
    } catch (e) {
      toast.error("Failed to save zone", { description: formatApiError(e) });
    } finally {
      setSaving(false);
    }
  };

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

  // Render saved zones as a single GeoJSON source with data-driven fill+line
  // colored by zone_type. Layers are inserted beneath the contact range rings
  // (and thus beneath the DOM contact markers) but above the graticule/scrim.
  // Empty `zones` => an empty FeatureCollection renders nothing (clean empty).
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const pal = ZONE_COLOR_HEX[theme] || ZONE_COLOR_HEX.dark;
    const render = () => {
      const fc = {
        type: "FeatureCollection",
        features: (zones || [])
          .filter((z) => z?.polygon?.type === "Polygon" && Array.isArray(z.polygon.coordinates))
          .map((z) => ({
            type: "Feature",
            properties: { zone_type: z.zone_type, name: z.name, id: z.id },
            geometry: z.polygon,
          })),
      };
      if (map.getSource("zones")) {
        map.getSource("zones").setData(fc);
      } else {
        const before = firstRingLayerId(map);
        map.addSource("zones", { type: "geojson", data: fc });
        map.addLayer(
          { id: "zones-fill", type: "fill", source: "zones", paint: { "fill-color": zoneColorExpr(pal), "fill-opacity": 0.14 } },
          before
        );
        map.addLayer(
          { id: "zones-line", type: "line", source: "zones", paint: { "line-color": zoneColorExpr(pal), "line-width": 2, "line-opacity": 0.9 } },
          before
        );
      }
      if (map.getLayer("zones-fill")) map.setPaintProperty("zones-fill", "fill-color", zoneColorExpr(pal));
      if (map.getLayer("zones-line")) map.setPaintProperty("zones-line", "line-color", zoneColorExpr(pal));
    };
    if (map.isStyleLoaded()) render();
    else map.once("load", render);
  }, [zones, theme]);

  // Render the in-progress draw ring (dashed line + vertex dots), previewed in
  // the currently-selected zone_type's color. Cleared when not drawing.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const pal = ZONE_COLOR_HEX[theme] || ZONE_COLOR_HEX.dark;
    const color = pal[zoneType] || pal.DETECTION;
    const draw = () => {
      const coords = ringCoords;
      const features = [];
      if (coords.length >= 1) {
        const lineCoords = ringClosed && coords.length >= 3 ? [...coords, coords[0]] : coords;
        if (lineCoords.length >= 2) {
          features.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: lineCoords } });
        }
        coords.forEach((c, i) => features.push({ type: "Feature", properties: { idx: i }, geometry: { type: "Point", coordinates: c } }));
      }
      const fc = { type: "FeatureCollection", features };
      if (map.getSource("zone-draw")) {
        map.getSource("zone-draw").setData(fc);
      } else {
        map.addSource("zone-draw", { type: "geojson", data: fc });
        map.addLayer({
          id: "zone-draw-line",
          type: "line",
          source: "zone-draw",
          filter: ["==", "$type", "LineString"],
          paint: { "line-color": color, "line-width": 2, "line-dasharray": [2, 1], "line-opacity": 0.95 },
        });
        map.addLayer({
          id: "zone-draw-verts",
          type: "circle",
          source: "zone-draw",
          filter: ["==", "$type", "Point"],
          paint: { "circle-radius": 4, "circle-color": color, "circle-stroke-width": 1, "circle-stroke-color": "#000" },
        });
      }
      if (map.getLayer("zone-draw-line")) map.setPaintProperty("zone-draw-line", "line-color", color);
      if (map.getLayer("zone-draw-verts")) map.setPaintProperty("zone-draw-verts", "circle-color", color);
    };
    if (map.isStyleLoaded()) draw();
    else map.once("load", draw);
  }, [ringCoords, ringClosed, zoneType, theme]);

  // Draw-mode map interaction: each click appends a [lon,lat] vertex; a quick
  // second click (double-click) closes the ring. Uses refs so the handler is
  // attached once per draw session. lat clamped to the same [-85,85] the grid
  // uses; lon clamped to [-180,180].
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !drawMode) return;
    const lastClick = { current: 0 };
    const onClick = (e) => {
      if (ringClosedRef.current) return; // ring already closed, awaiting save
      const t = Date.now();
      if (lastClick.current && t - lastClick.current < 350) {
        lastClick.current = 0;
        closeRing();
        return;
      }
      lastClick.current = t;
      const lng = Math.max(Math.min(e.lngLat.lng, 180), -180);
      const lat = Math.max(Math.min(e.lngLat.lat, 85), -85);
      setRingCoords((c) => [...c, [lng, lat]]);
    };
    map.doubleClickZoom.disable();
    const canvas = map.getCanvas();
    const prevCursor = canvas.style.cursor;
    canvas.style.cursor = "crosshair";
    map.on("click", onClick);
    return () => {
      map.off("click", onClick);
      map.doubleClickZoom.enable();
      canvas.style.cursor = prevCursor;
    };
  }, [drawMode, closeRing]);

  const zonePal = ZONE_COLOR_HEX[theme] || ZONE_COLOR_HEX.dark;

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

      {/* -------- ZONE CONTROL PANEL (Phase D1) -------- */}
      <div className="tactical-border p-4 space-y-3" style={{ background: "var(--bg-surface)" }}>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-2">
            <Hexagon size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
            <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--accent-info)" }}>
              Tactical Zones {zones.length > 0 ? `— ${zones.length} defined` : "— none defined"}
            </span>
          </div>

          <div className="flex items-center gap-2">
            {!drawMode ? (
              <button
                data-testid="draw-zone-btn"
                onClick={startDraw}
                disabled={!isCommander}
                title={isCommander ? "Draw a new tactical zone" : "Commander role required to draw zones"}
                className="flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest tactical-border transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
              >
                <Pencil size={13} strokeWidth={1.75} />
                Draw Zone
              </button>
            ) : (
              <>
                <button
                  data-testid="close-ring-btn"
                  onClick={closeRing}
                  disabled={ringCoords.length < 3 || ringClosed}
                  className="flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest tactical-border transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}
                >
                  <Hexagon size={13} strokeWidth={1.75} />
                  Close Ring
                </button>
                <button
                  data-testid="zone-cancel-btn"
                  onClick={resetDraw}
                  className="flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest tactical-border text-slate-400 hover-surface"
                >
                  <X size={13} strokeWidth={1.75} />
                  Cancel
                </button>
              </>
            )}
          </div>
        </div>

        {/* Zone-type -> color legend */}
        <div data-testid="zone-legend" className="flex items-center gap-4 flex-wrap pt-1">
          {ZONE_TYPES.map((zt) => (
            <div key={zt} className="flex items-center gap-2">
              <span
                className="inline-block"
                style={{ width: 12, height: 12, background: zonePal[zt], border: "1px solid rgba(0,0,0,0.4)" }}
              />
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">{zt}</span>
            </div>
          ))}
        </div>

        {/* In-draw guidance */}
        {drawMode && !ringClosed && (
          <div
            className="tactical-border px-3 py-2 font-mono text-[11px]"
            style={{ background: "rgba(56,189,248,0.06)", color: "var(--accent-info)" }}
          >
            DRAW MODE ACTIVE — click the map to add vertices ({ringCoords.length} placed). Double-click,
            or press CLOSE RING, to close the polygon. Minimum 3 vertices required.
          </div>
        )}

        {/* Honesty caption — spatial vs non-spatial evaluation */}
        <div
          className="tactical-border px-3 py-2 flex items-start gap-2"
          style={{ background: "rgba(234,179,8,0.05)", borderColor: "var(--accent-warning)" }}
        >
          <Info size={13} strokeWidth={1.75} style={{ color: "var(--accent-warning)", flexShrink: 0, marginTop: 2 }} />
          <div className="font-mono text-[10px] leading-relaxed" style={{ color: "var(--accent-warning)" }}>
            Zones are absolute geographic areas. Spatial (in-zone) SOP rules apply only to contacts with a
            real decoded position — RemoteID / ADS-B / DJI DroneID. Position-less RF-sweep / WiFi /
            control-link contacts are evaluated by non-spatial rules only (no DF hardware = no bearing).
          </div>
        </div>

        {/* Save form (shown after the ring is closed) */}
        {showForm && ringClosed && (
          <div
            data-testid="zone-save-form"
            className="tactical-border p-4 space-y-4"
            style={{ background: "rgba(56,189,248,0.04)", borderColor: "var(--accent-info)" }}
          >
            <div className="font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--accent-info)" }}>
              New Zone — {ringCoords.length} vertices
            </div>

            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Name (required)</span>
              <input
                data-testid="zone-name-input"
                type="text"
                value={zoneName}
                onChange={(e) => setZoneName(e.target.value)}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                placeholder="e.g. NORTH APPROACH — ALERT"
                required
              />
            </label>

            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Zone type</span>
              <select
                data-testid="zone-type-select"
                value={zoneType}
                onChange={(e) => setZoneType(e.target.value)}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              >
                {ZONE_TYPES.map((zt) => (
                  <option key={zt} value={zt}>{zt}</option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Notes (optional)</span>
              <textarea
                data-testid="zone-notes-input"
                value={zoneNotes}
                onChange={(e) => setZoneNotes(e.target.value)}
                rows={2}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                placeholder="Optional context / ROE reference"
              />
            </label>

            {/* Commander confirm-phrase gate (idiom reused from RangeAuthorizationControl / SafetyGate) */}
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Type the exact phrase to confirm: <span style={{ color: "var(--text-primary)" }}>{ZONE_CONFIRM_PHRASE}</span>
              </span>
              <input
                data-testid="zone-confirm-phrase"
                type="text"
                autoComplete="off"
                value={confirmPhrase}
                onChange={(e) => setConfirmPhrase(e.target.value)}
                className={`mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none ${
                  confirmPhrase && !phraseOk ? "border-accent-critical" : "focus-accent-info"
                }`}
                placeholder={ZONE_CONFIRM_PHRASE}
                required
              />
            </label>

            {!isCommander && (
              <div
                data-testid="zone-commander-required"
                className="tactical-border px-3 py-2 font-mono text-[10px]"
                style={{ borderColor: "var(--accent-critical)", color: "var(--accent-critical)" }}
              >
                COMMANDER ROLE REQUIRED — only a commander may create a zone. Zone writes are
                commander-gated server-side (require_commander) and audited to the mission log.
              </div>
            )}

            <div className="pt-1 flex items-center justify-between" style={{ borderTop: "1px solid var(--border-col)" }}>
              <button
                data-testid="zone-cancel-btn-form"
                onClick={resetDraw}
                className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
              >
                Cancel
              </button>
              <button
                data-testid="zone-save-btn"
                onClick={saveZone}
                disabled={!canSave}
                className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                  !canSave ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed" : "text-white"
                }`}
                style={canSave ? { background: "var(--accent-info)", borderColor: "var(--accent-info)" } : undefined}
              >
                <Hexagon size={13} strokeWidth={1.75} />
                {saving ? "SAVING…" : "SAVE ZONE"}
              </button>
            </div>
          </div>
        )}
      </div>

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
