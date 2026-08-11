#!/opt/homebrew/opt/bash/bin/bash
#
# deploy.sh
#
# Atomic, checksum-verified, version-stamped deploy of this repo's tracked
# deployable file set to primary (172.16.16.196:/CEMA/joydipdemo).
#
# NOTE: requires bash >= 4 (associative arrays). macOS ships bash 3.2 as
# /bin/bash, so this hardcodes the Homebrew bash path, same as
# check_deploy_drift.sh. Run explicitly via:
#   /opt/homebrew/opt/bash/bin/bash scripts/deploy.sh [options]
#
# WHAT THIS SCRIPT DOES
# ----------------------------------------------------------------------------
#   1. Builds the deployable file set from `git ls-files` (so .gitignore is
#      respected automatically: .env, logs, IQ captures, models, TLS certs,
#      __pycache__, drift reports, etc. are never touched or overwritten,
#      because they were never tracked in the first place).
#   2. Copies that file set to a STAGING directory on primary under
#      /CEMA/joydipdemo/.deploy_staging/<version>/ via rsync --checksum
#      (content-hash based, not mtime-based).
#   3. Independently re-verifies EVERY staged file with sha256 (Mac side vs
#      primary side, computed fresh over SSH) before anything is moved into
#      the live tree. If ANY file fails checksum verification, the script
#      aborts before touching the live tree at all.
#   4. Only after 100% of staged files verify does it atomically move each
#      file from staging into its final live path with `mv` (same
#      filesystem on primary, so mv is a rename - atomic per file, and the
#      whole batch completes in a tight loop so there is no long window of
#      a half-updated tree). A partial/interrupted transfer during step 2-3
#      never reaches this step, so it can never leave a half-updated live
#      tree.
#   5. Writes a version stamp:
#        - locally: appends a line to scripts/deploy_log/deploy_log.tsv
#          (git-tracked, so deploy history travels with the repo)
#        - on primary: overwrites /CEMA/joydipdemo/DEPLOYED_VERSION with the
#          git short-SHA, UTC timestamp, and which top-level dirs were
#          included in this deploy.
#   6. Prints, per top-level directory deployed, whether it requires a
#      rebuild/restart to take effect (backend/frontend -> docker rebuild;
#      field-bridge/rf-bridge -> systemd service restart; docker-compose.yml
#      / caddy -> compose restart) and explicitly does NOT perform any of
#      those actions. Restart/rebuild is always a separate, deliberate,
#      human-run follow-on step.
#
# WHAT THIS SCRIPT NEVER DOES
# ----------------------------------------------------------------------------
#   - Never touches anything not tracked by `git ls-files` (so .env, *.log,
#     iq_captures/, fpv_captures/, models/, caddy/*.crt, __pycache__/,
#     drift_reports/ etc. on primary are always left alone).
#   - Never restarts or rebuilds any service/container.
#   - Never proceeds past checksum verification if any file fails.
#   - Never runs without --apply (default is a dry run that reports exactly
#     what WOULD be deployed and exits 0/1 without touching primary's live
#     tree; it still creates a throwaway staging copy + verifies checksums,
#     so a dry run is a genuine rehearsal, not just a print statement).
#
# USAGE
#   scripts/deploy.sh --dry-run [--dirs backend,frontend,field-bridge,...]
#   scripts/deploy.sh --apply   [--dirs ...] [--yes]
#
#   --dirs <comma-list>  Restrict to these top-level deployable dirs/files.
#                         Default: backend,frontend,field-bridge,rf-bridge,
#                         docker-compose.yml,caddy
#   --dry-run             Stage + checksum-verify only. Never touches the
#                          live tree on primary. This is the default mode.
#   --apply                Actually swap verified files into the live tree
#                          on primary. Requires explicit confirmation unless
#                          --yes is also passed.
#   --yes                  Skip the interactive confirmation prompt (still
#                          requires --apply to do anything live).
#
# Credentials: read from environment (PRIMARY_SSH_PASS) if set, otherwise
# prompted for interactively via `read -s` (never written to disk).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="172.16.16.196"
REMOTE_USER="biswajit"
REMOTE_PATH="/CEMA/joydipdemo"
DEFAULT_DIRS="backend,frontend,field-bridge,rf-bridge,docker-compose.yml,caddy"

