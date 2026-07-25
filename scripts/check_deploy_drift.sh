#!/opt/homebrew/opt/bash/bin/bash
#
# check_deploy_drift.sh
#
# NOTE: requires bash >= 4 (associative arrays). macOS ships bash 3.2 as
# /bin/bash, so this script hardcodes the Homebrew bash path. If Homebrew
# bash lives elsewhere on your machine, adjust the shebang or run explicitly
# via: /opt/homebrew/opt/bash/bin/bash scripts/check_deploy_drift.sh
#
# Purpose: detect drift between the Mac repo (canonical source of truth for
# Meghaduta/CeCDS) and what is actually deployed/running on the primary
# server (172.16.16.196, /CEMA/joydipdemo).
#
# This is a READ-ONLY report tool. It NEVER copies files in either direction.
# Deploys remain a deliberate, reviewed, manual action (scp/rsync run by a
# human or an agent that has been explicitly asked to deploy).
#
# For every git-tracked file under backend/, field-bridge/, frontend/src/
# it compares the remote (primary) file content against:
#   1. The Mac working tree's current content (HEAD/working copy)
#   2. Every blob that path has ever had in Mac git history
#
# Classification per file:
#   IN SYNC              - remote content == Mac working tree content
#   PENDING DEPLOY        - remote content matches some OLDER commit of this
#                           file in Mac history, but not the current one =>
#                           Mac has changes primary hasn't received yet.
#   UNDOCUMENTED DRIFT    - remote content does not match the current Mac
#                           content NOR any past commit of this file =>
#                           primary was edited directly / out-of-band and
#                           Mac does not know about this version. Needs
#                           manual reconciliation before any deploy, or the
#                           work will be silently lost.
#   MISSING ON PRIMARY    - file is tracked in Mac git but does not exist
#                           on primary at all.
#   MISSING ON MAC        - file exists on primary under a watched
#                           directory but is not tracked in Mac git (could
#                           be a host-only file, or an untracked addition
#                           that needs to be added to git).
#
# Host-only / expected-to-differ files (e.g. field-bridge/.env, anything
# matching Mac's own .gitignore) are reported separately and never counted
# as drift.
#
# Usage:
#   scripts/check_deploy_drift.sh                 # human-readable report to stdout + log file
#   scripts/check_deploy_drift.sh --quiet         # only print the summary line + write log file
#
# Credentials: read from environment (PRIMARY_SSH_PASS) if set, otherwise
# prompted for interactively via `read -s` (never written to disk, never
# echoed, never passed as a bare argv password on the remote host).

set -uo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="172.16.16.196"
REMOTE_USER="biswajit"
REMOTE_PATH="/CEMA/joydipdemo"
WATCH_DIRS=("backend" "field-bridge" "frontend/src")
MAX_HISTORY_DEPTH=200   # how many past commits per file to check when classifying undocumented drift
LOG_DIR="${REPO_ROOT}/scripts/drift_reports"
TS="$(date +%Y%m%dT%H%M%S)"
LOG_FILE="${LOG_DIR}/drift_${TS}.log"
QUIET=0

for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
  esac
done

mkdir -p "$LOG_DIR"

log() {
  # print to stdout (unless --quiet) and always append to log file
  if [[ "$QUIET" -eq 0 ]]; then
    echo "$@"
  fi
  echo "$@" >> "$LOG_FILE"
}

cd "$REPO_ROOT" || exit 1

# ---------------------------------------------------------------------------
# SSH credential handling (non-printing, not written to disk)
# ---------------------------------------------------------------------------
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  read -rs -p "Password for ${REMOTE_USER}@${REMOTE_HOST}: " PRIMARY_SSH_PASS
  echo
fi
# sshpass -e reads the password from the SSHPASS env var specifically.
export SSHPASS="$PRIMARY_SSH_PASS"

ssh_remote() {
  # Run a command on primary. Never echoes the password. Password only lives
  # in this shell's environment, never on disk on either end.
  sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
    "${REMOTE_USER}@${REMOTE_HOST}" "$@"
}

# Sanity check connectivity before doing real work
if ! ssh_remote "echo OK" > /dev/null 2>&1; then
  echo "ERROR: cannot SSH to ${REMOTE_USER}@${REMOTE_HOST}. Aborting." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Determine which files are "expected to differ" (host-only / gitignored)
