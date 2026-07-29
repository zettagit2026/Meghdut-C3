"""CEMA-Enabled cUAS backend.

Endpoints (all under /api):
  auth: /login /logout /me
  detections: /detections /detections/ingest /detections/{id}/cema-advance /detections/{id}/killchain-advance /detections/{id}/authorize-target /detections/{id}
  swarm: /swarm/taxonomy (read-only Type-I..IV definitions)
         /swarm/clusters (read-only GET, computes but does not persist)
         /swarm/clusters/recompute (POST, computes+persists swarm_id via
         backend/swarm_classifier.py; see that module's docstring for full
         honesty scoping)
  iff: /iff/beacons/ingest (from field-bridge/iff_beacon_bridge.py, already HMAC-verified bridge-side)
       /iff/friendlies (current fresh friendly-asset roster; task #103 attestation gate consumer)
  spectrum: /spectrum/waterfall
  mavlink: /mavlink/craft (preview-only) /mavlink/broadcast (commander-only, transmits) /mavlink/packets  (ws /ws/mavlink)
  payloads: /payloads /payloads/deploy (commander-only + arm-token for CRITICAL/broadcast)
  jamming: /payloads/jam (commander-only + arm-token + jam-confirm-token, always CRITICAL)
           /jam/confirm (commander-only, issues a single-use jam-confirm token)
           /jam/status  (current/last jam session state)
  arm: /arm (commander-only, issues a 60s single-use arm token)
  emergency: /emergency/abort (any operator) /emergency/resume (commander-only)
  range-authorization: /range-authorization/status (any authenticated user)
           /range-authorization (commander + password re-auth + confirm phrase
           to enable; low-friction to disable) — see
           backend/RANGE_AUTHORIZATION_REDESIGN.md
  logs:  /logs

RBAC: two roles, "operator" and "commander". Anything that transmits a
kinetic/broadcast command (/payloads/deploy, /mavlink/broadcast, /payloads/jam)
requires "commander". CRITICAL-severity payload deploys, any broadcast
(target_system=0), and ALL real RF jamming additionally require a fresh arm
token from POST /arm. Targeting a specific detection requires it be
explicitly authorized via POST /detections/{id}/authorize-target
(friendly-fire interlock).

RF JAMMING (/payloads/jam) is a SEPARATE, additionally-gated capability on
top of everything above: it also requires a jam_confirm_token from POST
/jam/confirm, which the frontend is only supposed to request at the exact
moment an operator completes SafetyGate.jsx's two-step confirm (checklist +
ARM & FIRE -> CONFIRM FIRE) for the jam action — see frontend/src/pages/
Jamming.jsx. This backend-side token is necessary but NOT sufficient on its
own: the physical bridge host (field-bridge/jam_bridge.py) independently
checks this backend's live GET /api/range-authorization/status?effect=jam
lease before transmitting, regardless of what this backend has already
approved for the arm/jam-confirm tokens above. (Formerly this bridge-side
check was a static CEMA_AUTHORIZED_RANGE=1 env var on the bridge host itself;
that has been replaced by the GUI-controlled, auto-expiring
range-authorization lease described in backend/RANGE_AUTHORIZATION_REDESIGN.md
— see that document for the full threat model of this change.) See
field-bridge/jam_bridge.py's module docstring for the full defense-in-depth
chain and why it preserves — rather than casually approximates — the
original interactive "type TRANSMIT" gate in field-bridge/hackrf_jam.py.
"""
from __future__ import annotations

from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import asyncio
import base64
import binascii
import importlib.util
import json
import logging
import math
import os
import statistics
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from starlette.middleware.cors import CORSMiddleware

from mavlink_codec import (
    build_packet_v1,
    build_packet_v2,
    build_command_long_payload,
    describe_packet,
    hexdump,
    CRC_EXTRA,
)
from payload_library import PAYLOAD_CATALOG, PAYLOAD_BUILDERS, get_payload_by_id
from detection_state import (
    CEMA_STAGES,
    KILL_CHAIN,
    advance_cema,
    advance_kill_chain,
)
from swarm_classifier import build_swarm_clusters, SWARM_TAXONOMY

# ---------- Config ----------
# SECURITY: no hardcoded/default secrets. Operators MUST supply real values via
# the environment (e.g. a `.env` file next to docker-compose.yml — already
# excluded by .gitignore; never commit it). We fail fast at import time rather
# than silently booting with a known-weak or placeholder credential.
_PLACEHOLDER_SECRETS = {
    "", "change-me", "changeme", "change-this", "cema@2026", "password",
    "secret", "admin", "admin123",
}


def _require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(
            f"{name} is not set. Set it via the environment (e.g. a gitignored "
            f".env file) before starting the backend — no default is provided "
            f"for safety-critical deployments."
        )
    return val


JWT_SECRET = _require_env("JWT_SECRET")
if JWT_SECRET.lower() in _PLACEHOLDER_SECRETS or JWT_SECRET.lower().startswith("change-me"):
    raise RuntimeError(
        "JWT_SECRET matches a known placeholder/default value. Generate a real "
        "secret (e.g. `openssl rand -hex 32`) and set it via a gitignored .env file."
    )
JWT_ALGO = "HS256"

ADMIN_EMAIL = _require_env("ADMIN_EMAIL")

ADMIN_PASSWORD = _require_env("ADMIN_PASSWORD")
if ADMIN_PASSWORD.lower() in _PLACEHOLDER_SECRETS:
    raise RuntimeError(
        "ADMIN_PASSWORD matches a known placeholder/default value ('cema@2026', "
        "'change-me', etc). Set a strong password via a gitignored .env file."
    )

# ---------- Mongo ----------
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

# ---------- App ----------
app = FastAPI(title="CEMA cUAS Operator Console")
api = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cema")

# ---- Ingest health tracking (task #74) -------------------------------------
# Background: field-bridge scripts logged in once at startup; a 12h JWT TTL
# expiry caused silent, invisible 401-looping for hours before it was caught
# (fixed separately via _post_with_reauth()/_reauth_once() in every bridge).
# The gap this closes: preflight.sh / /api/health only checked log-file mtime
# freshness, never whether ingest writes were actually succeeding server-side.
# These paths are the full set of bridge->backend ingest endpoints (TX-side
# jam_bridge.py is deliberately out of scope — see AGENT.md task #74 notes).
INGEST_PATHS = {"/api/detections/ingest", "/api/spectrum/ingest", "/api/fpv/ingest"}
AUTH_FAIL_CONSECUTIVE_THRESHOLD = 3


# =====================================================================
# Auth helpers
# =====================================================================
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id, "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Dict[str, Any]:
    if creds is None or not creds.credentials:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(401, "Wrong token type")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(401, "User not found")
    return user


# ---- RBAC ----
# Two roles: "operator" (can observe, craft/preview, request arming) and
# "commander" (elevated — required for anything that transmits a kinetic /
# broadcast-takedown command). This is intentionally simple (single elevated
# role) rather than a full permission matrix — sufficient for this eval-stage
# fix per the review's scope.
async def require_commander(user: Dict = Depends(get_current_user)) -> Dict:
    if user.get("role") != "commander":
        raise HTTPException(403, "Commander role required for this action")
    return user


# ---- Arm-token (second factor for CRITICAL-severity / broadcast actions) ----
# A short-lived, single-use, server-side token. A commander must explicitly
# POST /arm to obtain one; it must then be presented on the very next
# CRITICAL-severity payload deploy or broadcast (target_system=0) request
# within ARM_TOKEN_TTL_S seconds. This is the "second factor" that stops a
# single click/request from ever being enough to trigger FORCE_DISARM,
# FLIGHT_TERMINATION, or a swarm-wide PL-010 broadcast takedown.
ARM_TOKEN_TTL_S = 60
_arm_tokens: Dict[str, datetime] = {}


def _issue_arm_token() -> Dict[str, Any]:
    token = str(uuid.uuid4())
    _arm_tokens[token] = datetime.now(timezone.utc) + timedelta(seconds=ARM_TOKEN_TTL_S)
    return {"arm_token": token, "expires_in_s": ARM_TOKEN_TTL_S}


def _consume_arm_token(token: Optional[str]) -> None:
    """Validate and burn a single-use arm token. Raises 403 if missing/expired/unknown."""
    if not token:
        raise HTTPException(
            403,
            "Arm token required: this action needs a fresh POST /api/arm "
            "(commander role) before it can proceed.",
        )
    expiry = _arm_tokens.pop(token, None)
    if not expiry or datetime.now(timezone.utc) > expiry:
        raise HTTPException(403, "Arm token invalid or expired — request a new one via POST /api/arm")


# ---- Jam-confirm token (SEPARATE from arm_token — the digital equivalent
# of physically typing 'TRANSMIT' at hackrf_jam.py's interactive prompt) ----
# RF jamming (/payloads/jam) needs its own, distinct, single-use token — NOT
# a reuse of the arm token — because it stands for a different thing: proof
# that the operator just walked through frontend/src/pages/Jamming.jsx's
# SafetyGate-style two-step confirm (5-point checklist + ARM & FIRE ->
# CONFIRM FIRE) for THIS specific jam action. POST /jam/confirm is meant to
# be called by the frontend at the exact instant that confirm completes —
# never pre-fetched, never cached, never reused across requests. This token
# is then forwarded (already consumed here) inside the jam_request WS
# message to field-bridge/jam_bridge.py, which additionally checks its own
# shape (see jam_bridge.MIN_CONFIRM_TOKEN_LEN) as a defense-in-depth belt —
# though the real single-use validation happens here, once, before the
# bridge ever sees it.
#
# Short TTL (30s, shorter than the arm token's 60s) since this is meant to be
# consumed within roughly one HTTP round-trip of the confirm click, not held
# for later use.
JAM_CONFIRM_TTL_S = 30
_jam_confirm_tokens: Dict[str, datetime] = {}


def _issue_jam_confirm_token() -> Dict[str, Any]:
    token = str(uuid.uuid4())
    _jam_confirm_tokens[token] = datetime.now(timezone.utc) + timedelta(seconds=JAM_CONFIRM_TTL_S)
    return {"jam_confirm_token": token, "expires_in_s": JAM_CONFIRM_TTL_S}


def _consume_jam_confirm_token(token: Optional[str]) -> None:
    if not token:
        raise HTTPException(
            403,
            "Jam confirmation token required: complete the SafetyGate checklist and "
            "ARM & FIRE -> CONFIRM FIRE sequence in the Jamming UI, which requests a "
            "fresh POST /api/jam/confirm at the moment of confirmation.",
        )
    expiry = _jam_confirm_tokens.pop(token, None)
    if not expiry or datetime.now(timezone.utc) > expiry:
        raise HTTPException(403, "Jam confirmation token invalid or expired — re-run the "
                                  "confirmation sequence in the Jamming UI.")


# ---- GNSS-spoof-confirm token (Task #103) — DELIBERATELY a SEPARATE token
# type from jam_confirm_token, NOT a shared token mechanism with an `effect`
# discriminator. Rationale (see field-bridge/GNSS_SPOOF_ARCHITECTURE.md §4):
# jam_bridge.py's confirm-token shape check
# (_looks_like_real_confirm_token) is deliberately dumb/shape-only, trusting
# that the backend already did the real single-use validation. If jam and
# spoof shared one token type, a caller bug that forwarded a valid
# jam_confirm_token where a spoof confirm was expected would be silently
# accepted by that shape check. Distinct token types make that class of bug
# a hard 422/403 at the backend instead of a silent cross-effect
# authorization leak — this is a deliberate defense-in-depth property, not
# an oversight to "simplify" later.
GNSS_SPOOF_CONFIRM_TTL_S = 30  # mirrors JAM_CONFIRM_TTL_S
_gnss_spoof_confirm_tokens: Dict[str, datetime] = {}

# Binds the exact friendly-asset-attestation text to the confirm token that
# was minted for it, so /payloads/gnss-spoof can verify the text resubmitted
# at fire-time matches what was attested at confirm-time (see §5a of the
# architecture doc) — closes the gap where a checkbox "vanishes" after being
# ticked once with no lasting record.
_gnss_spoof_confirm_attestations: Dict[str, str] = {}

# Minimum length for a friendly-asset attestation to be accepted as a real,
# actively-typed statement rather than a trivially fabricated placeholder —
# same "reject trivially fabricated values" posture as
# jam_bridge._looks_like_real_confirm_token's length floor.
MIN_FRIENDLY_ASSET_ATTESTATION_LEN = 20
_TRIVIAL_ATTESTATION_VALUES = {"n/a", "na", "none", "confirmed", "yes", "ok", "test"}


def _looks_like_real_attestation(text: Optional[str]) -> bool:
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < MIN_FRIENDLY_ASSET_ATTESTATION_LEN:
        return False
    if stripped.lower() in _TRIVIAL_ATTESTATION_VALUES:
        return False
    return True


def _issue_gnss_spoof_confirm_token(attestation: str) -> Dict[str, Any]:
    token = str(uuid.uuid4())
    _gnss_spoof_confirm_tokens[token] = datetime.now(timezone.utc) + timedelta(seconds=GNSS_SPOOF_CONFIRM_TTL_S)
    _gnss_spoof_confirm_attestations[token] = attestation
    return {"gnss_spoof_confirm_token": token, "expires_in_s": GNSS_SPOOF_CONFIRM_TTL_S}


def _consume_gnss_spoof_confirm_token(token: Optional[str], attestation: str) -> None:
    """Validates AND pops the token, and additionally checks that the
    attestation text resubmitted at fire-time matches the text that was
    attested at /gnss-spoof/confirm time for THIS token — see
    _gnss_spoof_confirm_attestations above."""
    if not token:
        raise HTTPException(
            403,
            "GNSS-spoof confirmation token required: complete the SafetyGate checklist "
            "and ARM & FIRE -> CONFIRM FIRE sequence in the GNSS Spoof UI, which requests "
            "a fresh POST /api/gnss-spoof/confirm at the moment of confirmation. This "
            "token is NOT interchangeable with jam_confirm_token.",
        )
    expiry = _gnss_spoof_confirm_tokens.pop(token, None)
    attested_text = _gnss_spoof_confirm_attestations.pop(token, None)
    if not expiry or datetime.now(timezone.utc) > expiry:
        raise HTTPException(403, "GNSS-spoof confirmation token invalid or expired — re-run the "
                                  "confirmation sequence in the GNSS Spoof UI.")
    if attested_text != attestation:
        raise HTTPException(
            400,
            "friendly_asset_attestation does not match the text attested at "
            "/gnss-spoof/confirm time for this token — the attestation must be "
            "identical between the confirm and fire calls (defense against the "
            "attestation text being swapped between confirm and fire).",
        )


_EARTH_RADIUS_M = 6371000.0  # mean earth radius, meters — standard spherical-earth approximation


def geodesic_destination(lat_deg: float, lon_deg: float, distance_m: float,
                         bearing_deg: float) -> tuple:
    """Standard great-circle destination-point formula (spherical earth):
    given a start lat/lon, a distance in meters, and an initial bearing in
    degrees (0=N, 90=E, 180=S, 270=W), returns (dest_lat_deg, dest_lon_deg).

    This is the textbook "direct geodesic problem" formula, e.g. as given in
    Ed Williams' Aviation Formulary / Movable Type Scripts'
    "Destination point given distance and bearing from start point":
        phi2 = asin( sin(phi1)*cos(delta) + cos(phi1)*sin(delta)*cos(theta) )
        lambda2 = lambda1 + atan2( sin(theta)*sin(delta)*cos(phi1),
                                    cos(delta) - sin(phi1)*sin(phi2) )
    where delta = distance_m / EARTH_RADIUS_M is the angular distance,
    theta is the bearing, phi/lambda are lat/lon in radians.

    Verified in backend/tests/test_gnss_spoof.py against a known reference
    case (see that file for the worked numbers)."""
    phi1 = math.radians(lat_deg)
    lambda1 = math.radians(lon_deg)
    theta = math.radians(bearing_deg)
    delta = distance_m / _EARTH_RADIUS_M

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    # normalize longitude to [-180, 180]
    lambda2 = (lambda2 + 3 * math.pi) % (2 * math.pi) - math.pi
    return math.degrees(phi2), math.degrees(lambda2)


def _bearing_compass(bearing_deg: float) -> str:
    """8-point compass label for a bearing in degrees, e.g. '047° NE'."""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((bearing_deg % 360) + 22.5) // 45) % 8
    return f"{bearing_deg % 360:03.0f}° {dirs[idx]}"


# ---- Range authorization (GUI-controlled replacement for the bridge-side
# CEMA_AUTHORIZED_RANGE env var) — see backend/RANGE_AUTHORIZATION_REDESIGN.md
# for the full threat model/rationale. Two INDEPENDENT in-memory leases
# (jam/mavlink), in-memory ONLY (never persisted to Mongo, never survives a
# restart — defaults OFF every boot, same convention as _arm_tokens/
# _jam_confirm_tokens), each a short (15 min) TTL lease that must be
# explicitly re-armed (with re-auth + confirm phrase) rather than a durable
# on/off switch. field-bridge/jam_bridge.py and rf-bridge/mavlink_bridge.py
# poll GET /api/range-authorization/status?effect=... at the moment of
# transmission and fail closed on any error, exactly mirroring the old
# `CEMA_AUTHORIZED_RANGE != "1"` behavior.
RANGE_AUTH_TTL_S = 15 * 60
RANGE_AUTH_CONFIRM_PHRASE = "AUTHORIZE LIVE RANGE"
RANGE_AUTH_EFFECTS = ("jam", "mavlink", "gnss_spoof")

# effect -> {"enabled": bool, "expires_at": datetime|None, "enabled_by": str|None,
#            "enabled_at": datetime|None}
_range_authorization: Dict[str, Dict[str, Any]] = {
    effect: {"enabled": False, "expires_at": None, "enabled_by": None, "enabled_at": None}
    for effect in RANGE_AUTH_EFFECTS
}

# ---- Basic in-memory throttle on failed range-authorization re-auth attempts
# (password or confirm-phrase mismatch) — per §2.6 of the redesign doc: without
# this, the re-auth step becomes a low-cost online password-guessing oracle
# against a commander account. Simple N-failures-per-window lockout, same
# spirit as the other in-memory tables above (no new infra).
RANGE_AUTH_MAX_FAILURES = 5
RANGE_AUTH_LOCKOUT_WINDOW_S = 60
_range_auth_failures: Dict[str, List[datetime]] = {}


def _range_auth_locked_out(key: str) -> bool:
    now = datetime.now(timezone.utc)
    attempts = [t for t in _range_auth_failures.get(key, [])
                if (now - t).total_seconds() <= RANGE_AUTH_LOCKOUT_WINDOW_S]
    _range_auth_failures[key] = attempts
    return len(attempts) >= RANGE_AUTH_MAX_FAILURES


def _record_range_auth_failure(key: str) -> None:
    _range_auth_failures.setdefault(key, []).append(datetime.now(timezone.utc))


# ---- Basic in-memory throttle on failed /auth/login attempts (OWASP gap
# analysis #84) — same spirit and same constants/style as the
# range-authorization throttle above: without this, /login is a low-cost
# credential-stuffing/brute-force oracle against any known email. Simple
# N-failures-per-window lockout, keyed by the attempted account email (the
# same "key the thing being attacked" convention as _range_auth_failures,
# which keys by the commander's email — there is no authenticated user yet
# at /login, but the attempted email is the account under attack).
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_WINDOW_S = 60
_login_failures: Dict[str, List[datetime]] = {}


def _login_locked_out(key: str) -> bool:
    now = datetime.now(timezone.utc)
    attempts = [t for t in _login_failures.get(key, [])
                if (now - t).total_seconds() <= LOGIN_LOCKOUT_WINDOW_S]
    _login_failures[key] = attempts
    return len(attempts) >= LOGIN_MAX_FAILURES


def _record_login_failure(key: str) -> None:
    _login_failures.setdefault(key, []).append(datetime.now(timezone.utc))


