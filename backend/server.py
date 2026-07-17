"""CEMA-Enabled cUAS backend.

Endpoints (all under /api):
  auth: /login /logout /me
  detections: /detections /detections/simulate /detections/upload /detections/{id}/cema-advance /detections/{id}/killchain-advance /detections/{id}
  spectrum: /spectrum/waterfall
  mavlink: /mavlink/craft /mavlink/broadcast /mavlink/packets  (ws /ws/mavlink)
  payloads: /payloads /payloads/deploy
  logs:  /logs
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
    File,
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
from simulator import (
    CEMA_STAGES,
    KILL_CHAIN,
    advance_cema,
    advance_kill_chain,
    generate_waterfall,
    new_detection,
    parse_iq_file_stub,
)

# ---------- Config ----------
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@cema.mil")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "cema@2026")

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
            "role": "admin",
            "clearance": "RESTRICTED",
            "password_hash": hash_password(ADMIN_PASSWORD),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Seeded admin operator.")
    elif not verify_password(ADMIN_PASSWORD, existing["password_hash"]):
        await db.users.update_one(
            {"email": ADMIN_EMAIL},
            {"$set": {"password_hash": hash_password(ADMIN_PASSWORD)}},
        )
        logger.info("Admin password hash refreshed.")

    # Seed initial fleet of detections (idempotent-ish; only if empty)
    count = await db.detections.count_documents({})
    if count == 0:
        for _ in range(6):
            det = new_detection()
            await db.detections.insert_one(det)
        logger.info("Seeded initial detections.")


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


class DeployPayloadBody(BaseModel):
    payload_id: str
    target_detection_id: Optional[str] = None
    broadcast: bool = False


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
@api.get("/detections")
async def list_detections(user: Dict = Depends(get_current_user)):
    docs = await db.detections.find({}, {"_id": 0}).to_list(500)
    return docs


@api.get("/detections/{det_id}")
async def get_detection(det_id: str, user: Dict = Depends(get_current_user)):
    doc = await db.detections.find_one({"id": det_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Detection not found")
    return doc


@api.post("/detections/simulate")
async def simulate_detection(user: Dict = Depends(get_current_user)):
    det = new_detection()
    await db.detections.insert_one(det.copy())
    await log_event("DETECTION",
                    f"New contact {det['callsign']} ({det['model']}) — {det['threat_level']}",
                    meta={"detection_id": det["id"]}, actor=user["email"])
    det.pop("_id", None)
    return det


@api.post("/detections/upload")
async def upload_iq(file: UploadFile = File(...),
                    user: Dict = Depends(get_current_user)):
    contents = await file.read()
    meta = parse_iq_file_stub(file.filename or "capture.iq", len(contents))
    det = new_detection()
    det["source"] = "UPLOAD"
    det["upload_meta"] = meta
    det["upload_filename"] = file.filename
    det["upload_size_bytes"] = len(contents)
    await db.detections.insert_one(det.copy())
    await log_event(
        "UPLOAD",
        f"IQ/pcap ingested: {file.filename} ({len(contents)} bytes) → contact {det['callsign']}",
        meta={"detection_id": det["id"], **meta},
        actor=user["email"],
    )
    det.pop("_id", None)
    return det


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
    # real hackrf_sweep cadence is slower than a 10s cutoff allowed for);
    # otherwise fall back to the simulator.
    ing = _last_spectrum_ingest
    if ing and (datetime.now(timezone.utc) - ing["ts"]).total_seconds() < 30:
        return {"bins": ing["bins"], "rows": ing["rows"], "source": "HACKRF"}
    return {"bins": bins,
            "rows": [generate_waterfall(bins) for _ in range(rows)],
            "source": "SIM"}


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
    altitude_m: float = 0.0
    speed_ms: float = 0.0
    system_id: int = 1
    component_id: int = 1
    encrypted: bool = False
    source: str = "HACKRF"


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
            "altitude_m": body.altitude_m,
            "speed_ms": body.speed_ms,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        await db.detections.update_one({"id": existing["id"]}, {"$set": updates})
        det = {**existing, **updates}
        det.pop("_id", None)
        return det

    det = new_detection()  # gives a sane skeleton with id/uuid + timestamps
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
        "altitude_m": body.altitude_m,
        "speed_ms": body.speed_ms,
        "system_id": body.system_id,
        "component_id": body.component_id,
        "encrypted": body.encrypted,
        "source": body.source,
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
async def broadcast_packet(body: MavlinkCraftBody, user: Dict = Depends(get_current_user)):
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
                         user: Dict = Depends(get_current_user)):
    spec = get_payload_by_id(body.payload_id)
    if not spec:
        raise HTTPException(404, "Unknown payload id")
    builder = PAYLOAD_BUILDERS.get(body.payload_id)
    if not builder:
        raise HTTPException(500, "No builder registered for this payload")

    target_sys = 0
    target_comp = 0
    detection = None
    if not body.broadcast:
        if not body.target_detection_id:
            raise HTTPException(400, "target_detection_id required unless broadcast=True")
        detection = await db.detections.find_one({"id": body.target_detection_id})
        if not detection:
            raise HTTPException(404, "Target detection not found")
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
# Routes: Emergency abort — halt all transmissions, mark ceasefire
# =====================================================================
@api.post("/emergency/abort")
async def emergency_abort(user: Dict = Depends(get_current_user)):
    # Broadcast a ceasefire signal to any listening TX bridge.
    await ws_manager.broadcast_json({
        "type": "abort",
        "ts": datetime.now(timezone.utc).isoformat(),
        "operator": user["email"],
    })
    await log_event("ABORT",
                    "EMERGENCY ABORT — all TX halted by operator",
                    actor=user["email"])
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


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

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
