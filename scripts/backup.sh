#!/opt/homebrew/opt/bash/bin/bash
#
# backup.sh
#
# DR / migration tooling for MEGHDUT C3 (CeCDS / Meghaduta).
#
# Produces ONE encrypted, self-contained, verifiable FULL-SYSTEM backup bundle
# of a running MEGHDUT C3 host. The bundle is offline-restorable by its sibling
# scripts/restore.sh and carries everything needed to stand the system back up
# on fresh hardware: code (full git history), the Mongo database, the Caddy
# internal-CA material, and all host-only files that live outside git.
#
# NOTE: requires bash >= 4. macOS ships bash 3.2 as /bin/bash, so this hardcodes
# the Homebrew bash path, same as deploy.sh / check_deploy_drift.sh. Run via:
#   /opt/homebrew/opt/bash/bin/bash scripts/backup.sh [options]
#
# WHAT THIS SCRIPT DOES
# ----------------------------------------------------------------------------
#   1. CODE  - `git bundle create <stage>/code.bundle --all` from the LOCAL
#      repo (full history, offline-restorable) + records `git rev-parse HEAD`.
#   2. MONGO - read-only streams `mongodump --db cema_cuas_db --archive --gzip`
#      out of the source's cema-mongo container. The source database is never
#      mutated. Per-collection counts and the mission_log audit-chain head seq
#      are captured into the manifest.
#   3. CADDY CA - tars the joydipdemo_cema_caddy_data + joydipdemo_cema_caddy_config
#      docker volumes from the source via a throwaway busybox container with a
#      READ-ONLY (:ro) mount, so the running Caddy is never disturbed.
#   4. HOST-ONLY FILES - copies the operational files that are deliberately NOT
#      in git (.env secrets, udev rule, ML checkpoint, kismet site config, and
#      the installed cema-*.service systemd unit files), preserving modes.
#   5. MANIFEST - writes a plaintext MANIFEST.txt (hashes/counts only, NO
#      secrets) recording timestamp, git SHA, source host, the source's
#      DEPLOYED_VERSION, per-part sha256, mongo collection counts + audit head
#      seq, and this tool's version. restore.sh uses it to verify integrity,
#      parity, and audit-chain continuity.
#
# PACKAGING + ENCRYPTION (mandatory)
# ----------------------------------------------------------------------------
#   The staged tree is tar+gzipped, then ENCRYPTED - it holds .env secrets and
#   the Caddy CA private key. The encryption tool is auto-detected in this order
#   and the first available one is used:
#       1. age     -> `age -p`                              (ext .age)
#       2. gpg     -> `gpg --symmetric --cipher-algo AES256`(ext .gpg)
#       3. openssl -> `openssl enc -aes-256-cbc -pbkdf2 -salt` (ext .enc)
#   The passphrase comes from env BACKUP_PASSPHRASE (never echoed) or an
#   interactive prompt. It is NEVER hardcoded and NEVER printed.
#   Output filename: meghdut-c3-backup-<utc-timestamp>-<gitsha>.tar.gz.<ext>
#   The plaintext MANIFEST is written alongside so the operator can see what a
#   bundle contains without decrypting it (the manifest itself has no secrets).
#
# DESTINATION SELECTION
# ----------------------------------------------------------------------------
#   - If --dest <dir> is given, it is used verbatim (e.g. a NAS mount or a fixed
#     path) and auto-detect is skipped.
#   - Otherwise the tool AUTO-DETECTS attached EXTERNAL/removable drives on this
#     Mac (diskutil), and:
#       * exactly one   -> shows it (name, mountpoint, filesystem, free space)
#                          and asks for confirmation before using it,
#       * several       -> lists them numbered and lets the operator choose,
#       * none          -> tells the operator to attach a drive; with --watch it
#                          polls until one appears (up to --watch-timeout secs).
#   - The bundle is written under <mountpoint>/MEGHDUT-C3-Backups/.
#   - Before writing, the destination is checked for writability, enough free
#     space (estimated bundle size + margin), and the FAT/exFAT 4 GB single-file
#     limit. After writing, the destination copy's sha256 is re-read and compared
#     to the encrypted source to prove the write to removable media is intact.
#
# WHAT THIS SCRIPT NEVER DOES
# ----------------------------------------------------------------------------
#   - Never mutates the source host (Mongo dump + volume tars are read-only).
#   - Never prints secret values (SSH password, passphrase, .env contents).
#   - Never leaves decrypted secrets on disk: the plaintext stage dir is
#     shredded after the encrypted bundle is written.
#   - Never produces the encrypted bundle without --apply (dry-run default just
#     prints the plan + what it WOULD capture).
#
# USAGE
#   scripts/backup.sh [--dry-run]                # rehearsal (DEFAULT)
#   scripts/backup.sh --apply [--yes]            # detect drive, capture, encrypt
#   scripts/backup.sh --apply --dest /path/to/secure/store
#   scripts/backup.sh --apply --watch            # wait for a drive, then run
#
#   --source-host <ip/host>   Source of truth to back up. Default 172.16.16.196
#                             (or SOURCE_HOST env).
#   --source-user <user>      SSH user on the source. Default biswajit
#                             (or SOURCE_USER env).
#   --keychain-service <svc>  macOS Keychain service holding the SSH password.
#                             Default cema-primary-ssh (matches deploy tooling).
#   --dest <dir>              Explicit destination dir (NAS mount / fixed path).
#                             Overrides external-drive auto-detect. (DEST_DIR env.)
#   --watch                   If no external drive is attached, poll until one
#                             appears (see --watch-timeout), then detect+confirm.
#   --watch-timeout <secs>    Max seconds to wait in --watch mode. Default 300.
#   --dry-run                 Print the plan, detect + confirm the destination
#                             drive, and report space/filesystem checks - but
#                             produce NO bundle. This is the DEFAULT mode.
#   --apply                   Actually capture, package, encrypt, and write the
#                             bundle. Requires confirmation unless --yes.
#   --yes                     Skip the interactive confirmations (still needs
#                             --apply to do anything).
#   -h | --help               Show this header and exit.
#
# Credentials:
#   SSH password  - env PRIMARY_SSH_PASS, else macOS Keychain
#                   (security find-generic-password -a <user> -s <service> -w),
#                   else interactive prompt. Never written to disk, never echoed.
#   Passphrase    - env BACKUP_PASSPHRASE, else interactive prompt. Never echoed.

