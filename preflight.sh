#!/usr/bin/env bash
# CEMA cUAS — Pre-demo preflight check.
#
# READ-ONLY health/connectivity verification. This script NEVER transmits
# anything over RF and NEVER issues a MAVLink/jamming command. It exists
# because the original jamming-demo failure was a *silent* one: deploy
# success was self-reported with no real confirmation the bridge was
# connected. The ACK-based state machine (AWAITING_ACK -> NEUTRALIZED /
# TX_FAILED / TX_TIMEOUT) now fixes that at the protocol level, but an
# operator still had to manually eyeball "is everything actually up" before
# a live demo. This script scriptedly runs the same checks DEMO_PLAYBOOK.md's
# "Failure recovery" table (section 8) currently tells a human to do by hand.
#
# Run this FROM THE REPO ROOT on the deployment host (primary/secondary),
# where docker compose and the systemd RX/TX bridge services actually exist.
# It does not start/enable/stop any service or container — observation only.
#
# Exit code: 0 if no FAIL (WARNs are OK — see per-check notes below).
#            1 if any check is marked FAIL.
#
# Usage:
#   ./preflight.sh
#   PREFLIGHT_EMAIL=operator@cema.mil PREFLIGHT_PASSWORD='...' ./preflight.sh
#
# PREFLIGHT_EMAIL / PREFLIGHT_PASSWORD are optional. If unset, the login
# check (and everything downstream of it: detection freshness) is SKIPPED
# with a WARN — never defaulted to a guessable credential.

set -u
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Config (override via env if the deployment host differs from the default)
# ---------------------------------------------------------------------------
BACKEND_URL="${PREFLIGHT_BACKEND_URL:-http://localhost:8001}"
DETECTION_FRESH_WINDOW_S="${PREFLIGHT_FRESH_WINDOW_S:-300}"   # 5 min sanity window

# ---------------------------------------------------------------------------
# Output helpers — mirrors start.sh's "[X] message" bracket convention,
# with color when the terminal supports it (start.sh itself is uncolored,
# but this script is meant to be scanned fast under demo-day pressure).
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { echo "  ${C_GREEN}[PASS]${C_RESET} $1"; PASS_COUNT=$((PASS_COUNT+1)); }
warn() { echo "  ${C_YELLOW}[WARN]${C_RESET} $1"; WARN_COUNT=$((WARN_COUNT+1)); }
fail() { echo "  ${C_RED}[FAIL]${C_RESET} $1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
section() { echo; echo "${C_BOLD}== $1 ==${C_RESET}"; }

need() {
  # need <cmd> -> warn (not fail) if a helper tool is missing, since
  # unavailability of e.g. jq shouldn't gate an otherwise-healthy system.
  command -v "$1" >/dev/null 2>&1
}

echo "==============================================="
echo " CEMA cUAS — Pre-Demo Preflight Check"
echo " (read-only; no RF TX, no MAVLink/jam commands)"
echo "==============================================="

HAVE_JQ=1
if ! need jq; then
  HAVE_JQ=0
  warn "jq not installed — falling back to grep-based JSON parsing (less robust). Install jq for reliable output."
fi

# ---------------------------------------------------------------------------
# 1. Docker containers
# ---------------------------------------------------------------------------
section "1. Docker containers"

if ! need docker; then
  fail "docker not found on PATH — cannot check containers."
else
  for c in cema-mongo cema-backend cema-frontend; do
    state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$c" 2>/dev/null || true)"
    if [ -z "$state" ]; then
      fail "$c — container not found (is docker compose up?)"
    elif [ "$state" != "running" ]; then
      fail "$c — state=$state (expected running)"
    elif [ "$health" = "unhealthy" ]; then
      fail "$c — running but healthcheck=unhealthy"
    else
      pass "$c — running (health=$health)"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 2. Backend API reachable + parse health payload
# ---------------------------------------------------------------------------
section "2. Backend API / health endpoint"

BACKEND_UP=0
HEALTH_JSON=""
LOGIN_TOKEN=""

if ! need curl; then
  fail "curl not found on PATH — cannot check backend API."
else
  ROOT_RESP="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BACKEND_URL/api/" 2>/dev/null || true)"
  if [ "$ROOT_RESP" = "200" ]; then
    pass "Backend reachable at $BACKEND_URL (GET /api/ -> 200)"
    BACKEND_UP=1
  else
    fail "Backend NOT reachable at $BACKEND_URL (GET /api/ -> '${ROOT_RESP:-no response}')"
  fi
