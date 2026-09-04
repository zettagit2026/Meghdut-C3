import { deriveTxChips, toneColor } from "@/lib/engagementGate";

// Live, plain-language, color-coded status chips for the transmit/engagement
// subsystem. Rendered two ways from ONE source of truth (deriveTxChips):
//   - compact: a dense inline strip for the persistent app header.
//   - full (default): bordered chips for the Engagement Control panel.
// Colors come from the theme's accent CSS vars, which are AA-legible in both
// the dark and light console themes. Every chip also exposes its status to
// assistive tech via title + an sr-only live description on the strip.
export default function TxStatusChips({ health, compact = false }) {
  const chips = deriveTxChips(health);

  if (compact) {
    return (
      <span className="flex items-center gap-2" data-testid="tx-status-chips-compact">
        {chips.map((c, i) => (
          <span key={c.key} className="flex items-center">
            {i > 0 && <span className="mx-1 text-slate-600">|</span>}
            <span
              data-testid={`tx-chip-${c.key}`}
              style={{ color: toneColor(c.tone) }}
              title={c.title}
              className="font-mono text-[10px] uppercase tracking-widest whitespace-nowrap"
            >
              ● {c.label}
            </span>
          </span>
        ))}
      </span>
    );
  }

  return (
    <div className="flex flex-wrap gap-2" data-testid="tx-status-chips">
      {chips.map((c) => (
        <span
          key={c.key}
          data-testid={`tx-chip-${c.key}`}
          title={c.title}
          className="px-2.5 py-1 tactical-border font-mono text-[10px] font-bold uppercase tracking-widest whitespace-nowrap"
          style={{
            color: toneColor(c.tone),
            borderColor: toneColor(c.tone),
            background: `color-mix(in srgb, ${toneColor(c.tone)} 8%, transparent)`,
          }}
        >
          ● {c.label}
        </span>
      ))}
    </div>
  );
}