def _range_auth_status(effect: str) -> Dict[str, Any]:
    lease = _range_authorization[effect]
    expires_at = lease["expires_at"]
    seconds_remaining = None
    if lease["enabled"] and expires_at is not None:
        seconds_remaining = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    return {
        "enabled": bool(lease["enabled"]),
        "effect": effect,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "seconds_remaining": seconds_remaining,
        "enabled_by": lease["enabled_by"],
        "enabled_at": lease["enabled_at"].isoformat() if lease["enabled_at"] else None,
    }


async def _expire_range_authorization() -> None:
    """Lazy/on-read expiry, same pattern as _expire_pending_acks/
    _expire_pending_jam: any lease whose TTL has passed with no explicit
    disable is flipped back to OFF and logged as RANGE_AUTH_EXPIRED. Called
    from GET/POST /api/range-authorization* before computing/returning state,
    so a stale 'enabled' can never be observed past its TTL."""
    now = datetime.now(timezone.utc)
    for effect, lease in _range_authorization.items():
        if lease["enabled"] and lease["expires_at"] is not None and now > lease["expires_at"]:
            lease["enabled"] = False
            lease["expires_at"] = None
            enabled_by = lease["enabled_by"]
            lease["enabled_by"] = None
            lease["enabled_at"] = None
            await log_event(
                "RANGE_AUTH_EXPIRED",
                f"Range authorization for effect={effect} expired ({RANGE_AUTH_TTL_S}s TTL) — "
                f"reverted to OFF (was enabled by {enabled_by})",
                meta={"effect": effect, "enabled_by": enabled_by},
                actor="SYSTEM",
            )
            await ws_manager.broadcast_json({"type": "range_authorization", **_range_auth_status(effect)})


# ---- Authoritative transmit-halt (server-side, checked before any TX) ----
# Set by /emergency/abort, cleared by /emergency/resume. /payloads/deploy and
# /mavlink/broadcast both check this BEFORE building/sending any frame — the
# prior implementation only broadcast a cooperative WebSocket notice with no
# server-side enforcement.
#
# In-memory ONLY (never persisted to Mongo, never survives a restart —
# defaults to the conservative/HALTED state every boot), same convention as
# _range_authorization/_arm_tokens/_jam_confirm_tokens above. Task #136 (see
# backend/TX_HALT_PERSISTENCE_SCOPE.md): a restart must never silently
# resume TX. A commander must explicitly POST /api/emergency/resume after
# every process start before jam/MAVLink-injection/GNSS-spoof TX is allowed,
# regardless of what state the flag was in immediately before the restart.
_tx_halted = True


def _check_tx_not_halted() -> None:
    if _tx_halted:
        raise HTTPException(409, "Transmission halted — EMERGENCY ABORT is in effect. "
                                  "A commander must POST /api/emergency/resume first.")


# ---- Bridge TX acknowledgment (closes the "silent success" gap) ----
# Root cause of the earlier live-demo failure: /payloads/deploy and
# /mavlink/broadcast used to build a frame, broadcast it over the WS to
# whichever bridge happened to be connected, and UNCONDITIONALLY mark the
# detection NEUTRALIZED — with no confirmation the bridge was even connected,
# let alone that it actually wrote the frame to the real serial radio. Now:
# every deploy gets a request_id, the detection is parked in AWAITING_ACK,
# and only a real tx_ack message FROM the bridge (rf-bridge/mavlink_bridge.py,
# sent after its actual pyserial/pymavlink write call) flips it to
# NEUTRALIZED (ok=True) or TX_FAILED (ok=False). If no ack arrives at all
# (bridge not connected/crashed) a lazy on-read timeout flips it to
# TX_TIMEOUT instead of leaving it stuck forever — distinct from TX_FAILED
# because a timeout means "unknown outcome", not "confirmed failure".
#
# In-memory pending-ack table, consistent with the existing _arm_tokens
# pattern (no new infra). request_id -> {ts, detection_ids, spec_name, broadcast}
_pending_acks: Dict[str, Dict[str, Any]] = {}

# 8s: real serial writes + WS round-trip to the bridge are sub-second; this
# generously covers WS scheduling/latency on the bridge host while still
# failing fast enough that an operator notices and can retry within the same
# engagement window, rather than a stale AWAITING_ACK sitting for minutes.
ACK_TIMEOUT_S = 8


async def _expire_pending_acks() -> None:
    """Lazy/on-read expiry (same pattern as _expire_stale_detections): any
    pending ack older than ACK_TIMEOUT_S with no response from the bridge is
    flipped from AWAITING_ACK to TX_TIMEOUT — a real 'we don't know' signal,
    never silently promoted to success."""
    now = datetime.now(timezone.utc)
    expired = [rid for rid, p in _pending_acks.items()
              if (now - p["ts"]).total_seconds() > ACK_TIMEOUT_S]
    for rid in expired:
        pending = _pending_acks.pop(rid, None)
        if not pending:
            continue
        det_ids = pending.get("detection_ids") or []
        if det_ids:
            await db.detections.update_many(
                {"id": {"$in": det_ids}, "status": "AWAITING_ACK"},
                {"$set": {"status": "TX_TIMEOUT"}},
            )
        await log_event(
            "BRIDGE_ACK",
            f"No bridge ACK within {ACK_TIMEOUT_S}s for request {rid} "
            f"({pending.get('spec_name', '?')}) — marking TX_TIMEOUT "
            f"(bridge not connected or not responding)",
            meta={"request_id": rid, "detection_ids": det_ids},
            actor="SYSTEM",
        )


# ---- RF jam session tracking (separate from _pending_acks/_handle_tx_ack
# above — jamming has its own richer state machine than a single
# ack/no-ack, because a real HackRF burst has an observable *duration*, not
# just a single accept/reject) ----
#
# request_id -> {ts, status, band/freq_mhz, duration_s, bandwidth_khz,
#                tx_gain, actor, error}
# status values: AWAITING_ACK -> JAM_ACTIVE -> JAM_COMPLETE | JAM_STOPPED
#                | TX_FAILED | TX_TIMEOUT
# (matches field-bridge/hackrf_jam.py's REAL behavior: a single bounded
# burst, hard-capped at MAX_DURATION_S seconds — NOT continuous. See
# field-bridge/jam_bridge.py's module docstring for why --continuous mode is
# deliberately not exposed through this integration.)
_pending_jam: Dict[str, Dict[str, Any]] = {}

# Bridge ack ("started") must arrive within this window of the request being
# sent, same reasoning as ACK_TIMEOUT_S above.
JAM_ACK_TIMEOUT_S = 8
# Once JAM_ACTIVE, a terminal ack (complete/failed/stopped) must arrive
# within duration_s + this margin, else we've lost contact with the bridge
# mid-burst (crashed, killed, network partition) — TX_TIMEOUT, not silently
# left "active" forever.
JAM_COMPLETE_MARGIN_S = 15


async def _expire_pending_jam() -> None:
    """Lazy/on-read expiry, same pattern as _expire_pending_acks. Two
    distinct expiry conditions since jam sessions have two live states:
      * AWAITING_ACK too long  -> bridge never even acknowledged the request
        (not connected / didn't see it) -> TX_TIMEOUT.
      * JAM_ACTIVE too long    -> bridge started transmitting but never sent
        a terminal ack (complete/failed/stopped) within its own declared
        duration + margin -> TX_TIMEOUT (distinct from TX_FAILED: we do not
        know whether RF is still being emitted, which is itself worth
        surfacing rather than silently clearing)."""
    now = datetime.now(timezone.utc)
    to_expire = []
    for rid, p in _pending_jam.items():
        if p["status"] == "AWAITING_ACK" and (now - p["ts"]).total_seconds() > JAM_ACK_TIMEOUT_S:
            to_expire.append(rid)
        elif p["status"] == "JAM_ACTIVE" and \
                (now - p["ts"]).total_seconds() > p.get("duration_s", 10) + JAM_COMPLETE_MARGIN_S:
            to_expire.append(rid)
    for rid in to_expire:
        p = _pending_jam.get(rid)
        if not p:
            continue
        p["status"] = "TX_TIMEOUT"
        await log_event(
            "JAM",
            f"No terminal bridge ack for jam request {rid} within expected window — "
            f"marking TX_TIMEOUT (bridge not connected, crashed, or lost mid-burst)",
            meta={"request_id": rid}, actor="SYSTEM",
        )
        await ws_manager.broadcast_json({"type": "jam_status", "request_id": rid, "status": "TX_TIMEOUT"})


async def _handle_jam_ack(msg: Dict[str, Any]) -> None:
    """Process a real {"type": "jam_ack", "phase": ..., ...} message from
    field-bridge/jam_bridge.py. phase is one of started/complete/failed/stopped.
    Only this function is allowed to move a jam session between states."""
    request_id = msg.get("request_id")
    phase = msg.get("phase")
    pending = _pending_jam.get(request_id) if request_id else None
    if not pending:
        logger.warning("jam_ack received for unknown/expired request_id=%s (phase=%s)", request_id, phase)
        return

    phase_to_status = {
        "started": "JAM_ACTIVE",
        "complete": "JAM_COMPLETE",
        "failed": "TX_FAILED",
        "stopped": "JAM_STOPPED",
    }
    status = phase_to_status.get(phase)
    if not status:
        logger.warning("jam_ack with unrecognized phase=%s for request_id=%s", phase, request_id)
        return

    pending["status"] = status
    pending["ts"] = datetime.now(timezone.utc)  # reset the clock for the next expiry window
    error = msg.get("error")
    if error:
        pending["error"] = error

    if status in ("JAM_COMPLETE", "TX_FAILED", "JAM_STOPPED"):
        # Terminal — leave the record in _pending_jam (for GET /jam/status
        # history/inspection) but it no longer needs expiry-tracking.
        pending["terminal"] = True

    await log_event(
        "JAM",
        (f"Bridge CONFIRMED jam TX started for request {request_id} "
         f"({pending.get('freq_mhz')} MHz)") if status == "JAM_ACTIVE" else
        (f"Jam burst complete for request {request_id}") if status == "JAM_COMPLETE" else
        (f"Jam burst STOPPED early (EMERGENCY ABORT) for request {request_id}") if status == "JAM_STOPPED" else
        (f"Jam burst FAILED for request {request_id}: {error or 'no reason given'}"),
        meta={"request_id": request_id, "status": status, "error": error},
        actor="BRIDGE",
    )
    await ws_manager.broadcast_json({"type": "jam_status", "request_id": request_id, "status": status,
                                     "error": error})



# =====================================================================
# GNSS spoof session tracking — parallel to _pending_jam above, own dict
# (never shares state with jamming; see architecture doc §1/§3).
# =====================================================================
_pending_gnss_spoof: Dict[str, Dict[str, Any]] = {}
GNSS_SPOOF_ACK_TIMEOUT_S = 8
GNSS_SPOOF_COMPLETE_MARGIN_S = 15


async def _expire_pending_gnss_spoof() -> None:
    """Lazy/on-read expiry, same pattern as _expire_pending_jam."""
    now = datetime.now(timezone.utc)
    to_expire = []
    for rid, p in _pending_gnss_spoof.items():
        if p["status"] == "AWAITING_ACK" and (now - p["ts"]).total_seconds() > GNSS_SPOOF_ACK_TIMEOUT_S:
            to_expire.append(rid)
        elif p["status"] == "GNSS_SPOOF_ACTIVE" and \
                (now - p["ts"]).total_seconds() > p.get("duration_s", GNSS_SPOOF_MAX_DURATION_S) + GNSS_SPOOF_COMPLETE_MARGIN_S:
            to_expire.append(rid)
    for rid in to_expire:
        p = _pending_gnss_spoof.get(rid)
        if not p:
            continue
        p["status"] = "TX_TIMEOUT"
        await log_event(
            "GNSS_SPOOF",
            f"No terminal bridge ack for gnss_spoof request {rid} within expected window — "
            f"marking TX_TIMEOUT (bridge not connected, crashed, or lost mid-burst)",
            meta={"request_id": rid}, actor="SYSTEM",
        )
        await ws_manager.broadcast_json({"type": "gnss_spoof_status", "request_id": rid, "status": "TX_TIMEOUT"})


async def _handle_gnss_spoof_ack(msg: Dict[str, Any]) -> None:
    """Process a real {"type": "gnss_spoof_ack", "phase": ..., ...} message
    from field-bridge/gnss_spoof_bridge.py. Mirrors _handle_jam_ack exactly,
    own dict, own log kind."""
    request_id = msg.get("request_id")
    phase = msg.get("phase")
    pending = _pending_gnss_spoof.get(request_id) if request_id else None
    if not pending:
        logger.warning("gnss_spoof_ack received for unknown/expired request_id=%s (phase=%s)", request_id, phase)
        return

    phase_to_status = {
        "started": "GNSS_SPOOF_ACTIVE",
        "complete": "GNSS_SPOOF_COMPLETE",
        "failed": "TX_FAILED",
        "stopped": "GNSS_SPOOF_STOPPED",
    }
    status = phase_to_status.get(phase)
    if not status:
        logger.warning("gnss_spoof_ack with unrecognized phase=%s for request_id=%s", phase, request_id)
        return

    pending["status"] = status
    pending["ts"] = datetime.now(timezone.utc)
    error = msg.get("error")
    if error:
        pending["error"] = error
    if status in ("GNSS_SPOOF_COMPLETE", "TX_FAILED", "GNSS_SPOOF_STOPPED"):
        pending["terminal"] = True

    await log_event(
        "GNSS_SPOOF_ACK",
        (f"Bridge CONFIRMED gnss_spoof TX started for request {request_id}") if status == "GNSS_SPOOF_ACTIVE" else
        (f"GNSS spoof burst complete for request {request_id}") if status == "GNSS_SPOOF_COMPLETE" else
        (f"GNSS spoof burst STOPPED early (EMERGENCY ABORT) for request {request_id}") if status == "GNSS_SPOOF_STOPPED" else
        (f"GNSS spoof burst FAILED for request {request_id}: {error or 'no reason given'}"),
        meta={"request_id": request_id, "status": status, "error": error},
        actor="BRIDGE",
    )
    await ws_manager.broadcast_json({"type": "gnss_spoof_status", "request_id": request_id, "status": status,
                                     "error": error})


async def _handle_tx_ack(msg: Dict[str, Any]) -> None:
    """Process a real {"type": "tx_ack", ...} message received FROM a
    connected bridge client over the mavlink WS — see
    rf-bridge/mavlink_bridge.py, sent only after its actual serial write
    call succeeds or raises. This is the only path that is allowed to
    transition a detection out of AWAITING_ACK into NEUTRALIZED."""
    request_id = msg.get("request_id")
    ok = bool(msg.get("ok"))
    pending = _pending_acks.pop(request_id, None) if request_id else None
    if not pending:
        logger.warning("tx_ack received for unknown/expired request_id=%s (ok=%s)", request_id, ok)
        return

    det_ids = pending.get("detection_ids") or []
    if det_ids:
        update: Dict[str, Any] = {"last_seen": datetime.now(timezone.utc).isoformat()}
        if ok:
            update.update({
                "status": "NEUTRALIZED",
                "kill_chain_stage": "DEFEAT",
                "kill_chain_index": len(KILL_CHAIN) - 1,
                "cema_stage": "EXPLOIT",
                "cema_stage_index": len(CEMA_STAGES) - 1,
            })
        else:
            update["status"] = "TX_FAILED"
        # Only advance detections still genuinely awaiting this ack (guards
        # against a late/duplicate ack clobbering a status changed since).
        await db.detections.update_many(
            {"id": {"$in": det_ids}, "status": "AWAITING_ACK"},
            {"$set": update},
        )

    err = msg.get("error")
    await log_event(
        "BRIDGE_ACK",
        (f"Bridge CONFIRMED real serial TX for request {request_id} "
         f"({pending.get('spec_name', '?')})") if ok else
        (f"Bridge reported TX FAILED for request {request_id} "
         f"({pending.get('spec_name', '?')}): {err or 'no reason given'}"),
        meta={"request_id": request_id, "ok": ok, "detection_ids": det_ids, "error": err},
        actor="BRIDGE",
    )


# =====================================================================
# Startup: seed admin, indexes
# =====================================================================
@app.on_event("startup")
async def startup() -> None:
    await db.users.create_index("email", unique=True)
    await db.ingest_health.create_index("bridge", unique=True)
    await db.iff_revocations.create_index("asset_id", unique=True)
    existing = await db.users.find_one({"email": ADMIN_EMAIL})
    if existing is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": ADMIN_EMAIL,
            "name": "Command Operator",
            "role": "commander",
            "clearance": "RESTRICTED",
            "password_hash": hash_password(ADMIN_PASSWORD),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Seeded commander operator.")
    elif not verify_password(ADMIN_PASSWORD, existing["password_hash"]):
        await db.users.update_one(
            {"email": ADMIN_EMAIL},
            {"$set": {"password_hash": hash_password(ADMIN_PASSWORD)}},
        )
        logger.info("Admin password hash refreshed.")

    # Task #136: unconditional, always-the-same, greppable startup audit
    # line — every boot starts TX-HALTED (fail-closed default, see
    # backend/TX_HALT_PERSISTENCE_SCOPE.md). There is no "resumed prior
    # state" case: a commander must always explicitly POST
    # /api/emergency/resume before TX is permitted again after a restart.
    await log_event(
        "TX_HALT_STARTUP",
        "Backend started in TX-HALTED state (fail-closed default) — a "
        "commander must POST /api/emergency/resume to enable TX.",
        actor="SYSTEM",
    )

    # NOTE: no synthetic/seeded detections are inserted here. An empty
    # detections collection on first boot is correct and honest — real
    # contacts are only ever created from real ingested data via
    # POST /detections/ingest (HackRF / SiK radio bridges).


@app.on_event("shutdown")
async def shutdown() -> None:
    client.close()


# =====================================================================
# Pydantic
# =====================================================================
class LoginBody(BaseModel):
    email: EmailStr
    password: str


class MavlinkCraftBody(BaseModel):
    version: str = Field("v2", pattern="^(v1|v2)$")
    system_id: int = 255
    component_id: int = 190
    sequence: int = 0
    message_id: int = 76  # COMMAND_LONG
    target_system: int = 1
    target_component: int = 1
    command: int = 21  # NAV_LAND
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    param5: float = 0.0
    param6: float = 0.0
    param7: float = 0.0
    arm_token: Optional[str] = None  # required when target_system == 0 (broadcast)


class DeployPayloadBody(BaseModel):
    payload_id: str
    target_detection_id: Optional[str] = None
    broadcast: bool = False
    arm_token: Optional[str] = None  # required for CRITICAL severity or broadcast


class AuthorizeTargetBody(BaseModel):
    authorized: bool = True


class JamRequestBody(BaseModel):
    # Either band (a validated preset — see JAM_BAND_PRESETS_MHZ, mirrored
    # from field-bridge/hackrf_jam.py's BAND_PRESETS_MHZ) or an explicit
    # freq_mhz must be given; freq_mhz wins if both are present, same
    # precedence as hackrf_jam.py's own CLI.
    # GNSS L1 presets (gps_l1/galileo_e1/beidou_b1/glonass_l1) added per
    # OPERATIONAL REQUIREMENTS.md — see field-bridge/hackrf_jam.py's
    # BAND_PRESETS_MHZ comment for exact freqs/GLONASS channelization note.
    band: Optional[str] = Field(None, pattern="^(433|915|2g4|bt_2g4|5g8|gps_l1|galileo_e1|beidou_b1|glonass_l1)$")
    freq_mhz: Optional[float] = None
    bandwidth_khz: float = 500.0
    duration_s: float = 5.0  # server-side clamps to JAM_MAX_DURATION_S regardless
    tx_gain: int = 20
    arm_token: str  # required unconditionally — jamming is always CRITICAL severity
    jam_confirm_token: str  # required unconditionally — see /jam/confirm


class JamConfirmBody(BaseModel):
    # No fields needed today — this endpoint's only job is to mint a token
    # at the moment it's called. Kept as an empty body (rather than no body
    # at all) so a future addition (e.g. an operator note) doesn't require a
    # breaking change to the call signature.
    pass


class RangeAuthorizationBody(BaseModel):
    effect: str = Field(pattern="^(jam|mavlink|gnss_spoof)$")
    enabled: bool
    # Required (and checked) only when enabled=True — see POST handler.
    password: Optional[str] = None
    confirm_phrase: Optional[str] = None