fi

# /api/health requires an authenticated operator (see backend/server.py),
# so it's fetched after login in section 3/6 below, not here. Root ("/")
# reachability above is the pure "process is up" signal.

# ---------------------------------------------------------------------------
# 3. Commander login (only if credentials supplied)
# ---------------------------------------------------------------------------
section "3. Commander login"

if [ -z "${PREFLIGHT_EMAIL:-}" ] || [ -z "${PREFLIGHT_PASSWORD:-}" ]; then
  warn "PREFLIGHT_EMAIL / PREFLIGHT_PASSWORD not set — skipping real login check (and everything downstream that needs a token). Set both env vars to also verify auth + live-data freshness."
elif [ "$BACKEND_UP" -ne 1 ]; then
  warn "Skipping login check — backend was not reachable in step 2."
else
  LOGIN_RESP="$(curl -s --max-time 5 -X POST "$BACKEND_URL/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${PREFLIGHT_EMAIL}\",\"password\":\"${PREFLIGHT_PASSWORD}\"}" 2>/dev/null || true)"

  if [ "$HAVE_JQ" -eq 1 ]; then
    LOGIN_TOKEN="$(echo "$LOGIN_RESP" | jq -r '.token // empty' 2>/dev/null)"
  else
    LOGIN_TOKEN="$(echo "$LOGIN_RESP" | grep -o '"token"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')"
  fi

  if [ -n "$LOGIN_TOKEN" ]; then
    pass "Login succeeded for $PREFLIGHT_EMAIL — token issued."
  else
    fail "Login FAILED for $PREFLIGHT_EMAIL — check credentials / ADMIN_EMAIL/ADMIN_PASSWORD in .env. (Response: ${LOGIN_RESP:0:200})"
  fi
fi

# Fetch the authenticated /api/health payload now, if we have a token — it
# backs sections 5, 6, and part of the summary below.
if [ -n "$LOGIN_TOKEN" ]; then
  HEALTH_JSON="$(curl -s --max-time 5 -H "Authorization: Bearer $LOGIN_TOKEN" "$BACKEND_URL/api/health" 2>/dev/null || true)"
fi

# ---------------------------------------------------------------------------
# 4. RX bridge systemd status (informational; passive detection RX)
# ---------------------------------------------------------------------------
section "4. RX bridge service (cema-hackrf-rx.service)"

RX_SVC="cema-hackrf-rx.service"
if ! need systemctl; then
  warn "$RX_SVC — systemctl not available on this host, cannot check (skipped)."
elif ! systemctl list-unit-files "$RX_SVC" 2>/dev/null | grep -q "^$RX_SVC"; then
  warn "$RX_SVC — unit not found on this host (may not be installed in this deployment). Skipped."
else
  RX_STATE="$(systemctl is-active "$RX_SVC" 2>/dev/null || true)"
  if [ "$RX_STATE" = "active" ]; then
    pass "$RX_SVC — active (passive spectrum RX running)"
  else
    warn "$RX_SVC — state=$RX_STATE (not active). Detections will rely on SiK / manual injects only."
  fi
fi

# ---------------------------------------------------------------------------
# 5. Live-data freshness sanity check (WARN-only, never FAIL)
# ---------------------------------------------------------------------------
section "5. Detection data freshness (sanity check, not a failure gate)"

if [ -z "$LOGIN_TOKEN" ]; then
  warn "No login token available — skipping freshness check (see section 3)."
