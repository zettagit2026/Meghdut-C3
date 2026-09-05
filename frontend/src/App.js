import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { ThemeProvider, useTheme } from "@/context/ThemeContext";
import { Toaster } from "@/components/ui/sonner";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Signals from "@/pages/Signals";
import Library from "@/pages/Library";
import Takeover from "@/pages/Takeover";
import Jamming from "@/pages/Jamming";
import WifiDefeat from "@/pages/WifiDefeat";
import GnssSpoof from "@/pages/GnssSpoof";
import KillChain from "@/pages/KillChain";
import DetectionHistory from "@/pages/DetectionHistory";
import MissionLog from "@/pages/MissionLog";
import MapView from "@/pages/Map";
import Zones from "@/pages/Zones";
import SopRules from "@/pages/SopRules";
import DecisionSupport from "@/pages/DecisionSupport";
import "@/App.css";

function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center font-mono text-xs text-slate-500" style={{ background: "var(--bg-base)" }}>
        establishing secure channel<span className="term-caret" />
      </div>
    );
  }
  return user ? children : <Navigate to="/login" replace />;
}

function ThemedToaster() {
  const { theme } = useTheme();
  return <Toaster position="top-right" theme={theme} />;
}

export default function App() {
  return (
    <ThemeProvider>
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<Protected><Layout /></Protected>}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            {/* Signals is off-nav but kept routed for its ?contact= deep-link. */}
            <Route path="signals"   element={<Signals />} />
            <Route path="library"   element={<Library />} />
            <Route path="jamming"   element={<Jamming />} />
            <Route path="gnss-spoof" element={<GnssSpoof />} />
            <Route path="takeover"  element={<Takeover />} />
            <Route path="wifi-defeat" element={<WifiDefeat />} />
            <Route path="killchain" element={<KillChain />} />
            <Route path="decision"  element={<DecisionSupport />} />
            <Route path="history"   element={<DetectionHistory />} />
            <Route path="map"       element={<MapView />} />
            <Route path="zones"     element={<Zones />} />
            <Route path="sop-rules" element={<SopRules />} />
            <Route path="logs"      element={<MissionLog />} />
            {/* Retired-route redirects — keep old deep-links from 404-ing. */}
            <Route path="threat-library"     element={<Navigate to="/library" replace />} />
            <Route path="protocols"          element={<Navigate to="/library" replace />} />
            <Route path="payloads"           element={<Navigate to="/takeover" replace />} />
            <Route path="sdr-mavlink-inject" element={<Navigate to="/takeover" replace />} />
            <Route path="mavlink"            element={<Navigate to="/takeover" replace />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
      <ThemedToaster />
    </AuthProvider>
    </ThemeProvider>
  );
}
