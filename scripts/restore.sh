#!/opt/homebrew/opt/bash/bin/bash
#
# restore.sh
#
# DR / migration tooling for MEGHDUT C3 (CeCDS / Meghaduta).
#
# The reverse of scripts/backup.sh: takes ONE encrypted backup bundle, decrypts
# it, independently verifies every part's sha256 against the in-bundle MANIFEST,
# and then guides/executes a full-system restore onto a TARGET host - git code,
# Mongo database, Caddy CA volumes, and all host-only files - finishing with a
# post-restore parity + audit-chain check against the manifest.
#
# NOTE: requires bash >= 4. macOS ships bash 3.2 as /bin/bash, so this hardcodes
# the Homebrew bash path, same as deploy.sh / backup.sh. Run via:
#   /opt/homebrew/opt/bash/bin/bash scripts/restore.sh [options]
#
# SAFETY POSTURE
# ----------------------------------------------------------------------------
#   - --target-host is REQUIRED and has NO default. There is deliberately no
#     "restore to the obvious box" convenience - you must name the target so a
#     restore can never silently clobber a live host.
#   - Restoring ONTO the source-of-truth host (default 172.16.16.196) is
#     REFUSED unless BOTH --force is passed AND a typed confirmation is given.
#   - Dry-run is the DEFAULT: it decrypts to a temp dir, verifies every part's
#     sha256 against the manifest, and prints the exact plan - without writing
#     anything to the target. --apply performs the restore.
#   - The decrypted plaintext staging dir (which holds .env secrets + the Caddy
#     CA private key) is shredded on exit, always.
#
# WHAT --apply DOES, IN ORDER
# ----------------------------------------------------------------------------
#   1. Decrypt the bundle (tool chosen by extension: .age/.gpg/.enc).
#   2. Verify sha256 of code.bundle, mongo.archive.gz, caddy_data.tar,
#      caddy_config.tar, hostfiles.tar against MANIFEST.txt. ANY mismatch aborts
#      before the target is touched.
#   3. CODE  - `git bundle verify`, then clone the bundle into the target's
#      /CEMA/joydipdemo if that path has no git repo yet (else guidance only).
#   4. MONGO - stream mongo.archive.gz into the target's cema-mongo via
#      `mongorestore --db cema_cuas_db --archive --gzip --drop`.
#   5. CADDY - unpack caddy_data.tar / caddy_config.tar back into the
#      joydipdemo_cema_caddy_{data,config} volumes (RW).
#   6. HOST  - place the .env files, udev rule, ML checkpoint, kismet config,
#      and cema-*.service unit files back at their absolute paths (mode-
#      preserving; /etc targets need root on the target, via sudo).
#   7. Print the remaining MANUAL steps (rebuild venv from requirements, docker
#      compose build, bring the stack up fail-closed).
#   8. Verify post-restore collection counts vs MANIFEST + the audit head seq.
#
# USAGE
#   scripts/restore.sh --bundle <file> --target-host <host>            # dry-run
#   scripts/restore.sh --bundle <file> --target-host <host> --apply [--yes]
#
#   --bundle <file>           Encrypted bundle produced by backup.sh. REQUIRED.
#   --target-host <host>      Host to restore ONTO. REQUIRED, no default.
#   --target-user <user>      SSH user on the target. Default biswajit
#                             (or TARGET_USER env).
#   --keychain-service <svc>  macOS Keychain service holding the SSH password.
#                             Default cema-primary-ssh.
#   --source-of-truth <host>  Host that must never be clobbered without --force.
#                             Default 172.16.16.196 (or SOURCE_OF_TRUTH env).
#   --force                   Permit restoring onto the source-of-truth host
#                             (still requires a typed confirmation).
#   --dry-run                 Decrypt + verify + print plan only. DEFAULT.
#   --apply                   Actually perform the restore onto the target.
#   --yes                     Skip the interactive confirmation (still needs
#                             --apply; does NOT bypass the source-of-truth typed
#                             confirmation).
#   -h | --help               Show this header and exit.
#
# Credentials:
#   SSH password  - env PRIMARY_SSH_PASS, else macOS Keychain
#                   (security find-generic-password -a <user> -s <service> -w),
#                   else interactive prompt. Never written to disk, never echoed.
#   Passphrase    - env BACKUP_PASSPHRASE, else interactive prompt. Never echoed.