else
  DET_RESP="$(curl -s --max-time 5 -H "Authorization: Bearer $LOGIN_TOKEN" "$BACKEND_URL/api/detections" 2>/dev/null || true)"
  now_epoch="$(date -u +%s)"
  freshest_age="-1"

  if [ "$HAVE_JQ" -eq 1 ]; then
    # Pull all last_seen timestamps, compute age of the most recent one.
    last_seens="$(echo "$DET_RESP" | jq -r '.[].last_seen // empty' 2>/dev/null)"
    if [ -n "$last_seens" ]; then
      while IFS= read -r ts; do
        [ -z "$ts" ] && continue
        ts_epoch="$(date -u -d "$ts" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%S" "${ts%%.*}" +%s 2>/dev/null || echo "")"
        [ -z "$ts_epoch" ] && continue
        age=$(( now_epoch - ts_epoch ))
        if [ "$freshest_age" -eq -1 ] || [ "$age" -lt "$freshest_age" ]; then
          freshest_age="$age"
        fi
      done <<< "$last_seens"
    fi
  else
    warn "jq unavailable — cannot reliably parse detection timestamps; skipping precise freshness calc."
  fi

  if [ "$freshest_age" != "-1" ]; then
    if [ "$freshest_age" -le "$DETECTION_FRESH_WINDOW_S" ]; then
      pass "Most recent detection last_seen is ${freshest_age}s old (<= ${DETECTION_FRESH_WINDOW_S}s window) — live data flowing."
    else
      warn "No detection with last_seen within ${DETECTION_FRESH_WINDOW_S}s (freshest is ${freshest_age}s old). This is NOT a system failure — there may legitimately be no drones in range right now — but confirm this is expected before the demo starts."
    fi
  elif [ "$HAVE_JQ" -eq 1 ]; then
    warn "No detections with a parseable last_seen returned by /api/detections — no live-data sanity signal available. Not a failure by itself."
  fi
fi

# ---------------------------------------------------------------------------
# 6. WebSocket / bridge connectivity — THE signal that was missing during
#    the original silent-failure incident (no bridge connected = no ws
#    client = a deploy command would silently do nothing). Called out
#    prominently on purpose.
# ---------------------------------------------------------------------------
section "6. WebSocket bridge connectivity  *** critical signal ***"

if [ -z "$HEALTH_JSON" ]; then
  warn "No authenticated /api/health payload available (see section 3) — cannot check ws_clients / hackrf / sik_radio signals. This is exactly the blind spot that caused the original incident; strongly recommend re-running with PREFLIGHT_EMAIL/PREFLIGHT_PASSWORD set before any live demo."
