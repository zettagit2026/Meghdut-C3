import { useState } from "react";
import { useNavigate, Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { ClassificationBanner } from "@/components/ClassificationBanner";
import { Shield, LockKeyhole, Terminal, ChevronRight } from "lucide-react";
import { toast } from "sonner";

export default function Login() {
  const { user, login, loading } = useAuth();
  const [email, setEmail] = useState("operator@cema.mil");
  const [password, setPassword] = useState("cema@2026");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  if (loading) return null;
  if (user) return <Navigate to="/dashboard" replace />;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    const res = await login(email, password);
    setBusy(false);
    if (res.ok) {
      toast.success("ACCESS GRANTED", { description: "Establishing secure channel..." });
      nav("/dashboard");
    } else {
      toast.error("ACCESS DENIED", { description: res.error });
    }
  };

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--bg-base)" }}>
      <ClassificationBanner position="top" />

      <div className="flex-1 grid grid-cols-1 lg:grid-cols-2">
        {/* Left brand column */}
        <div
          className="hidden lg:flex flex-col justify-between p-16 tactical-border-r"
          style={{
            background:
              "linear-gradient(180deg, #0C111D 0%, #050810 100%)",
          }}
        >
          <div className="flex items-center gap-3">
            <Shield size={32} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
            <div>
              <div className="font-heading font-black text-2xl tracking-tighter">MEGHDUT C3</div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Command · Control · Communications · Zettawise
              </div>
            </div>
          </div>

          <div>
            <div className="font-heading font-black text-6xl leading-[0.85] tracking-tighter uppercase">
              Detect.<br />
              <span style={{ color: "var(--accent-info)" }}>Demodulate.</span><br />
              Defeat.
            </div>
            <p className="font-body mt-8 text-slate-400 max-w-md">
              A seven-stage CEMA exploitation pipeline for the identification, protocol
              exploitation and cyber-physical neutralisation of adversary UAVs.
              Broadcast MAVLink takedowns. Payload-grade physical parameter attacks.
            </p>

            <div className="mt-10 grid grid-cols-3 gap-0 tactical-border">
              {["CAPTURE", "DEMODULATE", "EXPLOIT"].map((k) => (
                <div key={k} className="p-4 tactical-border-r last:border-r-0">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">STAGE</div>
                  <div className="font-heading font-black text-lg tracking-tighter">{k}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-600">
            <Terminal size={12} className="inline mr-2" strokeWidth={1.5} />
            EVAL BUILD · NOT FOR OPERATIONAL USE
          </div>
        </div>

        {/* Right form column */}
        <div className="flex items-center justify-center p-8">
          <form
            onSubmit={submit}
            data-testid="login-form"
            className="w-full max-w-md tactical-border p-8"
            style={{ background: "var(--bg-surface)" }}
          >
            <div className="flex items-center gap-2 mb-6">
              <LockKeyhole size={16} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
                Secure Terminal · Auth Required
              </span>
            </div>
            <h1 className="font-heading font-black text-3xl uppercase tracking-tighter mb-1">
              Operator Login
            </h1>
            <p className="font-mono text-xs text-slate-500 mb-6">
              Enter credentials to establish encrypted CEMA session.
            </p>

            <label className="block mb-4">
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">Operator ID</div>
              <input
                data-testid="login-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
                className="w-full bg-black/50 tactical-border px-3 py-2 font-mono text-sm text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>

            <label className="block mb-6">
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">Passphrase</div>
              <input
                data-testid="login-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                className="w-full bg-black/50 tactical-border px-3 py-2 font-mono text-sm text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>

            <button
              data-testid="login-submit"
              type="submit"
              disabled={busy}
              className="w-full flex items-center justify-center gap-2 py-3 tactical-border font-mono text-xs uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn disabled:opacity-50"
              style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
            >
              {busy ? "AUTHENTICATING…" : "ESTABLISH CHANNEL"} <ChevronRight size={14} strokeWidth={1.5} />
            </button>

            <div className="mt-6 pt-6 tactical-border-t font-mono text-[10px] uppercase tracking-widest text-slate-600">
              Default eval account:<br />
              <span className="text-slate-400">operator@cema.mil / cema@2026</span>
            </div>
          </form>
        </div>
      </div>

      <ClassificationBanner position="bottom" />
    </div>
  );
}