set -euo pipefail

RESTORE_TOOL_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# Config / defaults
# ---------------------------------------------------------------------------
BUNDLE=""
TARGET_HOST=""
TARGET_USER="${TARGET_USER:-biswajit}"
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-cema-primary-ssh}"
SOURCE_OF_TRUTH="${SOURCE_OF_TRUTH:-172.16.16.196}"
FORCE=0

REMOTE_PATH="/CEMA/joydipdemo"
MONGO_CONTAINER="cema-mongo"
MONGO_DB="cema_cuas_db"
CADDY_DATA_VOLUME="joydipdemo_cema_caddy_data"
CADDY_CONFIG_VOLUME="joydipdemo_cema_caddy_config"

MODE="dry-run"
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --target-host) TARGET_HOST="$2"; shift 2 ;;
    --target-user) TARGET_USER="$2"; shift 2 ;;
    --keychain-service) KEYCHAIN_SERVICE="$2"; shift 2 ;;
    --source-of-truth) SOURCE_OF_TRUTH="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --apply) MODE="apply"; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

usage() {
  echo "Usage: scripts/restore.sh --bundle <file> --target-host <host>" >&2
  echo "                          [--apply [--yes]] [--target-user U]" >&2
  echo "                          [--keychain-service S] [--force]" >&2
  echo "Run 'scripts/restore.sh --help' for the full description." >&2
}

# ---------------------------------------------------------------------------
# Required-arg + safety validation
# ---------------------------------------------------------------------------
if [[ -z "$BUNDLE" ]]; then
  echo "ERROR: --bundle is required." >&2; usage; exit 2
fi
if [[ ! -f "$BUNDLE" ]]; then
  echo "ERROR: bundle not found: ${BUNDLE}" >&2; exit 2
fi
if [[ -z "$TARGET_HOST" ]]; then
  echo "ERROR: --target-host is required (there is deliberately no default)." >&2
  usage; exit 2
fi

# Refuse the source-of-truth host unless --force AND a typed confirmation.
if [[ "$TARGET_HOST" == "$SOURCE_OF_TRUTH" ]]; then
  if [[ "$FORCE" -ne 1 ]]; then
    echo "REFUSING: --target-host ${TARGET_HOST} is the source-of-truth host." >&2
    echo "A restore would OVERWRITE live data. Pass --force AND type the" >&2
    echo "confirmation if you truly intend to restore onto it." >&2
    exit 3
  fi
  echo "WARNING: target ${TARGET_HOST} is the SOURCE-OF-TRUTH host."
  echo "This will OVERWRITE its live Mongo DB, Caddy CA, and host files."
  read -rp "Type exactly 'OVERWRITE ${TARGET_HOST}' to proceed: " TYPED
  if [[ "$TYPED" != "OVERWRITE ${TARGET_HOST}" ]]; then
    echo "Confirmation did not match. Aborting." >&2
    exit 3
  fi
fi

# ---------------------------------------------------------------------------
# Encryption tool by bundle extension
# ---------------------------------------------------------------------------
case "$BUNDLE" in
  *.age) DEC_TOOL="age" ;;
  *.gpg) DEC_TOOL="gpg" ;;
  *.enc) DEC_TOOL="openssl" ;;
  *) echo "ERROR: cannot infer encryption tool from extension of ${BUNDLE}" >&2
     echo "       expected one of: .age .gpg .enc" >&2; exit 2 ;;
esac
if ! command -v "$DEC_TOOL" >/dev/null 2>&1; then
  echo "ERROR: bundle needs '${DEC_TOOL}' to decrypt, but it is not installed." >&2
  exit 3
fi

# ---------------------------------------------------------------------------
# SSH credential handling (env -> Keychain -> prompt). Never printed/persisted.
# ---------------------------------------------------------------------------
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  PRIMARY_SSH_PASS="$(security find-generic-password -a "$TARGET_USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
fi
if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  read -rs -p "Password for ${TARGET_USER}@${TARGET_HOST}: " PRIMARY_SSH_PASS
  echo