# ---- GNSS L1 civil-signal spoofing ("soft-kill") — Task #103. See
# field-bridge/GNSS_SPOOF_ARCHITECTURE.md for the full design. This is a
# STRUCTURALLY DIFFERENT effect from jamming: instead of denying GNSS
# reception with noise, it transmits a synthesized, structurally valid GPS
# L1 C/A signal carrying a FABRICATED position. Arming effect=jam does NOT
# implicitly authorize effect=gnss_spoof — they are independent
# range-authorization leases (see RANGE_AUTH_EFFECTS above) and use
# entirely separate, non-interchangeable confirm tokens (see
# _gnss_spoof_confirm_tokens above vs _jam_confirm_tokens).
GNSS_SPOOF_MAX_DURATION_S = 3.0  # deliberately shorter than JAM_MAX_DURATION_S (10s) — see
                                  # architecture doc §2 for why a much shorter cap is correct
                                  # for a deception effect vs. a denial effect.
GNSS_SPOOF_DEFAULT_DURATION_S = 2.0


class GnssSpoofPreviewBody(BaseModel):
    """Pure computation input — no tokens involved. See gnss_spoof_preview()."""
    true_lat: float
    true_lon: float
    true_alt_m: float = 0.0
    fake_offset_m: float
    fake_bearing_deg: float


class GnssSpoofRequestBody(BaseModel):
    band: str = Field(pattern="^(gps_l1)$")  # only gps_l1 at launch — see architecture doc §4
    duration_s: float = GNSS_SPOOF_DEFAULT_DURATION_S  # clamped server-side, see below
    tx_gain: int = 20
    fake_offset_m: float                     # REQUIRED, no default
    fake_bearing_deg: float                  # REQUIRED, no default
    true_lat: float                          # last-known-true position, REQUIRED
    true_lon: float
    true_alt_m: float = 0.0
    friendly_asset_attestation: str          # REQUIRED, logged verbatim, must match /confirm
    arm_token: str                           # required unconditionally — gnss_spoof is always CRITICAL
    gnss_spoof_confirm_token: str            # required unconditionally — see /gnss-spoof/confirm


class GnssSpoofConfirmBody(BaseModel):
    friendly_asset_attestation: str  # re-submitted here too — binds the attestation text to
                                      # THIS specific confirm-token mint (see architecture doc §5a)


# =====================================================================
# WebSocket manager for live MAVLink packet feed
# =====================================================================
class WSManager:
    def __init__(self) -> None:
        self.clients: List[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.clients:
            self.clients.remove(ws)

    async def broadcast_json(self, data: Dict) -> None:
        stale: List[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(data)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


ws_manager = WSManager()

# ---- TX-path health: WS upgrade capability -------------------------------
# Root-cause signal for the real bug found this session: /api/ws/mavlink was
# completely broken for a period because the `websockets` package (uvicorn's
# WebSocket implementation) was missing from the environment. RX-side health
# signals (mongo/hackrf/sik_radio) never caught this because RX bridges POST
# over plain HTTP (/detections/ingest, /spectrum/ingest) and don't touch the
# WS upgrade path at all — only a TX bridge (rf-bridge/mavlink_bridge.py,
# field-bridge/jam_bridge.py) needs a working WS upgrade to receive
# jam_request/packet messages and send tx_ack/jam_ack back.
#
# This is intentionally a narrow, honest proxy: "is the WS upgrade mechanism
# even POSSIBLE" (the dependency uvicorn needs to speak the WebSocket
# protocol is importable), NOT "is a bridge currently connected" (that's
# ws_clients, already reported below) and NOT a live self-test of the route
# (a synchronous health handler can't cleanly open a WS connection to itself
# without its own client/event-loop gymnastics that would add more failure
# surface than they remove). Computed once at import time since the
# installed package set doesn't change while the process is running.
WS_UPGRADE_CAPABLE = importlib.util.find_spec("websockets") is not None
if not WS_UPGRADE_CAPABLE:
    logger.error(
        "websockets package is NOT importable — the /api/ws/mavlink WebSocket "
        "route cannot accept ANY connection (this is the exact failure mode "
        "found and fixed this session: no bridge, TX or RX, could ever "
        "connect, regardless of ws_clients or whether one 'looks' connected)."
    )

# Recent-outcome window for the TX health tally below: long enough to reflect
# the current engagement/session rather than the whole mission history, short
# enough that a fixed problem (e.g. bridge reconnected) stops showing up
# quickly. Independent of DETECTION_STALE_TIMEOUT_S (that's about detection
# liveness, this is about recent TX ack outcomes).
TX_HEALTH_RECENT_WINDOW_S = 900  # 15 min

# Recency window used by system_health() to distinguish "actively failing
# auth" from "sensor legitimately idle" — see _record_ingest_outcome/
# system_health below (task #74).
INGEST_AUTH_FAIL_RECENT_WINDOW_S = 300  # 5 min


async def _record_ingest_outcome(bridge: str, status_code: int) -> None:
    """Upsert one doc per bridge into db.ingest_health reflecting the outcome
    of the most recent ingest POST. Never allowed to raise into the request
    path — callers must wrap this in try/except (see middleware below)."""
    now = datetime.now(timezone.utc).isoformat()
    if 200 <= status_code < 300:
        doc = await db.ingest_health.find_one_and_update(
            {"bridge": bridge},
            {
                "$set": {
                    "last_success_ts": now,
                    "last_attempt_ts": now,
                    "consecutive_failures": 0,
                },
                "$setOnInsert": {"bridge": bridge},
            },
            upsert=True,
            return_document=True,
        )
        return

    doc = await db.ingest_health.find_one_and_update(
        {"bridge": bridge},
        {
            "$set": {"last_error_ts": now, "last_attempt_ts": now,
                      "last_error_status": status_code},
            "$inc": {"consecutive_failures": 1},
            "$setOnInsert": {"bridge": bridge},
        },
        upsert=True,
        return_document=True,
    )
    # Edge-triggered: fire exactly once per failure episode (== not >=), so a
    # bridge stuck failing doesn't spam the mission log on every subsequent
    # request.
    if doc and doc.get("consecutive_failures") == AUTH_FAIL_CONSECUTIVE_THRESHOLD:
        await log_event(
            "INGEST_HEALTH",
            f"Ingest bridge '{bridge}' has failed {AUTH_FAIL_CONSECUTIVE_THRESHOLD} "
            f"consecutive requests (last status {status_code}) — likely the "
            f"silent-401-loop failure mode from task #74.",
            meta={"bridge": bridge, "consecutive_failures": doc.get("consecutive_failures"),
                  "last_error_status": status_code},
        )


@app.middleware("http")
async def ingest_health_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in INGEST_PATHS:
        try:
            bridge = request.headers.get("x-bridge-name") or f"unknown@{request.url.path}"
            await _record_ingest_outcome(bridge, response.status_code)
        except Exception:
            # Never let health bookkeeping fail or delay the actual response.
            logger.exception("ingest_health_middleware: failed to record outcome")
    return response


# =====================================================================
# Mission log helper
# =====================================================================
async def log_event(kind: str, message: str, meta: Optional[Dict] = None,
                    actor: Optional[str] = None) -> Dict:
    entry = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": message,
        "actor": actor or "SYSTEM",
        "meta": meta or {},
    }
    await db.mission_log.insert_one(entry.copy())
    return entry


# =====================================================================
# Routes: Auth
# =====================================================================
@api.post("/auth/login")
async def login(body: LoginBody):
    login_key = body.email.lower()
    if _login_locked_out(login_key):
        await log_event(
            "AUTH_LOGIN_FAILED",
            f"Login for {login_key} REFUSED: too many recent failed attempts "
            f"(locked out {LOGIN_LOCKOUT_WINDOW_S}s)",
            meta={"email": login_key, "reason": "locked_out"},
        )
        raise HTTPException(429, "Too many failed login attempts — try again shortly.")

    user = await db.users.find_one({"email": login_key})
    if not user or not verify_password(body.password, user["password_hash"]):
        _record_login_failure(login_key)
        await log_event(
            "AUTH_LOGIN_FAILED",
            f"Login for {login_key} REFUSED: invalid credentials",
            meta={"email": login_key, "reason": "bad_credentials"},
        )
        raise HTTPException(401, "Invalid credentials")
    token = create_access_token(user["id"], user["email"])
    await log_event("AUTH", f"Operator login: {user['email']}", actor=user["email"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"],
                 "name": user["name"], "role": user["role"],
                 "clearance": user.get("clearance", "RESTRICTED")},
    }


@api.get("/auth/me")
async def me(user: Dict = Depends(get_current_user)):
    return user


@api.post("/auth/logout")
async def logout(user: Dict = Depends(get_current_user)):
    await log_event("AUTH", f"Operator logout: {user['email']}", actor=user["email"])
    return {"ok": True}


# =====================================================================
# Routes: Detections
# =====================================================================
def _new_detection_skeleton() -> Dict[str, Any]:
    """Build the non-RF, non-fabricated skeleton (id/callsign-label/state
    machine defaults/timestamps) for a brand-new detection record. All actual
    RF/positional/identity fields (model, protocol, RSSI, bearing, distance,
    etc.) MUST come from a real ingest source (see /detections/ingest) — this
    helper never invents them. The callsign default is derived from the
    real, randomly-generated record id (not a fabricated attribute of the
    contact itself) purely so the UI has a stable short label until a real
    callsign is supplied by the ingest source. The prefix is deliberately
    classification-NEUTRAL ("CONTACT-", not "UAV-"): this label is assigned
    before any model/protocol classification (including ML reclassification,
    see _ml_wifi_reclassification) has happened, and a detection created here
    may later turn out to be Wi-Fi, not a drone. A classification-specific
    prefix would falsely imply drone identity for non-drone contacts and
    would need to be corrected on every current/future reclassification path."""
    det_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": det_id,
        "callsign": f"CONTACT-{det_id[:8].upper()}",
        "swarm_id": None,
        "cema_stage": "CAPTURE",
        "cema_stage_index": 0,
        "kill_chain_stage": "DETECT",
        "kill_chain_index": 0,
        "status": "ACTIVE",
        # Friendly-fire interlock: a detection is NOT a valid kinetic-payload
        # target until an operator explicitly authorizes it (POST
        # /detections/{id}/authorize-target). Defaults closed.
        "authorized_target": False,
        "first_seen": now,
        "last_seen": now,
        # Real re-confirmation event log (bounded, see RECONFIRM_EVENTS_CAP) --
        # every timestamp at which an ingest matched this SAME id within
        # DETECTION_MERGE_WINDOW_S. This is the raw data backing cadence
        # analysis (see /detections/{id}/cadence): it is NOT a synthetic
        # sampling of presence/absence, only the real moments a re-ingest
        # actually happened, so it can only support statistics that are
        # honest about that (event timing/regularity), not an on/off duty
        # cycle over continuous time.
        "reconfirm_events": [now],
    }


