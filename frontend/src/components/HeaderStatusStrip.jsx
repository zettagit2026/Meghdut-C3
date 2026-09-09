import { ShieldCheck, ShieldAlert } from "lucide-react";
import { deriveTxChips, deriveReadiness, toneColor, TONE, CHIP_GROUP } from "@/lib/engagementGate";

// HeaderStatusStrip — the persistent, shared top-of-console readout.
// Rendered once from Layout.jsx so it is identical on every page. Answers the
// ONLY question that matters on a live-fire console, in this order:
//   1. A single READY TO FIRE / NOT READY — <next action> verdict, the most
//      prominent element, leading the strip. Derived from
//      engagementGate.deriveReadiness — the SAME derivation the Engagement
//      Control panel uses, so the two can never disagree.
//   2. The supporting state, GROUPED into small labeled clusters (TRANSMIT /
//      AUTHORIZATION / SENSORS / LINK) instead of one flat pipe-separated
//      list, so it parses structurally at a glance. Chip labels are
//      deliberately unambiguous ("TX BRIDGES: …" / "MASTER-HALT: …") so two
//      chips never read as opposites of the same word.
//
// This component only reads and re-labels state that deriveTxChips /
// deriveReadiness already compute from the backend /health payload — it does
// not evaluate any new precondition and cannot change what a chip MEANS.
function Chip({ chip }) {
  return (
    <span
      data-testid={`tx-chip-${chip.key}`}
      title={chip.title}
      style={{ color: toneColor(chip.tone) }}
      className="font-mono text-[10px] uppercase tracking-widest whitespace-nowrap"
    >
      ● {chip.label}
    </span>
  );
}

function Group({ label, testid, children }) {
  return (
    <span
      data-testid={testid}
      className="flex items-center gap-2 px-2 py-1 tactical-border shrink-0"
      style={{ background: "var(--bg-elev)" }}
    >
      <span
        className="font-mono text-[8px] uppercase tracking-widest shrink-0"
        style={{ color: "var(--text-muted)" }}
      >
        {label}
      </span>
      <span className="flex items-center gap-2">{children}</span>
    </span>
  );
}

export default function HeaderStatusStrip({ health }) {
  const chips = deriveTxChips(health);
  const byGroup = (g) => chips.filter((c) => c.group === g);
  const readiness = deriveReadiness(health);
  const RIcon = readiness.ready ? ShieldCheck : ShieldAlert;

  const hackrfUp = !!health?.hackrf;
  const wsClients = health?.ws_clients ?? "—";

  return (
    <div className="flex items-center gap-2 min-w-0" data-testid="header-status-strip">
      {/* 1. THE single readiness verdict — leads the strip, most prominent element. */}
      <span
        data-testid="fire-readiness-verdict"
        role="status"
        aria-live="polite"
        title={readiness.detail}
        className={`flex items-center gap-1.5 px-2.5 py-1 tactical-border font-mono text-[10px] font-bold uppercase tracking-widest whitespace-nowrap shrink-0 ${
          !readiness.ready && readiness.tone === TONE.crit ? "pulse-crit" : ""
        }`}
        style={{
          color: readiness.ready ? "#000" : toneColor(readiness.tone),
          background: readiness.ready
            ? toneColor(readiness.tone)
            : `color-mix(in srgb, ${toneColor(readiness.tone)} 14%, transparent)`,
          borderColor: toneColor(readiness.tone),
        }}
      >
        <RIcon size={12} strokeWidth={2.5} />
        {readiness.ready ? "READY TO FIRE" : `NOT READY — ${readiness.action}`}
      </span>

      <span className="mx-0.5 text-slate-600 shrink-0">|</span>

      {/* 2. Grouped supporting state — clustered, not one flat pipe row.
          (Horizontal scroll is handled by the header strip's own container in
          Layout.jsx, so this stays a plain nowrap flex row.) */}
      <div className="flex items-center gap-1.5">
        <Group label="TRANSMIT" testid="tx-group-transmit">
          {byGroup(CHIP_GROUP.transmit).map((c) => (
            <Chip key={c.key} chip={c} />
          ))}
        </Group>

        <Group label="AUTH" testid="tx-group-authorization">
          {byGroup(CHIP_GROUP.authorization).map((c) => (
            <Chip key={c.key} chip={c} />
          ))}
        </Group>

        <Group label="SENSORS" testid="tx-group-sensors">
          <span
            data-testid="tx-chip-hackrf"
            title="HackRF passive RX sniffer liveness."
            style={{ color: hackrfUp ? "var(--accent-success)" : "var(--accent-critical)" }}
            className="font-mono text-[10px] uppercase tracking-widest whitespace-nowrap"
          >
            ● HackRF RX {hackrfUp ? "UP" : "DOWN"}
          </span>
        </Group>

        <Group label="LINK" testid="tx-group-link">
          {byGroup(CHIP_GROUP.link).map((c) => (
            <Chip key={c.key} chip={c} />
          ))}
          <span
            data-testid="tx-chip-ws-clients"
            title="Connected WebSocket clients (dashboard/telemetry consumers)."
            style={{ color: "var(--accent-info)" }}
            className="font-mono text-[10px] uppercase tracking-widest whitespace-nowrap"
          >
            WS CLIENTS: {wsClients}
          </span>
        </Group>
      </div>
    </div>
  );
}