fi
export SSHPASS="$PRIMARY_SSH_PASS"

# Drop any stale known_hosts entry for the target BEFORE the first connection.
# accept-new only auto-accepts an UNKNOWN key; it does NOT accept a CHANGED key.
# A DR restore onto a freshly re-imaged target has a NEW host key, which would
# otherwise trigger a host-key-mismatch failure. Removing the stale entry first,
# then connecting with accept-new, lets a re-imaged box connect while still
# recording its new key. (We deliberately do NOT weaken to StrictHostKeyChecking=no.)
ssh-keygen -R "$TARGET_HOST" >/dev/null 2>&1 || true

ssh_remote() {
  # accept-new records the (now-unknown) host key on first use; a stale key was
  # already dropped above so a freshly imaged target connects cleanly.
  # Password lives only in SSHPASS, never on argv.
  sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    "${TARGET_USER}@${TARGET_HOST}" "$@"
}

# ---------------------------------------------------------------------------
# Decrypt to a private temp stage. Shredded on exit no matter what.
# ---------------------------------------------------------------------------
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/meghdut-restore.XXXXXX")"
shred_stage() {
  if [[ -n "${STAGE_DIR:-}" && -d "${STAGE_DIR:-/nonexistent}" ]]; then
    find "$STAGE_DIR" -type f -exec rm -P {} + 2>/dev/null || true
    rm -rf "$STAGE_DIR" 2>/dev/null || true
  fi
}
trap shred_stage EXIT

# Passphrase: gpg/openssl take it from env/prompt; age prompts itself on tty.
if [[ "$DEC_TOOL" != "age" ]]; then
  if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    read -rs -p "Backup passphrase (for ${DEC_TOOL} decryption): " BACKUP_PASSPHRASE
    echo
  fi
  export BACKUP_PASSPHRASE
fi

# ---------------------------------------------------------------------------
# openssl (.enc) is UNAUTHENTICATED encryption. backup.sh ships a detached
# HMAC-SHA256 over the CIPHERTEXT next to the bundle (<bundle>.hmac), keyed by
# sha256(passphrase). VERIFY it BEFORE decrypting, fail-closed: a missing or
# mismatched HMAC aborts before we ever feed attacker-controlled ciphertext to
# openssl. age/gpg bundles carry their own AEAD/MDC integrity and skip this.
# ---------------------------------------------------------------------------
if [[ "$DEC_TOOL" == "openssl" ]]; then
  HMAC_FILE="${BUNDLE}.hmac"
  if [[ ! -f "$HMAC_FILE" ]]; then
    echo "ERROR: openssl bundle requires a detached ciphertext HMAC, but" >&2
    echo "       ${HMAC_FILE} is missing. Refusing to decrypt unauthenticated" >&2
    echo "       ciphertext. Aborting." >&2
    exit 4
  fi
  echo "Verifying ciphertext HMAC (openssl bundle is unauthenticated) ..."
  _hmac_key="$(printf %s "$BACKUP_PASSPHRASE" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  HMAC_GOT="$(openssl dgst -sha256 -mac HMAC -macopt "hexkey:${_hmac_key}" -r "$BUNDLE" | cut -d' ' -f1)"
  unset _hmac_key
  HMAC_WANT="$(tr -d ' \t\r\n' < "$HMAC_FILE")"
  if [[ -z "$HMAC_GOT" || "$HMAC_GOT" != "$HMAC_WANT" ]]; then
    echo "ERROR: ciphertext HMAC mismatch (want=${HMAC_WANT:0:16} got=${HMAC_GOT:0:16})." >&2
    echo "       The bundle was tampered with, corrupted, or the passphrase is" >&2
    echo "       wrong. Refusing to decrypt. Aborting - target untouched." >&2
    exit 4
  fi
  echo "  [OK] ciphertext HMAC verified (${HMAC_GOT:0:16})."
fi