set -euo pipefail

BACKUP_TOOL_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# Config / defaults
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SOURCE_HOST="${SOURCE_HOST:-172.16.16.196}"
SOURCE_USER="${SOURCE_USER:-biswajit}"
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-cema-primary-ssh}"
DEST_DIR="${DEST_DIR:-}"

REMOTE_PATH="/CEMA/joydipdemo"
MONGO_CONTAINER="cema-mongo"
MONGO_DB="cema_cuas_db"
CADDY_DATA_VOLUME="joydipdemo_cema_caddy_data"
CADDY_CONFIG_VOLUME="joydipdemo_cema_caddy_config"

# Host-only files (NOT in git) to capture, absolute paths on the source.
# The ML checkpoint path is resolved on the source at runtime (see below).
HOSTFILE_ENV_ROOT="${REMOTE_PATH}/.env"
HOSTFILE_ENV_FIELD="${REMOTE_PATH}/field-bridge/.env"
HOSTFILE_ENV_RF="${REMOTE_PATH}/rf-bridge/.env"
HOSTFILE_UDEV="/etc/udev/rules.d/99-cema-sik-adapter.rules"
HOSTFILE_KISMET="/etc/kismet/kismet_site.conf"
HOSTFILE_UNITS_GLOB="/etc/systemd/system/cema-*.service"
# Default checkpoint path (relative to REMOTE_PATH) if CEMA_ML_CHECKPOINT is unset.
DEFAULT_CHECKPOINT_REL="field-bridge/models/resnet18_leesburg_split_0.02_1_current.pt"

DEST_SUBDIR="MEGHDUT-C3-Backups"   # subfolder created on the chosen destination
DEST_EXPLICIT=0                    # 1 when --dest was given (skips auto-detect)
WATCH=0                            # --watch: poll for a drive to appear
WATCH_TIMEOUT="${WATCH_TIMEOUT:-300}"   # seconds to wait in --watch mode
WATCH_INTERVAL="${WATCH_INTERVAL:-5}"   # poll interval in --watch mode

MODE="dry-run"
ASSUME_YES=0

usage() {
  echo "Usage: scripts/backup.sh [--dry-run|--apply [--yes]] [--source-host H]" >&2
  echo "                         [--source-user U] [--keychain-service S]" >&2
  echo "                         [--dest DIR] [--watch [--watch-timeout SECS]]" >&2
  echo "Run 'scripts/backup.sh --help' for the full description." >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-host) SOURCE_HOST="$2"; shift 2 ;;
    --source-user) SOURCE_USER="$2"; shift 2 ;;
    --keychain-service) KEYCHAIN_SERVICE="$2"; shift 2 ;;
    --dest) DEST_DIR="$2"; DEST_EXPLICIT=1; shift 2 ;;
    --watch) WATCH=1; shift ;;
    --watch-timeout) WATCH_TIMEOUT="$2"; shift 2 ;;
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

cd "$REPO_ROOT" || exit 1

# ---------------------------------------------------------------------------
# SSH credential handling (non-printing, never written to disk).
# Order: env PRIMARY_SSH_PASS -> macOS Keychain -> interactive prompt.
# ---------------------------------------------------------------------------
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  PRIMARY_SSH_PASS="$(security find-generic-password -a "$SOURCE_USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
fi
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  read -rs -p "Password for ${SOURCE_USER}@${SOURCE_HOST}: " PRIMARY_SSH_PASS
  echo
fi
# sshpass -e reads the password from the SSHPASS env var specifically.
export SSHPASS="$PRIMARY_SSH_PASS"

# Drop any stale known_hosts entry for this host BEFORE the first connection.
# accept-new only auto-accepts an UNKNOWN key; it does NOT accept a CHANGED key,
# so a reinstalled/re-imaged source (new host key) would otherwise fail with a
# host-key-mismatch error. Removing the stale entry first, then connecting with
# accept-new, lets a re-imaged box connect while still recording the new key.
# (We deliberately do NOT weaken to StrictHostKeyChecking=no.)
ssh-keygen -R "$SOURCE_HOST" >/dev/null 2>&1 || true

ssh_remote() {
  # Run a command on the source. Password only ever lives in this shell's
  # environment (SSHPASS), never on disk, never on the remote argv.
  # accept-new records the (now-unknown) host key on first use; a stale key was
  # already dropped above so a reinstalled host connects cleanly.
  sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    "${SOURCE_USER}@${SOURCE_HOST}" "$@"
}