else
  if [ "$HAVE_JQ" -eq 1 ]; then
    ws_clients="$(echo "$HEALTH_JSON" | jq -r '.ws_clients // "null"')"
    ws_upgrade_capable="$(echo "$HEALTH_JSON" | jq -r 'if .ws_upgrade_capable == null then "null" else (.ws_upgrade_capable | tostring) end')"
    mongo_ok="$(echo "$HEALTH_JSON" | jq -r 'if .mongo == null then "null" else (.mongo | tostring) end')"
    hackrf_live="$(echo "$HEALTH_JSON" | jq -r 'if .hackrf == null then "null" else (.hackrf | tostring) end')"
    sik_live="$(echo "$HEALTH_JSON" | jq -r 'if .sik_radio == null then "null" else (.sik_radio | tostring) end')"
    active_targets="$(echo "$HEALTH_JSON" | jq -r '.active_targets // "null"')"
    tx_pending_acks="$(echo "$HEALTH_JSON" | jq -r '.tx_pending_acks // "null"')"
    tx_awaiting_ack_detections="$(echo "$HEALTH_JSON" | jq -r '.tx_awaiting_ack_detections // "null"')"
    tx_recent_timeout="$(echo "$HEALTH_JSON" | jq -r '.tx_recent_timeout // "null"')"
    tx_recent_failed="$(echo "$HEALTH_JSON" | jq -r '.tx_recent_failed // "null"')"
    tx_recent_neutralized="$(echo "$HEALTH_JSON" | jq -r '.tx_recent_neutralized // "null"')"
    tx_path_degraded="$(echo "$HEALTH_JSON" | jq -r 'if .tx_path_degraded == null then "null" else (.tx_path_degraded | tostring) end')"
  else
    ws_clients="$(echo "$HEALTH_JSON" | grep -o '"ws_clients"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    ws_upgrade_capable="$(echo "$HEALTH_JSON" | grep -o '"ws_upgrade_capable"[[:space:]]*:[[:space:]]*[a-z]*' | grep -o '[a-z]*$')"
    mongo_ok="$(echo "$HEALTH_JSON" | grep -o '"mongo"[[:space:]]*:[[:space:]]*[a-z]*' | grep -o '[a-z]*$')"
    hackrf_live="$(echo "$HEALTH_JSON" | grep -o '"hackrf"[[:space:]]*:[[:space:]]*[a-z]*' | grep -o '[a-z]*$')"
    sik_live="$(echo "$HEALTH_JSON" | grep -o '"sik_radio"[[:space:]]*:[[:space:]]*[a-z]*' | grep -o '[a-z]*$')"
    active_targets="?"
    tx_pending_acks="$(echo "$HEALTH_JSON" | grep -o '"tx_pending_acks"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    tx_awaiting_ack_detections="$(echo "$HEALTH_JSON" | grep -o '"tx_awaiting_ack_detections"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    tx_recent_timeout="$(echo "$HEALTH_JSON" | grep -o '"tx_recent_timeout"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    tx_recent_failed="$(echo "$HEALTH_JSON" | grep -o '"tx_recent_failed"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    tx_recent_neutralized="$(echo "$HEALTH_JSON" | grep -o '"tx_recent_neutralized"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$')"
    tx_path_degraded="$(echo "$HEALTH_JSON" | grep -o '"tx_path_degraded"[[:space:]]*:[[:space:]]*[a-z]*' | grep -o '[a-z]*$')"
  fi

  echo "  Backend health payload: mongo=$mongo_ok  hackrf=$hackrf_live  sik_radio=$sik_live  ws_clients=$ws_clients  active_targets=$active_targets"
  echo "  TX-path health: ws_upgrade_capable=$ws_upgrade_capable  tx_pending_acks=$tx_pending_acks  tx_awaiting_ack_detections=$tx_awaiting_ack_detections  tx_recent(neutralized=$tx_recent_neutralized timeout=$tx_recent_timeout failed=$tx_recent_failed)  tx_path_degraded=$tx_path_degraded"

  if [ "$mongo_ok" = "true" ]; then
    pass "Mongo ping OK (per /api/health)."
  else
    fail "Mongo ping FAILED (per /api/health: mongo=$mongo_ok)."
  fi

  if [ -n "$ws_clients" ] && [ "$ws_clients" != "null" ] && [ "$ws_clients" -gt 0 ] 2>/dev/null; then
    pass "ws_clients=$ws_clients — at least one bridge/frontend is connected over the WebSocket bus."
  else
    fail "ws_clients=0 (or unreported) — NO bridge/frontend is connected to the WebSocket bus. This is the exact silent-failure condition from the original incident: a deploy command would appear to succeed but nothing downstream would receive it. Do NOT proceed with a live demo until a bridge/frontend client is confirmed connected."
  fi

  # *** critical signal, added after this session's real incident ***: even
  # with ws_clients==0 above being a WARN/FAIL-worthy state on its own, that
  # signal alone cannot distinguish "no bridge happens to be connected right
  # now" from "the WS endpoint is fundamentally broken and NOTHING could ever
  # connect" — which is exactly what a missing `websockets` dependency caused
  # this session. ws_upgrade_capable is a real, checkable proxy for the
  # latter: it is FALSE only when the WS mechanism itself is unusable,
  # regardless of whether any bridge happens to be connected right now.
  if [ "$ws_upgrade_capable" = "true" ]; then
    pass "ws_upgrade_capable=true — the WebSocket upgrade mechanism (websockets package) is available; /api/ws/mavlink can accept connections."
  elif [ "$ws_upgrade_capable" = "false" ]; then
    fail "ws_upgrade_capable=false — the websockets package is NOT importable in the backend. /api/ws/mavlink CANNOT accept ANY connection, regardless of ws_clients or whether a bridge 'looks' connected. This is the exact dependency bug found this session — rebuild/redeploy the backend with the websockets package installed before any live demo."
  else
    warn "ws_upgrade_capable not reported (unexpected — check backend version)."
  fi

  if [ "$hackrf_live" = "true" ]; then
    pass "HackRF spectrum ingest is live (< 30s old)."
  else
    warn "HackRF spectrum ingest not live (hackrf=$hackrf_live). Detections may still arrive via SiK. Confirm this is expected for today's demo config."
  fi

  if [ "$sik_live" = "true" ]; then
    pass "SiK radio detection stream is live (< 60s old)."
  else
    warn "SiK radio detection stream not live (sik_radio=$sik_live). Confirm this is expected for today's demo config."
  fi

  # TX ack/outcome signals — catches a bridge that LOOKS connected (ws_clients
  # > 0) but whose deploy/broadcast requests never actually resolve to a real
  # confirmed NEUTRALIZED outcome (stuck bridge, serial write failing, etc).
  if [ -n "$tx_pending_acks" ] && [ "$tx_pending_acks" != "null" ] && [ "$tx_pending_acks" -gt 0 ] 2>/dev/null; then
    warn "tx_pending_acks=$tx_pending_acks — TX request(s) currently awaiting bridge confirmation. Expected to be transient (resolves within ACK_TIMEOUT_S); if this persists across repeated preflight runs, the bridge may be stuck."
  else
    pass "tx_pending_acks=0 — no TX requests stuck awaiting bridge confirmation."
  fi

  if [ "$tx_path_degraded" = "true" ]; then
    fail "tx_path_degraded=true — every recent TX outcome (last 15 min) was TX_TIMEOUT/TX_FAILED with zero NEUTRALIZED confirmations (timeout=$tx_recent_timeout failed=$tx_recent_failed). A bridge may be connected (ws_clients) but the TX path itself is not actually working. Investigate before a live demo."
  else
    pass "tx_path_degraded=false — no evidence of a systematically failing TX path in the last 15 min (neutralized=$tx_recent_neutralized timeout=$tx_recent_timeout failed=$tx_recent_failed)."
  fi
fi

# ---------------------------------------------------------------------------
# 7. TX-capable bridge services — informational only. Either being inactive
#    is EXPECTED (held back pending range authorization) and must NOT be
#    treated as a failure. This script never starts them.
# ---------------------------------------------------------------------------
section "7. TX-capable services (informational only — not started, not required)"

report_tx_service() {
  local svc="$1"
  if ! need systemctl; then
    echo "  [INFO] $svc — systemctl not available on this host, cannot check."
    return
  fi
  if ! systemctl list-unit-files "$svc" 2>/dev/null | grep -q "^$svc"; then
    echo "  [INFO] $svc — unit not found on this host (not installed in this deployment)."
    return
  fi
  local state
  state="$(systemctl is-active "$svc" 2>/dev/null || true)"
  echo "  [INFO] $svc — $state (informational; inactive is normal/expected pending range authorization)"
}

report_tx_service cema-rf-bridge.service
report_tx_service cema-jam-bridge.service
report_tx_service cema-mavlink-sniffer.service

echo "  NOTE: this script never starts, enables, or otherwise actuates these services."

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section "Summary"
echo "  PASS: $PASS_COUNT   WARN: $WARN_COUNT   FAIL: $FAIL_COUNT"
echo

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "${C_RED}${C_BOLD}[✗] PREFLIGHT FAILED — do NOT start the live demo until FAIL items above are resolved.${C_RESET}"
  exit 1
elif [ "$WARN_COUNT" -gt 0 ]; then
  echo "${C_YELLOW}${C_BOLD}[!] PREFLIGHT PASSED WITH WARNINGS — review WARN items above before proceeding.${C_RESET}"
  exit 0
else
  echo "${C_GREEN}${C_BOLD}[✓] PREFLIGHT PASSED — system appears ready for demo.${C_RESET}"
  exit 0
fi
