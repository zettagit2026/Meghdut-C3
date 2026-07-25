#!/usr/bin/env bash
# Start the MAVLink serial TX/RX bridge (SiK / RFD900 / FPV telemetry radio).
#
# NOTE (task #36 consolidation): this directory used to also run a HackRF
# wide-band scanner (hackrf_scanner.py) alongside this bridge. That scanner
# was a duplicate, earlier iteration of what field-bridge/hackrf_rx.py does
# (and does more completely — device serial pinning, re-auth-on-401, RX-only
# safety framing). It has been removed from this directory; HackRF
# detection now lives exclusively in field-bridge/. This script only starts
# the MAVLink bridge, which has no equivalent in field-bridge and remains
# the live TX path referenced throughout backend/server.py.
#
#   ./run.sh          → MAVLink serial bridge (foreground)
#   ./run.sh bridge   → same, explicit form
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "!! .venv missing. Run ./install-deps.sh first."
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

MODE="${1:-bridge}"

case "$MODE" in
    bridge)
        exec python mavlink_bridge.py
        ;;
    *)
        echo "Usage: $0 [bridge]"
        exit 1
        ;;
esac