PLAINTEXT_TARBALL="${STAGE_DIR}/bundle.tar.gz"
echo "Decrypting bundle with ${DEC_TOOL} ..."
case "$DEC_TOOL" in
  age)
    age -d -o "$PLAINTEXT_TARBALL" "$BUNDLE"
    ;;
  gpg)
    printf '%s' "$BACKUP_PASSPHRASE" | \
      gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 \
        --decrypt --output "$PLAINTEXT_TARBALL" "$BUNDLE"
    ;;
  openssl)
    openssl enc -d -aes-256-cbc -pbkdf2 \
      -in "$BUNDLE" -out "$PLAINTEXT_TARBALL" -pass env:BACKUP_PASSPHRASE
    ;;
esac

if [[ ! -s "$PLAINTEXT_TARBALL" ]]; then
  echo "ERROR: decryption produced no output (wrong passphrase or corrupt bundle)." >&2
  exit 4
fi

# ---------------------------------------------------------------------------
# Extract + verify every part against the in-bundle MANIFEST.
# ---------------------------------------------------------------------------
EXTRACT_DIR="${STAGE_DIR}/extracted"
mkdir -p "$EXTRACT_DIR"
tar xzf "$PLAINTEXT_TARBALL" -C "$EXTRACT_DIR"

MANIFEST="${EXTRACT_DIR}/MANIFEST.txt"
if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: MANIFEST.txt missing from bundle - cannot verify integrity." >&2
  exit 4
fi

manifest_val() { grep -E "^$1=" "$MANIFEST" | head -n1 | cut -d= -f2-; }
sha_of() { shasum -a 256 "$1" | awk '{print $1}'; }

verify_part() {
  # $1 = manifest key, $2 = file in EXTRACT_DIR
  local key="$1" file="${EXTRACT_DIR}/$2"
  local want got
  want="$(manifest_val "$key")"
  if [[ ! -f "$file" ]]; then
    echo "  [MISSING] $2 (expected by manifest)"; return 1
  fi
  got="$(sha_of "$file")"
  if [[ -z "$want" ]]; then
    echo "  [NO-MANIFEST-HASH] $2 (present but manifest has no ${key})"; return 1
  fi
  if [[ "$want" != "$got" ]]; then
    echo "  [MISMATCH] $2  want=${want:0:16} got=${got:0:16}"; return 1
  fi
  echo "  [OK] $2  ${got:0:16}"
  return 0
}

echo "Verifying part checksums against MANIFEST ..."
VERIFY_FAIL=0
verify_part sha256_code_bundle   code.bundle       || VERIFY_FAIL=1
verify_part sha256_mongo_archive mongo.archive.gz  || VERIFY_FAIL=1
verify_part sha256_caddy_data    caddy_data.tar    || VERIFY_FAIL=1
verify_part sha256_caddy_config  caddy_config.tar  || VERIFY_FAIL=1
verify_part sha256_hostfiles     hostfiles.tar     || VERIFY_FAIL=1

if [[ "$VERIFY_FAIL" -ne 0 ]]; then
  echo "ERROR: one or more parts failed checksum verification. Aborting - target untouched." >&2
  exit 5
fi
echo "All parts verified against MANIFEST."

# ---------------------------------------------------------------------------
# Manifest facts for the plan + post-restore parity check
# ---------------------------------------------------------------------------
M_GIT_SHA="$(manifest_val git_sha)"
M_SRC_HOST="$(manifest_val source_host)"
M_CREATED="$(manifest_val created_utc)"
M_CKPT="$(manifest_val checkpoint_path)"
M_COUNT_DET="$(manifest_val count_detections)"
M_COUNT_ML="$(manifest_val count_mission_log)"
M_COUNT_USERS="$(manifest_val count_users)"
M_AUDIT_HEAD="$(manifest_val audit_head_seq)"