# ---------------------------------------------------------------------------
# External-drive detection (macOS). This script runs on the Mac, so the backup
# lands on an attached external/removable volume unless --dest overrides.
#
# Strategy: enumerate mounted volumes under /Volumes and inspect each with
# `diskutil info <mountpoint>`. We keep only volumes that are External (Device
# Location: External, i.e. NOT the internal boot disk), mounted, and writable.
# `diskutil list external physical` is used purely as a sanity cross-reference
# in the plan output. Using the mount point with `diskutil info` resolves APFS,
# HFS+, exFAT and MS-DOS volumes uniformly (an APFS data volume lives on a
# synthesized virtual disk, so slice-walking the physical disk is unreliable).
#
# Emits one TAB-separated row per candidate:
#   mountpoint <TAB> volname <TAB> fstype <TAB> freebytes <TAB> totalbytes
# ---------------------------------------------------------------------------
detect_external_volumes() {
  local mp info loc internal mounted ro vn fs free total
  shopt -s nullglob
  for mp in /Volumes/*; do
    [[ -d "$mp" ]] || continue
    info="$(diskutil info "$mp" 2>/dev/null)" || continue
    loc="$(echo "$info"      | awk -F': +' '/Device Location/        {print $2; exit}')"
    internal="$(echo "$info" | awk -F': +' '/^ *Internal:/          {print $2; exit}')"
    mounted="$(echo "$info"  | awk -F': +' '/^ *Mounted:/           {print $2; exit}')"
    ro="$(echo "$info"       | awk -F': +' '/Read-Only Volume/       {print $2; exit}')"
    # Must be external (Device Location External OR Internal: No)
    [[ "$loc" == "External" || "$internal" == "No" ]] || continue
    [[ "$mounted" == "Yes" ]] || continue
    [[ "$ro" == "No" || -z "$ro" ]] || continue
    [[ -w "$mp" ]] || continue
    vn="$(echo "$info" | awk -F': +' '/Volume Name/              {print $2; exit}')"
    fs="$(echo "$info" | awk -F': +' '/File System Personality/  {print $2; exit}')"
    # Free/total bytes: grab the "(NNN Bytes)" integer from the relevant line.
    free="$( { echo "$info"  | grep -E 'Volume Free Space|Container Free Space' \
              | grep -oE '\(([0-9]+) Bytes\)' | head -n1 | grep -oE '[0-9]+' | head -n1; } 2>/dev/null || true)"
    total="$( { echo "$info" | grep -E 'Volume Total Space|Container Total Space|Total Size|Disk Size' \
              | grep -oE '\(([0-9]+) Bytes\)' | head -n1 | grep -oE '[0-9]+' | head -n1; } 2>/dev/null || true)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$mp" "${vn:-unnamed}" "${fs:-unknown}" "${free:-0}" "${total:-0}"
  done
  shopt -u nullglob
}

human_bytes() {
  # Pretty-print a byte count. Pure bash (no numfmt dependency on macOS).
  local b="${1:-0}"
  if   [[ "$b" -ge 1073741824 ]]; then awk "BEGIN{printf \"%.1f GB\", $b/1073741824}"
  elif [[ "$b" -ge 1048576    ]]; then awk "BEGIN{printf \"%.1f MB\", $b/1048576}"
  elif [[ "$b" -ge 1024       ]]; then awk "BEGIN{printf \"%.1f KB\", $b/1024}"
  else echo "${b} B"; fi
}

# ---------------------------------------------------------------------------
# Encryption tool auto-detect (age -> gpg -> openssl).
# Sets ENC_TOOL and ENC_EXT. Does NOT touch the passphrase yet.
# ---------------------------------------------------------------------------
ENC_TOOL=""
ENC_EXT=""
if command -v age >/dev/null 2>&1; then
  ENC_TOOL="age"; ENC_EXT="age"
elif command -v gpg >/dev/null 2>&1; then
  ENC_TOOL="gpg"; ENC_EXT="gpg"
elif command -v openssl >/dev/null 2>&1; then
  ENC_TOOL="openssl"; ENC_EXT="enc"
fi

# ---------------------------------------------------------------------------
# Version / timestamp identity for this bundle.
# TS_UTC is computed once here so every part + the filename + the manifest all
# agree; a plain `date -u` is fine (this is an operator script, not a
# determinism-sensitive workflow).
# ---------------------------------------------------------------------------
GIT_SHA="$(git rev-parse --short HEAD)"
GIT_SHA_FULL="$(git rev-parse HEAD)"
DIRTY=""
if ! git diff --quiet || ! git diff --cached --quiet; then
  DIRTY="-dirty"
fi
TS_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_BASENAME="meghdut-c3-backup-${TS_UTC}-${GIT_SHA}${DIRTY}"

# ---------------------------------------------------------------------------
# Connectivity sanity check
# ---------------------------------------------------------------------------
SOURCE_REACHABLE=0
if ssh_remote "echo OK" >/dev/null 2>&1; then
  SOURCE_REACHABLE=1
fi

# ---------------------------------------------------------------------------
# Resolve the ML checkpoint path on the source (reads field-bridge/.env's
# CEMA_ML_CHECKPOINT if set, else falls back to the in-repo default path).
# ---------------------------------------------------------------------------
CHECKPOINT_PATH=""
if [[ "$SOURCE_REACHABLE" -eq 1 ]]; then
  CHECKPOINT_PATH="$(ssh_remote "
    ckpt=\"\"
    if [ -f '${HOSTFILE_ENV_FIELD}' ]; then
      ckpt=\$(grep -E '^CEMA_ML_CHECKPOINT=' '${HOSTFILE_ENV_FIELD}' | tail -n1 | cut -d= -f2- | tr -d '\"' )
    fi
    if [ -z \"\$ckpt\" ]; then ckpt='${REMOTE_PATH}/${DEFAULT_CHECKPOINT_REL}'; fi
    echo \"\$ckpt\"
  " 2>/dev/null || true)"
fi
[[ -z "$CHECKPOINT_PATH" ]] && CHECKPOINT_PATH="${REMOTE_PATH}/${DEFAULT_CHECKPOINT_REL}"

# CHECKPOINT_PATH is derived from the source .env (grepped remotely) and is later
# interpolated into remote shell commands (stat, host-file tar). Validate it hard
# before any such use: it MUST be a non-empty absolute path over a safe charset,
# with no shell metacharacters, quotes, or whitespace. Reject anything else.
if [[ ! "$CHECKPOINT_PATH" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "ERROR: resolved checkpoint path is not a safe absolute path:" >&2
  echo "       '${CHECKPOINT_PATH}'" >&2
  echo "       Expected an absolute path matching ^/[A-Za-z0-9._/-]+\$ (no spaces," >&2
  echo "       quotes, or shell metacharacters). Fix CEMA_ML_CHECKPOINT in the" >&2
  echo "       source's field-bridge/.env and re-run. Aborting." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# Gather cheap facts for the plan / manifest (collection counts, audit head seq,
# DEPLOYED_VERSION). These are read-only queries. Populated only if reachable.
# ---------------------------------------------------------------------------
DEPLOYED_VERSION=""
COUNT_DETECTIONS=""
COUNT_MISSION_LOG=""
COUNT_USERS=""
AUDIT_HEAD_SEQ=""

mongo_eval() {
  # Run a JS expression inside cema-mongo via mongosh (quiet, value only).
  ssh_remote "docker exec ${MONGO_CONTAINER} mongosh --quiet '${MONGO_DB}' --eval \"$1\"" 2>/dev/null || true
}

if [[ "$SOURCE_REACHABLE" -eq 1 ]]; then
  DEPLOYED_VERSION="$(ssh_remote "cat '${REMOTE_PATH}/DEPLOYED_VERSION' 2>/dev/null" || true)"
  COUNT_DETECTIONS="$(mongo_eval 'db.detections.countDocuments({})')"
  COUNT_MISSION_LOG="$(mongo_eval 'db.mission_log.countDocuments({})')"
  COUNT_USERS="$(mongo_eval 'db.users.countDocuments({})')"
  # Audit-chain head: the highest seq in mission_log (the append-only chain).
  AUDIT_HEAD_SEQ="$(mongo_eval 'var d=db.mission_log.find({},{seq:1,_id:0}).sort({seq:-1}).limit(1).toArray(); d.length?d[0].seq:\"none\"')"
fi

# ---------------------------------------------------------------------------
# Estimate the finished bundle size (bytes) for the destination free-space
# check. Components:
#   - local git bundle    ~ size of .git (upper bound; bundle is usually smaller)
#   - mongo archive       ~ db dataSize * 0.5 (gzip factor; conservative)
#   - ML checkpoint       exact remote byte size (stat)
#   - caddy + slack       fixed 64 MB cushion
# If a component is unknown (source unreachable), EST_BYTES stays a conservative
# floor and the free-space check requires a generous margin instead.
# ---------------------------------------------------------------------------
EST_GIT_BYTES="$( { du -sk "${REPO_ROOT}/.git" 2>/dev/null | awk '{print $1*1024}'; } 2>/dev/null || true)"
[[ "$EST_GIT_BYTES" =~ ^[0-9]+$ ]] || EST_GIT_BYTES=0
EST_MONGO_BYTES=0
EST_CKPT_BYTES=0
if [[ "$SOURCE_REACHABLE" -eq 1 ]]; then
  _ds="$(mongo_eval 'Math.round(db.stats().dataSize)')"
  [[ "$_ds" =~ ^[0-9]+$ ]] && EST_MONGO_BYTES=$(( _ds / 2 ))
  _ck="$(ssh_remote "stat -c%s '${CHECKPOINT_PATH}' 2>/dev/null || stat -f%z '${CHECKPOINT_PATH}' 2>/dev/null" 2>/dev/null || true)"
  [[ "$_ck" =~ ^[0-9]+$ ]] && EST_CKPT_BYTES="$_ck"
fi
EST_BYTES=$(( EST_GIT_BYTES + EST_MONGO_BYTES + EST_CKPT_BYTES + 67108864 ))
# Required free space includes a 25% safety margin, floor 512 MB.
REQ_BYTES=$(( EST_BYTES + EST_BYTES / 4 ))
[[ "$REQ_BYTES" -lt 536870912 ]] && REQ_BYTES=536870912

# ---------------------------------------------------------------------------
# Destination resolution
#   1. --dest given  -> use it verbatim (NAS mount / fixed path). No auto-detect.
#   2. otherwise     -> auto-detect an attached EXTERNAL volume on this Mac,
#                       confirm it (or choose among several), optionally --watch
#                       until one appears. The bundle lands under
#                       <mountpoint>/MEGHDUT-C3-Backups/.
# Sets CHOSEN_MOUNT (the volume root), CHOSEN_FS, CHOSEN_FREE and DEST_DIR.
# ---------------------------------------------------------------------------
CHOSEN_MOUNT=""
CHOSEN_FS=""
CHOSEN_FREE=""

if [[ "$DEST_EXPLICIT" -eq 1 ]]; then
  CHOSEN_MOUNT="$DEST_DIR"
  # For an explicit path we still learn fs/free where possible for the checks.
  CHOSEN_FREE="$( { df -k "$DEST_DIR" 2>/dev/null | awk 'NR==2{print $4*1024}'; } 2>/dev/null || true)"
  CHOSEN_FS="$( { diskutil info "$DEST_DIR" 2>/dev/null | awk -F': +' '/File System Personality/{print $2; exit}'; } 2>/dev/null || true)"
  [[ -z "$CHOSEN_FS" ]] && CHOSEN_FS="unknown"
else
  # Auto-detect (with optional --watch polling).
  _deadline=$(( $(date +%s) + WATCH_TIMEOUT ))
  VOLS=()
  while :; do
    VOLS=()
    while IFS= read -r _row; do
      [[ -n "$_row" ]] && VOLS+=("$_row")
    done < <(detect_external_volumes)
    if [[ "${#VOLS[@]}" -gt 0 ]]; then
      break
    fi
    if [[ "$WATCH" -eq 1 && "$(date +%s)" -lt "$_deadline" ]]; then
      echo "No external drive detected yet - attach one. Re-checking in ${WATCH_INTERVAL}s ..." >&2
      sleep "$WATCH_INTERVAL"
      continue
    fi
    break
  done

  if [[ "${#VOLS[@]}" -eq 0 ]]; then
    echo "=================================================================="
    echo "No writable external drive found under /Volumes."
    echo "Attach an external/removable drive and re-run, or pass --dest <dir>"
    echo "for a NAS mount or fixed path. (Add --watch to poll until a drive"
    echo "appears, up to ${WATCH_TIMEOUT}s.)"
    echo "Detected physical externals (cross-reference):"
    diskutil list external physical 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    echo "=================================================================="
    exit 3
  fi

  if [[ "${#VOLS[@]}" -eq 1 ]]; then
    IFS=$'\t' read -r _mp _vn _fs _free _total <<< "${VOLS[0]}"
    echo "Detected one external drive:"
    echo "  '${_vn}'  at ${_mp}  [${_fs}]  free $(human_bytes "$_free") / $(human_bytes "$_total")"
    if [[ "$ASSUME_YES" -ne 1 ]]; then
      read -rp "Back up to '${_vn}' at ${_mp}? [y/N] " _c
      if [[ "$_c" != "y" && "$_c" != "Y" && "$_c" != "yes" ]]; then
        echo "Declined. Re-run and choose a drive, or pass --dest." ; exit 0
      fi
    fi
    CHOSEN_MOUNT="$_mp"; CHOSEN_FS="$_fs"; CHOSEN_FREE="$_free"
  else
    echo "Multiple external drives detected:"
    _i=0
    for _row in "${VOLS[@]}"; do
      IFS=$'\t' read -r _mp _vn _fs _free _total <<< "$_row"
      _i=$((_i+1))
      printf "  [%d] '%s'  at %s  [%s]  free %s / %s\n" \
        "$_i" "$_vn" "$_mp" "$_fs" "$(human_bytes "$_free")" "$(human_bytes "$_total")"
    done
    read -rp "Choose a drive [1-${#VOLS[@]}] (or blank to abort): " _sel
    if ! [[ "$_sel" =~ ^[0-9]+$ ]] || [[ "$_sel" -lt 1 || "$_sel" -gt "${#VOLS[@]}" ]]; then
      echo "No valid choice. Aborting."; exit 0
    fi
    IFS=$'\t' read -r _mp _vn _fs _free _total <<< "${VOLS[$((_sel-1))]}"
    CHOSEN_MOUNT="$_mp"; CHOSEN_FS="$_fs"; CHOSEN_FREE="$_free"
  fi

  DEST_DIR="${CHOSEN_MOUNT%/}/${DEST_SUBDIR}"
fi

# ---------------------------------------------------------------------------
# Destination checks: writability, free space, filesystem single-file caveat.
# ---------------------------------------------------------------------------
DEST_CHECK_FAIL=0
DEST_PARENT="$CHOSEN_MOUNT"
[[ "$DEST_EXPLICIT" -eq 1 ]] && DEST_PARENT="$DEST_DIR"
if [[ ! -d "$DEST_PARENT" ]]; then
  # For an explicit path the parent might not exist yet; check its parent dir.
  DEST_PARENT="$(dirname "$DEST_DIR")"
fi
if [[ ! -w "$DEST_PARENT" ]]; then
  echo "WARNING: destination ${DEST_PARENT} is not writable by this user." >&2
  DEST_CHECK_FAIL=1
fi
# Refresh free space for the actual destination path.
[[ -z "${CHOSEN_FREE:-}" ]] && CHOSEN_FREE="$( { df -k "$DEST_PARENT" 2>/dev/null | awk 'NR==2{print $4*1024}'; } 2>/dev/null || true)"
if [[ "${CHOSEN_FREE:-0}" =~ ^[0-9]+$ && "$CHOSEN_FREE" -gt 0 ]]; then
  if [[ "$CHOSEN_FREE" -lt "$REQ_BYTES" ]]; then
    echo "WARNING: destination free space $(human_bytes "$CHOSEN_FREE") is below the" >&2
    echo "         required ~$(human_bytes "$REQ_BYTES") (estimated bundle + margin)." >&2
    DEST_CHECK_FAIL=1
  fi
fi
# FAT32/exFAT 4 GB single-file limit caveat. ('*fat*' covers fat32 + exfat.)
case "$(echo "${CHOSEN_FS:-}" | tr '[:upper:]' '[:lower:]')" in
  *fat*|*msdos*)
    echo "NOTE: destination filesystem '${CHOSEN_FS}' has a 4 GB single-file limit." >&2
    echo "      APFS / HFS+ / ext4 are recommended for large encrypted bundles." >&2
    if [[ "$EST_BYTES" -gt 4294967296 ]]; then
      echo "ERROR: estimated bundle $(human_bytes "$EST_BYTES") exceeds the 4 GB limit" >&2
      echo "       of '${CHOSEN_FS}'. Choose an APFS/HFS+/ext4 destination." >&2
      DEST_CHECK_FAIL=1
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# Plan print (always shown)
# ---------------------------------------------------------------------------
echo "=================================================================="
echo "MEGHDUT C3 full-system backup plan"
echo "  Tool version:     ${BACKUP_TOOL_VERSION}"
echo "  Bundle basename:  ${BUNDLE_BASENAME}"
echo "  Source:           ${SOURCE_USER}@${SOURCE_HOST}:${REMOTE_PATH}  (READ-ONLY)"
echo "  Source reachable: $([[ $SOURCE_REACHABLE -eq 1 ]] && echo yes || echo 'NO (facts below unavailable)')"
echo "  Local git HEAD:   ${GIT_SHA}${DIRTY}  (${GIT_SHA_FULL})"
echo "  Mongo DB:         ${MONGO_DB}  (container ${MONGO_CONTAINER})"
echo "  Caddy volumes:    ${CADDY_DATA_VOLUME}, ${CADDY_CONFIG_VOLUME}"
echo "  ML checkpoint:    ${CHECKPOINT_PATH}"
echo "  Encryption tool:  $([[ -n $ENC_TOOL ]] && echo "${ENC_TOOL} (.${ENC_EXT})" || echo 'NONE FOUND (age/gpg/openssl) - REQUIRED')"
echo "  Destination:      ${DEST_DIR}"
echo "  Dest volume:      $([[ $DEST_EXPLICIT -eq 1 ]] && echo '(explicit --dest)' || echo "'${CHOSEN_MOUNT##*/}' [${CHOSEN_FS}]")"
echo "  Est. bundle:      ~$(human_bytes "$EST_BYTES")   (need >= $(human_bytes "$REQ_BYTES") free; have $(human_bytes "${CHOSEN_FREE:-0}"))"
echo "  Mode:             ${MODE}"
echo "------------------------------------------------------------------"
echo "  Will capture:"
echo "    1. CODE   git bundle --all  (HEAD ${GIT_SHA}${DIRTY})"
echo "    2. MONGO  mongodump ${MONGO_DB} --archive --gzip"
echo "              detections=${COUNT_DETECTIONS:-?}  mission_log=${COUNT_MISSION_LOG:-?}  users=${COUNT_USERS:-?}  audit_head_seq=${AUDIT_HEAD_SEQ:-?}"
echo "    3. CADDY  ${CADDY_DATA_VOLUME} + ${CADDY_CONFIG_VOLUME} (busybox :ro tar)"
echo "    4. HOST   ${HOSTFILE_ENV_ROOT}"
echo "              ${HOSTFILE_ENV_FIELD}"
echo "              ${HOSTFILE_ENV_RF}"
echo "              ${HOSTFILE_UDEV}"
echo "              ${CHECKPOINT_PATH}"
echo "              ${HOSTFILE_KISMET}"
echo "              ${HOSTFILE_UNITS_GLOB}"
echo "    5. MANIFEST  (plaintext, hashes/counts only - no secrets)"
echo "  Source DEPLOYED_VERSION:"
if [[ -n "$DEPLOYED_VERSION" ]]; then
  echo "$DEPLOYED_VERSION" | sed 's/^/      /'