async def _expire_stale_detections() -> None:
    """Flip any ACTIVE detection that hasn't been re-confirmed within
    DETECTION_STALE_TIMEOUT_S to LOST. Lazy/on-read expiry: this app has no
    background scheduler (asyncio is only used for locks/websockets), so we
    run this check inline whenever detections are read, keeping reads
    self-consistent without adding new infrastructure. Records are only
    updated in place (status change), never deleted, to preserve the Mission
    Log / audit trail.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=DETECTION_STALE_TIMEOUT_S)).isoformat()
    await db.detections.update_many(
        {"status": "ACTIVE", "last_seen": {"$lt": cutoff}},
        {"$set": {"status": "LOST"}},
    )


# --- Sensor (HackRF RX site) fixed position ---------------------------------
# This is the ONLY geolocation we can honestly claim: the sensor's own fixed
# ground position, configured via env vars. It is NOT a detection position.
# We cannot compute a detection's absolute lat/lon because bearing_deg is
# always a 0.0 placeholder in field-bridge/hackrf_rx.py (no direction-finding
# antenna array exists on this hardware) -- see DetectionIngestBody.bearing_deg
# and hackrf_rx.py's detection dicts. distance_m/distance_estimated is a
# coarse RSSI path-loss estimate at best, with no bearing to pair it with.
# So the map can show "sensor here, contact at ~Xm, direction unknown" but
# must never plot a fabricated pin for the drone itself.
# SENSOR_LAT/SENSOR_LON default to None (not a fake real-world coordinate) so
# the frontend can render an explicit "sensor position not configured" state
# instead of silently plotting null-island or some other misleading default.
def _parse_optional_float(name: str) -> Optional[float]:
    val = os.environ.get(name, "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


SENSOR_LAT = _parse_optional_float("SENSOR_LAT")
SENSOR_LON = _parse_optional_float("SENSOR_LON")
SENSOR_LABEL = os.environ.get("SENSOR_LABEL", "RX-1")


@api.get("/sensor/position")
async def get_sensor_position(user: Dict = Depends(get_current_user)):
    configured = SENSOR_LAT is not None and SENSOR_LON is not None
    return {
        "configured": configured,
        "lat": SENSOR_LAT,
        "lon": SENSOR_LON,
        "label": SENSOR_LABEL,
        # Honesty flag surfaced to the frontend: bearing is never a real
        # measurement today, so detections can only be shown as range-only
        # (distance known, direction unknown), never as absolute pins.
        "bearing_available": False,
    }


@api.get("/detections")
async def list_detections(user: Dict = Depends(get_current_user)):
    await _expire_stale_detections()
    await _expire_pending_acks()
    docs = await db.detections.find({}, {"_id": 0}).sort("last_seen", -1).to_list(500)
    return docs


@api.get("/detections/{det_id}")
async def get_detection(det_id: str, user: Dict = Depends(get_current_user)):
    await _expire_pending_acks()
    doc = await db.detections.find_one({"id": det_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Detection not found")
    return doc


@api.get("/swarm/taxonomy")
async def get_swarm_taxonomy(user: Dict = Depends(get_current_user)):
    """The Army's real Type-I..IV swarm taxonomy (see backend/
    swarm_classifier.py module docstring for the source document). Exposed
    read-only so the frontend/operators can see the authoritative
    definitions this system classifies against -- and, just as importantly,
    which of those types this system can actually assign vs. not (see
    per_drone_type_gap)."""
    return {
        "taxonomy": SWARM_TAXONOMY,
        "per_drone_type_gap": (
            "This system can only assign Type-IV (co-ordinated attack "
            "swarm) today, as a candidate, from concurrent-detection "
            "timing clustering. Type-I/II/III require a flight-behaviour/ "
            "airframe classifier this system does not yet have, and are "
            "never guessed."
        ),
    }


async def _compute_swarm_clusters() -> List[Dict[str, Any]]:
    """Shared read-only computation: expire stale detections, fetch
    currently ACTIVE detections, and compute swarm candidate clusters (see
    backend/swarm_classifier.py). Performs no writes -- callers decide
    whether/how to persist the result."""
    await _expire_stale_detections()
    active_docs = await db.detections.find({"status": "ACTIVE"}, {"_id": 0}).to_list(500)
    return build_swarm_clusters(active_docs)


@api.get("/swarm/clusters")
async def get_swarm_clusters(user: Dict = Depends(get_current_user)):
    """Read-only: compute and return swarm candidate clusters from
    currently ACTIVE detections. This endpoint performs no writes -- it
    does not persist swarm_id onto detections and does not write an audit
    log entry (a GET must be safe/side-effect-free per HTTP semantics). To
    recompute AND persist swarm_id back onto member detections (e.g. so
    Dashboard.jsx's "Swarms Detected" stat tile / KillChain.jsx's
    per-detection badge stay current), call POST /swarm/clusters/recompute
    instead. Detections not part of any >=2-member concurrent cluster are
    never fabricated a swarm membership."""
    clusters = await _compute_swarm_clusters()
    return {"clusters": clusters}


@api.post("/swarm/clusters/recompute")
async def recompute_swarm_clusters(user: Dict = Depends(get_current_user)):
    """Compute swarm candidate clusters from currently ACTIVE detections and
    write the resulting swarm_id back onto each member detection so
    Dashboard.jsx's "Swarms Detected" stat tile and KillChain.jsx's
    per-detection badge reflect the latest clustering. Detections not part
    of any >=2-member concurrent cluster keep swarm_id=None -- this
    endpoint never fabricates a swarm membership for a single, unclustered
    contact. This is the write/side-effecting counterpart to the read-only
    GET /swarm/clusters; it is a POST specifically because it mutates state
    and writes an audit log entry."""
    clusters = await _compute_swarm_clusters()

    clustered_ids = {mid for c in clusters for mid in c["member_ids"]}
    for cluster in clusters:
        await db.detections.update_many(
            {"id": {"$in": cluster["member_ids"]}},
            {"$set": {"swarm_id": cluster["swarm_id"]}},
        )
    # Any previously-clustered detection that is no longer part of a
    # cluster this cycle (e.g. cluster broke up) gets its swarm_id cleared
    # rather than left stale.
    await db.detections.update_many(
        {"status": "ACTIVE", "swarm_id": {"$ne": None}, "id": {"$nin": list(clustered_ids)}},
        {"$set": {"swarm_id": None}},
    )

    if clusters:
        await log_event(
            "SWARM_CLASSIFY",
            f"{len(clusters)} swarm candidate cluster(s) identified across "
            f"{len(clustered_ids)} concurrent detection(s)",
            meta={"clusters": [c["swarm_id"] for c in clusters]},
            actor=user["email"],
        )
    return {"clusters": clusters}


def _interval_stats(timestamps_iso: List[str]) -> Optional[Dict[str, Any]]:
    """Given >=2 ISO timestamps (already sorted ascending), compute real
    inter-event interval statistics. Returns None if there aren't enough
    events to say anything (need at least 2 events -> 1 interval; stddev
    needs >=2 intervals -> 3 events). Never fabricates a value: stddev/CV are
    omitted (None) rather than reported as 0 when sample size is too small
    to mean anything."""
    if len(timestamps_iso) < 2:
        return None
    times = [datetime.fromisoformat(t) for t in timestamps_iso]
    deltas_s = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    mean_s = statistics.fmean(deltas_s)
    result: Dict[str, Any] = {
        "sample_count": len(deltas_s),  # number of intervals, i.e. N events - 1
        "mean_interval_s": round(mean_s, 2),
        "min_interval_s": round(min(deltas_s), 2),
        "max_interval_s": round(max(deltas_s), 2),
        "stddev_interval_s": None,
        "coefficient_of_variation": None,
    }
    if len(deltas_s) >= 2:
        stdev_s = statistics.pstdev(deltas_s)
        result["stddev_interval_s"] = round(stdev_s, 2)
        # Coefficient of variation (stddev/mean) is a normalized regularity
        # measure: low CV => intervals cluster tightly around the mean
        # (regular cadence, e.g. periodic beacon/patrol); high CV => intervals
        # are scattered (irregular/bursty). This is a descriptive statistic
        # on real observed gaps, not a classification or confidence score.
        if mean_s > 0:
            result["coefficient_of_variation"] = round(stdev_s / mean_s, 3)
    return result


@api.get("/detections/{det_id}/cadence")
async def get_detection_cadence(det_id: str, user: Dict = Depends(get_current_user)):
    """Traffic-behavior/cadence analysis (backlog B3) built entirely from
    real timestamps already captured by the ingest/merge pipeline -- no new
    hardware, no fabricated scores.

    Two distinct, honestly-scoped analyses are returned:

    1. `session` -- re-confirmation cadence WITHIN this single detection id,
       derived from `reconfirm_events` (see detection_ingest's $push). Each
       event is a real moment an ingest matched this id inside
       DETECTION_MERGE_WINDOW_S. NOTE: this is NOT a continuous on/off duty
       cycle -- the backend only ever sees "present" events (a re-ingest),
       never an explicit "absent" sample, so we report interval regularity
       between confirmations, not a presence percentage.

    2. `cross_session` -- reappearance-interval regularity across DIFFERENT
       detection ids that share the same (source, model, protocol), ordered
       by first_seen. Today, detection_ingest's merge query only matches
       status=ACTIVE records, so once a contact goes LOST and reappears it
       is created as a NEW id (see DETECTION_MERGE_WINDOW_S/merge query
       above) -- this endpoint does NOT change that ingest/merge behavior,
       it only performs a read-time correlation across the resulting
       sessions to see whether the gaps between them are regular (e.g. a
       patrol/periodic beacon) or effectively random. This is a real
       statistic on real first_seen/last_seen timestamps of distinct
       records, not a claim that the underlying contact identity has been
       cryptographically re-linked.
    """
    doc = await db.detections.find_one({"id": det_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Detection not found")

    events = sorted(doc.get("reconfirm_events") or [doc.get("first_seen"), doc.get("last_seen")])
    session_stats = _interval_stats(events)
    first_seen = doc.get("first_seen")
    last_seen = doc.get("last_seen")
    observation_span_s = None
    if first_seen and last_seen:
        observation_span_s = round(
            (datetime.fromisoformat(last_seen) - datetime.fromisoformat(first_seen)).total_seconds(), 2
        )

    session = {
        "reconfirm_count": len(doc.get("reconfirm_events") or []),
        "observation_span_s": observation_span_s,
        "interval_stats": session_stats,
        "note": (
            "Based on real re-confirmation timestamps for this detection id. "
            "Reflects how regularly this contact was RE-CONFIRMED while ACTIVE, "
            "not a measured on/off duty cycle (no explicit absence samples exist)."
        ),
    }

    sibling_docs = await db.detections.find(
        {"source": doc.get("source"), "model": doc.get("model"), "protocol": doc.get("protocol")},
        {"_id": 0, "id": 1, "first_seen": 1, "last_seen": 1},
    ).sort("first_seen", 1).to_list(200)

    # Build non-overlapping sessions ordered by first_seen, then compute the
    # gap between one session's last_seen and the next session's first_seen.
    sibling_docs.sort(key=lambda d: d.get("first_seen") or "")
    gaps_s: List[float] = []
    for prev, nxt in zip(sibling_docs, sibling_docs[1:]):
        try:
            prev_end = datetime.fromisoformat(prev["last_seen"])
            next_start = datetime.fromisoformat(nxt["first_seen"])
        except (KeyError, TypeError, ValueError):
            continue
        gap_s = (next_start - prev_end).total_seconds()
        if gap_s >= 0:  # skip overlapping/concurrent sessions (not a real gap)
            gaps_s.append(round(gap_s, 2))

    cross_session: Dict[str, Any] = {
        "session_count": len(sibling_docs),
        "gap_count": len(gaps_s),
        "gaps_s": gaps_s,
        "gap_stats": None,
        "note": (
            "Correlates DISTINCT detection ids sharing the same source/model/protocol, "
            "ordered by first_seen, to see if the silence gaps between them are regular. "
            "This does NOT change ingest/merge identity logic -- it is a read-time "
            "aggregation only."
        ),
    }
    if len(gaps_s) >= 1:
        mean_gap = statistics.fmean(gaps_s)
        gap_stats: Dict[str, Any] = {
            "sample_count": len(gaps_s),
            "mean_gap_s": round(mean_gap, 2),
            "min_gap_s": round(min(gaps_s), 2),
            "max_gap_s": round(max(gaps_s), 2),
            "stddev_gap_s": None,
            "coefficient_of_variation": None,
        }
        if len(gaps_s) >= 2:
            stdev_gap = statistics.pstdev(gaps_s)
            gap_stats["stddev_gap_s"] = round(stdev_gap, 2)
            if mean_gap > 0:
                gap_stats["coefficient_of_variation"] = round(stdev_gap / mean_gap, 3)
        cross_session["gap_stats"] = gap_stats

    return {
        "detection_id": det_id,
        "source": doc.get("source"),
        "model": doc.get("model"),
        "protocol": doc.get("protocol"),
        "session": session,
        "cross_session": cross_session,
    }


@api.post("/detections/{det_id}/cema-advance")
async def cema_advance(det_id: str, user: Dict = Depends(get_current_user)):
    doc = await db.detections.find_one({"id": det_id})
    if not doc:
        raise HTTPException(404, "Detection not found")
    doc = advance_cema(doc)
    await db.detections.replace_one({"id": det_id}, doc)
    await log_event("CEMA",
                    f"{doc['callsign']} advanced to {doc['cema_stage']}",
                    meta={"detection_id": det_id, "stage": doc["cema_stage"]},
                    actor=user["email"])
    doc.pop("_id", None)
    return doc


@api.post("/detections/{det_id}/killchain-advance")
async def kc_advance(det_id: str, user: Dict = Depends(get_current_user)):
    doc = await db.detections.find_one({"id": det_id})
    if not doc:
        raise HTTPException(404, "Detection not found")
    doc = advance_kill_chain(doc)
    await db.detections.replace_one({"id": det_id}, doc)
    await log_event("KILLCHAIN",
                    f"{doc['callsign']} → {doc['kill_chain_stage']}",
                    meta={"detection_id": det_id, "stage": doc["kill_chain_stage"]},
                    actor=user["email"])
    doc.pop("_id", None)
    return doc


@api.post("/detections/{det_id}/authorize-target")
async def authorize_target(det_id: str, body: AuthorizeTargetBody,
                           user: Dict = Depends(get_current_user)):
    """Friendly-fire interlock: explicitly mark a detection as an authorized
    kinetic-payload target (or revoke that authorization). Any authenticated
    operator may authorize a single, individually-identified target — this is
    the routine "yes, engage this contact" action. Broadcast/target_system=0
    actions are NOT covered by this and separately require commander role +
    an arm token (see /payloads/deploy, /mavlink/broadcast)."""
    doc = await db.detections.find_one({"id": det_id})
    if not doc:
        raise HTTPException(404, "Detection not found")
    await db.detections.update_one({"id": det_id}, {"$set": {"authorized_target": body.authorized}})
    await log_event(
        "TARGETING",
        f"{doc['callsign']} {'AUTHORIZED' if body.authorized else 'DE-AUTHORIZED'} as kinetic target",
        meta={"detection_id": det_id, "authorized": body.authorized},
        actor=user["email"],
    )
    return {"ok": True, "detection_id": det_id, "authorized_target": body.authorized}


class AttachIqCaptureBody(BaseModel):
    basename: str


# Task #117 (scoped by Software Architect task #108): "Export IQ for RE
# analysis". field-bridge/iq_capture.py is a standalone, manually-invoked
# tool (its own docstring is explicit: "This script intentionally does NOT
# wire into the live detection pipeline (no backend/server.py changes, no
# new ingest endpoint)"). So there is NO existing automatic association
# between a detection record and an IQ capture file -- that had to be
# designed here, not assumed.
#
# Minimal design chosen: a detection can have an `iq_capture_basename`
# field (just a bare filename stem, no path). An operator who has manually
# run iq_capture.py for a contact attaches the resulting capture via
# POST .../iq-capture with that basename; the underlying pair of files
# (`<basename>.sigmf-data` + `<basename>.sigmf-meta`) is expected to live in
# IQ_CAPTURE_DIR. This is intentionally NOT a bigger new data model (no
# capture registry/collection) -- one honest optional field is all the
# current manual workflow justifies. Basename is restricted to a safe
# charset (no `/`, `..`, etc.) both to prevent path traversal and because
# SigMF basenames are simple by convention.
IQ_CAPTURE_DIR = Path(
    os.environ.get("IQ_CAPTURE_DIR", str(ROOT_DIR.parent / "field-bridge" / "iq_captures"))
)

import re as _re
_SAFE_BASENAME_RE = _re.compile(r"^[A-Za-z0-9_.-]+$")


@api.post("/detections/{det_id}/iq-capture")
async def attach_iq_capture(det_id: str, body: AttachIqCaptureBody,
                             user: Dict = Depends(get_current_user)):
    """Attach a manually-captured SigMF IQ pair (see iq_capture.py) to a
    detection record, so it can later be fetched via GET .../iq-export.
    Does NOT capture anything itself, does NOT touch hardware -- purely
    records that `<basename>.sigmf-data`/`.sigmf-meta` in IQ_CAPTURE_DIR
    belong to this detection."""
    doc = await db.detections.find_one({"id": det_id})
    if not doc:
        raise HTTPException(404, "Detection not found")
    basename = body.basename.strip()
    if not basename or not _SAFE_BASENAME_RE.match(basename):
        raise HTTPException(400, "basename must be a bare filename stem (letters/digits/._- only, no path separators)")
    data_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-data"
    meta_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-meta"
    if not data_path.is_file() or not meta_path.is_file():
        raise HTTPException(
            404,
            f"No .sigmf-data/.sigmf-meta pair named '{basename}' found in {IQ_CAPTURE_DIR}. "
            "Run field-bridge/iq_capture.py first and place its output there before attaching.",
        )
    await db.detections.update_one({"id": det_id}, {"$set": {"iq_capture_basename": basename}})
    await log_event("DETECTION", f"IQ capture '{basename}' attached to {doc.get('callsign', det_id)}",
                     meta={"detection_id": det_id, "basename": basename}, actor=user["email"])
    return {"ok": True, "detection_id": det_id, "iq_capture_basename": basename}


@api.get("/detections/{det_id}/iq-export")
async def export_iq_capture(det_id: str, user: Dict = Depends(get_current_user)):
    """Serve the SigMF IQ capture pair (.sigmf-data + .sigmf-meta) attached
    to a detection, bundled as a zip, for an analyst to import into URH
    (Universal Radio Hacker -- external tool, not vendored here) for manual
    RE work. Primarily intended for confidence_type == "unclassified_signal"
    detections (a genuinely unknown emitter is exactly the case that
    benefits from manual signal inspection), but not hard-restricted to
    that case: if a capture is attached to any other detection, the
    underlying capability (get me the real IQ bytes) is still legitimately
    useful and there is no honesty reason to block it. NO demodulation,
    bit-slicing, or protocol inference happens here or anywhere else in
    this codebase's export path -- that stays a manual analyst workflow in
    URH itself."""
    doc = await db.detections.find_one({"id": det_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Detection not found")
    basename = doc.get("iq_capture_basename")
    if not basename:
        raise HTTPException(
            404,
            "No IQ capture is associated with this detection. iq_capture.py must be run "
            "manually against this contact and attached via POST .../iq-capture first -- "
            "there is no automatic capture-to-detection linkage in this system.",
        )
    data_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-data"
    meta_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-meta"
    if not data_path.is_file() or not meta_path.is_file():
        raise HTTPException(
            404,
            f"Detection references IQ capture '{basename}' but its files are no longer "
            f"present in {IQ_CAPTURE_DIR} (moved or deleted after attaching).",
        )

    import io
    import zipfile
    from fastapi.responses import StreamingResponse

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(data_path, arcname=f"{basename}.sigmf-data")
        zf.write(meta_path, arcname=f"{basename}.sigmf-meta")
    buf.seek(0)

    await log_event("DETECTION", f"IQ export downloaded for {doc.get('callsign', det_id)}",
                     meta={"detection_id": det_id, "basename": basename,
                           "confidence_type": doc.get("confidence_type")},
                     actor=user["email"])

    zip_name = f"{basename}_iq_export.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@api.delete("/detections/{det_id}")
async def delete_detection(det_id: str, user: Dict = Depends(get_current_user)):
    res = await db.detections.delete_one({"id": det_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Detection not found")
    await log_event("DETECTION", f"Contact removed {det_id}", actor=user["email"])
    return {"ok": True}


# =====================================================================
# Routes: RF Spectrum
# =====================================================================
@api.get("/spectrum/waterfall")
async def spectrum_waterfall(bins: int = 96, rows: int = 24,
                             user: Dict = Depends(get_current_user)):
    # Serve real waterfall from the RF bridge if it has published data
    # within the last 30 seconds (matches /health's hackrf_live window —
    # real hackrf_sweep cadence is slower than a 10s cutoff allowed for).
    # No synthetic fallback: if there is no live ingest, we honestly report
    # an empty spectrum rather than fabricating rows.
    ing = _last_spectrum_ingest
    if ing and (datetime.now(timezone.utc) - ing["ts"]).total_seconds() < 30:
        return {"bins": ing["bins"], "rows": ing["rows"], "source": "HACKRF"}
    return {"bins": bins, "rows": [], "source": "NONE"}


# =====================================================================
# Routes: RF bridge ingest (HackRF + SiK radio → app)
# =====================================================================
_last_spectrum_ingest: Optional[Dict] = None


class SpectrumIngestBody(BaseModel):
    bins: int
    rows: List[List[float]]
    center_freq_ghz: Optional[float] = None
    span_mhz: Optional[float] = None


class IFFBeaconIngestBody(BaseModel):
    """Body posted by field-bridge/iff_beacon_bridge.py after it has already
    cryptographically verified a LoRa IFF beacon (HMAC-SHA256 over asset_id/
    mission_id/timestamp_slot/geocell/counter -- see field-bridge/
    iff_crypto.py for the full construction). This endpoint trusts that
    verification already happened bridge-side; it does NOT re-verify any
    HMAC itself (the mission master secret never leaves the bridge/registry
    file, by design -- see iff_beacon_bridge.py's AssetRegistry docstring)."""
    asset_id: int
    callsign: str
    mission_id: int
    geocell: int
    geocell_known: bool = False
    bearing_deg: Optional[float] = None
    distance_m: Optional[float] = None


class DetectionIngestBody(BaseModel):
    callsign: Optional[str] = None
    model: str = "Unknown UAV"
    protocol: str = "Unknown"
    threat_level: str = "MEDIUM"
    center_freq_ghz: float
    bandwidth_mhz: float = 20.0
    rssi_dbm: float = -80.0
    snr_db: float = 10.0
    bearing_deg: float = 0.0
    distance_m: float = 0.0
    # True when distance_m is a model-based RSSI path-loss estimate, not a real
    # range measurement (radar/TDOA/etc). Defaults False so existing sources
    # (simulated data, any future real-ranging source) are not retroactively
    # mislabeled as estimates.
    distance_estimated: bool = False
    altitude_m: float = 0.0
    speed_ms: float = 0.0
    system_id: int = 1
    component_id: int = 1
    encrypted: bool = False
    source: str = "HACKRF"
    # True only when this detection came from a genuinely decoded, real
    # protocol-level message (e.g. a real MAVLink HEARTBEAT whose `autopilot`
    # field was actually parsed off the wire — see
    # field-bridge/mavlink_sniffer.py). False (the default) means this is an
    # RF-energy heuristic / guess (RSSI thresholding, persistence filtering,
    # etc.), same as every existing ingest source before this field existed.
    # Defaults False for backward compatibility, following the same pattern
    # used for distance_estimated above.
    protocol_confirmed: bool = False
    # --- ML classify-and-ingest bridge fields (field-bridge/ml_classify_bridge.py) ---
    # This is an ADDITIONAL, SUPPLEMENTARY signal layered on top of the
    # RSSI-heuristic detection above -- it does NOT replace protocol_confirmed
    # or any existing field, and it is populated by a separate, independent
    # bridge process, not by hackrf_rx.py itself.
    #
    # ml_label / ml_confidence come from real inference (a pretrained
    # GamutRF-style ResNet18 checkpoint) run against a real captured IQ
    # window. KNOWN LIMITATION: the only checkpoint currently deployed
    # (resnet18_leesburg_split_0.02_1_current.pt) is a CLOSED-WORLD 3-class
    # model -- {drone, wifi_2_4, wifi_5} -- with NO idle/noise/background/
    # "none of the above" class, so it always emits a confident label even
    # when the true signal doesn't match any of its 3 classes. Empirical
    # testing on this deployment (real IQ capture at 3.6GHz, a genuinely
    # quiet band) showed >99% confident "drone" predictions on pure
    # noise-floor energy. Weight ml_label/ml_confidence accordingly,
    # especially at lower confidence values -- this is informational,
    # not a substitute for protocol_confirmed or the RSSI heuristic.
    #
    # ml_gated indicates whether ml_classify_bridge.py's energy gate passed
    # before it ran inference (peak power above that band's established
    # BAND_NOISE_FLOOR_DBM + DETECT_THRESHOLD_DB) -- this is the concrete
    # mitigation for the noise-hallucination finding above: the classifier
    # is only ever invoked on real above-floor energy, never on silence.
    # Defaults None/False so existing sources (hackrf_rx.py, simulated data)
    # are not retroactively mislabeled.
    ml_label: Optional[str] = None
    ml_confidence: Optional[float] = None
    ml_gated: bool = False
    # --- Unified confidence-type classifier (see backend/CONFIDENCE_MODEL.md) ---
    # Describes the EPISTEMIC CATEGORY of this detection's confidence, not a
    # blended numeric score -- a CRC-verified decode, a softmax probability,
    # a persistence heuristic, and a presence-only advisory are not
    # comparable on one 0-1 scale, and this field deliberately does not try
    # to force them onto one. One of: "heuristic_binary", "ml_probability",
    # "protocol_verified", "advisory_only", "unclassified_signal" (real
    # energy-gated RF whose ML top-class confidence was too weak to trust
    # any of the classifier's 3 known classes -- see
    # field-bridge/ml_classify_bridge.py UNCLASSIFIED_MAX_CONFIDENCE).
    # Optional/None for any source not
    # yet updated to set it -- absence means "render as before" (backward
    # compatible), same pattern as distance_estimated/protocol_confirmed above.
    confidence_type: Optional[str] = None


# =====================================================================
# Routes: analog FPV video bridge ingest (field-bridge/fpv_video_bridge.py)
# =====================================================================
# HONESTY NOTE (read before trusting anything served here): this ingests
# whatever fpv_video_bridge.py produced from a REAL HackRF IQ capture, but
# that bridge's own AM-envelope-demod + naive scanline reconstruction is
# UNTESTED against a live analog FPV transmitter (no live analog FPV
# transmitter was available this session) -- see that script's module
# docstring for the full, load-bearing disclosure. This endpoint does not
# strengthen or weaken that claim; it just stores/serves whatever the
# bridge reports, including its own `validated_against_live_signal: False`
# flag, unmodified, so the frontend can render the same caveat.
#
# DJI digital (OcuSync) video content is explicitly NOT decoded anywhere in
# this pipeline -- only RF energy presence in-band could ever be implied,
# never actual DJI video content. See fpv_video_bridge.py's docstring.
_last_fpv_frame: Optional[Dict] = None
_last_fpv_frame_png: Optional[bytes] = None

# ---------------------------------------------------------------------------
# GUI-only capture trigger. Standing rule for tomorrow's demo: the operator
# must never need SSH/manual CLI access to trigger an FPV capture. This is a
# simple in-memory request record -- same pattern as _arm_tokens/
# _range_authorization above -- that field-bridge/fpv_video_bridge.py's new
# --poll mode consumes. RX-only/non-destructive (it only asks the bridge to
# do one more real HackRF RX capture+demod+ingest cycle, identical to what an
# operator would otherwise SSH in and run by hand), so it uses the same
# get_current_user dependency as /fpv/ingest and /fpv/latest-frame -- no
# stricter RBAC gate is warranted than what already guards those routes.
_fpv_capture_request: Optional[Dict] = None


class FpvCaptureRequestBody(BaseModel):
    channel: Optional[str] = None


@api.post("/fpv/capture-request")
async def fpv_capture_request(body: FpvCaptureRequestBody,
                               user: Dict = Depends(get_current_user)):
    """Record an operator request for one more FPV capture cycle. The
    field bridge (in --poll mode) picks this up via GET
    /fpv/capture-request/status, performs ONE real capture+demod+ingest
    cycle, then the request is cleared. This is the entire mechanism that
    removes the need for an operator to SSH into the bridge host to
    trigger a capture -- it is a request/consume queue of depth 1, not a
    job scheduler."""
    global _fpv_capture_request
    _fpv_capture_request = {
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": user["email"],
        "channel": body.channel,
        "consumed": False,
    }
    await log_event(
        "FPV_CAPTURE_REQUESTED",
        f"FPV capture requested via GUI, channel={body.channel or 'default'}",
        actor=user["email"],
    )
    return {"ok": True, "queued": True, **_fpv_capture_request}


@api.get("/fpv/capture-request/status")
async def fpv_capture_request_status(user: Dict = Depends(get_current_user)):
    """Polled by fpv_video_bridge.py --poll. Returns the pending request
    (if any) and marks it consumed so it is only ever acted on once. No
    synthetic/fabricated request is ever returned -- pending=False means
    honestly nothing is queued."""
    global _fpv_capture_request
    if _fpv_capture_request is None or _fpv_capture_request.get("consumed"):
        return {"pending": False}
    req = _fpv_capture_request
    _fpv_capture_request = {**req, "consumed": True}
    return {"pending": True, "channel": req.get("channel"),
            "requested_at": req.get("requested_at")}


@api.post("/fpv/ingest")
async def fpv_ingest(
    metadata: str = Form(...),
    frame: Optional[UploadFile] = File(None),
    user: Dict = Depends(get_current_user),
):
    """Receive one capture-and-demod result from fpv_video_bridge.py.

    `metadata` is the JSON dict fpv_video_bridge.py's capture_and_demod()
    returns (verbatim, including its honesty flags). `frame` is the
    optional reconstructed PNG. Stores only the latest frame in memory
    (this is a live-operator console, not a video archive) -- same
    "no synthetic fallback, report exactly what was received" pattern as
    /spectrum/ingest above.
    """
    global _last_fpv_frame, _last_fpv_frame_png
    try:
        meta = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="metadata must be valid JSON")

    png_bytes = await frame.read() if frame is not None else None

    _last_fpv_frame = {
        **meta,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "has_frame": png_bytes is not None,
    }
    _last_fpv_frame_png = png_bytes

    await log_event(
        "FPV_INGEST",
        f"FPV video bridge ingest: channel={meta.get('channel')} "
        f"freq={meta.get('center_freq_hz', 0)/1e6:.3f}MHz "
        f"validated={meta.get('validated_against_live_signal')} "
        f"has_frame={png_bytes is not None}",
        actor=user["email"],
    )
    return {"ok": True, "stored": True, "has_frame": png_bytes is not None}


@api.get("/fpv/latest-frame")
async def fpv_latest_frame_meta(user: Dict = Depends(get_current_user)):
    """Metadata for the most recent FPV capture, including this pipeline's
    own honesty flags (validated_against_live_signal, dji_digital_video_decoded,
    demod_method) -- no synthetic fallback: if nothing has been ingested yet,
    this honestly reports that instead of fabricating a frame."""
    if _last_fpv_frame is None:
        return {"available": False, "source": "NONE"}
    return {"available": True, "source": "HACKRF_REAL_IQ", **_last_fpv_frame}


@api.get("/fpv/latest-frame.png")
async def fpv_latest_frame_png(user: Dict = Depends(get_current_user)):
    """The most recent reconstructed PNG itself, if one was produced."""
    from fastapi.responses import Response as _Response
    if _last_fpv_frame_png is None:
        raise HTTPException(status_code=404, detail="no FPV frame captured yet")
    return _Response(content=_last_fpv_frame_png, media_type="image/png")


@api.post("/spectrum/ingest")
async def spectrum_ingest(body: SpectrumIngestBody,
                          user: Dict = Depends(get_current_user)):
    global _last_spectrum_ingest
    _last_spectrum_ingest = {
        "ts": datetime.now(timezone.utc),
        "bins": body.bins,
        "rows": body.rows,
        "center_freq_ghz": body.center_freq_ghz,
        "span_mhz": body.span_mhz,
    }
    return {"ok": True, "accepted_rows": len(body.rows)}


DETECTION_MERGE_WINDOW_S = 20  # re-ingests of the same real contact within this
                               # window update the existing record instead of
                               # spawning a new one — a continuously-running RX
                               # bridge otherwise floods the log with dozens of
                               # near-duplicate "new" detections per minute.

RECONFIRM_EVENTS_CAP = 50  # bound on detections.reconfirm_events (B3 cadence
                               # analysis, see /detections/{id}/cadence). This
                               # is a rolling window of the most recent N
                               # re-confirmation timestamps for a single
                               # detection id, not the full history -- kept
                               # bounded so a long-lived contact's document
                               # doesn't grow without limit under a fast
                               # ingest cadence.

DETECTION_STALE_TIMEOUT_S = 600  # (10 min) NOT the same thing as the merge
                               # window above. This is how long a detection
                               # may go without a re-confirmation before it
                               # stops counting as ACTIVE/tracked on the
                               # operator dashboard. It must be meaningfully
                               # longer than DETECTION_MERGE_WINDOW_S: real RF
                               # contacts naturally have gaps between
                               # confirmation cycles (especially given the
                               # USB/sweep timing issues seen on this site),
                               # and 20s would cause live contacts to flicker
                               # in and out of ACTIVE. Detections that go
                               # stale are marked LOST (not deleted) so they
                               # remain in the Mission Log / audit history.


ML_RECLASSIFY_MIN_CONFIDENCE = 0.60
# When the ML classify bridge's REAL inference explicitly says "wifi_2_4" or
# "wifi_5" -- i.e. NOT "drone" -- at or above this confidence, we correct the
# DISPLAYED model/protocol/threat_level away from the RSSI-heuristic's stale
# drone-candidate guess. Root cause this fixes: field-bridge/
# ml_classify_bridge.py always posts a drone-shaped model/protocol
# ("DJI Mini (candidate)" / "MAVLink craft (candidate)") regardless of what
# ml_label actually comes back as -- those values exist purely so the
# merge-match query below finds the right existing record, not because the
# bridge believes the contact is a drone. Left uncorrected, a detection could
# display e.g. model="DJI Mini (candidate)" right next to an ML badge saying
# "wifi_2_4" -- a visible, confusing contradiction an operator can see on the
# Dashboard.
#
# This correction is intentionally ONE-DIRECTIONAL: only drone-guess -> wifi/
# non-threat, never the reverse. ml_classify_bridge.py's own module docstring
# documents a reproduced finding that this closed-world 3-class model (no
# idle/noise/reject class) hallucinates "drone" at >99% confidence on pure
# noise-floor energy -- so ml_label=="drone" must NOT be trusted to upgrade
# or override a non-drone RSSI-heuristic guess. Choosing "wifi" over "drone"
# is a different, more trustworthy signal (the flaw is false-positive drone
# detection, not failure to recognize real wifi), so only that direction is
# corrected here.
ML_WIFI_RECLASSIFY_DISPLAY = {
    "wifi_2_4": ("Wi-Fi 2.4GHz (ML reclassified)", "Wi-Fi 802.11"),
    "wifi_5": ("Wi-Fi 5GHz (ML reclassified)", "Wi-Fi 802.11"),
}


def _ml_wifi_reclassification(ml_label: Optional[str], ml_confidence: Optional[float]):
    """Returns (display_model, display_protocol) if this ingest's ML result
    should reclassify the detection's displayed identity away from the
    RSSI-heuristic's drone guess, else None. See ML_RECLASSIFY_MIN_CONFIDENCE
    docstring above for why this is one-directional (wifi only, never drone)."""
    if ml_label not in ML_WIFI_RECLASSIFY_DISPLAY:
        return None
    if ml_confidence is None or ml_confidence < ML_RECLASSIFY_MIN_CONFIDENCE:
        return None
    return ML_WIFI_RECLASSIFY_DISPLAY[ml_label]


# UNCLASSIFIED-SIGNAL DISPLAY OVERRIDE (2026-07-23): mirrors
# ML_WIFI_RECLASSIFY_DISPLAY above. field-bridge/ml_classify_bridge.py
# deliberately sends the plain heuristic-consistent model/protocol/
# threat_level ("DJI Mini (candidate)"/"OcuSync/Wi-Fi"/MEDIUM, etc.) over
# the wire for confidence_type=="unclassified_signal" ingests too -- NOT
# "Unclassified emitter (candidate)"/"Unknown"/LOW -- because those fields
# are also the merge-match key backend/server.py's detection_ingest uses to
# find the existing ACTIVE record created by hackrf_rx.py. If the wire
# values changed for the unclassified case, they would stop matching that
# existing record and this ingest would spawn a second, duplicate ACTIVE
# detection for the same physical contact instead of merging into it (see
# ml_classify_bridge.py's det-dict comment for the full incident this
# fixes). So the honest "unclassified" display the operator actually sees
# is computed HERE, server-side, off confidence_type -- exactly the same
# division of responsibility as the wifi-reclassification path above.
UNCLASSIFIED_DISPLAY = ("Unclassified emitter (candidate)", "Unknown")


def _ml_unclassified_display(confidence_type: Optional[str]):
    """Returns (display_model, display_protocol) if this ingest's
    confidence_type says the ML classifier could not confidently place the
    signal in any of its 3 known classes, else None."""
    if confidence_type != "unclassified_signal":
        return None
    return UNCLASSIFIED_DISPLAY


# HEURISTIC-BINARY GENERIC-DISPLAY OVERRIDE (2026-07-24, B5): mirrors
# _ml_wifi_reclassification() / _ml_unclassified_display() above. Operator
# complaint: the dashboard's PRIMARY label showed a specific manufacturer
# name ("DJI Mini (candidate)") for detections that are, and may remain for
# their entire lifetime, backed by nothing but a bare RSSI/persistence
# heuristic in hackrf_rx.py (no ML opinion, no protocol decode ever
# arrived). A small muted "(unconfirmed)" tag next to a big confident-
# looking manufacturer name does not fix that confusion -- the primary
# label itself must not assert an identity that was never earned. See
# backend/DETECTION_DISPLAY_MODEL.md for the full design rationale.
#
# Same wire/display split as the two overrides above: hackrf_rx.py's
# heuristic ingest still POSTS "DJI Mini (candidate)" / "MAVLink craft
# (candidate)" on the wire (those exact strings are the merge-match key in
# detection_ingest's initial find_one() query -- changing them breaks
# re-confirmation merging). The honest generic category name is substituted
# into the DISPLAYED model/protocol fields here, server-side, with the raw
# heuristic guess preserved in original_model/original_protocol (the same
# field the frontend already renders as a muted secondary line).
HEURISTIC_GENERIC_DISPLAY = {
    "DJI Mini (candidate)": ("Unidentified 2.4GHz Emitter", "Unconfirmed (RF heuristic)"),
    "MAVLink craft (candidate)": ("Unidentified RF Emitter — SiK/MAVLink band", "Unconfirmed (RF heuristic)"),
}


def _heuristic_display(model: Optional[str], confidence_type: Optional[str]):
    """Returns (display_model, display_protocol) when this detection has
    ONLY a bare RSSI/persistence heuristic behind it -- no ML opinion, no
    protocol decode -- else None. Only fires for confidence_type ==
    "heuristic_binary"; never overrides a display that a real ML or
    protocol signal already earned (those branches take precedence and are
    checked first at each call site -- see detection_ingest)."""
    if confidence_type != "heuristic_binary":
        return None
    return HEURISTIC_GENERIC_DISPLAY.get(model)


# =====================================================================
# IFF (Identification Friend-or-Foe) LoRa beacon integration -- task #60.
#
# field-bridge/iff_beacon_bridge.py does the actual cryptographic
# verification (HMAC-SHA256, see field-bridge/iff_crypto.py) of a LoRa
# beacon from a friendly asset, entirely bridge-side -- the mission master
# secret never reaches this backend. This backend only ever sees ALREADY-
# VERIFIED beacons via POST /iff/beacons/ingest, stored in db.iff_friendlies
# keyed by asset_id (one current record per asset, latest-seen wins, same
# "upsert current state" pattern as e.g. /jam/status, not an unbounded log).
#
# Two consumers of that roster:
#   (a) detection_ingest() below: suppress/downgrade an RF-heuristic
#       detection to "FRIENDLY (IFF verified)" when a fresh, bearing-
#       consistent friendly beacon exists, so a friendly asset's own RF
#       emissions are not misclassified as a hostile contact.
#   (b) GET /iff/friendlies: the roster task #103's (GNSS-spoofing) and any
#       future control-link-injection authorization gate is meant to check
#       BEFORE authorizing an effect whose footprint could cover a fresh
#       friendly beacon. See check_no_friendly_in_footprint() below -- the
#       concrete function #103's authorization code should call.
# =====================================================================

# A friendly is considered "fresh" (i.e. actually still out there, not a
# stale roster entry from an asset that departed or lost power) for this
# many seconds after its last verified beacon. 3x iff_crypto.INTERVAL_S
# (30s beacon interval) gives a couple of missed beacons' worth of grace
# before an asset silently drops off the "currently in range" roster,
# without keeping a departed asset's last-known position around indefinitely.
IFF_FRESHNESS_S = 90

# Bearing tolerance for correlating an RF-heuristic detection's bearing_deg
# against a friendly beacon's last-reported bearing_deg (which, per
# iff_beacon_bridge.py's docstring, is None/absent for a plain omni LoRa
# receiver with no angle-of-arrival capability -- in which case this
# correlation simply cannot fire; see _check_iff_friendly_match below).
# 15 degrees is a deliberately generous window given hackrf_rx.py's own
# bearing estimate is itself a coarse single-antenna RSSI-based guess, not a
# precision DF fix -- see DIRECTION_FINDING_NOTES.md. A false SUPPRESS
# (treating a real hostile as friendly) is the dangerous failure direction
# here, so this tolerance should stay conservative (tight, not wide) until a
# real direction-finding capability replaces the single-antenna guess.
IFF_BEARING_TOLERANCE_DEG = 15.0


async def _iff_ingest_beacon(body: "IFFBeaconIngestBody") -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.iff_friendlies.update_one(
        {"asset_id": body.asset_id},
        {"$set": {
            "asset_id": body.asset_id,
            "callsign": body.callsign,
            "mission_id": body.mission_id,
            "geocell": body.geocell,
            "geocell_known": body.geocell_known,
            "bearing_deg": body.bearing_deg,
            "distance_m": body.distance_m,
            "last_seen": now,
        }},
        upsert=True,
    )


async def _revoked_asset_ids() -> set:
    """Asset IDs an operator has revoked via POST /iff/assets/{id}/revoke
    (see iff_revoke_asset() below). This is backend-side defense-in-depth,
    NOT a substitute for updating field-bridge/iff_beacon_bridge.py's own
    AssetRegistry.revoked_asset_ids -- the bridge is the one that decides
    whether to accept a beacon at all; this backend only ever sees beacons
    the bridge already verified and forwarded. Revoking here immediately
    stops a captured asset's LAST-INGESTED beacon record from continuing to
    count as "fresh friendly" for detection-suppression/attestation purposes
    even if the bridge process hasn't reloaded its own registry file yet
    (e.g. operator revokes from the console the instant capture is
    reported, before anyone can walk out to the bridge host)."""
    ids = set()
    async for doc in db.iff_revocations.find({}):
        ids.add(doc["asset_id"])
    return ids


async def _fresh_friendlies(freshness_s: int = IFF_FRESHNESS_S) -> List[Dict]:
    since = (datetime.now(timezone.utc) - timedelta(seconds=freshness_s)).isoformat()
    revoked = await _revoked_asset_ids()
    cursor = db.iff_friendlies.find({"last_seen": {"$gt": since}})
    out = []
    async for doc in cursor:
        doc.pop("_id", None)
        if doc["asset_id"] in revoked:
            continue
        out.append(doc)
    return out


async def _check_iff_friendly_match(bearing_deg: Optional[float]) -> Optional[Dict]:
    """Returns the matching friendly roster entry if a currently-fresh
    verified friendly beacon's bearing is within IFF_BEARING_TOLERANCE_DEG
    of `bearing_deg`, else None.

    Deliberately conservative: if the friendly's bearing_deg is None (the
    normal case for a plain omni LoRa receiver, per iff_beacon_bridge.py's
    docstring -- no angle-of-arrival hardware exists yet), no match is
    produced from that record. A friendly asset being IN RANGE (a verified
    beacon exists) is not, by itself, proof that THIS PARTICULAR RF
    detection at THIS bearing is that asset -- only a bearing-consistent
    match is treated as a suppression-worthy correlation. This is a real,
    stated limitation (see IFF_BEARING_TOLERANCE_DEG comment): until a real
    bearing/AoA capability exists on the IFF receive side, this check can
    only fire when hackrf_rx.py's own coarse bearing estimate happens to
    line up with a friendly's last self-reported position, which is expected
    to be the uncommon case at least until the asset side also reports a
    real GPS-derived bearing/position back through the beacon geocell."""
    if bearing_deg is None:
        return None
    for friendly in await _fresh_friendlies():
        f_bearing = friendly.get("bearing_deg")
        if f_bearing is None:
            continue
        diff = abs(((bearing_deg - f_bearing) + 180) % 360 - 180)
        if diff <= IFF_BEARING_TOLERANCE_DEG:
            return friendly
    return None


def check_no_friendly_in_footprint_sync_note() -> None:
    """Not a real function -- a pointer for readers. The async equivalent an
    authorization gate should call is `_check_iff_friendly_match(bearing_deg)`
    (returns the matching friendly dict or None) or `_fresh_friendlies()` (the
    full current roster) above. Task #103 (GNSS-spoofing attestation) and any
    future control-link-injection authorization path should import and call
    one of those two directly from this module rather than re-implementing
    freshness/bearing-matching logic independently -- see this section's
    module-level docstring for the full integration contract."""


@api.post("/iff/beacons/ingest")
async def iff_beacon_ingest(body: IFFBeaconIngestBody,
                            user: Dict = Depends(get_current_user)):
    """Ingest a single already-HMAC-verified IFF beacon from field-bridge/
    iff_beacon_bridge.py. See IFFBeaconIngestBody docstring -- this endpoint
    trusts the bridge's verification and does not re-check any HMAC."""
    await _iff_ingest_beacon(body)
    return {"ok": True, "asset_id": body.asset_id, "callsign": body.callsign}


@api.get("/iff/friendlies")
async def iff_friendlies(user: Dict = Depends(get_current_user)):
    """Current roster of friendly assets with a fresh (within
    IFF_FRESHNESS_S) IFF-verified LoRa beacon. This is the concrete
    "known friendly assets currently in range" list task #103's (GNSS-
    spoofing) authorization gate is meant to check before authorizing an
    effect -- see check_no_friendly_in_footprint_sync_note() above for the
    integration contract."""
    return {"friendlies": await _fresh_friendlies(), "freshness_s": IFF_FRESHNESS_S}


@api.post("/iff/assets/{asset_id}/revoke")
async def iff_revoke_asset(asset_id: int, user: Dict = Depends(require_commander)):
    """Revoke a single IFF asset without touching the mission-wide master
    secret (see field-bridge/iff_beacon_bridge.py AssetRegistry docstring
    for the full rationale/tradeoff). commander-role-gated: this is a
    security-sensitive trust decision about a specific physical asset, same
    gating this codebase already uses for other destructive/security-
    sensitive actions (require_commander, see e.g. deploy_jam,
    deploy_gnss_spoof). Idempotent: revoking an already-revoked asset_id is
    a no-op success, not an error.

    NOTE: this only updates this backend's own revocation record (used by
    _fresh_friendlies()/detection-suppression). It does NOT reach out and
    update the actual field-bridge/iff_beacon_bridge.py process's in-memory
    or on-disk AssetRegistry -- that bridge runs independently (possibly on
    different hardware with no path back to this API) and must have its own
    registry file's revoked_asset_ids updated out-of-band by the operator,
    same as the rest of that registry's provisioning. This endpoint's real
    effect today is immediate: it stops this backend from treating that
    asset's last-ingested beacon as "fresh friendly" for detection-
    suppression / GNSS-spoof-attestation purposes, without waiting on that
    out-of-band bridge-side update."""
    now = datetime.now(timezone.utc).isoformat()
    await db.iff_revocations.update_one(
        {"asset_id": asset_id},
        {"$set": {"asset_id": asset_id, "revoked_at": now, "revoked_by": user["email"]}},
        upsert=True,
    )
    await log_event("IFF_ASSET_REVOKED",
                     f"IFF asset {asset_id} revoked by {user['email']}",
                     meta={"asset_id": asset_id}, actor=user["email"])
    return {"ok": True, "asset_id": asset_id, "revoked": True}


@api.post("/iff/assets/{asset_id}/unrevoke")
async def iff_unrevoke_asset(asset_id: int, user: Dict = Depends(require_commander)):
    """Reverse a previous revoke (e.g. asset recovered intact, or revoked in
    error). Same commander gating as iff_revoke_asset(); same "does not
    reach the bridge's own registry" caveat applies."""
    await db.iff_revocations.delete_one({"asset_id": asset_id})
    await log_event("IFF_ASSET_UNREVOKED",
                     f"IFF asset {asset_id} unrevoked by {user['email']}",
                     meta={"asset_id": asset_id}, actor=user["email"])
    return {"ok": True, "asset_id": asset_id, "revoked": False}


@api.get("/iff/assets/revoked")
async def iff_list_revoked(user: Dict = Depends(get_current_user)):
    """List currently-revoked asset_ids known to this backend. Observe-only
    (get_current_user, not require_commander) -- matches this codebase's
    convention that read/observe endpoints stay at the base auth level while
    only the destructive/security-sensitive write actions require commander."""
    return {"revoked_asset_ids": sorted(await _revoked_asset_ids())}


@api.post("/detections/ingest")
async def detection_ingest(body: DetectionIngestBody,
                           user: Dict = Depends(get_current_user)):
    since = (datetime.now(timezone.utc) - timedelta(seconds=DETECTION_MERGE_WINDOW_S)).isoformat()
    # MERGE-MATCH ROOT-CAUSE FIX (2026-07-24): match on the immutable
    # match_model/match_protocol fields, NEVER on the currently-DISPLAYED
    # model/protocol fields. Field-bridge scripts (hackrf_rx.py,
    # ml_classify_bridge.py) always POST the same raw literal on every
    # re-confirmation cycle (e.g. "DJI Mini (candidate)"), but model/
    # protocol get overwritten to a display value by any of THREE
    # independent override paths below (_ml_wifi_reclassification,
    # _ml_unclassified_display, _heuristic_display). Matching the raw
    # incoming body.model/body.protocol against the STORED (possibly
    # already-overridden) model/protocol meant the very next re-
    # confirmation cycle after ANY override applied would fail to find
    # the existing record and silently spawn a duplicate ACTIVE
    # detection -- forever, every cycle, for that contact. match_model/
    # match_protocol are populated ONCE from the raw ingest at document
    # creation (see the creation branch below) and are never touched by
    # any override path, so they always equal exactly what the field-
    # bridge script sends on the wire, regardless of how many times the
    # displayed model/protocol have been overridden in between.
    #
    # The second $or branch is a backward-compat fallback for documents
    # created before this fix (no match_model field yet): it matches on
    # model/protocol as before, which still works for any such legacy
    # document that has never been display-overridden. Legacy documents
    # that WERE already overridden before this fix shipped may already be
    # duplicated in the live database -- that pre-existing data is a
    # separate cleanup, not something this query can retroactively repair.
    # Any legacy document found via the fallback branch gets match_model/
    # match_protocol backfilled below so it self-heals onto the primary
    # match path from this point forward.
    existing = await db.detections.find_one({
        "source": body.source,
        "status": "ACTIVE",
        "last_seen": {"$gt": since},
        "$or": [
            {"match_model": body.model, "match_protocol": body.protocol},
            {
                "match_model": {"$exists": False},
                "model": body.model,
                "protocol": body.protocol,
            },
        ],
    })

    if existing:
        wifi_display = _ml_wifi_reclassification(body.ml_label, body.ml_confidence)
        # DECISIVE-ML-ONLY confidence_type fix (see isUnconfirmedDetection()
        # false-negative audit finding): a sub-threshold ML read (ml_label is
        # e.g. "wifi_2_4" but ml_confidence < ML_RECLASSIFY_MIN_CONFIDENCE, so
        # wifi_display is None below) does NOT reclassify model/protocol/
        # threat_level -- it is explicitly inconclusive. Previously,
        # confidence_type was still overwritten to "ml_probability" any time
        # ml_label was present, even when inconclusive. Because confidence_type
        # is never reset back to "heuristic_binary" by later non-ML ingests
        # (see below), that permanently and silently disabled the frontend's
        # isUnconfirmedDetection()/UnconfirmedTag logic for this detection --
        # the record kept its original unreclassified heuristic model/threat
        # display but stopped being flagged as unconfirmed, even though
        # nothing had actually confirmed it. Only let an ML read change
        # confidence_type when it is decisive: it actually reclassified the
        # display (wifi_display truthy) or it confirms the drone guess
        # (ml_label == "drone").
        # unclassified_signal is also a decisive (i.e. deliberate, non-stale)
        # ML read -- ml_classify_bridge.py sends it precisely when the top
        # softmax class's confidence was too weak to trust ANY of the 3
        # known classes, regardless of which class happened to be on top.
        # Without this, a weak top-class of "wifi_2_4"/"wifi_5" (which
        # doesn't satisfy the wifi_display path above, and isn't "drone")
        # would silently fail to ever apply confidence_type=
        # "unclassified_signal" on a merge into an existing record.
        ml_is_decisive = (
            wifi_display is not None
            or body.ml_label == "drone"
            or body.confidence_type == "unclassified_signal"
        )
        updates = {
            "threat_level": body.threat_level,
            "rssi_dbm": body.rssi_dbm,
            "snr_db": body.snr_db,
            "bearing_deg": body.bearing_deg,
            "distance_m": body.distance_m,
            "distance_estimated": body.distance_estimated,
            "altitude_m": body.altitude_m,
            "speed_ms": body.speed_ms,
            "protocol_confirmed": body.protocol_confirmed,
            # ROOT CAUSE FIX (see false-positive ml_label=null audit finding):
            # hackrf_rx.py NEVER sends ml_label/ml_confidence/confidence_type
            # (they default to None/"heuristic_binary" on its DetectionIngestBody).
            # hackrf_rx.py re-confirms the SAME detection every ~3s (well inside
            # DETECTION_MERGE_WINDOW_S=20s), while ml_classify_bridge.py only
            # posts a real ml_label every ~12s+ per band (often slower under
            # HackRF device-lock contention). Unconditionally overwriting these
            # fields on every merge meant hackrf_rx.py's very next 3s
            # re-confirmation almost always clobbered ml_classify_bridge.py's
            # ml_label back to null within seconds of it being set -- this is
            # NOT the earlier-fixed "stale label" bug, it's active clobbering
            # on every non-ML ingest. Fix: only overwrite these ML-derived
            # fields when THIS ingest actually carries ML data (ml_label is not
            # None); otherwise preserve whatever the existing record already
            # has, so hackrf_rx.py's frequent re-confirmations no longer erase
            # ml_classify_bridge.py's slower, less frequent classifications.
            "ml_label": body.ml_label if body.ml_label is not None else existing.get("ml_label"),
            "ml_confidence": body.ml_confidence if body.ml_label is not None else existing.get("ml_confidence"),
            "ml_gated": body.ml_gated if body.ml_label is not None else existing.get("ml_gated", False),
            "confidence_type": body.confidence_type if (body.ml_label is not None and ml_is_decisive) else existing.get("confidence_type"),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            # Self-heal: backfill the immutable match key for legacy
            # documents (pre-dating this fix) found via the fallback $or
            # branch above. Once set, match_model/match_protocol are never
            # overwritten again by any later ingest -- see the three
            # override branches below, none of which touch these fields.
            "match_model": existing.get("match_model") or body.model,
            "match_protocol": existing.get("match_protocol") or body.protocol,
        }
        unclassified_display = _ml_unclassified_display(body.confidence_type) if ml_is_decisive else None
        if wifi_display:
            # Preserve the FIRST-ever RSSI-heuristic guess only -- don't
            # clobber it on a second/third consecutive reclassified update
            # for the same detection id.
            updates["original_model"] = existing.get("original_model") or existing.get("model")
            updates["original_protocol"] = existing.get("original_protocol") or existing.get("protocol")
            updates["model"], updates["protocol"] = wifi_display
            # A Wi-Fi AP is not a drone threat -- downgrade accordingly.
            updates["threat_level"] = "LOW"
            # The ML result is now the operative classification for this
            # record, not the stale RSSI heuristic guess.
            updates["confidence_type"] = "ml_probability"
        elif unclassified_display:
            # Same display-override pattern as wifi_display above: the wire
            # payload keeps model/protocol byte-identical to the RSSI
            # heuristic guess (see _ml_unclassified_display docstring) so the
            # merge-match above works; the honest "unclassified" identity is
            # substituted into the DISPLAYED fields only, here.
            updates["original_model"] = existing.get("original_model") or existing.get("model")
            updates["original_protocol"] = existing.get("original_protocol") or existing.get("protocol")
            updates["model"], updates["protocol"] = unclassified_display
            updates["threat_level"] = "LOW"
            updates["confidence_type"] = "unclassified_signal"
        else:
            # Neither a decisive ML reclassification nor an unclassified
            # read fired on this ingest. If the record is (still) purely
            # heuristic_binary -- i.e. nothing has ever confirmed it --
            # re-apply the generic-display override on every re-confirmation
            # re-post, using the record's OWN raw model as the lookup key
            # (existing.get("original_model") if already overridden on a
            # prior cycle, else existing.get("model") on the very first
            # re-confirmation after creation) so hackrf_rx.py's ~3s
            # heartbeat re-posts don't clobber the generic display back to
            # the raw manufacturer guess.
            resolved_ct = updates.get("confidence_type") or existing.get("confidence_type")
            raw_model = existing.get("original_model") or existing.get("model")
            heuristic_display = _heuristic_display(raw_model, resolved_ct)
            if heuristic_display:
                updates["original_model"] = existing.get("original_model") or existing.get("model")
                updates["original_protocol"] = existing.get("original_protocol") or existing.get("protocol")
                updates["model"], updates["protocol"] = heuristic_display

        # IFF suppression (task #60): a fresh, bearing-consistent verified
        # friendly beacon downgrades this detection's threat_level rather
        # than deleting/hiding the record -- see _check_iff_friendly_match
        # docstring for exactly what "bearing-consistent" requires and why
        # this deliberately does NOT fire just because some friendly is
        # somewhere in range.
        iff_friendly = await _check_iff_friendly_match(updates.get("bearing_deg"))
        if iff_friendly:
            updates["threat_level"] = "FRIENDLY (IFF verified)"
            updates["iff_verified"] = True
            updates["iff_asset_id"] = iff_friendly["asset_id"]
            updates["iff_callsign"] = iff_friendly["callsign"]
        await db.detections.update_one(
            {"id": existing["id"]},
            {
                "$set": updates,
                # Real re-confirmation event log: record the actual moment
                # this ingest matched the existing id, capped to the most
                # recent RECONFIRM_EVENTS_CAP entries via $slice so the
                # document can't grow unbounded under a long-lived/fast-
                # cadence contact. This is the only place event history is
                # written -- see _new_detection_skeleton for the initial
                # entry on creation.
                "$push": {
                    "reconfirm_events": {
                        "$each": [updates["last_seen"]],
                        "$slice": -RECONFIRM_EVENTS_CAP,
                    }
                },
            },
        )
        det = {**existing, **updates}
        det["reconfirm_events"] = (existing.get("reconfirm_events") or []) + [updates["last_seen"]]
        det["reconfirm_events"] = det["reconfirm_events"][-RECONFIRM_EVENTS_CAP:]
        det.pop("_id", None)
        return det

    det = _new_detection_skeleton()  # id/timestamps/state only — no fabricated RF fields
    model, protocol, threat_level = body.model, body.protocol, body.threat_level
    original_model, original_protocol = None, None
    confidence_type = body.confidence_type
    wifi_display = _ml_wifi_reclassification(body.ml_label, body.ml_confidence)
    unclassified_display = _ml_unclassified_display(body.confidence_type)
    if wifi_display:
        # Same one-directional correction as the merge/update path above,
        # applied on first-ever creation too (the ML bridge can itself be the
        # first ingest to create a record, ahead of hackrf_rx.py's own post,
        # within the merge window).
        original_model, original_protocol = body.model, body.protocol
        model, protocol = wifi_display
        threat_level = "LOW"
        confidence_type = "ml_probability"
    elif unclassified_display:
        # Same display-override pattern, applied on first-ever creation too
        # (ml_classify_bridge.py's own gate-check can itself be the first
        # ingest to create a record, e.g. if it beats hackrf_rx.py's next
        # cycle within the merge window). Wire model/protocol/threat_level
        # stay the plain heuristic guess (see _ml_unclassified_display); the
        # honest "unclassified" identity is substituted here for display.
        original_model, original_protocol = body.model, body.protocol
        model, protocol = unclassified_display
        threat_level = "LOW"
        confidence_type = "unclassified_signal"
    else:
        # First-ever creation of a purely heuristic_binary detection (the
        # common case: hackrf_rx.py's own ingest is almost always the first
        # ingest for a new contact). Apply the generic-display override from
        # the moment the record is created, not just on later re-confirmation.
        heuristic_display = _heuristic_display(body.model, body.confidence_type)
        if heuristic_display:
            original_model, original_protocol = body.model, body.protocol
            model, protocol = heuristic_display
    det.update({
        "callsign": body.callsign or det["callsign"],
        "model": model,
        "protocol": protocol,
        "original_model": original_model,
        "original_protocol": original_protocol,
        # Immutable merge-match key (2026-07-24 fix): captured ONCE here,
        # from the raw incoming body.model/body.protocol, BEFORE any
        # display override is applied above. Never overwritten by any
        # later ingest/update -- see detection_ingest's merge query and the
        # self-heal comment in the update-existing branch above. This is
        # what lets subsequent re-confirmation cycles (which always POST
        # the same raw literal) keep finding this exact document no matter
        # how many times its displayed model/protocol get overridden.
        "match_model": body.model,
        "match_protocol": body.protocol,
        "threat_level": threat_level,
        "center_freq_ghz": body.center_freq_ghz,
        "bandwidth_mhz": body.bandwidth_mhz,
        "rssi_dbm": body.rssi_dbm,
        "snr_db": body.snr_db,
        "bearing_deg": body.bearing_deg,
        "distance_m": body.distance_m,
        "distance_estimated": body.distance_estimated,
        "altitude_m": body.altitude_m,
        "speed_ms": body.speed_ms,
        "system_id": body.system_id,
        "component_id": body.component_id,
        "encrypted": body.encrypted,
        "source": body.source,
        "protocol_confirmed": body.protocol_confirmed,
        "ml_label": body.ml_label,
        "ml_confidence": body.ml_confidence,
        "ml_gated": body.ml_gated,
        "confidence_type": confidence_type,
    })

    # IFF suppression (task #60) -- see identical comment in the
    # update-existing branch above for what this does and does not check.
    iff_friendly = await _check_iff_friendly_match(det.get("bearing_deg"))
    if iff_friendly:
        det["threat_level"] = "FRIENDLY (IFF verified)"
        det["iff_verified"] = True
        det["iff_asset_id"] = iff_friendly["asset_id"]
        det["iff_callsign"] = iff_friendly["callsign"]

    await db.detections.insert_one(det.copy())
    await log_event("DETECTION",
                    f"[{body.source}] LIVE contact {det['callsign']} @ {body.center_freq_ghz} GHz "
                    f"(RSSI {body.rssi_dbm} dBm)",
                    meta={"detection_id": det["id"], "source": body.source},
                    actor=user["email"])
    det.pop("_id", None)
    return det



# =====================================================================
# Routes: MAVLink crafting / broadcast
# =====================================================================
def _craft(body: MavlinkCraftBody) -> bytes:
    if body.message_id == 76:  # COMMAND_LONG payload
        payload = build_command_long_payload(
            body.target_system, body.target_component, body.command,
            0, body.param1, body.param2, body.param3, body.param4,
            body.param5, body.param6, body.param7,
        )
    else:
        # For other message ids, produce a synthetic payload of 8 zero bytes
        # (real-world crafting would require per-message schemas).
        if body.message_id not in CRC_EXTRA:
            raise HTTPException(400, f"CRC_EXTRA not registered for msgid={body.message_id}")
        payload = b"\x00" * 8
    if body.version == "v1":
        if body.message_id > 255:
            raise HTTPException(400, "MAVLink v1 supports msgid <= 255")
        return build_packet_v1(body.message_id, payload,
                               system_id=body.system_id,
                               component_id=body.component_id,
                               sequence=body.sequence)
    return build_packet_v2(body.message_id, payload,
                           system_id=body.system_id,
                           component_id=body.component_id,
                           sequence=body.sequence)


@api.post("/mavlink/craft")
async def craft_packet(body: MavlinkCraftBody, user: Dict = Depends(get_current_user)):
    frame = _craft(body)
    return {
        "hex": frame.hex().upper(),
        "base64": base64.b64encode(frame).decode(),
        "length": len(frame),
        "hexdump": hexdump(frame),
        "decoded": describe_packet(frame),
    }


@api.post("/mavlink/broadcast")
async def broadcast_packet(body: MavlinkCraftBody, user: Dict = Depends(require_commander)):
    # #1/#13: raw MAVLink crafting can produce ANY MAV_CMD bypassing the vetted
    # PAYLOAD_CATALOG metadata — transmitting it therefore requires commander
    # role. /mavlink/craft (preview-only, never transmitted/persisted/broadcast
    # to the RF bridge) remains available to any authenticated operator.
    _check_tx_not_halted()
    if body.target_system == 0:
        # #4: broadcast (target_system=0) hits every drone in RF range,
        # including friendlies — require a freshly-issued arm token.
        _consume_arm_token(body.arm_token)
    frame = _craft(body)
    # Same request_id/ack correlation as /payloads/deploy — this frame has no
    # associated detection to gate, but we still want a real bridge
    # confirmation logged rather than declaring victory the instant the frame
    # hits the WS (see _handle_tx_ack / _expire_pending_acks).
    request_id = str(uuid.uuid4())
    pkt = {
        "id": str(uuid.uuid4()),
        "request_id": request_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "hex": frame.hex().upper(),
        "length": len(frame),
        "system_id": body.system_id,
        "component_id": body.component_id,
        "target_system": body.target_system,
        "message_id": body.message_id,
        "command": body.command if body.message_id == 76 else None,
        "actor": user["email"],
        "hexdump": hexdump(frame),
        "decoded": describe_packet(frame),
    }
    await db.mav_packets.insert_one(pkt.copy())
    pkt.pop("_id", None)
    # RACE FIX: register the pending ack BEFORE broadcasting the packet. A
    # real, fast bridge (rf-bridge/mavlink_bridge.py) can write to serial and
    # send its tx_ack back over the same WS in under a millisecond -- faster
    # than this coroutine would otherwise get back around to inserting into
    # _pending_acks after the broadcast. If that happens, _handle_tx_ack pops
    # nothing (logs "unknown/expired"), the ack is silently dropped, and the
    # request incorrectly rides out to TX_TIMEOUT even though the bridge did
    # everything right. Registering first closes that window.
    _pending_acks[request_id] = {
        "ts": datetime.now(timezone.utc),
        "detection_ids": [],
        "spec_name": f"raw msgid={body.message_id}",
        "broadcast": True,
    }
    await ws_manager.broadcast_json({"type": "packet", "packet": pkt})
    await log_event("MAVLINK",
                    f"Requested broadcast msgid={body.message_id} cmd={body.command} → "
                    f"sys={body.target_system} — awaiting bridge TX confirmation (request {request_id})",
                    meta={"packet_id": pkt["id"], "length": len(frame), "request_id": request_id},
                    actor=user["email"])
    pkt["status"] = "AWAITING_ACK"
    return pkt


@api.get("/mavlink/packets")
async def list_packets(limit: int = 100, user: Dict = Depends(get_current_user)):
    docs = await db.mav_packets.find({}, {"_id": 0}).sort("ts", -1).to_list(limit)
    return docs


@app.websocket("/api/ws/mavlink")
@api.websocket("/ws/mavlink")           # duplicate registration for robustness
async def ws_mavlink(ws: WebSocket):
    # Simple token check via query param
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        if payload.get("type") != "access":
            await ws.close(code=1008)
            return
    except jwt.PyJWTError:
        await ws.close(code=1008)
        return
    await ws_manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "ts": datetime.now(timezone.utc).isoformat()})
        while True:
            # Bridge clients (rf-bridge/mavlink_bridge.py) send real messages
            # back on this same connection now — specifically {"type":
            # "tx_ack", ...} after a real serial write attempt. Anything else
            # (or anything we fail to parse) is ignored, same as before.
            raw = await ws.receive_text()
            try:
                incoming = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(incoming, dict) and incoming.get("type") == "tx_ack":
                await _handle_tx_ack(incoming)
            elif isinstance(incoming, dict) and incoming.get("type") == "jam_ack":
                # From field-bridge/jam_bridge.py, after a real (or refused)
                # hackrf_transfer attempt — see _handle_jam_ack.
                await _handle_jam_ack(incoming)
            elif isinstance(incoming, dict) and incoming.get("type") == "gnss_spoof_ack":
                # From field-bridge/gnss_spoof_bridge.py — see _handle_gnss_spoof_ack.
                await _handle_gnss_spoof_ack(incoming)
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)


