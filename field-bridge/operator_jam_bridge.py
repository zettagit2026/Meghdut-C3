#!/usr/bin/env python3
"""Operator Jam Bridge — the OPERATOR'S OWN jammer, run through the SAME
governed spine as MEGHDUT's built-in barrage jam.

=============================================================================
WHY THIS EXISTS
=============================================================================
The operator supplied their own GNU Radio jammer (see
field-bridge/operator_jam_wrapper.py for the full description). This bridge
lets a governed jam request with jam_mode="operator" run that jammer — pinned
to the TX serial and hard-time-bounded — so the operator can A/B their own
waveform against MEGHDUT's built-in barrage jam.

=============================================================================
IT ADDS *NO* NEW AUTHORIZATION PATH
=============================================================================
OperatorJamBridge is a thin subclass of field-bridge/jam_bridge.py's
JamBridge. It inherits — unchanged — every gate in that bridge's docstring:

  * the live GET /api/range-authorization/status?effect=jam lease check made
    at the moment of transmission (fails closed),
  * the jam-confirm-token shape check (proof a human completed the app's
    SafetyGate two-step confirm),
  * the local EMERGENCY-ABORT / tx_halt state (refuses new bursts, and kills
    an in-progress one), and
  * the backend-side commander role + single-use arm token + single-use
    jam-confirm token that gate POST /api/payloads/jam before the request is
    ever broadcast.

The ONLY thing this subclass changes is the radiator: it overrides
_do_transmit() to run the operator's flowgraph (via operator_jam_wrapper) in
place of hackrf_jam.transmit_burst(). It also only picks up jam_mode="operator"
requests (JAM_MODE below), so the MEGHDUT bridge and this bridge can share the
one WS channel without double-firing.

If GNU Radio / gr-osmosdr / the operator module is not installed, a request
comes back as a clean 'failed' jam_ack carrying
"Operator mode unavailable: ..." — the service never crashes and never falls
through to an ungoverned transmit.

Env (systemd EnvironmentFile, see cema-operator-jam-bridge.service):
  CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD  — same as the MEGHDUT jam bridge.
  CEMA_BRIDGE_TOKEN                          — diagnostic bridge-identity secret.
  HACKRF_TX_SERIAL                           — REQUIRED: the pinned TX unit's
                                               serial (e.g. the ...930c TX unit).
                                               Without it the wrapper fails
                                               closed rather than key the RX
                                               detection radio.
  OPERATOR_JAM_DIR                           — directory holding the operator's
                                               UNMODIFIED files (default
                                               /CEMA/operator-jam).
"""
from __future__ import annotations

import logging
import os
import sys
import threading

from jam_bridge import JamBridge
from operator_jam_wrapper import (
    OPERATOR_BANDS,
    OperatorJamUnavailable,
    ensure_operator_jam_available,
    run_operator_jam,
)

log = logging.getLogger("operator-jam-bridge")


class OperatorJamBridge(JamBridge):
    """JamBridge whose radiator is the operator's own jammer. Same spine,
    different waveform. Handles ONLY jam_mode="operator" requests."""

    JAM_MODE = "operator"

    def __init__(self) -> None:
        super().__init__()
        self.tx_serial = os.environ.get("HACKRF_TX_SERIAL") or None
        if not self.tx_serial:
            log.warning(
                "HACKRF_TX_SERIAL is not set — the operator jammer FAILS CLOSED "
                "without a pinned TX serial (it will not fall back to index-based "
                "'hackrf=0', which could key the RX detection radio). Set "
                "HACKRF_TX_SERIAL to the TX unit serial before engaging operator jam.")
        # Surface GNU Radio availability once at startup (diagnostic only — never
        # a gate, never fatal). A missing stack simply means each operator jam
        # request will fail cleanly with "Operator mode unavailable".
        try:
            ensure_operator_jam_available()
            log.info("Operator jam stack available (GNU Radio + gr-osmosdr + "
                     "operator module importable from %s).",
                     os.environ.get("OPERATOR_JAM_DIR") or "/CEMA/operator-jam")
        except OperatorJamUnavailable as e:
            log.warning("Operator jam stack NOT available at startup: %s. Operator "
                        "jam requests will fail cleanly until this is resolved.", e)

    def _do_transmit(self, params: dict, stop_event: threading.Event,
                     on_started) -> dict:
        """Route the (already fully-gated) request through the operator's own
        jammer instead of MEGHDUT's transmit_burst. Returns the identical
        result-dict shape the base bridge acks on."""
        band = params.get("band")
        if band not in OPERATOR_BANDS:
            return {"ok": False, "stopped_early": False,
                    "error": f"operator jam: unsupported band {band!r} "
                             f"(supports {sorted(OPERATOR_BANDS)})"}
        return run_operator_jam(
            band,
            self.tx_serial,
            params["duration_s"],
            # Directive #1: operator-adjustable TX gain (clamped to the HackRF TX
            # VGA hardware ceiling 0-47 dB in the wrapper, no artificial cap).
            # Driven onto the operator flowgraph's osmosdr sink; None leaves their
            # baked-in gain untouched.
            tx_gain=params.get("tx_gain"),
            abort_event=stop_event,
            # Honor a mid-burst EMERGENCY ABORT exactly like the MEGHDUT jam:
            # the base bridge sets self.tx_halted on abort AND sets stop_event;
            # we poll both so an abort kills an in-progress operator burst too.
            tx_halt_check=lambda: self.tx_halted,
            on_started=on_started,
        )


def main() -> int:
    return OperatorJamBridge().run()


if __name__ == "__main__":
    sys.exit(main())