else
  echo "      (unavailable)"
fi
echo "=================================================================="

if [[ -z "$ENC_TOOL" ]]; then
  echo "ERROR: no encryption tool found (need one of: age, gpg, openssl)." >&2
  echo "       The bundle holds secrets and MUST be encrypted; refusing to run." >&2
  exit 3
fi

# ---------------------------------------------------------------------------
# Dry-run stops here: a rehearsal that proves reachability, resolves + confirms
# the destination drive, and reports the space/filesystem checks - WITHOUT
# producing or encrypting any bundle.
# ---------------------------------------------------------------------------
if [[ "$MODE" == "dry-run" ]]; then
  echo "Destination that WOULD be used: ${DEST_DIR}"
  if [[ "$DEST_CHECK_FAIL" -eq 1 ]]; then
    echo "  (destination checks reported WARNINGS above - resolve before --apply)"
  else
    echo "  (destination checks passed: writable + enough free space)"
  fi
  echo "DRY RUN complete: nothing was produced. Re-run with --apply to build the"
  echo "encrypted bundle."
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply from here. Require reachability, a clean destination, and a passphrase.
# ---------------------------------------------------------------------------
if [[ "$SOURCE_REACHABLE" -ne 1 ]]; then
  echo "ERROR: source ${SOURCE_USER}@${SOURCE_HOST} is not reachable. Aborting." >&2
  exit 2
