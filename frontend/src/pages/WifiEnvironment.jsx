import { useEffect, useMemo, useRef, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { Rss, ShieldAlert, Search, ArrowUp, ArrowDown } from "lucide-react";
import {
  filterAps, sortAps, isPossibleUas, encryptionLabel, pmfLabel, lastSeenLabel,
} from "@/lib/wifiEnvironment";

// WI-FI ENVIRONMENT — RF situational awareness.
//
// A strictly READ-ONLY window onto the ambient Wi-Fi picture from Kismet (via
// the backend's GET /api/wifi-environment proxy). It lets the operator SEE
// every AP/device in range. It is NOT a target list: there is deliberately NO
// engage / arm / deauth / transmit affordance anywhere on this page, and it
// never calls any mutating/transmitting endpoint (it only ever `api.get`s the
// survey). A "possible UAS" tag is a non-actionable visual hint; real
// engagement stays on the drone-contact target flow, never here.

const COLUMNS = [
  { key: "ssid", label: "SSID", sortable: true },
  { key: "bssid", label: "BSSID", sortable: true },
  { key: "channel", label: "CH", sortable: true },
  { key: "rssi_dbm", label: "RSSI", sortable: true },
  { key: "vendor", label: "VENDOR", sortable: true },
  { key: "encryption", label: "ENCRYPTION / PMF", sortable: true },
  { key: "client_count", label: "CLIENTS", sortable: true },
  { key: "last_seen", label: "LAST SEEN", sortable: true },
];

export default function WifiEnvironment() {
  const [survey, setSurvey] = useState(null);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState("rssi_dbm");
  const [sortDir, setSortDir] = useState("desc");
  const [loadError, setLoadError] = useState(null);

  const cancelledRef = useRef(false);
  useEffect(() => () => { cancelledRef.current = true; }, []);

  const load = async () => {
    try {
      const { data } = await api.get("/wifi-environment");
      if (cancelledRef.current) return;
      setSurvey(data);
      setLoadError(null);
    } catch (e) {
      if (cancelledRef.current) return;
      setLoadError(formatApiError(e));
    }
  };

  // Live view: poll every 4s like the other live surfaces (Signals/FPV).
  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const aps = useMemo(() => survey?.aps || [], [survey]);
  const rows = useMemo(
    () => sortAps(filterAps(aps, query), sortKey, sortDir),
    [aps, query, sortKey, sortDir]
  );
  const possibleUasCount = useMemo(
    () => aps.filter((ap) => isPossibleUas(ap)).length, [aps]
  );

  const toggleSort = (key) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "ssid" || key === "vendor" || key === "bssid" ? "asc" : "desc");
    }
  };

  const configured = survey ? survey.configured : true;
  const available = survey ? survey.available : false;

  return (
    <div className="space-y-5" data-testid="wifi-environment-page">
      {/* Header */}
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Rss size={12} className="inline mr-2" strokeWidth={1.5} /> RF Situational Awareness
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Wi-Fi Environment
          </h1>
        </div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 text-right">
          <div>
            SOURCE: <span style={{ color: "var(--text-primary)" }}>KISMET (RX-only)</span>
          </div>
          {survey && (
            <div className="mt-0.5">
              <span style={{ color: available ? "var(--accent-success)" : "var(--accent-warning)" }}>
                ● {available ? "LIVE" : "NO DATA"}
              </span>
              {available && (
                <> · {survey.ap_count}/{survey.total_seen} shown{survey.polled_at ? ` · ${new Date(survey.polled_at).toLocaleTimeString()}` : ""}</>
              )}
            </div>
          )}
        </div>
      </div>

      {/* SITUATIONAL-AWARENESS / NOT-A-TARGET-LIST banner (prominent, always on) */}
      <div
        data-testid="wifi-env-sa-banner"
        className="tactical-border p-3 flex items-start gap-3"
        style={{
          borderColor: "var(--accent-warning)",
          background: "color-mix(in srgb, var(--accent-warning) 10%, var(--bg-surface))",
        }}
      >
        <ShieldAlert size={18} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} className="mt-0.5 shrink-0" />
        <div className="font-mono text-[11px] leading-relaxed" style={{ color: "var(--text-primary)" }}>
          <span className="font-bold uppercase tracking-widest" style={{ color: "var(--accent-warning)" }}>
            Situational awareness — not a target list.
          </span>{" "}
          Access points shown here are <span className="font-bold">not engageable</span>. This is a passive,
          read-only view of the ambient Wi-Fi picture from Kismet. Any “possible UAS” tag is an
          advisory hint only — it does not select, arm, or authorise any effect. Engagement stays on
          the drone-contact target flow (Kill Chain / Decide), never on this panel.
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 tactical-border px-3 py-2" style={{ background: "var(--bg-surface)" }}>
          <Search size={13} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
          <input
            data-testid="wifi-env-filter"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter SSID / BSSID / vendor / channel…"
            className="bg-transparent outline-none font-mono text-xs w-72 max-w-[70vw]"
            style={{ color: "var(--text-primary)" }}
          />
        </div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
          {possibleUasCount > 0 && (
            <span style={{ color: "var(--accent-warning)" }}>
              ⚑ {possibleUasCount} possible-UAS tagged (non-actionable)
            </span>
          )}
        </div>
      </div>

      {loadError && (
        <div className="font-mono text-[11px] p-2 tactical-border"
             style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
          Load failed: {loadError}
        </div>
      )}

      {/* Honest empty/unconfigured states */}
      {survey && !configured && (
        <div data-testid="wifi-env-unconfigured" className="font-mono text-xs p-4 tactical-border" style={{ color: "var(--text-muted)" }}>
          Kismet is not configured on the backend (KISMET_URL unset). Set KISMET_URL / KISMET_APIKEY
          in the backend environment to enable the Wi-Fi environment survey.
        </div>
      )}
      {survey && configured && !available && (
        <div data-testid="wifi-env-unavailable" className="font-mono text-xs p-4 tactical-border" style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}>
          Kismet reachable status: {survey.status || "no data available"}. No AP survey to show yet.
        </div>
      )}

      {/* Survey table */}
      {available && (
        <div className="tactical-border overflow-x-auto" style={{ background: "var(--bg-surface)" }}>
          <table className="w-full border-collapse font-mono text-xs" data-testid="wifi-env-table">
            <thead>
              <tr className="tactical-border-b" style={{ background: "var(--bg-base)" }}>
                {COLUMNS.map((c) => {
                  const active = c.key === sortKey;
                  return (
                    <th
                      key={c.key}
                      onClick={c.sortable ? () => toggleSort(c.key) : undefined}
                      className={`px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap ${c.sortable ? "cursor-pointer select-none" : ""}`}
                      style={{ color: active ? "var(--accent-info)" : "var(--text-muted)" }}
                    >
                      <span className="inline-flex items-center gap-1">
                        {c.label}
                        {active && (sortDir === "asc"
                          ? <ArrowUp size={11} strokeWidth={2} />
                          : <ArrowDown size={11} strokeWidth={2} />)}
                      </span>
                    </th>
                  );
                })}
                <th className="px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
                  TAG
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ap, i) => {
                const uas = isPossibleUas(ap);
                return (
                  <tr key={ap.bssid || i} className="tactical-border-b" data-testid={`wifi-env-row-${ap.bssid}`}>
                    <td className="px-3 py-2" style={{ color: "var(--text-primary)" }}>
                      {ap.ssid || <span className="text-slate-600 italic">(hidden)</span>}
                    </td>
                    <td className="px-3 py-2 text-slate-400">{ap.bssid}</td>
                    <td className="px-3 py-2 text-slate-300">{ap.channel || "—"}</td>
                    <td className="px-3 py-2" style={{ color: rssiColor(ap.rssi_dbm) }}>
                      {ap.rssi_dbm != null ? `${ap.rssi_dbm} dBm` : "—"}
                    </td>
                    <td className="px-3 py-2 text-slate-300">{ap.vendor || "unknown"}</td>
                    <td className="px-3 py-2 text-slate-300">
                      {encryptionLabel(ap)}
                      <span className="text-slate-500"> · PMF {pmfLabel(ap)}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-300">{ap.client_count != null ? ap.client_count : "—"}</td>
                    <td className="px-3 py-2 text-slate-400">{lastSeenLabel(ap)}</td>
                    <td className="px-3 py-2">
                      {uas ? (
                        <span
                          className="px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap"
                          title={ap.possible_uas_reason || "matches a drone OUI/SSID pattern — advisory only, non-actionable"}
                          style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
                        >
                          ⚑ Possible UAS
                        </span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={COLUMNS.length + 1} className="px-3 py-6 text-center text-slate-600">
                    {query ? "No APs match the filter." : "No access points in range."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer honesty note */}
      <div className="font-mono text-[10px] p-3 tactical-border" style={{ color: "var(--text-muted)" }}>
        Passive RX-only survey via Kismet (monitor-mode NIC). Vendor is Kismet's OUI-derived guess;
        SSID / OUI are spoofable, so a “possible UAS” tag is a candidate hint, never an identification.
        This panel has no transmit, engage, deauth, or arm capability of any kind.
      </div>
    </div>
  );
}

function rssiColor(rssi) {
  if (rssi == null) return "var(--text-muted)";
  if (rssi >= -55) return "var(--accent-success)";
  if (rssi >= -70) return "var(--accent-info)";
  if (rssi >= -80) return "var(--accent-warning)";
  return "var(--text-muted)";
}
