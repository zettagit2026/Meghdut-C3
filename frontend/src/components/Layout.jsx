import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { ClassificationBanner } from "@/components/ClassificationBanner";
import RangeAuthorizationBanner from "@/components/RangeAuthorizationBanner";
import EmergencyAbort from "@/components/EmergencyAbort";
import { useAuth } from "@/context/AuthContext";
import {
  Radar, Waves, Radio, Bomb, Crosshair, ScrollText, LogOut, Terminal, Shield, Zap, History, MapPin,
} from "lucide-react";

const NAV = [
  { to: "/dashboard",   label: "COMMAND CENTER",   icon: Radar,     testid: "nav-dashboard" },
  { to: "/signals",     label: "SIGNAL ANALYSIS",  icon: Waves,     testid: "nav-signals" },
  { to: "/mavlink",     label: "MAVLINK CONSOLE",  icon: Radio,     testid: "nav-mavlink" },
  { to: "/payloads",    label: "PAYLOAD LIBRARY",  icon: Bomb,      testid: "nav-payloads" },
  { to: "/jamming",     label: "RF BARRAGE JAM",   icon: Zap,       testid: "nav-jamming" },
  { to: "/killchain",   label: "KILL CHAIN",       icon: Crosshair, testid: "nav-killchain" },
  { to: "/history",     label: "DETECTION HISTORY",icon: History,   testid: "nav-history" },
  { to: "/map",         label: "TACTICAL MAP",     icon: MapPin,    testid: "nav-map" },
  { to: "/logs",        label: "MISSION LOG",      icon: ScrollText,testid: "nav-logs" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();

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

          <nav className="flex-1 py-4">
            {NAV.map(({ to, label, icon: Icon, testid }) => (
              <NavLink
                key={to}
                to={to}
                data-testid={testid}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-6 py-3 font-mono text-xs uppercase tracking-widest transition-colors ${
                    isActive
                      ? "bg-[#1A2235] text-[#00F0FF] border-l-2 border-[#00F0FF]"
                      : "text-slate-400 hover:text-white hover:bg-[#0F1626] border-l-2 border-transparent"
                  }`
                }
              >
                <Icon size={16} strokeWidth={1.5} />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="tactical-border-t p-4">
            <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">Operator</div>
            <div className="font-mono text-xs text-white break-all">{user?.email}</div>
            <div className="font-mono text-[10px] uppercase tracking-widest mt-1" style={{ color: "var(--accent-success)" }}>
              ● {user?.clearance || "RESTRICTED"}
            </div>
            <button
              data-testid="logout-btn"
              onClick={async () => { await logout(); nav("/login"); }}
              className="mt-3 w-full flex items-center justify-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest hover:bg-[#FF3B30] hover:text-black transition-colors scanline-btn"
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
            <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 min-w-0 truncate">
              <Terminal size={12} className="inline mr-2" strokeWidth={1.5} />
              <span className="text-slate-300">SECURE CHANNEL</span>
              <span className="mx-3">|</span>
              <span style={{ color: "var(--accent-success)" }}>● LINK-16 UP</span>
              <span className="mx-3">|</span>
              <span style={{ color: "var(--accent-info)" }}>SDR: HACKRF · 1MHz–6GHz</span>
            </div>
            <div className="flex items-center gap-4 shrink-0">
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                MISSION-ID: <span className="text-white">CEMA-2026-{new Date().getFullYear()}-A</span>
              </div>
              <EmergencyAbort />
            </div>
          </div>

          <div className="p-8 flex-1">
            <Outlet />
          </div>
        </main>
      </div>

      <ClassificationBanner position="bottom" />
    </div>
  );
}
