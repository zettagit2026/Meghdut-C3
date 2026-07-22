"""CEMA-Enabled cUAS backend.

Endpoints (all under /api):
  auth: /login /logout /me
  detections: /detections /detections/ingest /detections/{id}/cema-advance /detections/{id}/killchain-advance /detections/{id}/authorize-target /detections/{id}
  spectrum: /spectrum/waterfall
  mavlink: /mavlink/craft (preview-only) /mavlink/broadcast (commander-only, transmits) /mavlink/packets  (ws /ws/mavlink)
  payloads: /payloads /payloads/deploy (commander-only + arm-token for CRITICAL/broadcast)
  arm: /arm (commander-only, issues a 60s single-use arm token)
  emergency: /emergency/abort (any operator) /emergency/resume (commander-only)
  logs:  /logs

RBAC: two roles, "operator" and "commander". Anything that transmits a
kinetic/broadcast command (/payloads/deploy, /mavlink/broadcast) requires
"commander". CRITICAL-severity payload deploys and any broadcast
(target_system=0) additionally require a fresh arm token from POST /arm.
Targeting a specific detection requires it be explicitly authorized via
POST /detections/{id}/authorize-target (friendly-fire interlock).
"""
from __future__ import annotations

from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import asyncio
import base64
import binascii
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
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


# ---- Authoritative transmit-halt (server-side, checked before any TX) ----
# Set by /emergency/abort, cleared by /emergency/resume. /payloads/deploy and
# /mavlink/broadcast both check this BEFORE building/sending any frame — the
# prior implementation only broadcast a cooperative WebSocket notice with no
# server-side enforcement.
_tx_halted = False


def _check_tx_not_halted() -> None:
    if _tx_halted:
        raise HTTPException(409, "Transmission halted — EMERGENCY ABORT is in effect. "
                                  "A commander must POST /api/emergency/resume first.")


# =====================================================================
# Startup: seed admin, indexes
# =====================================================================
@app.on_event("startup")
async def startup() -> None:
    await db.users.create_index("email", unique=True)
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
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not verify_password(body.password, user["password_hash"]):
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
    callsign is supplied by the ingest source."""
    det_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": det_id,
        "callsign": f"UAV-{det_id[:8].upper()}",
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


@api.get("/detections")
async def list_detections(user: Dict = Depends(get_current_user)):
    await _expire_stale_detections()
    docs = await db.detections.find({}, {"_id": 0}).to_list(500)
    return docs


@api.get("/detections/{det_id}")
async def get_detection(det_id: str, user: Dict = Depends(get_current_user)):
    doc = await db.detections.find_one({"id": det_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Detection not found")
    return doc


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


@api.post("/detections/ingest")
async def detection_ingest(body: DetectionIngestBody,
                           user: Dict = Depends(get_current_user)):
    since = (datetime.now(timezone.utc) - timedelta(seconds=DETECTION_MERGE_WINDOW_S)).isoformat()
    existing = await db.detections.find_one({
        "source": body.source,
        "model": body.model,
        "protocol": body.protocol,
        "status": "ACTIVE",
        "last_seen": {"$gt": since},
    })

    if existing:
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
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        await db.detections.update_one({"id": existing["id"]}, {"$set": updates})
        det = {**existing, **updates}
        det.pop("_id", None)
        return det

    det = _new_detection_skeleton()  # id/timestamps/state only — no fabricated RF fields
    det.update({
        "callsign": body.callsign or det["callsign"],
        "model": body.model,
        "protocol": body.protocol,
        "threat_level": body.threat_level,
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
    })
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
    pkt = {
        "id": str(uuid.uuid4()),
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
    await ws_manager.broadcast_json({"type": "packet", "packet": pkt})
    await log_event("MAVLINK",
                    f"Broadcast msgid={body.message_id} cmd={body.command} → sys={body.target_system}",
                    meta={"packet_id": pkt["id"], "length": len(frame)},
                    actor=user["email"])
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
            # keep the connection open; ignore client messages
            await ws.receive_text()
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
    pkt = {
        "id": str(uuid.uuid4()),
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
    await ws_manager.broadcast_json({"type": "packet", "packet": pkt})
    await log_event(
        "PAYLOAD",
        f"Deployed {spec.name} ({spec.severity}) on {'BROADCAST' if body.broadcast else detection.get('callsign','?')}",
        meta={"payload_id": spec.id, "packet_id": pkt["id"], "broadcast": body.broadcast,
              "target_detection_id": body.target_detection_id},
        actor=user["email"],
    )

    # If targeting a specific drone, mark it neutralized after payload deploy
    if detection is not None:
        await db.detections.update_one(
            {"id": detection["id"]},
            {"$set": {
                "status": "NEUTRALIZED",
                "kill_chain_stage": "DEFEAT",
                "kill_chain_index": len(KILL_CHAIN) - 1,
                "cema_stage": "EXPLOIT",
                "cema_stage_index": len(CEMA_STAGES) - 1,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "last_payload": spec.name,
            }},
        )
    elif body.broadcast:
        await db.detections.update_many(
            {"status": "ACTIVE"},
            {"$set": {"status": "NEUTRALIZED",
                      "kill_chain_stage": "DEFEAT",
                      "kill_chain_index": len(KILL_CHAIN) - 1,
                      "last_payload": spec.name}},
        )

    return pkt


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
        "last_seen": {"$gt": since.isoformat()},
    })

    # Run the same lazy staleness expiry the detections list uses, so this
    # health tile's "active_targets" count doesn't disagree with the
    # dashboard's Active Contacts count.
    await _expire_stale_detections()
    active_targets = await db.detections.count_documents({"status": "ACTIVE"})
    total_packets = await db.mav_packets.count_documents({})

    return {
        "backend": True,
        "mongo": mongo_ok,
        "hackrf": hackrf_live,
        "sik_radio": sik_count > 0,
        "ws_clients": len(ws_manager.clients),
        "active_targets": active_targets,
        "total_packets_tx": total_packets,
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