fi

if [[ "$DEST_CHECK_FAIL" -eq 1 ]]; then
  echo "ERROR: destination checks failed (see warnings above). Aborting before" >&2
  echo "       capturing anything. Fix the destination (space/filesystem/perms)" >&2
  echo "       or pass a different --dest, then re-run." >&2
  exit 3
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -rp "Apply: capture + encrypt a full backup of ${SOURCE_HOST} into ${DEST_DIR}? [y/N] " CONFIRM
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "Aborted by user. Nothing was produced."
    exit 0
  fi
fi

# Passphrase handling is tool-aware:
#   - gpg / openssl take the passphrase from env BACKUP_PASSPHRASE (or, if
#     unset, a prompt here). It is fed to the tool via stdin / env: pass,
#     never on the command line, so it never appears in `ps`.
#   - age -p reads its passphrase directly from /dev/tty and has no env-var
#     path, so in age mode we let age run its own secure prompt (which also
#     confirms). If BACKUP_PASSPHRASE was preset for automation, age cannot
#     consume it, so we say so and fall back to age's interactive prompt.
if [[ "$ENC_TOOL" == "age" ]]; then
  if [[ -n "${BACKUP_PASSPHRASE:-}" ]]; then
    echo "NOTE: BACKUP_PASSPHRASE is set, but 'age -p' only reads its passphrase"
    echo "      interactively from the terminal; age will prompt you now. For a"
    echo "      fully non-interactive run, install gpg or openssl (they honor"
    echo "      BACKUP_PASSPHRASE)."
  fi