echo "=================================================================="
echo "MEGHDUT C3 restore plan"
echo "  Tool version:   ${RESTORE_TOOL_VERSION}"
echo "  Bundle:         ${BUNDLE}"
echo "  Bundle git SHA: ${M_GIT_SHA}   created ${M_CREATED}   from ${M_SRC_HOST}"
echo "  Target:         ${TARGET_USER}@${TARGET_HOST}:${REMOTE_PATH}"
echo "  Mongo:          ${MONGO_DB} (container ${MONGO_CONTAINER})"
echo "  Caddy volumes:  ${CADDY_DATA_VOLUME}, ${CADDY_CONFIG_VOLUME}"
echo "  Checkpoint dst: ${M_CKPT}"
echo "  Expected counts: detections=${M_COUNT_DET} mission_log=${M_COUNT_ML} users=${M_COUNT_USERS} audit_head_seq=${M_AUDIT_HEAD}"
echo "  Mode:           ${MODE}"
echo "=================================================================="

if [[ "$MODE" == "dry-run" ]]; then
  echo "DRY RUN complete: bundle decrypted + fully verified. Target NOT touched."
  echo "Re-run with --apply to restore onto ${TARGET_HOST}."
  exit 0
fi

# ---------------------------------------------------------------------------
# --apply from here.
# ---------------------------------------------------------------------------
if ! ssh_remote "echo OK" >/dev/null 2>&1; then
  echo "ERROR: cannot SSH to ${TARGET_USER}@${TARGET_HOST}. Aborting." >&2
  exit 2
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -rp "Apply: restore this bundle onto ${TARGET_HOST}? [y/N] " CONFIRM
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "Aborted by user. Target untouched."
    exit 0
  fi
fi

# ---- 3. CODE -------------------------------------------------------------
echo "[3/8] CODE  git bundle verify + clone ..."
git bundle verify "${EXTRACT_DIR}/code.bundle" >/dev/null
# Copy the bundle to the target and clone if REMOTE_PATH has no repo yet.
ssh_remote "mkdir -p '$(dirname "$REMOTE_PATH")'"
sshpass -e scp -o StrictHostKeyChecking=accept-new \
  "${EXTRACT_DIR}/code.bundle" "${TARGET_USER}@${TARGET_HOST}:/tmp/meghdut-code.bundle"
if ssh_remote "test -d '${REMOTE_PATH}/.git'"; then
  echo "  NOTE: ${REMOTE_PATH}/.git already exists on target - NOT cloning over it."
  echo "        To adopt bundle history manually on the target:"
  echo "          cd ${REMOTE_PATH} && git fetch /tmp/meghdut-code.bundle '*:*'"
else
  ssh_remote "git clone /tmp/meghdut-code.bundle '${REMOTE_PATH}'"
  echo "  Cloned bundle -> ${REMOTE_PATH}"
fi
ssh_remote "rm -f /tmp/meghdut-code.bundle"