# =====================================================================
# Routes: Payload library
# =====================================================================
@api.get("/payloads")
async def list_payloads(user: Dict = Depends(get_current_user)):
    return [p.to_dict() for p in PAYLOAD_CATALOG]


@api.post("/payloads/deploy")
async def deploy_payload(body: DeployPayloadBody,
                         user: Dict = Depends(require_commander)):
    # #1: FORCE_DISARM/FLIGHT_TERMINATION/PL-010 broadcast-takedown all deploy
    # through this endpoint — commander role is required unconditionally.
    _check_tx_not_halted()

    spec = get_payload_by_id(body.payload_id)
    if not spec:
        raise HTTPException(404, "Unknown payload id")
    builder = PAYLOAD_BUILDERS.get(body.payload_id)
    if not builder:
        raise HTTPException(500, "No builder registered for this payload")

    # #1: CRITICAL-severity payloads (FORCE_DISARM, FLIGHT_TERMINATION,
    # PROPELLER_STOP, MEMORY_ERASE, PL-010 broadcast, ...) additionally need a
    # freshly-issued, single-use arm token as a server-enforced second factor.
    # #4: any broadcast (target_system=0) needs the same, regardless of
    # severity, since it can strike friendlies in RF range.
    if spec.severity == "CRITICAL" or body.broadcast:
        _consume_arm_token(body.arm_token)

    target_sys = 0
    target_comp = 0
    detection = None
    if not body.broadcast:
        if not body.target_detection_id:
            raise HTTPException(400, "target_detection_id required unless broadcast=True")
        detection = await db.detections.find_one({"id": body.target_detection_id})
        if not detection:
            raise HTTPException(404, "Target detection not found")
        # #4: friendly-fire interlock — refuse to engage anything not
        # explicitly authorized as a target (see /detections/{id}/authorize-target).
        if not detection.get("authorized_target"):
            raise HTTPException(
                403,
                "Target not authorized — friendly-fire interlock: "
                "POST /api/detections/{id}/authorize-target first.",
            )
        target_sys = detection.get("system_id", 1)
        target_comp = detection.get("component_id", 1)

    # target_sys/target_comp are already 0/0 for the broadcast case (set above),
    # so always pass them through — calling builder(seq=0) alone previously
    # raised TypeError for every payload except PL-010 (missing required
    # positional target_sys), which FastAPI surfaced as an unhandled 500.
    frame = builder(target_sys, target_comp, 0)
    # SECURITY/RELIABILITY: request_id correlates this specific deploy to the
    # tx_ack the bridge sends back after its real serial write — see
    # _handle_tx_ack / rf-bridge/mavlink_bridge.py. Nothing here is marked
    # NEUTRALIZED until that ack actually arrives with ok=True.
    request_id = str(uuid.uuid4())
    pkt = {
        "id": str(uuid.uuid4()),
        "request_id": request_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "hex": frame.hex().upper(),
        "length": len(frame),
        "payload_id": spec.id,
        "payload_name": spec.name,
        "severity": spec.severity,
        "broadcast": body.broadcast,
        "target_detection_id": body.target_detection_id,
        "target_system": target_sys,
        "hexdump": hexdump(frame),
        "decoded": describe_packet(frame),
        "actor": user["email"],
    }
    await db.mav_packets.insert_one(pkt.copy())
    pkt.pop("_id", None)

    # Do NOT mark NEUTRALIZED here — this is the exact bug that caused the
    # earlier live-demo failure (frame broadcast over WS to whichever bridge
    # happened to be connected, detection unconditionally marked defeated,
    # with zero confirmation the bridge wrote it to the real radio). Instead
    # park the detection(s) in AWAITING_ACK and register the pending ack so
    # _handle_tx_ack / _expire_pending_acks can resolve it for real.
    #
    # RACE FIX: all of this (DB updates + _pending_acks registration) MUST
    # happen BEFORE ws_manager.broadcast_json() below. A real, fast bridge
    # (rf-bridge/mavlink_bridge.py) can receive the packet, write it to
    # serial, and send its tx_ack back over the same WS in under a
    # millisecond — faster than this coroutine would otherwise get back
    # around to inserting into _pending_acks after the broadcast. If the
    # broadcast goes out first, _handle_tx_ack pops nothing for the ack
    # (logs "unknown/expired"), the ack is silently dropped, and the
    # request incorrectly rides out to TX_TIMEOUT even though the bridge
    # did everything right and the bytes genuinely reached the radio.
    detection_ids: List[str] = []
    if detection is not None:
        detection_ids = [detection["id"]]
        await db.detections.update_one(
            {"id": detection["id"]},
            {"$set": {
                "status": "AWAITING_ACK",
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "last_payload": spec.name,
            }},
        )
    elif body.broadcast:
        active = await db.detections.find(
            {"status": "ACTIVE"}, {"_id": 0, "id": 1}
        ).to_list(1000)
        detection_ids = [d["id"] for d in active]
        if detection_ids:
            await db.detections.update_many(
                {"id": {"$in": detection_ids}},
                {"$set": {"status": "AWAITING_ACK", "last_payload": spec.name}},
            )

    _pending_acks[request_id] = {
        "ts": datetime.now(timezone.utc),
        "detection_ids": detection_ids,
        "spec_name": spec.name,
        "broadcast": body.broadcast,
    }

    await ws_manager.broadcast_json({"type": "packet", "packet": pkt})
    await log_event(
        "PAYLOAD",
        f"Requested {spec.name} ({spec.severity}) on "
        f"{'BROADCAST' if body.broadcast else detection.get('callsign','?')} "
        f"— awaiting bridge TX confirmation (request {request_id})",
        meta={"payload_id": spec.id, "packet_id": pkt["id"], "broadcast": body.broadcast,
              "target_detection_id": body.target_detection_id, "request_id": request_id},
        actor=user["email"],
    )

    pkt["status"] = "AWAITING_ACK"
    return pkt