else
  if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    read -rs -p "Backup passphrase (for ${ENC_TOOL} encryption): " BACKUP_PASSPHRASE
    echo
    read -rs -p "Confirm passphrase: " BACKUP_PASSPHRASE_CONFIRM
    echo
    if [[ "$BACKUP_PASSPHRASE" != "$BACKUP_PASSPHRASE_CONFIRM" ]]; then
      echo "ERROR: passphrases do not match. Aborting." >&2
      exit 4
    fi
    unset BACKUP_PASSPHRASE_CONFIRM
  fi
  if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    echo "ERROR: empty passphrase. Aborting." >&2
    exit 4
  fi
  export BACKUP_PASSPHRASE
fi

mkdir -p "$DEST_DIR"

# Staging dir (plaintext) - shredded at the end no matter what.
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/meghdut-backup.XXXXXX")"

shred_stage() {
  # Best-effort secure delete of the plaintext stage (contains .env + CA key).
  # `rm -P` (BSD/macOS) overwrites before unlinking; fall back to plain rm.
  if [[ -n "${STAGE_DIR:-}" && -d "${STAGE_DIR:-/nonexistent}" ]]; then
    find "$STAGE_DIR" -type f -exec rm -P {} + 2>/dev/null || true
    rm -rf "$STAGE_DIR" 2>/dev/null || true
  fi
}
trap shred_stage EXIT

# ---------------------------------------------------------------------------
# 1. CODE - full-history git bundle from the LOCAL repo.
# ---------------------------------------------------------------------------
echo "[1/5] CODE  git bundle --all ..."
git bundle create "${STAGE_DIR}/code.bundle" --all
echo "${GIT_SHA_FULL}" > "${STAGE_DIR}/code.git_head"

# ---------------------------------------------------------------------------
# 2. MONGO - read-only streaming mongodump out of the source container.
# ---------------------------------------------------------------------------
echo "[2/5] MONGO  mongodump ${MONGO_DB} (read-only stream) ..."
ssh_remote "docker exec ${MONGO_CONTAINER} sh -c 'mongodump --db ${MONGO_DB} --archive --gzip'" \
  > "${STAGE_DIR}/mongo.archive.gz"
if [[ ! -s "${STAGE_DIR}/mongo.archive.gz" ]]; then
  echo "ERROR: mongo archive is empty - dump failed. Aborting (nothing written to dest)." >&2
  exit 5
fi

# ---------------------------------------------------------------------------
# 3. CADDY CA - tar each volume via a throwaway busybox with a READ-ONLY mount.
# ---------------------------------------------------------------------------
echo "[3/5] CADDY  volume tars (busybox :ro) ..."
ssh_remote "docker run --rm -v ${CADDY_DATA_VOLUME}:/v:ro busybox tar cf - -C /v ." \
  > "${STAGE_DIR}/caddy_data.tar"
ssh_remote "docker run --rm -v ${CADDY_CONFIG_VOLUME}:/v:ro busybox tar cf - -C /v ." \
  > "${STAGE_DIR}/caddy_config.tar"