# ---------------------------------------------------------------------------
is_expected_hostonly() {
  local path="$1"
  case "$path" in
    field-bridge/.env|*/.env|*.env|*.env.*) return 0 ;;
    */.venv/*|*/venv/*|*/.e2e-venv/*|*/__pycache__/*|*.pyc) return 0 ;;
    */.pytest_cache/*|*/node_modules/*|*/.git/*) return 0 ;;
  esac
  # anything Mac's own .gitignore would exclude is host-only/local by
  # definition and is never expected to be identical across hosts.
  if git check-ignore -q -- "$path" 2>/dev/null; then
    return 0
  fi
  return 1
}

# ---------------------------------------------------------------------------
# Build file lists
# ---------------------------------------------------------------------------
MAC_FILES_TMP="$(mktemp)"
git ls-files "${WATCH_DIRS[@]}" > "$MAC_FILES_TMP"

REMOTE_FILES_TMP="$(mktemp)"
# List all files under the watched dirs on primary (tracked or not by its
# own local git state - we don't trust or use primary's git history at all,
# we only care about live file content).
find_expr=""
for d in "${WATCH_DIRS[@]}"; do
  find_expr="${find_expr} ${REMOTE_PATH}/${d}"
done
ssh_remote "cd ${REMOTE_PATH} && find ${WATCH_DIRS[*]} -type f 2>/dev/null" > "$REMOTE_FILES_TMP"

cleanup() {
  rm -f "$MAC_FILES_TMP" "$REMOTE_FILES_TMP" "${REMOTE_HASHES_TMP:-}" 2>/dev/null
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Get remote sha256 for every remote-side file in one round trip
# ---------------------------------------------------------------------------
REMOTE_HASHES_TMP="$(mktemp)"
ssh_remote "cd ${REMOTE_PATH} && find ${WATCH_DIRS[*]} -type f -exec shasum -a 256 {} \; 2>/dev/null" > "$REMOTE_HASHES_TMP"

declare -A REMOTE_HASH
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  h="${line%% *}"
  f="${line#* }"
  # normalize potential leading '*' from shasum binary mode marker, and
  # collapse repeated spaces between hash and filename
  f="${f#\*}"
  f="$(echo "$f" | sed -E 's/^ +//')"
  REMOTE_HASH["$f"]="$h"
done < "$REMOTE_HASHES_TMP"

# ---------------------------------------------------------------------------
# Get local sha256 for every mac-tracked file
# ---------------------------------------------------------------------------
declare -A MAC_HASH
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if [[ -f "$f" ]]; then
    h="$(shasum -a 256 "$f" | awk '{print $1}')"
    MAC_HASH["$f"]="$h"
  fi
done < "$MAC_FILES_TMP"

# ---------------------------------------------------------------------------
# Classify each Mac-tracked file
# ---------------------------------------------------------------------------
PENDING_DEPLOY=()
UNDOCUMENTED=()
MISSING_ON_PRIMARY=()
IN_SYNC_COUNT=0
EXPECTED_DIFF=()

find_blob_match_commit() {
  # Given a path and a target sha256, search Mac git history for a commit
  # where this path's blob content hashes to target sha256. Prints the
  # short commit hash + date if found, else prints nothing.
  local path="$1" target="$2"
  local commits
  commits="$(git log --format=%H -n "$MAX_HISTORY_DEPTH" -- "$path" 2>/dev/null)"
  local c
  for c in $commits; do
    local h
    h="$(git show "${c}:${path}" 2>/dev/null | shasum -a 256 | awk '{print $1}')"
    if [[ "$h" == "$target" ]]; then
      local d
      d="$(git show -s --format=%ci "$c")"
      echo "${c:0:9} (${d})"
      return 0
    fi
  done
  return 1
}

while IFS= read -r f; do
  [[ -z "$f" ]] && continue

  if is_expected_hostonly "$f"; then
    EXPECTED_DIFF+=("$f")
    continue
  fi

  mac_h="${MAC_HASH[$f]:-}"
  rem_h="${REMOTE_HASH[$f]:-}"

  if [[ -z "$rem_h" ]]; then
    MISSING_ON_PRIMARY+=("$f")
    continue
  fi

  if [[ "$mac_h" == "$rem_h" ]]; then
    IN_SYNC_COUNT=$((IN_SYNC_COUNT + 1))
    continue
  fi

  # Content differs. Determine direction.
  match="$(find_blob_match_commit "$f" "$rem_h")"
  if [[ -n "$match" ]]; then
    PENDING_DEPLOY+=("$f  (primary is running an older version, last matched Mac commit ${match})")
  else
    UNDOCUMENTED+=("$f  (primary's current content does not match Mac HEAD or any past commit in Mac history for this file - was it edited directly on primary?)")
  fi
done < "$MAC_FILES_TMP"

# ---------------------------------------------------------------------------
# Files present on primary but not tracked in Mac git at all (new/rogue files)
# ---------------------------------------------------------------------------
MAC_ONLY_REL_SET="$(sort "$MAC_FILES_TMP")"
UNTRACKED_ON_PRIMARY=()
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if ! grep -qxF "$f" "$MAC_FILES_TMP"; then
    if is_expected_hostonly "$f"; then
      EXPECTED_DIFF+=("$f (primary-only, expected)")
    else
      UNTRACKED_ON_PRIMARY+=("$f")
    fi
  fi
done < "$REMOTE_FILES_TMP"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
: > "$LOG_FILE"
log "=================================================================="
log "Deploy drift report: Mac repo  vs  primary (${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH})"
log "Generated: $(date)"
log "Watched paths: ${WATCH_DIRS[*]}"
log "=================================================================="
log ""
log "IN SYNC: ${IN_SYNC_COUNT} file(s) identical between Mac and primary."
log ""

if [[ "${#PENDING_DEPLOY[@]}" -gt 0 ]]; then
  log "---- PENDING DEPLOY (Mac has changes primary does NOT have yet) ----"
  log "  Action: these need to be shipped to primary via scp/rsync when ready."
  for f in "${PENDING_DEPLOY[@]}"; do
    log "  [PENDING DEPLOY] $f"
  done
  log ""
else
  log "---- PENDING DEPLOY: none ----"
  log ""
fi

if [[ "${#UNDOCUMENTED[@]}" -gt 0 ]]; then
  log "---- UNDOCUMENTED DRIFT (primary has content Mac has never seen) ----"
  log "  Action: DO NOT deploy over these blindly. Pull primary's actual"
  log "  content and reconcile/commit to Mac git before any further deploy,"
  log "  or the out-of-band change will be silently overwritten and lost."
  for f in "${UNDOCUMENTED[@]}"; do
    log "  [UNDOCUMENTED DRIFT] $f"
  done
  log ""
else
  log "---- UNDOCUMENTED DRIFT: none ----"
  log ""
fi

if [[ "${#MISSING_ON_PRIMARY[@]}" -gt 0 ]]; then
  log "---- MISSING ON PRIMARY (tracked in Mac git, absent on primary) ----"
  for f in "${MISSING_ON_PRIMARY[@]}"; do
    log "  [MISSING ON PRIMARY] $f"
  done
  log ""
fi

if [[ "${#UNTRACKED_ON_PRIMARY[@]}" -gt 0 ]]; then
  log "---- FILES ON PRIMARY NOT TRACKED IN MAC GIT (possible rogue/local additions) ----"
  for f in "${UNTRACKED_ON_PRIMARY[@]}"; do
    log "  [UNTRACKED ON PRIMARY] $f"
  done
  log ""
fi

if [[ "${#EXPECTED_DIFF[@]}" -gt 0 ]]; then
  log "---- EXPECTED HOST-ONLY DIFFERENCES (not drift, informational only) ----"
  for f in "${EXPECTED_DIFF[@]}"; do
    log "  [expected] $f"
  done
  log ""
fi

log "=================================================================="
STATUS="CLEAN"
if [[ "${#UNDOCUMENTED[@]}" -gt 0 ]]; then
  STATUS="UNDOCUMENTED_DRIFT_PRESENT"
elif [[ "${#PENDING_DEPLOY[@]}" -gt 0 ]]; then
  STATUS="DEPLOY_PENDING"
fi
log "SUMMARY: status=${STATUS}  pending_deploy=${#PENDING_DEPLOY[@]}  undocumented_drift=${#UNDOCUMENTED[@]}  missing_on_primary=${#MISSING_ON_PRIMARY[@]}  untracked_on_primary=${#UNTRACKED_ON_PRIMARY[@]}  in_sync=${IN_SYNC_COUNT}"
log "Full log: ${LOG_FILE}"
log "=================================================================="

# Exit code encodes severity for scripting/cron use:
# 0 = clean, 1 = pending deploy only (informational), 2 = undocumented drift (needs attention)
if [[ "$STATUS" == "UNDOCUMENTED_DRIFT_PRESENT" ]]; then
  exit 2
elif [[ "$STATUS" == "DEPLOY_PENDING" ]]; then
  exit 1
else
  exit 0
fi
