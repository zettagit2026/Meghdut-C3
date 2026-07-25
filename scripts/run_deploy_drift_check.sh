#!/opt/homebrew/opt/bash/bin/bash
#
# run_deploy_drift_check.sh
#
# Non-interactive wrapper for check_deploy_drift.sh, intended to be invoked
# by a launchd user agent (or any other unattended scheduler).
#
# It pulls the primary-server SSH password out of the macOS Keychain (never
# from a file, never hardcoded here) and exports it as PRIMARY_SSH_PASS,
# which check_deploy_drift.sh reads instead of prompting interactively.
#
# Keychain item expected (create once, interactively, per-machine):
#   security add-generic-password -a biswajit -s cema-primary-ssh -w '<password>'
#
# This script contains no secrets. It is safe to commit to git.

set -uo pipefail

# launchd user agents run with a minimal default PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) that does not include Homebrew's prefix, so
# dependencies like sshpass (and the Homebrew bash used by the shebang above)
# would otherwise fail to resolve when this is invoked unattended.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

KEYCHAIN_ACCOUNT="biswajit"
KEYCHAIN_SERVICE="cema-primary-ssh"

PRIMARY_SSH_PASS="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"

if [[ -z "${PRIMARY_SSH_PASS:-}" ]]; then
  echo "ERROR: could not read SSH password from Keychain (account=${KEYCHAIN_ACCOUNT}, service=${KEYCHAIN_SERVICE})." >&2
  echo "Run: security add-generic-password -a ${KEYCHAIN_ACCOUNT} -s ${KEYCHAIN_SERVICE} -w '<password>'" >&2
  exit 3
fi

export PRIMARY_SSH_PASS

exec "${REPO_ROOT}/scripts/check_deploy_drift.sh" --quiet