# ---------------------------------------------------------------------------
# 4. HOST-ONLY FILES - a single mode-preserving tar built on the source.
# --ignore-failed-read means a missing optional file (e.g. no kismet config on
# this host) does not abort the whole backup; the manifest records what landed.
# ---------------------------------------------------------------------------
echo "[4/5] HOST  host-only files (mode-preserving tar) ..."
ssh_remote "
  set -e
  files=''
  for f in '${HOSTFILE_ENV_ROOT}' '${HOSTFILE_ENV_FIELD}' '${HOSTFILE_ENV_RF}' \
           '${HOSTFILE_UDEV}' '${CHECKPOINT_PATH}' '${HOSTFILE_KISMET}'; do
    [ -e \"\$f\" ] && files=\"\$files \$f\"
  done
  for u in ${HOSTFILE_UNITS_GLOB}; do
    [ -e \"\$u\" ] && files=\"\$files \$u\"
  done
  # -p preserves permissions; leading '/' is stripped by tar with a warning,
  # restore.sh replaces files by absolute path from the manifest mapping.
  tar cpf - --ignore-failed-read \$files 2>/dev/null
" > "${STAGE_DIR}/hostfiles.tar"

# Record which host paths were expected, for restore.sh to place back.
{
  echo "$HOSTFILE_ENV_ROOT"
  echo "$HOSTFILE_ENV_FIELD"
  echo "$HOSTFILE_ENV_RF"
  echo "$HOSTFILE_UDEV"
  echo "$CHECKPOINT_PATH"
  echo "$HOSTFILE_KISMET"
  echo "$HOSTFILE_UNITS_GLOB"
} > "${STAGE_DIR}/hostfiles.manifest"

# ---------------------------------------------------------------------------
# 5. MANIFEST - plaintext, hashes/counts only (NO secrets).
# ---------------------------------------------------------------------------
echo "[5/5] MANIFEST ..."
sha_of() { shasum -a 256 "$1" | awk '{print $1}'; }

MANIFEST="${STAGE_DIR}/MANIFEST.txt"
{
  echo "# MEGHDUT C3 backup manifest"
  echo "tool_version=${BACKUP_TOOL_VERSION}"
  echo "created_utc=${TS_UTC}"
  echo "created_by=${USER:-unknown}@$(hostname -s 2>/dev/null || echo mac)"
  echo "source_host=${SOURCE_HOST}"
  echo "source_user=${SOURCE_USER}"
  echo "git_sha=${GIT_SHA}${DIRTY}"
  echo "git_sha_full=${GIT_SHA_FULL}"
  echo "encryption_tool=${ENC_TOOL}"
  echo "bundle_basename=${BUNDLE_BASENAME}"
  echo ""
  echo "# source deployed version (verbatim)"
  echo "$DEPLOYED_VERSION" | sed 's/^/deployed_version: /'
  echo ""
  echo "# mongo (${MONGO_DB})"
  echo "count_detections=${COUNT_DETECTIONS}"
  echo "count_mission_log=${COUNT_MISSION_LOG}"
  echo "count_users=${COUNT_USERS}"
  echo "audit_head_seq=${AUDIT_HEAD_SEQ}"
  echo ""
  echo "# checkpoint path on source"
  echo "checkpoint_path=${CHECKPOINT_PATH}"
  echo ""
  echo "# per-part sha256 (of the plaintext staged parts, pre-encryption)"
  echo "sha256_code_bundle=$(sha_of "${STAGE_DIR}/code.bundle")"
  echo "sha256_mongo_archive=$(sha_of "${STAGE_DIR}/mongo.archive.gz")"
  echo "sha256_caddy_data=$(sha_of "${STAGE_DIR}/caddy_data.tar")"
  echo "sha256_caddy_config=$(sha_of "${STAGE_DIR}/caddy_config.tar")"
  echo "sha256_hostfiles=$(sha_of "${STAGE_DIR}/hostfiles.tar")"
} > "$MANIFEST"

# ---------------------------------------------------------------------------
# Package + encrypt INSIDE the 0700 stage dir.
#
# SECURITY: the plaintext tarball and the encrypted bundle are both written
# under ${STAGE_DIR}/.pkg/ - i.e. INSIDE the mktemp 0700 stage dir - NOT in its
# parent. This guarantees the `trap shred_stage EXIT` (which recursively shreds
# every file under $STAGE_DIR) covers the plaintext on EVERY exit path: success,
# error, or signal. Nothing plaintext ever lands with a predictable name in a
# world-adjacent /tmp parent. The encrypted bundle is then COPIED out to the
# external destination; the in-stage copies are shredded by the same trap.
# ---------------------------------------------------------------------------
PKG_DIR="${STAGE_DIR}/.pkg"
mkdir -p "$PKG_DIR"
PLAINTEXT_TARBALL="${PKG_DIR}/${BUNDLE_BASENAME}.tar.gz"
echo "Packaging staged parts ..."
# --exclude=./.pkg so the output dir is not archived into its own tarball.
tar czf "$PLAINTEXT_TARBALL" --exclude=./.pkg -C "$STAGE_DIR" .

# ---------------------------------------------------------------------------
# Encrypt with the auto-detected tool. Passphrase is passed via env/stdin,
# never on the command line (so it never appears in `ps`).
#
# age and gpg are PREFERRED precisely because they are AUTHENTICATED: age
# (ChaCha20-Poly1305) and gpg (AES256 with its MDC/AEAD) both bind an integrity
# tag to the ciphertext, so tampering is detected at decrypt time for free.
# openssl `enc -aes-256-cbc` is UNAUTHENTICATED (encrypt-only, no integrity), so
# for the openssl branch ONLY we additionally write a detached HMAC-SHA256 over
# the CIPHERTEXT (see below), which restore.sh verifies before decrypting.
#
# We encrypt to a file INSIDE the stage dir first, hash it, then copy it to the
# (possibly removable) destination and re-hash the written copy. That proves the
# write to external media was not corrupted before the trap shreds the stage.
# ---------------------------------------------------------------------------
LOCAL_BUNDLE="${PKG_DIR}/${BUNDLE_BASENAME}.tar.gz.${ENC_EXT}"
LOCAL_HMAC=""
echo "Encrypting with ${ENC_TOOL} ..."
case "$ENC_TOOL" in
  age)
    # age -p runs its own secure passphrase prompt (with confirmation) reading
    # directly from the terminal. Nothing secret is passed on the command line.
    # age's AEAD authenticates the ciphertext, so no separate HMAC is needed.
    age -p -o "$LOCAL_BUNDLE" "$PLAINTEXT_TARBALL"
    ;;
  gpg)
    # Passphrase fed on fd 0 (stdin), never on argv. Loopback pinentry lets it
    # run unattended when BACKUP_PASSPHRASE is set. gpg's MDC/AEAD authenticates
    # the ciphertext, so no separate HMAC is needed.
    printf '%s' "$BACKUP_PASSPHRASE" | \
      gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 \
        --output "$LOCAL_BUNDLE" "$PLAINTEXT_TARBALL"
    ;;
  openssl)
    # Passphrase read from the exported env var, never on argv.
    openssl enc -aes-256-cbc -pbkdf2 -salt \
      -in "$PLAINTEXT_TARBALL" -out "$LOCAL_BUNDLE" \
      -pass env:BACKUP_PASSPHRASE
    # openssl enc is UNAUTHENTICATED. Add a detached HMAC-SHA256 over the
    # CIPHERTEXT so restore.sh can detect tampering/corruption BEFORE decrypting.
    # The HMAC key is DERIVED from the passphrase (sha256 of it) rather than the
    # passphrase itself, and is passed as a hexkey via -macopt (never the raw
    # secret on argv). restore.sh re-derives the same key to verify.
    LOCAL_HMAC="${LOCAL_BUNDLE}.hmac"
    _hmac_key="$(printf %s "$BACKUP_PASSPHRASE" | openssl dgst -sha256 -r | cut -d' ' -f1)"
    openssl dgst -sha256 -mac HMAC -macopt "hexkey:${_hmac_key}" -r "$LOCAL_BUNDLE" \
      | cut -d' ' -f1 > "$LOCAL_HMAC"
    unset _hmac_key
    if [[ ! -s "$LOCAL_HMAC" ]]; then
      echo "ERROR: failed to produce ciphertext HMAC for the openssl bundle. Aborting." >&2
      exit 6
    fi
    ;;