# =====================================================================
# Routes: RF Jamming (real HackRF barrage-jam TX via field-bridge/jam_bridge.py)
# =====================================================================
# Mirrors field-bridge/hackrf_jam.py's own BAND_PRESETS_MHZ / MAX_DURATION_S —
# duplicated here (rather than imported) because the backend and the field
# bridge are separate deployable processes/hosts; kept as the same values by
# convention. If hackrf_jam.py's presets ever change, update this dict too.
JAM_BAND_PRESETS_MHZ = {
    "433": 435.0, "915": 915.0, "2g4": 2450.0,
    # Bluetooth Classic/BLE — see field-bridge/hackrf_jam.py's BAND_PRESETS_MHZ
    # comment: this is the SAME shared 2.4-2.4835GHz ISM band "2g4" already
    # targets, just an explicitly-labeled preset for operator clarity. Not a
    # distinct hop-following jammer.
    "bt_2g4": 2442.0,
    "5g8": 5800.0,
    # GNSS L1 targets — see field-bridge/hackrf_jam.py's BAND_PRESETS_MHZ
    # comment for exact freqs and the GLONASS FDMA-channelization caveat.
    "gps_l1": 1575.42, "galileo_e1": 1575.42, "beidou_b1": 1561.098, "glonass_l1": 1602.0,
}
# Bands that deny satellite navigation rather than a comms/video link — used
# only to decide whether /payloads/jam logs the extra GNSS caveat below.
# Mirrors field-bridge/hackrf_jam.py's GNSS_BANDS.
JAM_GNSS_BANDS = {"gps_l1", "galileo_e1", "beidou_b1", "glonass_l1"}
JAM_MAX_DURATION_S = 10.0  # matches field-bridge/hackrf_jam.py's MAX_DURATION_S