DIRS_CSV="$DEFAULT_DIRS"
MODE="dry-run"
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dirs) DIRS_CSV="$2"; shift 2 ;;
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

IFS=',' read -r -a DIRS <<< "$DIRS_CSV"

cd "$REPO_ROOT" || exit 1

# ---------------------------------------------------------------------------
# SSH credential handling (non-printing, not written to disk)
# ---------------------------------------------------------------------------
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  read -rs -p "Password for ${REMOTE_USER}@${REMOTE_HOST}: " PRIMARY_SSH_PASS
  echo
fi
export SSHPASS="$PRIMARY_SSH_PASS"

ssh_remote() {
  sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
    "${REMOTE_USER}@${REMOTE_HOST}" "$@"
}

if ! ssh_remote "echo OK" > /dev/null 2>&1; then
  echo "ERROR: cannot SSH to ${REMOTE_USER}@${REMOTE_HOST}. Aborting." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Build the deployable file list strictly from git (so .gitignore is
# respected: .env, logs, captures, models, certs, caches never included).
# ---------------------------------------------------------------------------
GIT_SHA="$(git rev-parse --short HEAD)"
DIRTY=""
if ! git diff --quiet || ! git diff --cached --quiet; then
  DIRTY="-dirty"
fi
TS_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
VERSION="${GIT_SHA}${DIRTY}_${TS_UTC}"

FILE_LIST_TMP="$(mktemp)"
: > "$FILE_LIST_TMP"
for d in "${DIRS[@]}"; do
  if [[ -f "$d" ]]; then
    # a single tracked root file, e.g. docker-compose.yml
    if git ls-files --error-unmatch "$d" > /dev/null 2>&1; then
      echo "$d" >> "$FILE_LIST_TMP"
    fi
  elif [[ -d "$d" ]]; then
    git ls-files "$d" >> "$FILE_LIST_TMP"
  else
    echo "WARNING: '$d' not found in repo, skipping." >&2
  fi
done
sort -u -o "$FILE_LIST_TMP" "$FILE_LIST_TMP"

NUM_FILES="$(wc -l < "$FILE_LIST_TMP" | tr -d ' ')"
if [[ "$NUM_FILES" -eq 0 ]]; then
  echo "Nothing to deploy for dirs: ${DIRS_CSV}"
  rm -f "$FILE_LIST_TMP"
  exit 0
fi

echo "=================================================================="
echo "Deploy plan"
echo "  Version:  ${VERSION}"
echo "  Dirs:     ${DIRS_CSV}"
echo "  Files:    ${NUM_FILES} (git-tracked only; .gitignore respected)"
echo "  Mode:     ${MODE}"
echo "=================================================================="

# ---------------------------------------------------------------------------
# Stage: rsync the exact file list into a version-named staging dir on
# primary. rsync --checksum forces content-hash comparison (not mtime), and
# staging is always a fresh directory so a prior failed run can never
# contaminate this one.
# ---------------------------------------------------------------------------
STAGING_REL=".deploy_staging/${VERSION}"
STAGING_ABS="${REMOTE_PATH}/${STAGING_REL}"

ssh_remote "mkdir -p '${STAGING_ABS}'" || { echo "ERROR: could not create staging dir on primary" >&2; exit 3; }

echo "Staging ${NUM_FILES} file(s) to primary:${STAGING_ABS} ..."
rsync -a --checksum --relative --files-from="$FILE_LIST_TMP" \
  -e "sshpass -e ssh -o StrictHostKeyChecking=accept-new" \
  "$REPO_ROOT/./" "${REMOTE_USER}@${REMOTE_HOST}:${STAGING_ABS}/"
RSYNC_STATUS=$?
if [[ "$RSYNC_STATUS" -ne 0 ]]; then
  echo "ERROR: rsync to staging failed (exit ${RSYNC_STATUS}). Live tree untouched. Leaving staging dir for inspection: ${STAGING_ABS}" >&2
  rm -f "$FILE_LIST_TMP"
  exit 4
fi

# ---------------------------------------------------------------------------
# Verify: independently sha256 every staged file against the Mac source,
# BEFORE touching the live tree. Any mismatch aborts with the live tree
# completely untouched.
# ---------------------------------------------------------------------------
echo "Verifying checksums (staged vs Mac source) ..."
REMOTE_HASHES_TMP="$(mktemp)"
(cd "$REPO_ROOT" && cat "$FILE_LIST_TMP") | ssh_remote "cd '${STAGING_ABS}' && xargs -I{} sh -c 'test -f \"{}\" && shasum -a 256 \"{}\" || echo MISSING__{}' 2>/dev/null" > "$REMOTE_HASHES_TMP"

declare -A REMOTE_HASH
FAILED=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  if [[ "$line" == MISSING__* ]]; then
    echo "  [MISSING IN STAGING] ${line#MISSING__}" >&2
    FAILED=1
    continue
  fi
  h="${line%% *}"
  f="${line#* }"
  f="${f#\*}"
  f="$(echo "$f" | sed -E 's/^ +//')"
  REMOTE_HASH["$f"]="$h"
done < "$REMOTE_HASHES_TMP"

MISMATCH_COUNT=0
VERIFIED_COUNT=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  mac_h="$(shasum -a 256 "$f" | awk '{print $1}')"
  rem_h="${REMOTE_HASH[$f]:-}"
  if [[ -z "$rem_h" ]]; then
    echo "  [MISSING IN STAGING] $f" >&2
    MISMATCH_COUNT=$((MISMATCH_COUNT+1))
  elif [[ "$mac_h" != "$rem_h" ]]; then
    echo "  [CHECKSUM MISMATCH] $f  mac=${mac_h:0:16} staged=${rem_h:0:16}" >&2
    MISMATCH_COUNT=$((MISMATCH_COUNT+1))
  else
    VERIFIED_COUNT=$((VERIFIED_COUNT+1))
  fi
done < "$FILE_LIST_TMP"

rm -f "$REMOTE_HASHES_TMP"

if [[ "$MISMATCH_COUNT" -gt 0 || "$FAILED" -eq 1 ]]; then
  echo "ERROR: ${MISMATCH_COUNT} file(s) failed checksum verification. Aborting - live tree on primary is UNTOUCHED." >&2
  echo "Staging dir left in place for inspection: ${STAGING_ABS}" >&2
  rm -f "$FILE_LIST_TMP"
  exit 5
fi

echo "All ${VERIFIED_COUNT} staged files verified (sha256 match)."

if [[ "$MODE" == "dry-run" ]]; then
  echo "=================================================================="
  echo "DRY RUN complete: staged + fully checksum-verified, live tree NOT touched."
  echo "Re-run with --apply to swap these files into place."
  echo "Cleaning up staging dir on primary ..."
  ssh_remote "rm -rf '${STAGING_ABS}'"
  rm -f "$FILE_LIST_TMP"
  exit 0
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -rp "Apply: move ${VERIFIED_COUNT} verified file(s) into the live tree on primary now? [y/N] " CONFIRM
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "Aborted by user. Live tree untouched. Cleaning up staging dir ..."
    ssh_remote "rm -rf '${STAGING_ABS}'"
    rm -f "$FILE_LIST_TMP"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Apply: move each verified staged file into place. Staging and live tree
# are on the same filesystem on primary, so `mv` per file is an atomic
# rename - no file is ever partially written into its live path, and the
# loop is tight (no network I/O per file, pure local mv) so the whole batch
# completes in a fraction of a second's wall-clock window per file.
# ---------------------------------------------------------------------------
echo "Applying: moving verified files into live tree ..."
MOVE_SCRIPT_TMP="$(mktemp)"
{
  echo "set -e"
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    dir="$(dirname "$f")"
    echo "mkdir -p '${REMOTE_PATH}/${dir}'"
    echo "mv -f '${STAGING_ABS}/${f}' '${REMOTE_PATH}/${f}'"
  done < "$FILE_LIST_TMP"
} > "$MOVE_SCRIPT_TMP"

cat "$MOVE_SCRIPT_TMP" | ssh_remote "bash -s"
APPLY_STATUS=$?
rm -f "$MOVE_SCRIPT_TMP"

if [[ "$APPLY_STATUS" -ne 0 ]]; then
  echo "ERROR: apply step failed partway (exit ${APPLY_STATUS}). Live tree may be PARTIALLY updated - inspect immediately." >&2
  echo "Remaining staged files (if any) are at: ${STAGING_ABS}" >&2
  rm -f "$FILE_LIST_TMP"
  exit 6
fi

ssh_remote "rm -rf '${STAGING_ABS}'"

# ---------------------------------------------------------------------------
# Version stamp: primary-side DEPLOYED_VERSION + repo-side deploy log
# ---------------------------------------------------------------------------
DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ssh_remote "cat > '${REMOTE_PATH}/DEPLOYED_VERSION' <<EOF
git_sha=${GIT_SHA}${DIRTY}
version=${VERSION}
deployed_at_utc=${DEPLOYED_AT}
dirs=${DIRS_CSV}
files_count=${NUM_FILES}
deployed_by=${USER:-unknown}@$(hostname -s 2>/dev/null || echo mac)
EOF"

mkdir -p "${REPO_ROOT}/scripts/deploy_log"
LOG_FILE="${REPO_ROOT}/scripts/deploy_log/deploy_log.tsv"
if [[ ! -f "$LOG_FILE" ]]; then
  printf "timestamp_utc\tgit_sha\tversion\tdirs\tfiles_count\n" > "$LOG_FILE"
fi
printf "%s\t%s\t%s\t%s\t%s\n" "$DEPLOYED_AT" "${GIT_SHA}${DIRTY}" "$VERSION" "$DIRS_CSV" "$NUM_FILES" >> "$LOG_FILE"

echo "=================================================================="
echo "DEPLOY COMPLETE: ${VERIFIED_COUNT} file(s) live on primary."
echo "Version stamp written to primary:${REMOTE_PATH}/DEPLOYED_VERSION"
echo "Deploy logged to: ${LOG_FILE}"
echo ""
echo "Rebuild/restart REQUIRED to activate this code (NOT performed by this script):"
for d in "${DIRS[@]}"; do
  case "$d" in
    backend)  echo "  - backend/       -> docker compose build backend && docker compose up -d backend  (or restart cema-backend container)" ;;
    frontend) echo "  - frontend/      -> docker compose build frontend && docker compose up -d frontend (or restart cema-frontend container)" ;;
    field-bridge) echo "  - field-bridge/  -> restart the relevant systemd unit(s) on primary (cema-*.service), per-parser" ;;
    rf-bridge)    echo "  - rf-bridge/     -> restart cema-rf-bridge.service on primary" ;;
    docker-compose.yml) echo "  - docker-compose.yml -> docker compose up -d (recreate affected containers)" ;;
    caddy)    echo "  - caddy/         -> docker compose restart caddy (or reload if only Caddyfile changed)" ;;
  esac
done
echo "This script deliberately does NOT restart/rebuild anything. TX-halt state and"
echo "running services are untouched by this script by design."
echo "=================================================================="

rm -f "$FILE_LIST_TMP"
exit 0
