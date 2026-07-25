#!/usr/bin/env bash
# CEMA cUAS RF Bridge — one-time host install.
# Debian / Ubuntu / Kali. Tolerant of partial apt failures.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# --- helpers ------------------------------------------------------------
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yell()  { printf '\033[33m%s\033[0m\n' "$*"; }
have()  { command -v "$1" >/dev/null 2>&1; }

# sudo prefix: empty when running as root (Kali default), else 'sudo'
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# task #36: HackRF packages (hackrf, libhackrf-dev, libhackrf0,
# libusb-1.0-0-dev) were removed here — the HackRF scanner that needed them
# (hackrf_scanner.py) was a duplicate of field-bridge/hackrf_rx.py and has
# been removed from this directory. This bridge only needs the serial
# (SiK/RFD900) radio, not the HackRF. If you're installing field-bridge's
# HackRF tooling, see field-bridge/'s own install steps for those packages.
APT_PKGS=(pkg-config python3-venv python3-pip)

# --- 1. apt update (best-effort; don't die on 3rd-party repo failure) ---
echo "==> apt: refreshing package index (best effort)"
$SUDO apt-get update -o Acquire::AllowInsecureRepositories=true 2>&1 | tail -20 || \
    yell "  (apt update returned non-zero — probably a broken 3rd-party repo; continuing)"

# --- 2. install packages one by one so a single miss doesn't abort ------
echo "==> apt: installing Python venv deps"
MISSING=()
for pkg in "${APT_PKGS[@]}"; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        printf '   [ok]      %s (already installed)\n' "$pkg"
        continue
    fi
    if $SUDO apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1; then
        printf '   [install] %s\n' "$pkg"
    else
        MISSING+=("$pkg")
        printf '   [MISS]    %s\n' "$pkg"
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    red "!! Could not install: ${MISSING[*]}"
    yell "   Try:  $SUDO apt install ${MISSING[*]}"
    yell "   (Fix any broken repo lines under /etc/apt/sources.list.d/ first.)"
fi

# --- 3. permissions ------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "==> udev: adding you to dialout, plugdev"
    $SUDO usermod -a -G dialout,plugdev "$USER" || \
        yell "  (usermod failed — non-fatal)"
fi

# --- 4. python venv + deps ----------------------------------------------
if [ ! -d ".venv" ]; then
    echo "==> creating python venv (.venv)"
    if ! python3 -m venv .venv; then
        red "!! python3 -m venv failed. Install with:  $SUDO apt install python3-venv"
        exit 2
    fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> pip install -r requirements.txt"
pip install --quiet --upgrade pip
if ! pip install --quiet -r requirements.txt; then
    red "!! pip install failed. See full log with:  pip install -r requirements.txt"
    exit 2
fi
green "   Python deps installed."

# --- 5. .env config file -------------------------------------------------
if [ ! -f ".env" ]; then
    cp env.example .env
    green "==> created .env from env.example — edit if needed."
else
    yell "==> .env already exists (kept as-is)"
fi

# --- 6. sanity checks ----------------------------------------------------
echo
echo "==> Sanity checks"
if [ -e /dev/ttyUSB0 ]; then
    green "   /dev/ttyUSB0 present."
else
    yell "   /dev/ttyUSB0 not present — plug in the FPV telemetry radio."
fi

echo
green "==> DONE."
echo "    Next steps:"
echo "      source .venv/bin/activate     # if not already active"
echo "      cat .env                      # edit CEMA_API_URL etc. if needed"
echo "      ./run.sh                      # start the mavlink bridge"
echo
echo "    (Log out and back in if you're a non-root user — new groups need to take effect.)"
