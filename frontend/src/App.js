import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { ThemeProvider, useTheme } from "@/context/ThemeContext";
import { Toaster } from "@/components/ui/sonner";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Signals from "@/pages/Signals";
import MavlinkConsole from "@/pages/MavlinkConsole";
import Payloads from "@/pages/Payloads";
import ProtocolLibrary from "@/pages/ProtocolLibrary";
import Jamming from "@/pages/Jamming";
import GnssSpoof from "@/pages/GnssSpoof";
import KillChain from "@/pages/KillChain";
import DetectionHistory from "@/pages/DetectionHistory";
import MissionLog from "@/pages/MissionLog";
import MapView from "@/pages/Map";
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
            <Route path="signals"   element={<Signals />} />
            <Route path="mavlink"   element={<MavlinkConsole />} />
            <Route path="payloads"  element={<Payloads />} />
            <Route path="protocols" element={<ProtocolLibrary />} />
            <Route path="jamming"   element={<Jamming />} />
            <Route path="gnss-spoof" element={<GnssSpoof />} />
            <Route path="killchain" element={<KillChain />} />
            <Route path="history"   element={<DetectionHistory />} />
            <Route path="map"       element={<MapView />} />
            <Route path="logs"      element={<MissionLog />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
      <ThemedToaster />
    </AuthProvider>
    </ThemeProvider>
  );
}
