import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";
import { ClassificationBanner } from "@/components/ClassificationBanner";
import RangeAuthorizationBanner from "@/components/RangeAuthorizationBanner";
import EmergencyAbort from "@/components/EmergencyAbort";
import ErrorBoundary from "@/components/ErrorBoundary";
// Display control: dark is the console's default, but the operator can flip to
// the low-glare light theme from the top status bar; the choice persists
// (localStorage, see ThemeContext).
import ThemeToggle from "@/components/ThemeToggle";
import TxStatusChips from "@/components/TxStatusChips";
import AnnunciationPanel from "@/components/AnnunciationPanel";
import AutoManualToggle from "@/components/AutoManualToggle";
import { api } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import {
  Radar, Crosshair, ScrollText, LogOut, Terminal, Shield, Zap, History, MapPin, BookOpen,
  Satellite, RadioTower, Layers, Gauge,
} from "lucide-react";

// Grouped IA (FIGHT / PLAN / IDENTIFY / REVIEW). FIGHT stays always-visible —
// the direct weapons (RF Jam / GNSS Deny / MAVLink Takeover) are never collapsed
// behind DECIDE. Merged-away surfaces (Signals/MavlinkConsole/Payloads/Protocol
// & Threat Library/SDR inject) are off-nav; their content lives in DECIDE, the
// merged Library, and the merged Takeover pages.
const NAV_GROUPS = [
  {
    key: "fight",
    label: "FIGHT",
    items: [
      { to: "/dashboard", label: "COMMAND CENTER",   icon: Radar,     testid: "nav-dashboard" },
      { to: "/map",       label: "TACTICAL MAP",     icon: MapPin,    testid: "nav-map" },
      { to: "/killchain", label: "KILL CHAIN",       icon: Crosshair, testid: "nav-killchain" },
      { to: "/decision",  label: "DECIDE",           icon: Gauge,     testid: "nav-decision" },
      { to: "/jamming",   label: "RF JAM",           icon: Zap,       testid: "nav-jamming" },
      { to: "/gnss-spoof",label: "GNSS DENY",        icon: Satellite, testid: "nav-gnss-spoof" },
      { to: "/takeover",  label: "MAVLINK TAKEOVER", icon: RadioTower,testid: "nav-takeover" },
    ],
  },
  {
    key: "plan",
    label: "PLAN",
    items: [
      { to: "/zones",     label: "ZONES",            icon: Layers,    testid: "nav-zones" },
      { to: "/sop-rules", label: "SOP RULES",        icon: ScrollText,testid: "nav-sop-rules" },
    ],
  },
  {
    key: "identify",
    label: "IDENTIFY",
    items: [
      { to: "/library",   label: "THREAT & PROTOCOL LIBRARY", icon: BookOpen, testid: "nav-library" },
    ],
  },
  {
    key: "review",
    label: "REVIEW",
    items: [
      { to: "/history",   label: "DETECTION HISTORY",icon: History,   testid: "nav-history" },
      { to: "/logs",      label: "MISSION LOG",      icon: ScrollText,testid: "nav-logs" },
    ],
  },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const location = useLocation();

  // Real backend-sourced link/RX status for the header strip. Only surface
  // status the /health endpoint actually reports — no fabricated datalinks.
  const [health, setHealth] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const { data } = await api.get("/health");
        if (!cancelled) setHealth(data);
      } catch {
        if (!cancelled) setHealth(null);
      }
    };
    load();
    const id = setInterval(load, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const hackrfUp = !!health?.hackrf;
  const year = new Date().getFullYear();

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg-base)" }}>
      <RangeAuthorizationBanner />
      <ClassificationBanner position="top" />

      <div className="flex-1 flex">
        {/* Sidebar */}
        <aside
          data-testid="side-nav"
          className="w-64 tactical-border-r flex flex-col"
          style={{ background: "var(--bg-surface)" }}
        >
          <div className="p-6 tactical-border-b">
            <div className="flex items-center gap-2">
              <Shield size={22} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
              <span className="font-heading font-black text-lg tracking-tighter">MEGHDUT C³</span>
            </div>
            <div className="mt-1 font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--text-muted)" }}>
              Command · Control · Communications
            </div>
          </div>

          <nav className="flex-1 py-2">
            {NAV_GROUPS.map(({ key, label: groupLabel, items }, gi) => (
              <div key={key} className={gi > 0 ? "tactical-border-t mt-2 pt-2" : ""}>
                <div
                  data-testid={`nav-group-${key}`}
                  className="px-6 pt-2 pb-1 font-mono text-[9px] uppercase tracking-widest"
                  style={{ color: "var(--text-muted)" }}
                >
                  {groupLabel}
                </div>
                {items.map(({ to, label, icon: Icon, testid }) => (
                  <NavLink
                    key={to}
                    to={to}
                    data-testid={testid}
                    className={({ isActive }) =>
                      `nav-link flex items-center gap-3 px-6 py-3 font-mono text-xs uppercase tracking-widest transition-colors ${
                        isActive ? "is-active" : ""
                      }`
                    }
                  >
                    <Icon size={16} strokeWidth={1.5} />
                    {label}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>

          <div className="tactical-border-t p-4">
            <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">Operator</div>
            <div className="font-mono text-xs break-all" style={{ color: "var(--text-primary)" }}>{user?.email}</div>
            <div className="font-mono text-[10px] uppercase tracking-widest mt-1" style={{ color: "var(--accent-success)" }}>
              ● {user?.clearance || "RESTRICTED"}
            </div>
            <button
              data-testid="logout-btn"
              onClick={async () => { await logout(); nav("/login"); }}
              className="mt-3 w-full flex items-center justify-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-critical transition-colors scanline-btn"
            >
              <LogOut size={12} strokeWidth={1.5} />
              LOG OUT
            </button>
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 min-w-0 flex flex-col">
          <div
            className="tactical-border-b px-8 py-3 flex items-center justify-between gap-4"
            style={{ background: "var(--bg-surface)" }}
          >
            <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 min-w-0 flex items-center gap-2 overflow-x-auto">
              <Terminal size={12} className="inline shrink-0" strokeWidth={1.5} />
              {/* Persistent engagement-subsystem indicator (TX ONLINE/OFFLINE,
                  TX HALTED/LIVE, SiK LINK, TX RADIO 930c, RANGE-AUTH) — the
                  compact twin of the Command Center Engagement Control panel. */}
              <TxStatusChips health={health} compact />
              <span className="mx-1 text-slate-600 shrink-0">|</span>
              <span className="shrink-0 whitespace-nowrap" style={{ color: hackrfUp ? "var(--accent-success)" : "var(--accent-critical)" }}>
                ● HackRF RX {hackrfUp ? "UP" : "DOWN"}
              </span>
              <span className="mx-1 text-slate-600 shrink-0">|</span>
              <span className="shrink-0 whitespace-nowrap" style={{ color: "var(--accent-info)" }}>WS CLIENTS: {health?.ws_clients ?? "—"}</span>
              <span className="mx-1 text-slate-600 shrink-0">|</span>
              <AutoManualToggle />
            </div>
            <div className="flex items-center gap-4 shrink-0">
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                MISSION-ID: <span style={{ color: "var(--text-primary)" }}>CEMA-cUAS-{year}-A</span>
              </div>
              <ThemeToggle />
              <EmergencyAbort />
            </div>
          </div>

          <div className="p-8 flex-1 space-y-4">
            <AnnunciationPanel />
            {/* Per-page fault isolation: a render throw in a routed module shows
                a tactical MODULE FAULT card here while the console chrome and
                EmergencyAbort stay live. Keyed on pathname so navigating away
                clears a faulted state. */}
            <ErrorBoundary key={location.pathname}>
              <Outlet />
            </ErrorBoundary>
          </div>
        </main>
      </div>

      <ClassificationBanner position="bottom" />
    </div>
  );
}