@api.post("/jam/confirm")
async def jam_confirm(user: Dict = Depends(require_commander)):
    """Mint a single-use jam_confirm_token, valid for JAM_CONFIRM_TTL_S
    seconds. The frontend (frontend/src/pages/Jamming.jsx) must call this
    EXACTLY at the moment its SafetyGate-style two-step confirm (5-point
    checklist + ARM & FIRE -> CONFIRM FIRE) completes — never earlier, never
    cached. This token is what stands in for hackrf_jam.py's interactive
    'type TRANSMIT' prompt once the request reaches the WS-driven bridge
    (field-bridge/jam_bridge.py), which cannot present that prompt itself."""
    tok = _issue_jam_confirm_token()
    await log_event("JAM_CONFIRM",
                    f"Jam confirmation token issued (valid {JAM_CONFIRM_TTL_S}s) — "
                    f"operator completed SafetyGate checklist + two-click confirm",
                    actor=user["email"])
    return tok


@api.post("/payloads/jam")
async def deploy_jam(body: JamRequestBody, user: Dict = Depends(require_commander)):
    """Request a real, bounded-duration HackRF barrage-jam burst.

    Layered gates, ALL independently required (see field-bridge/jam_bridge.py's
    module docstring for the full chain including the bridge-side gates this
    endpoint cannot itself enforce):
      1. require_commander (above).
      2. _check_tx_not_halted — EMERGENCY ABORT blocks this like any other TX.
      3. arm_token — jamming is unconditionally CRITICAL severity; always required.
      4. jam_confirm_token — proof the frontend's SafetyGate-style two-step
         confirm actually happened for THIS request; always required.
    Neither token is optional or conditional here (unlike /payloads/deploy's
    severity-dependent arm_token) — every jam request needs both, every time.
    """
    _check_tx_not_halted()
    _consume_arm_token(body.arm_token)
    _consume_jam_confirm_token(body.jam_confirm_token)

    freq_mhz = body.freq_mhz if body.freq_mhz is not None else JAM_BAND_PRESETS_MHZ.get(body.band)
    if not freq_mhz:
        raise HTTPException(400, "Provide either `band` (433|915|2g4|bt_2g4|5g8|gps_l1|galileo_e1|beidou_b1|"
                                  "glonass_l1) or an explicit `freq_mhz`.")
    duration_s = min(body.duration_s, JAM_MAX_DURATION_S)

    request_id = str(uuid.uuid4())

    if body.band in JAM_GNSS_BANDS:
        # Logging only — NOT an additional gate. The extra GNSS-denial-radius
        # warning is surfaced to the operator in the SAME SafetyGate confirm
        # flow (frontend/src/pages/Jamming.jsx), before arm_token/
        # jam_confirm_token were ever minted for this request.
        logger.warning(
            "GNSS-target jam request %s: band=%s freq=%.3fMHz — GNSS denial has a "
            "proportionally larger effective radius than comms jamming at the same "
            "TX power (GPS-band receive levels are ~-130dBm).", request_id, body.band, freq_mhz,
        )
    _pending_jam[request_id] = {
        "ts": datetime.now(timezone.utc),
        "status": "AWAITING_ACK",
        "band": body.band,
        "freq_mhz": freq_mhz,
        "bandwidth_khz": body.bandwidth_khz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
        "actor": user["email"],
    }

    await ws_manager.broadcast_json({
        "type": "jam_request",
        "request_id": request_id,
        "band": body.band,
        "freq_mhz": freq_mhz,
        "bandwidth_khz": body.bandwidth_khz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
        # Forwarded AFTER being consumed above — its presence here is the
        # bridge's evidence a real UI confirmation happened, not a live
        # credential the bridge itself validates against the backend.
        "jam_confirm_token": body.jam_confirm_token,
        "actor": user["email"],
    })

    await log_event(
        "JAM",
        f"Requested RF jam burst: {freq_mhz} MHz, {body.bandwidth_khz}kHz BW, "
        f"{duration_s}s, gain={body.tx_gain} — awaiting bridge TX confirmation (request {request_id})",
        meta={"request_id": request_id, "freq_mhz": freq_mhz, "duration_s": duration_s},
        actor=user["email"],
    )
    await ws_manager.broadcast_json({"type": "jam_status", "request_id": request_id, "status": "AWAITING_ACK"})

    return {
        "request_id": request_id,
        "status": "AWAITING_ACK",
        "freq_mhz": freq_mhz,
        "bandwidth_khz": body.bandwidth_khz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
    }


@api.get("/jam/status")
async def jam_status(user: Dict = Depends(get_current_user)):
    """Current/most-recent jam session state(s), for the Jamming UI to poll
    (same poll-and-render pattern as GET /detections used by
    KillChain.jsx/Payloads.jsx) rather than needing its own WS consumer."""
    await _expire_pending_jam()
    sessions = sorted(
        ({"request_id": rid, **{k: v for k, v in p.items() if k != "ts"},
          "ts": p["ts"].isoformat()} for rid, p in _pending_jam.items()),
        key=lambda s: s["ts"], reverse=True,
    )
    return {"sessions": sessions[:20]}


# =====================================================================
# Routes: GNSS L1 civil-signal spoofing ("soft-kill") — Task #103.
# See field-bridge/GNSS_SPOOF_ARCHITECTURE.md for the full design.
# =====================================================================
@api.post("/gnss-spoof/preview")
async def gnss_spoof_preview(body: GnssSpoofPreviewBody, user: Dict = Depends(require_commander)):
    """Pure computation, no tokens minted, nothing transmitted. Returns the
    EXACT fabricated position BEFORE any token is minted, so the frontend's
    SafetyGate checklist can render the real numbers (not a template) — see
    architecture doc §5b. Logged only as an INFO-level breadcrumb, NOT part
    of the authorization audit chain."""
    fake_lat, fake_lon = geodesic_destination(
        body.true_lat, body.true_lon, body.fake_offset_m, body.fake_bearing_deg
    )
    bearing_compass = _bearing_compass(body.fake_bearing_deg)
    distance_description = (
        f"{body.fake_offset_m:.0f} m offset, bearing {bearing_compass} from last-known-true position"
    )
    result = {
        "fake_lat": fake_lat,
        "fake_lon": fake_lon,
        "fake_alt_m": body.true_alt_m,
        "offset_m": body.fake_offset_m,
        "bearing_deg": body.fake_bearing_deg,
        "bearing_compass": bearing_compass,
        "distance_description": distance_description,
    }
    await log_event(
        "gnss_spoof_preview_viewed",
        f"GNSS spoof preview computed: {distance_description} "
        f"(fake position {fake_lat:.6f},{fake_lon:.6f})",
        meta={"true_lat": body.true_lat, "true_lon": body.true_lon,
              "fake_offset_m": body.fake_offset_m, "fake_bearing_deg": body.fake_bearing_deg,
              **result},
        actor=user["email"],
    )
    return result


@api.post("/gnss-spoof/confirm")
async def gnss_spoof_confirm(body: GnssSpoofConfirmBody, user: Dict = Depends(require_commander)):
    """Mints a single-use gnss_spoof_confirm_token. Requires
    body.friendly_asset_attestation to be non-trivial (reject with 400
    otherwise) — this is the durable record of WHAT was attested, tied to
    WHEN the token was minted, logged to mission_log immediately so the
    attestation survives even if /payloads/gnss-spoof never arrives."""
    if not _looks_like_real_attestation(body.friendly_asset_attestation):
        raise HTTPException(
            400,
            f"friendly_asset_attestation must be a real, actively-typed statement "
            f"(minimum {MIN_FRIENDLY_ASSET_ATTESTATION_LEN} chars, not a placeholder "
            f"like 'n/a'/'none'/'confirmed') — describe the friendly-asset review performed.",
        )
    tok = _issue_gnss_spoof_confirm_token(body.friendly_asset_attestation)
    await log_event(
        "gnss_spoof_attestation",
        f"GNSS-spoof friendly-asset attestation recorded (confirm token valid "
        f"{GNSS_SPOOF_CONFIRM_TTL_S}s): {body.friendly_asset_attestation}",
        meta={"friendly_asset_attestation": body.friendly_asset_attestation},
        actor=user["email"],
    )
    return tok


@api.post("/payloads/gnss-spoof")
async def deploy_gnss_spoof(body: GnssSpoofRequestBody, user: Dict = Depends(require_commander)):
    """Requests a real, bounded-duration GPS L1 C/A spoof burst carrying a
    fabricated position. Layered gates, ALL independently required (mirrors
    deploy_jam exactly — see field-bridge/gnss_spoof_bridge.py's module
    docstring for the bridge-side gates this endpoint cannot itself enforce):
      1. require_commander (above).
      2. _check_tx_not_halted — EMERGENCY ABORT blocks this like any other TX.
      3. arm_token — gnss_spoof is unconditionally CRITICAL severity.
      4. gnss_spoof_confirm_token — proof the SafetyGate two-step confirm
         happened for THIS request, AND that friendly_asset_attestation
         matches what was attested at /gnss-spoof/confirm time.
    """
    _check_tx_not_halted()
    _consume_arm_token(body.arm_token)
    _consume_gnss_spoof_confirm_token(body.gnss_spoof_confirm_token, body.friendly_asset_attestation)

    duration_s = min(body.duration_s, GNSS_SPOOF_MAX_DURATION_S)
    freq_mhz = JAM_BAND_PRESETS_MHZ["gps_l1"]  # 1575.42 MHz, the only supported band at launch

    fake_lat, fake_lon = geodesic_destination(
        body.true_lat, body.true_lon, body.fake_offset_m, body.fake_bearing_deg
    )
    fake_alt_m = body.true_alt_m

    request_id = str(uuid.uuid4())
    _pending_gnss_spoof[request_id] = {
        "ts": datetime.now(timezone.utc),
        "status": "AWAITING_ACK",
        "band": body.band,
        "freq_mhz": freq_mhz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
        "actor": user["email"],
    }

    # Logged BEFORE the WS message is sent, per architecture doc §6 —
    # single source of truth for what "the preview showed" vs "what gets
    # transmitted" (same numbers, computed once, here).
    await log_event(
        "gnss_spoof_fired",
        f"Requested GNSS spoof burst: fake position {fake_lat:.6f},{fake_lon:.6f} "
        f"(offset {body.fake_offset_m:.0f}m @ {body.fake_bearing_deg:.0f}°) from true "
        f"{body.true_lat:.6f},{body.true_lon:.6f}, {duration_s}s @ {freq_mhz} MHz, "
        f"gain={body.tx_gain} — awaiting bridge TX confirmation (request {request_id})",
        meta={
            "request_id": request_id, "freq_mhz": freq_mhz, "duration_s": duration_s,
            "tx_gain": body.tx_gain, "true_lat": body.true_lat, "true_lon": body.true_lon,
            "true_alt_m": body.true_alt_m, "fake_lat": fake_lat, "fake_lon": fake_lon,
            "fake_alt_m": fake_alt_m, "offset_m": body.fake_offset_m,
            "bearing_deg": body.fake_bearing_deg,
            "friendly_asset_attestation": body.friendly_asset_attestation,
        },
        actor=user["email"],
    )

    await ws_manager.broadcast_json({
        "type": "gnss_spoof_request",
        "request_id": request_id,
        "band": body.band,
        "freq_mhz": freq_mhz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
        "true_lat": body.true_lat,
        "true_lon": body.true_lon,
        "true_alt_m": body.true_alt_m,
        "fake_lat": fake_lat,
        "fake_lon": fake_lon,
        "fake_alt_m": fake_alt_m,
        # Forwarded AFTER being consumed above — same convention as
        # jam_request's jam_confirm_token forwarding.
        "gnss_spoof_confirm_token": body.gnss_spoof_confirm_token,
        "actor": user["email"],
    })
    await ws_manager.broadcast_json({"type": "gnss_spoof_status", "request_id": request_id, "status": "AWAITING_ACK"})

    return {
        "request_id": request_id,
        "status": "AWAITING_ACK",
        "freq_mhz": freq_mhz,
        "duration_s": duration_s,
        "tx_gain": body.tx_gain,
        "fake_lat": fake_lat,
        "fake_lon": fake_lon,
        "fake_alt_m": fake_alt_m,
    }