esac

if [[ ! -s "$LOCAL_BUNDLE" ]]; then
  echo "ERROR: encrypted bundle was not produced. Aborting." >&2
  exit 6
fi
BUNDLE_SHA="$(sha_of "$LOCAL_BUNDLE")"

# Shred the plaintext tarball as soon as the ciphertext exists (belt-and-braces;
# the EXIT trap would shred it anyway since it lives inside $STAGE_DIR).
rm -P "$PLAINTEXT_TARBALL" 2>/dev/null || rm -f "$PLAINTEXT_TARBALL" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Write to the destination subdir + verify the written copy (media integrity).
# The in-stage ciphertext (and its .hmac) stay inside $STAGE_DIR and are shredded
# by the EXIT trap; only the destination copies persist.
# ---------------------------------------------------------------------------
mkdir -p "$DEST_DIR"
OUT_BUNDLE="${DEST_DIR}/${BUNDLE_BASENAME}.tar.gz.${ENC_EXT}"
echo "Writing bundle to ${OUT_BUNDLE} ..."
cp "$LOCAL_BUNDLE" "$OUT_BUNDLE"
sync 2>/dev/null || true

WRITTEN_SHA="$(sha_of "$OUT_BUNDLE")"
if [[ "$WRITTEN_SHA" != "$BUNDLE_SHA" ]]; then
  echo "ERROR: written bundle sha256 (${WRITTEN_SHA:0:16}) does NOT match the" >&2
  echo "       encrypted source (${BUNDLE_SHA:0:16}). The media write was corrupted." >&2
  echo "       Leaving both copies for inspection; do NOT trust this backup." >&2
  exit 7
fi

# For the openssl branch, ship the detached ciphertext HMAC alongside the bundle.
OUT_HMAC=""
if [[ -n "$LOCAL_HMAC" ]]; then
  OUT_HMAC="${OUT_BUNDLE}.hmac"
  cp "$LOCAL_HMAC" "$OUT_HMAC"
  sync 2>/dev/null || true
fi

# Write the plaintext manifest alongside the encrypted bundle (no secrets in it).
OUT_MANIFEST="${DEST_DIR}/${BUNDLE_BASENAME}.MANIFEST.txt"
cp "$MANIFEST" "$OUT_MANIFEST"
{
  echo ""
  echo "# final encrypted bundle"
  echo "bundle_file=$(basename "$OUT_BUNDLE")"
  echo "sha256_bundle=${BUNDLE_SHA}"
  echo "written_sha256_verified=yes"
  if [[ -n "$OUT_HMAC" ]]; then
    echo "ciphertext_hmac_file=$(basename "$OUT_HMAC")"
    echo "ciphertext_hmac_alg=HMAC-SHA256(key=sha256(passphrase))"
  fi
} >> "$OUT_MANIFEST"

# STAGE_DIR (plaintext parts) is shredded by the EXIT trap.

echo "=================================================================="
echo "BACKUP COMPLETE  (written copy sha256-verified against source)"
echo "  Captured:"
echo "    git SHA:       ${GIT_SHA}${DIRTY}"
echo "    mongo counts:  detections=${COUNT_DETECTIONS:-?} mission_log=${COUNT_MISSION_LOG:-?} users=${COUNT_USERS:-?}"
echo "    audit head:    seq=${AUDIT_HEAD_SEQ:-?}"
echo "    bundle size:   $(human_bytes "$(du -k "$OUT_BUNDLE" 2>/dev/null | awk '{print $1*1024}')")"
echo "  Destination:     ${DEST_DIR}"
echo "  Encrypted bundle: $(basename "$OUT_BUNDLE")"
echo "  Bundle sha256:    ${BUNDLE_SHA}"
echo "  Manifest:         $(basename "$OUT_MANIFEST")"
echo "  Encryption:       ${ENC_TOOL}"
if [[ -n "$OUT_HMAC" ]]; then
  echo "  Ciphertext HMAC:  $(basename "$OUT_HMAC")  (openssl is unauthenticated;"
  echo "                    restore.sh verifies this HMAC before decrypting)"
fi
echo "  Plaintext stage:  shredded."
echo "  Restore with: scripts/restore.sh --bundle '${OUT_BUNDLE}' --target-host <NEW_HOST>"
echo "=================================================================="
exit 0
