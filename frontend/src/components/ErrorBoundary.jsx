import { Component } from "react";
import { AlertOctagon, RotateCcw } from "lucide-react";

// Tactical error boundary. A single render throw in a routed page must not
// white-screen the whole operator console mid-demo — it is caught here and
// shown as a self-contained "MODULE FAULT" card while the console chrome
// (sidebar, banners, EmergencyAbort) stays live.
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    // Keep a console trace for post-demo triage; no telemetry side effects.
    // eslint-disable-next-line no-console
    console.error("[MODULE FAULT]", error, info?.componentStack);
  }

  handleReload = () => {
    window.location.reload();
  };

  render() {
    if (!this.state.hasError) return this.props.children;

    return (
      <div
        data-testid="module-fault"
        role="alert"
        className="tactical-border p-8 max-w-xl mx-auto mt-12"
        style={{ background: "var(--bg-surface)" }}
      >
        <div className="flex items-center gap-3 mb-3">
          <AlertOctagon size={22} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
          <span
            className="font-heading font-black text-2xl uppercase tracking-tighter"
            style={{ color: "var(--accent-critical)" }}
          >
            Module Fault
          </span>
        </div>
        <p className="font-mono text-xs text-slate-400 mb-1">
          This module hit a render fault and was isolated. The console and safety
          controls remain live.
        </p>
        {this.state.error?.message && (
          <p className="font-mono text-[10px] text-slate-600 break-all mb-5">
            {String(this.state.error.message)}
          </p>
        )}
        <button
          type="button"
          data-testid="module-fault-reload"
          onClick={this.handleReload}
          className="flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest hover:bg-[var(--hover-surface)] transition-colors scanline-btn"
          style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
        >
          <RotateCcw size={14} strokeWidth={1.5} /> Reload Console
        </button>
      </div>
    );
  }
}