@api.get("/gnss-spoof/status")
async def gnss_spoof_status(user: Dict = Depends(get_current_user)):
    """Current/most-recent gnss_spoof session state(s), poll-and-render
    pattern, mirrors GET /jam/status."""
    await _expire_pending_gnss_spoof()
    sessions = sorted(
        ({"request_id": rid, **{k: v for k, v in p.items() if k != "ts"},
          "ts": p["ts"].isoformat()} for rid, p in _pending_gnss_spoof.items()),
        key=lambda s: s["ts"], reverse=True,
    )
    return {"sessions": sessions[:20]}


# =====================================================================
# Routes: Range authorization (GUI-controlled replacement for the bridge-side
# CEMA_AUTHORIZED_RANGE env var — see RANGE_AUTHORIZATION_REDESIGN.md)
# =====================================================================
@api.get("/range-authorization/status")
async def range_authorization_status(effect: str, user: Dict = Depends(get_current_user)):
    """Any authenticated user may read this (needed by the persistent banner
    component on every page, and polled by field-bridge/rf-bridge services at
    the moment of transmission)."""
    if effect not in RANGE_AUTH_EFFECTS:
        raise HTTPException(400, f"effect must be one of {RANGE_AUTH_EFFECTS}")
    await _expire_range_authorization()
    return _range_auth_status(effect)


@api.post("/range-authorization")
async def set_range_authorization(body: RangeAuthorizationBody, request: Request,
                                  user: Dict = Depends(require_commander)):
    """Arm/disarm a range-authorization lease for one effect (jam|mavlink).

    enabled=True:  requires re-entering the current password (step-up auth —
                   a stolen JWT alone is not enough) AND the fixed confirm
                   phrase, both checked here even though require_commander
                   already ran. Failures are throttled and fully audited.
    enabled=False: no password/phrase required — disabling must always be
                   low-friction (§2.3/2.6 of the redesign doc).
    """
    if body.effect not in RANGE_AUTH_EFFECTS:
        raise HTTPException(400, f"effect must be one of {RANGE_AUTH_EFFECTS}")
    await _expire_range_authorization()

    source_ip = request.client.host if request.client else None
    throttle_key = user["email"]

    if body.enabled:
        if _range_auth_locked_out(throttle_key):
            await log_event(
                "RANGE_AUTH_ENABLE_FAILED",
                f"Range authorization enable for effect={body.effect} REFUSED: "
                f"too many recent failed attempts (locked out {RANGE_AUTH_LOCKOUT_WINDOW_S}s)",
                meta={"effect": body.effect, "reason": "locked_out", "source_ip": source_ip},
                actor=user["email"],
            )
            raise HTTPException(429, "Too many failed range-authorization attempts — try again shortly.")

        # Re-verify the user's CURRENT password against their stored hash —
        # the bare JWT from require_commander is deliberately not treated as
        # sufficient on its own for this specific action (see §2.1/2.2).
        full_user = await db.users.find_one({"id": user["id"]})
        if not body.password or not full_user or not verify_password(body.password, full_user["password_hash"]):
            _record_range_auth_failure(throttle_key)
            await log_event(
                "RANGE_AUTH_ENABLE_FAILED",
                f"Range authorization enable for effect={body.effect} REFUSED: bad password",
                meta={"effect": body.effect, "reason": "bad_password", "source_ip": source_ip},
                actor=user["email"],
            )
            raise HTTPException(401, "Password re-verification failed.")

        if body.confirm_phrase != RANGE_AUTH_CONFIRM_PHRASE:
            _record_range_auth_failure(throttle_key)
            await log_event(
                "RANGE_AUTH_ENABLE_FAILED",
                f"Range authorization enable for effect={body.effect} REFUSED: "
                f"confirm phrase mismatch",
                meta={"effect": body.effect, "reason": "bad_confirm_phrase", "source_ip": source_ip},
                actor=user["email"],
            )
            raise HTTPException(400, f"Confirmation phrase must exactly match \"{RANGE_AUTH_CONFIRM_PHRASE}\".")

        now = datetime.now(timezone.utc)
        lease = _range_authorization[body.effect]
        lease["enabled"] = True
        lease["expires_at"] = now + timedelta(seconds=RANGE_AUTH_TTL_S)
        lease["enabled_by"] = user["email"]
        lease["enabled_at"] = now
        await log_event(
            "RANGE_AUTH_ENABLE",
            f"Range authorization ENABLED for effect={body.effect} "
            f"(expires in {RANGE_AUTH_TTL_S}s)",
            meta={"effect": body.effect, "source_ip": source_ip,
                 "expires_at": lease["expires_at"].isoformat()},
            actor=user["email"],
        )
    else:
        lease = _range_authorization[body.effect]
        lease["enabled"] = False
        lease["expires_at"] = None
        lease["enabled_by"] = None
        lease["enabled_at"] = None
        await log_event(
            "RANGE_AUTH_DISABLE",
            f"Range authorization DISABLED for effect={body.effect}",
            meta={"effect": body.effect, "source_ip": source_ip},
            actor=user["email"],
        )

    status = _range_auth_status(body.effect)
    await ws_manager.broadcast_json({"type": "range_authorization", **status})
    return status


# =====================================================================
# Routes: Mission log
# =====================================================================
@api.get("/logs")
async def list_logs(limit: int = 200, user: Dict = Depends(get_current_user)):
    docs = await db.mission_log.find({}, {"_id": 0}).sort("ts", -1).to_list(limit)
    return docs


# =====================================================================
# Routes: System health (dashboard tile + pre-demo check)
# =====================================================================
@api.get("/health")
async def system_health(user: Dict = Depends(get_current_user)):
    # Mongo ping
    mongo_ok = True
    try:
        await db.command("ping")
    except Exception:
        mongo_ok = False

    # HackRF live if we have a spectrum ingest within 30s (real hackrf_sweep passes
    # over 2.4/5.8GHz bands routinely take several seconds each; 10s was too tight
    # for genuine per-band sweep cadence and made a truly-live feed look "down").
    ing = _last_spectrum_ingest
    hackrf_live = bool(ing and (datetime.now(timezone.utc) - ing["ts"]).total_seconds() < 30)

    # SiK live if any detection with source SIK_RADIO seen in last 60s
    since = datetime.now(timezone.utc) - timedelta(seconds=60)
    sik_count = await db.detections.count_documents({
        "source": "SIK_RADIO",
        "protocol_confirmed": True,
        "last_seen": {"$gt": since.isoformat()},
    })

    # Run the same lazy staleness expiry the detections list uses, so this
    # health tile's "active_targets" count doesn't disagree with the
    # dashboard's Active Contacts count.
    await _expire_stale_detections()
    active_targets = await db.detections.count_documents({"status": "ACTIVE"})
    total_packets = await db.mav_packets.count_documents({})

    # ---- TX-path health signals -----------------------------------------
    # These exist specifically to catch the TWO real TX failure modes this
    # project has actually experienced, neither of which the RX-side signals
    # above (mongo/hackrf/sik_radio/ws_clients>0 alone) would catch:
    #   1. The original live-demo incident: a bridge silently not connected,
    #      so a deploy command had nowhere to go (mitigated by the
    #      AWAITING_ACK/tx_ack state machine — surfaced here as pending/
    #      recent-outcome counts).
    #   2. This session's real bug: /api/ws/mavlink fundamentally unable to
    #      accept ANY connection because `websockets` wasn't installed — a
    #      failure ws_clients alone can't distinguish from "no bridge happens
    #      to be connected right now" (surfaced here as ws_upgrade_capable).

    # Lazy-expire first so the counts below reflect true current state, same
    # pattern as /detections and /detections/{id} above.
    await _expire_pending_acks()
    await _expire_pending_jam()

    tx_pending_acks = len(_pending_acks)

    recent_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=TX_HEALTH_RECENT_WINDOW_S)
    ).isoformat()
    tx_recent_neutralized = await db.detections.count_documents({
        "status": "NEUTRALIZED", "last_seen": {"$gt": recent_cutoff},
    })
    tx_recent_timeout = await db.detections.count_documents({
        "status": "TX_TIMEOUT", "last_seen": {"$gt": recent_cutoff},
    })
    tx_recent_failed = await db.detections.count_documents({
        "status": "TX_FAILED", "last_seen": {"$gt": recent_cutoff},
    })
    tx_awaiting_ack = await db.detections.count_documents({"status": "AWAITING_ACK"})

    # Heuristic flag: every recent terminal TX outcome was bad and at least
    # one bad outcome actually happened — i.e. the bridge IS reachable enough
    # to be attempted against, but every attempt in the recent window failed
    # or timed out. This is exactly "looks connected but TX path is broken",
    # distinct from ws_clients==0 (nothing connected at all). Deliberately
    # conservative: says nothing when there's no recent TX activity to judge.
    tx_recent_total = tx_recent_neutralized + tx_recent_timeout + tx_recent_failed
    tx_path_degraded = tx_recent_total > 0 and tx_recent_neutralized == 0

    # ---- Ingest-side auth/health signals (task #74) ----------------------
    # Distinguishes "bridge actively failing auth" from "sensor legitimately
    # idle" — a bridge that has never posted (or hasn't posted in a very long
    # time) has null/huge ages and auth_failing=False, whereas one stuck in a
    # 401 loop has a recent last_attempt_ts and a high consecutive_failures.
    now_utc = datetime.now(timezone.utc)
    ingest_docs = await db.ingest_health.find({}, {"_id": 0}).to_list(50)
    ingest_sources = []
    for d in ingest_docs:
        def _age_s(ts_str: Optional[str]) -> Optional[float]:
            if not ts_str:
                return None
            try:
                return (now_utc - datetime.fromisoformat(ts_str)).total_seconds()
            except ValueError:
                return None

        last_success_age_s = _age_s(d.get("last_success_ts"))
        last_attempt_age_s = _age_s(d.get("last_attempt_ts"))
        consecutive_failures = d.get("consecutive_failures", 0)
        auth_failing = bool(
            consecutive_failures >= AUTH_FAIL_CONSECUTIVE_THRESHOLD
            and last_attempt_age_s is not None
            and last_attempt_age_s < INGEST_AUTH_FAIL_RECENT_WINDOW_S
        )
        ingest_sources.append({
            "bridge": d.get("bridge"),
            "last_success_age_s": last_success_age_s,
            "last_attempt_age_s": last_attempt_age_s,
            "consecutive_401": consecutive_failures,
            "auth_failing": auth_failing,
        })

    return {
        "backend": True,
        "mongo": mongo_ok,
        "tx_halted": _tx_halted,
        "hackrf": hackrf_live,
        "sik_radio": sik_count > 0,
        "ws_clients": len(ws_manager.clients),
        "ws_upgrade_capable": WS_UPGRADE_CAPABLE,
        "active_targets": active_targets,
        "total_packets_tx": total_packets,
        "tx_pending_acks": tx_pending_acks,
        "tx_awaiting_ack_detections": tx_awaiting_ack,
        "tx_recent_neutralized": tx_recent_neutralized,
        "tx_recent_timeout": tx_recent_timeout,
        "tx_recent_failed": tx_recent_failed,
        "tx_path_degraded": tx_path_degraded,
        "ingest_sources": ingest_sources,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# =====================================================================
# Routes: Arm token (second factor for CRITICAL payloads / broadcasts)
# =====================================================================
@api.post("/arm")
async def request_arm_token(user: Dict = Depends(require_commander)):
    """Issue a single-use arm token, valid for ARM_TOKEN_TTL_S seconds, that a
    commander must present when deploying a CRITICAL-severity payload or any
    broadcast (target_system=0) action."""
    tok = _issue_arm_token()
    await log_event("ARM", f"Arm token issued (valid {ARM_TOKEN_TTL_S}s)", actor=user["email"])
    return tok


# =====================================================================
# Routes: Emergency abort — halt all transmissions, mark ceasefire
# =====================================================================
@api.post("/emergency/abort")
async def emergency_abort(user: Dict = Depends(get_current_user)):
    # Any authenticated operator may hit the emergency stop — this is a safety
    # control, not a privileged one. #3: this now ALSO sets an authoritative,
    # server-side flag that /payloads/deploy and /mavlink/broadcast check
    # before building/sending any frame (previously this only broadcast a
    # cooperative WebSocket notice with no server-side enforcement).
    global _tx_halted
    _tx_halted = True
    await ws_manager.broadcast_json({
        "type": "abort",
        "ts": datetime.now(timezone.utc).isoformat(),
        "operator": user["email"],
    })
    await log_event("ABORT",
                    "EMERGENCY ABORT — all TX halted by operator",
                    actor=user["email"])
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@api.post("/emergency/resume")
async def emergency_resume(user: Dict = Depends(require_commander)):
    # Clearing the halt is commander-only — an emergency stop should not be
    # liftable by whoever's nearest a keyboard.
    global _tx_halted
    _tx_halted = False
    await ws_manager.broadcast_json({
        "type": "resume",
        "ts": datetime.now(timezone.utc).isoformat(),
        "operator": user["email"],
    })
    await log_event("ABORT", "TX resumed by commander after emergency abort",
                    actor=user["email"])
    return {"ok": True, "tx_halted": _tx_halted, "ts": datetime.now(timezone.utc).isoformat()}


# =====================================================================
# Routes: Mission PDF report (leave-behind for evaluators)
# =====================================================================
@api.get("/report/mission.pdf")
async def mission_pdf(user: Dict = Depends(get_current_user)):
    from io import BytesIO
    import hashlib
    from fastapi.responses import Response
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )

    detections = await db.detections.find({}, {"_id": 0}).to_list(1000)
    packets    = await db.mav_packets.find({}, {"_id": 0}).sort("ts", -1).to_list(500)
    logs       = await db.mission_log.find({}, {"_id": 0}).sort("ts", 1).to_list(2000)

    # Build a simple hash chain over log events for tamper-evident audit trail
    prev = ""
    hash_chain = []
    for e in logs:
        h = hashlib.sha256(f"{prev}|{e['ts']}|{e['kind']}|{e['message']}|{e['actor']}".encode()).hexdigest()
        hash_chain.append(h)
        prev = h
    final_hash = prev or "0" * 64

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    S = getSampleStyleSheet()

    red_banner = ParagraphStyle(
        "RedBanner", parent=S["Normal"],
        alignment=1, textColor=colors.white, backColor=colors.red,
        fontName="Helvetica-Bold", fontSize=9, leading=14,
    )
    title = ParagraphStyle("Title", parent=S["Title"], fontSize=22, leading=26,
                           spaceAfter=6, textColor=colors.HexColor("#0C111D"))
    h2 = ParagraphStyle("h2", parent=S["Heading2"], fontSize=12,
                        textColor=colors.HexColor("#00758F"), spaceBefore=10, spaceAfter=4)
    mono = ParagraphStyle("mono", parent=S["Normal"], fontName="Courier",
                          fontSize=7.5, leading=9)
    body = ParagraphStyle("body", parent=S["Normal"], fontSize=9, leading=12)

    story = []
    story.append(Paragraph("// RESTRICTED — INDIAN MINISTRY OF DEFENCE — CEMA-cUAS EVAL //", red_banner))
    story.append(Spacer(1, 6))
    story.append(Paragraph("CEMA cUAS · Mission Report", title))
    story.append(Paragraph(
        f"Session: <b>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</b> · "
        f"Operator: <b>{user.get('email','?')}</b> · "
        f"Clearance: <b>{user.get('clearance','RESTRICTED')}</b>", body))
    story.append(Spacer(1, 8))

    # Summary tile
    active = sum(1 for d in detections if d["status"] == "ACTIVE")
    neutralized = sum(1 for d in detections if d["status"] == "NEUTRALIZED")
    story.append(Paragraph("Executive Summary", h2))
    sum_tbl = Table(
        [
            ["Contacts detected", str(len(detections)), "Active", str(active)],
            ["Neutralized", str(neutralized), "MAVLink packets emitted", str(len(packets))],
            ["Mission log entries", str(len(logs)), "Audit chain hash", final_hash[:16] + "…"],
        ],
        colWidths=[45*mm, 25*mm, 45*mm, 55*mm],
    )
    sum_tbl.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "Helvetica", 9),
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#E8EEF5")),
        ("BACKGROUND", (2,0), (2,-1), colors.HexColor("#E8EEF5")),
        ("BOX", (0,0), (-1,-1), 0.5, colors.grey),
        ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
    ]))
    story.append(sum_tbl)

    # Contacts table
    story.append(Paragraph("Detected Contacts", h2))
    rows = [["CALLSIGN", "MODEL", "PROTOCOL", "FREQ (GHz)", "RSSI", "SRC", "CEMA", "KC", "STATUS"]]
    for d in detections[:60]:
        rows.append([
            d.get("callsign",""), d.get("model","")[:20], d.get("protocol","")[:14],
            f"{d.get('center_freq_ghz',0):.4f}", f"{d.get('rssi_dbm',0):.1f}",
            d.get("source","SIM"), d.get("cema_stage",""),
            d.get("kill_chain_stage",""), d.get("status",""),
        ])
    t = Table(rows, colWidths=[20*mm, 30*mm, 24*mm, 20*mm, 12*mm, 18*mm, 18*mm, 14*mm, 20*mm])
    t.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "Helvetica", 7),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold", 7),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0C111D")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.2, colors.lightgrey),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F4F6FA")]),
    ]))
    story.append(t)

    # MAVLink packets emitted
    story.append(Paragraph("MAVLink Packets Transmitted", h2))
    prows = [["TS (UTC)", "MSGID", "TGT SYS", "PAYLOAD", "SEVERITY", "LEN", "HEX (first 32B)"]]
    for p in packets[:40]:
        hexs = (p.get("hex","") or "")[:64]
        prows.append([
            (p.get("ts","")[:19]).replace("T"," "),
            str(p.get("decoded",{}).get("message_id","")),
            str(p.get("target_system","")),
            p.get("payload_name","-") or "-",
            p.get("severity","-") or "-",
            str(p.get("length","")),
            hexs,
        ])
    pt = Table(prows, colWidths=[28*mm, 15*mm, 15*mm, 30*mm, 20*mm, 12*mm, 55*mm])
    pt.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "Courier", 6.5),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold", 7),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0C111D")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.2, colors.lightgrey),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F4F6FA")]),
    ]))
    story.append(pt)

    # Mission log (hash-chained)
    story.append(PageBreak())
    story.append(Paragraph("Chronological Audit Trail (SHA-256 chained)", h2))
    lrows = [["#", "TS (UTC)", "KIND", "MESSAGE", "ACTOR", "HASH"]]
    for i, (e, h) in enumerate(zip(logs, hash_chain), start=1):
        lrows.append([
            str(i), (e.get("ts","")[:19]).replace("T"," "),
            e.get("kind",""), (e.get("message","") or "")[:65],
            e.get("actor",""), h[:12] + "…",
        ])
    lt = Table(lrows, colWidths=[8*mm, 30*mm, 18*mm, 70*mm, 30*mm, 25*mm], repeatRows=1)
    lt.setStyle(TableStyle([
        ("FONT", (0,0), (-1,-1), "Courier", 6),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold", 7),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0C111D")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.15, colors.lightgrey),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F4F6FA")]),
    ]))
    story.append(lt)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<b>Final chain hash:</b> <font face='Courier'>{final_hash}</font>", mono))
    story.append(Paragraph(
        "Any modification to prior log entries would invalidate this hash.", body))
    story.append(Spacer(1, 6))
    story.append(Paragraph("// RESTRICTED — NOT FOR OPERATIONAL USE //", red_banner))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    fname = f"cema-mission-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# =====================================================================
# Public health
# =====================================================================
@api.get("/")
async def root():
    return {"service": "cema-cuas", "status": "online",
            "cema_stages": CEMA_STAGES, "kill_chain": KILL_CHAIN}


# ---------- Register router + CORS ----------
app.include_router(api)

# #12: CORS_ORIGINS "*" combined with allow_credentials=True lets any site
# make credentialed requests against this API. Default to an explicit,
# localhost-only allow-list; deployments MUST override CORS_ORIGINS (comma
# separated) with their real frontend origin(s) — e.g. the LAN/server IP the
# frontend is actually served from.
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