# ---- 4. MONGO ------------------------------------------------------------
# SAFETY SNAPSHOT (before the destructive `mongorestore --drop`): dump the
# CURRENT target DB to a timestamped archive ON THE TARGET host, so a failure
# part-way through the drop+restore is recoverable. If the target has no
# cema-mongo container or an empty DB (a fresh box), there is nothing to lose,
# so the snapshot is skipped with a note.
SNAP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
SAFETY_PATH="${REMOTE_PATH}/restore-safety-${SNAP_TS}.archive.gz"
echo "[4/8] MONGO  pre-restore safety snapshot of the CURRENT target DB ..."
SNAP_OUT="$(ssh_remote "
  C='${MONGO_CONTAINER}'
  if ! docker inspect \"\$C\" >/dev/null 2>&1; then echo '__SKIP__'; exit 0; fi
  ncoll=\$(docker exec \"\$C\" mongosh --quiet '${MONGO_DB}' --eval 'db.getCollectionNames().length' 2>/dev/null || echo 0)
  case \"\$ncoll\" in (*[!0-9]*|'') ncoll=0 ;; esac
  if [ \"\$ncoll\" -eq 0 ]; then echo '__SKIP__'; exit 0; fi
  if docker exec \"\$C\" sh -c 'mongodump --db ${MONGO_DB} --archive --gzip' > '${SAFETY_PATH}' 2>/dev/null && [ -s '${SAFETY_PATH}' ]; then
    echo '__OK__'
  else
    echo '__FAIL__'
  fi
")" || true

# Fail-CLOSED: only an explicit __OK__ (snapshot written) or __SKIP__ (nothing
# to snapshot) may proceed to the destructive --drop. Any other outcome -
# __FAIL__, an empty string from a transient SSH failure, or an unexpected
# token - is treated as "snapshot state unknown" and ABORTS before any drop.
if echo "$SNAP_OUT" | grep -q '__OK__'; then
  echo "  +--------------------------------------------------------------+"
  echo "  | SAFETY SNAPSHOT WRITTEN on the TARGET:                        |"
  echo "  |   ${SAFETY_PATH}"
  echo "  +--------------------------------------------------------------+"
elif echo "$SNAP_OUT" | grep -q '__SKIP__'; then
  echo "  (fresh target: no existing cema-mongo/DB to snapshot - nothing to lose)"
  SAFETY_PATH=""
else
  echo "ERROR: pre-restore safety snapshot state is INDETERMINATE (result: '${SNAP_OUT}')." >&2
  echo "       Refusing to run a destructive --drop restore without a confirmed" >&2
  echo "       recoverable snapshot of the current DB. Aborting - target Mongo untouched." >&2
  exit 6
fi

# Prominent pre-drop warning: the next command DESTROYS the target DB (--drop).
echo "  =============================================================="
echo "  !! DESTRUCTIVE STEP: 'mongorestore --drop' is about to DROP every"
echo "  !! collection in ${MONGO_DB} on ${TARGET_HOST} and replace it with"
echo "  !! the bundle's data. The current target data will be GONE."
if [[ -n "$SAFETY_PATH" ]]; then
  echo "  !! To ROLL BACK to the pre-restore state, run ON THE TARGET:"
  echo "  !!   docker exec -i ${MONGO_CONTAINER} sh -c \\"
  echo "  !!     'mongorestore --db ${MONGO_DB} --archive --gzip --drop' \\"
  echo "  !!     < '${SAFETY_PATH}'"
fi
echo "  =============================================================="

echo "[4/8] MONGO  mongorestore --drop ..."
cat "${EXTRACT_DIR}/mongo.archive.gz" | \
  ssh_remote "docker exec -i ${MONGO_CONTAINER} sh -c 'mongorestore --db ${MONGO_DB} --archive --gzip --drop'"

# ---- 5. CADDY ------------------------------------------------------------
echo "[5/8] CADDY  unpack CA volumes ..."
cat "${EXTRACT_DIR}/caddy_data.tar" | \
  ssh_remote "docker run --rm -i -v ${CADDY_DATA_VOLUME}:/v busybox tar xf - -C /v"
cat "${EXTRACT_DIR}/caddy_config.tar" | \
  ssh_remote "docker run --rm -i -v ${CADDY_CONFIG_VOLUME}:/v busybox tar xf - -C /v"

# ---- 6. HOST FILES -------------------------------------------------------
echo "[6/8] HOST  placing .env / udev / checkpoint / kismet / units ..."
# hostfiles.tar was created on the source with absolute paths (tar stripped the
# leading '/'); extracting with -C / would restore them to absolute locations.
# SECURITY: do NOT blindly extract arbitrary absolute paths as root. First list
# the archive locally and validate EVERY member against an explicit allowlist of
# the files this tool is supposed to carry (the three .env, the udev rule, the ML
# checkpoint, the kismet config, and cema-*.service units). Abort if the archive
# contains anything outside that allowlist, so a tampered bundle can never write
# a rogue file (e.g. /etc/sudoers.d/*, an ssh authorized_keys, a cron unit) to /.
HOSTFILES_TAR="${EXTRACT_DIR}/hostfiles.tar"

# Allowlist of exact member paths, leading '/' stripped to match tar's storage.
declare -a HOST_ALLOW=(
  "${REMOTE_PATH#/}/.env"
  "${REMOTE_PATH#/}/field-bridge/.env"
  "${REMOTE_PATH#/}/rf-bridge/.env"
  "etc/udev/rules.d/99-cema-sik-adapter.rules"
  "etc/kismet/kismet_site.conf"
  "${M_CKPT#/}"
)
# cema-*.service units are matched by glob in host_member_allowed below.

host_member_allowed() {
  local m="$1" a
  m="${m#./}"; m="${m#/}"; m="${m%/}"   # strip leading ./ , leading / , trailing /
  [[ -z "$m" ]] && return 0             # ignore empty / pure-directory lines
  for a in "${HOST_ALLOW[@]}"; do
    [[ "$m" == "$a" ]] && return 0
  done
  [[ "$m" == etc/systemd/system/cema-*.service ]] && return 0
  return 1
}

HOST_BAD=0
while IFS= read -r _m; do
  [[ -z "$_m" ]] && continue
  if ! host_member_allowed "$_m"; then
    echo "  [DISALLOWED] ${_m}" >&2
    HOST_BAD=1
  fi
done < <(tar tf "$HOSTFILES_TAR")

if [[ "$HOST_BAD" -ne 0 ]]; then
  echo "ERROR: hostfiles.tar contains member(s) outside the expected allowlist" >&2
  echo "       (.env x3, udev rule, ML checkpoint, kismet config, cema-*.service" >&2
  echo "       units). Refusing to extract arbitrary absolute paths as root." >&2
  echo "       Aborting - no host files written to the target." >&2
  exit 7
fi

# All members validated against the allowlist. Extract to the target's absolute
# paths. /etc targets and the checkpoint dir may need root, so use sudo; -p
# preserves modes. stderr is NOT suppressed - extraction failures are surfaced.
cat "$HOSTFILES_TAR" | \
  ssh_remote "sudo tar xpf - -C / || tar xpf - -C /"
echo "  If any /etc/* or unit files were skipped for permissions, re-run the"
echo "  extraction on the target as root. After placing unit files, run:"
echo "     sudo systemctl daemon-reload"

# ---- 7. MANUAL STEPS -----------------------------------------------------
echo "[7/8] REMAINING MANUAL STEPS (NOT performed by this script):"
echo "   a. Rebuild the field-bridge/rf-bridge Python venvs from requirements"
echo "      (they hold torch/numpy/pyserial and are host-specific, not in the"
echo "      bundle). See MIGRATION_RUNBOOK.md."
echo "   b. cd ${REMOTE_PATH} && docker compose build   (rebuild backend/frontend images)"
echo "   c. Bring the stack up FAIL-CLOSED (TX-halt), verify Caddy serves the"
echo "      restored internal-CA leaf certs, then reload systemd units:"
echo "         sudo systemctl daemon-reload && docker compose up -d"
echo "   d. Confirm CEMA_ML_CHECKPOINT resolves to ${M_CKPT} and the .pt is present."

# ---- 8. POST-RESTORE PARITY ----------------------------------------------
echo "[8/8] POST-RESTORE parity check (counts + audit head vs manifest) ..."
mongo_eval() {
  ssh_remote "docker exec ${MONGO_CONTAINER} mongosh --quiet '${MONGO_DB}' --eval \"$1\"" 2>/dev/null || true
}
NOW_DET="$(mongo_eval 'db.detections.countDocuments({})')"
NOW_ML="$(mongo_eval 'db.mission_log.countDocuments({})')"
NOW_USERS="$(mongo_eval 'db.users.countDocuments({})')"
NOW_AUDIT="$(mongo_eval 'var d=db.mission_log.find({},{seq:1,_id:0}).sort({seq:-1}).limit(1).toArray(); d.length?d[0].seq:\"none\"')"

parity() {
  # $1 label, $2 want, $3 got
  if [[ "$2" == "$3" ]]; then
    echo "  [OK]    $1: ${3}"
  else
    echo "  [DIFF]  $1: manifest=${2} restored=${3}"
  fi
}
parity "detections"      "$M_COUNT_DET"   "$NOW_DET"
parity "mission_log"     "$M_COUNT_ML"    "$NOW_ML"
parity "users"           "$M_COUNT_USERS" "$NOW_USERS"
parity "audit_head_seq"  "$M_AUDIT_HEAD"  "$NOW_AUDIT"

echo "=================================================================="
echo "RESTORE COMPLETE (data + CA + host files placed on ${TARGET_HOST})."
echo "Finish the MANUAL steps above before the system is operational."
echo "Decrypted stage: shredded."
echo "=================================================================="
exit 0
