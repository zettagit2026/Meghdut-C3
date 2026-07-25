#!/usr/bin/env python3
"""IFF LoRa beacon RX/verify bridge -- task #60.

RX + VERIFY ONLY. This script never transmits anything, on LoRa or any
other radio. It stays in the same "passive/defensive" risk category as
hackrf_rx.py, kismet_bridge.py, mavlink_sniffer.py, droneid_decode_bridge.py,
remoteid_decode_bridge.py -- NOT the TX-capable jam/spoof bridges
(hackrf_jam.py, sik_mavlink_bridge.py), even though, unlike a pure passive
RX bridge, it does perform active cryptographic verification (HMAC compare)
on what it receives. Verifying a signal you received is not transmitting.

=============================================================================
WHAT THIS IS
=============================================================================
Friendly assets (drones, ground units, vehicles) provisioned pre-mission
with this project's IFF scheme (see iff_crypto.py for the full, concrete
cryptographic construction -- HKDF-derived per-asset keys, HMAC-SHA256 over
asset_id/mission_id/timestamp_slot/geocell/counter, replay-window defense)
transmit a 23-byte beacon frame over LoRa every INTERVAL_S seconds. This
script is this system's receive side: it listens for those frames, verifies
them (iff_crypto.verify_frame() + a local replay cache), and on a
successful verification, posts a "friendly asset seen" record to the
backend (POST /api/iff/beacons/ingest -- see backend/server.py), which
feeds BOTH:
  (a) detection-suppression: the general RF-detection pipeline
      (detection_ingest() in backend/server.py) checks recent IFF-verified
      friendlies before finalizing a new detection's threat_level, so a
      friendly asset's own RF emissions are not classified as a hostile
      contact purely because it also happens to radiate energy the
      RSSI/ML heuristics would otherwise flag.
  (b) GNSS-spoofing/jamming attestation (task #103, in parallel design):
      GET /api/iff/friendlies exposes the current "known friendly assets
      in range" roster (asset_id, callsign, last_seen_ts, bearing_deg,
      distance_m, freshness) that #103's authorization gate is meant to
      check against BEFORE authorizing a spoof/jam effect whose footprint
      would cover a still-fresh friendly beacon. See INTEGRATION CONTRACT
      below for the concrete function #103's code should call.

=============================================================================
HARDWARE STATUS (READ BEFORE ASSUMING THIS RECEIVES ANYTHING)
=============================================================================
HARDWARE-BLOCKED, same pattern as canopen_parser.py/dronecan_parser.py: no
LoRa transceiver module exists on this project's hardware yet, on either
side (the friendly-asset TX side, or this receiver's RX side). A LoRa
receiver here would need a real SX1276/RFM95-class module (SPI, ~$10-20,
e.g. Adafruit RFM95W LoRa Radio Breakout or a HopeRF RFM95W board) wired to
whatever host runs this script, plus a driver/library (e.g. pySX127x or a
thin pyserial link to an ESP32/RFM95 front-end that hands frames up over
USB-serial -- SkySweep32's ESP32+CC1101 hardware, already evaluated
elsewhere in this project and flagged GPLv3, is a DIFFERENT chip (CC1101,
sub-GHz OOK/FSK, not a LoRa chirp-spread-spectrum radio) and was not reused
here for that reason -- no real LoRa reference hardware/library exists in
this project's ecosystem to build against).

Consequently `run_live()` below is a clearly-marked stub: it prints exactly
this limitation and exits non-zero rather than pretending to listen on a
serial port that doesn't exist or fabricating any "beacon received" output.
Everything else in this file -- frame verification, the replay cache, the
backend POST -- is real code, exercised end-to-end in --self-test mode
against synthetic-but-cryptographically-real frames built with
iff_crypto.build_frame() using the exact same HMAC construction a real
asset would use. See test_iff_beacon_bridge.py.

When LoRa hardware exists: `run_live()` needs to be filled in with an actual
read loop (pyserial read from an ESP32/RFM95 USB-serial bridge, or a pySX127x
SPI receive callback) that hands each received FRAME_LEN-byte payload to
`handle_frame()` -- that function is already real and already tested.

=============================================================================
HARDENING ADDED ON TOP OF TASK #60: REVOCATION + CHALLENGE-RESPONSE
=============================================================================
Two gaps found by a Security Architect's gap analysis (against the Army's
"continuous authentication -- zero trust architecture" requirement wording
and against how real Mode 4/5 IFF actually works) are addressed here:

  1. Per-asset revocation: AssetRegistry now carries a revoked_asset_ids
     set (loadable from the same JSON registry file). handle_frame()
     rejects a revoked asset's frame with a distinct "revoked: ..." reason
     even when its HMAC verifies -- so a single captured asset can be
     locked out without rotating the whole mission's master secret and
     punishing every other friendly asset.

  2. Challenge-response (see iff_challenge.py for the full construction and
     rationale): the one-way periodic beacon this file was built around
     cannot fully defeat a jam-and-instantly-relay attack within its
     timing-skew window -- stated plainly in iff_crypto.py's REPLAY DEFENSE
     section. NonceStore + build_and_track_challenge()/handle_challenge()/
     handle_response() below add an interrogate-on-demand path: a fresh,
     single-use nonce per query closes that window entirely, matching how
     real Mode 4/5 interrogation works. This is ADDITIVE -- the one-way
     beacon and handle_frame() are unchanged in behavior and remain a valid
     lower-power/lower-latency secondary "is this asset roughly still
     around" signal; challenge-response is the recommended PRIMARY path
     when a positive, current, wormhole-resistant answer is actually
     needed (e.g. immediately before authorizing an RF effect near a
     claimed friendly position). Both modes share the same per-asset key
     derivation (iff_crypto.derive_asset_secret()) and the same
     AssetRegistry/mission provisioning -- no separate secret material.

     Same hardware caveat as everything else in this file: this needs a
     BIDIRECTIONAL LoRa link (both sides can transmit AND receive), which
     does not exist on this project's hardware any more than the one-way
     link does. NonceStore/handle_challenge()/handle_response() are real,
     tested logic (see test_iff_challenge.py, test_iff_beacon_bridge.py)
     exercised only against synthetic-but-cryptographically-real frames --
     NOT validated over any real RF link. Do not treat this as hardware-
     validated; it isn't.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import requests

import iff_challenge
import iff_crypto as iff

BRIDGE_NAME = "iff_beacon_bridge"

# Bounded per-asset replay cache: (asset_id) -> deque of (timestamp_slot, counter)
# seen recently. Bounded so a compromised/malfunctioning asset spamming frames
# cannot grow this without limit; old entries fall out of the window on their
# own since verify_frame() already rejects anything outside MAX_SLOT_SKEW.
_REPLAY_CACHE_MAXLEN = 64


class ReplayCache:
    """Per-asset bounded cache of (timestamp_slot, counter) pairs already
    accepted, so an exact retransmission of a previously-accepted frame is
    rejected even though its HMAC tag is (correctly) still valid -- see
    iff_crypto.py's REPLAY DEFENSE section for what this does and does not
    protect against."""

    def __init__(self, maxlen: int = _REPLAY_CACHE_MAXLEN):
        self._maxlen = maxlen
        self._seen: Dict[int, Deque[Tuple[int, int]]] = {}

    def seen_before(self, asset_id: int, slot: int, counter: int) -> bool:
        dq = self._seen.get(asset_id)
        return dq is not None and (slot, counter) in dq

    def record(self, asset_id: int, slot: int, counter: int) -> None:
        dq = self._seen.setdefault(asset_id, deque(maxlen=self._maxlen))
        dq.append((slot, counter))


class NonceStore:
    """Interrogator-side bounded store of outstanding challenge nonces --
    the stateful half of iff_challenge.py's challenge-response scheme (that
    module is deliberately pure/stateless, same split as iff_crypto.py vs.
    this file). A nonce is "outstanding" from the moment a challenge is
    issued (issue()) until it is consumed exactly once, either by a matching
    response (consume(), whether or not that response ultimately verifies)
    or by expiring (checked lazily on consume(); a background sweep is not
    needed since a bounded maxlen already caps memory use even if nothing
    ever expires it).

    This is what actually closes the relay/wormhole window described in
    iff_challenge.py's module docstring: a captured-and-replayed response is
    rejected here (nonce no longer outstanding) even if its HMAC tag is
    (correctly) still valid for some now-already-answered nonce.
    """

    def __init__(self, ttl_s: int = iff_challenge.NONCE_TTL_S, maxlen: int = 256):
        self._ttl_s = ttl_s
        self._maxlen = maxlen
        # nonce (bytes) -> (asset_id, mission_id, issued_time)
        self._outstanding: Dict[bytes, Tuple[int, int, float]] = {}

    def issue(self, nonce: bytes, asset_id: int, mission_id: int,
              issued_time: Optional[float] = None) -> None:
        if len(self._outstanding) >= self._maxlen:
            # Drop the oldest entry rather than grow unbounded -- a burst of
            # unanswered challenges should not let this store leak memory.
            oldest = min(self._outstanding, key=lambda k: self._outstanding[k][2])
            self._outstanding.pop(oldest, None)
        self._outstanding[nonce] = (asset_id, mission_id,
                                     time.time() if issued_time is None else issued_time)

    def consume(self, nonce: bytes, now: Optional[float] = None) -> Optional[Tuple[int, int]]:
        """Pop and return (asset_id, mission_id) for `nonce` if it is
        currently outstanding and not expired; else return None (and, if it
        WAS present but expired, remove it anyway -- an expired nonce is
        never answerable, consumed or not). A nonce is removed on the very
        first call regardless of outcome -- this is single-use by
        construction, the core defense against replaying a captured
        response."""
        entry = self._outstanding.pop(nonce, None)
        if entry is None:
            return None
        asset_id, mission_id, issued_time = entry
        now = time.time() if now is None else now
        if now - issued_time > self._ttl_s:
            return None
        return asset_id, mission_id


def build_and_track_challenge(store: NonceStore, mission_master_secret: bytes,
                               mission_id: int, asset_id: int) -> bytes:
    """Interrogator side: build a fresh challenge for `asset_id` and record
    its nonce as outstanding in `store`. Returns the raw wire frame to
    transmit (over whatever the eventual bidirectional LoRa link is)."""
    raw, nonce = iff_challenge.build_challenge(mission_master_secret, mission_id, asset_id)
    store.issue(nonce, asset_id, mission_id)
    return raw


def handle_challenge(raw: bytes, registry: AssetRegistry, own_asset_id: int) -> dict:
    """Asset side: verify an incoming challenge is genuinely from someone
    holding the mission secret, targets this asset, and is fresh, THEN
    check revocation (a revoked asset must not answer challenges either --
    same "even if crypto is valid" reasoning as handle_frame() above), and
    if all of that passes, build the response frame to transmit back.
    Returns a result dict; "ok": True carries "response_frame" (raw bytes
    ready to transmit), "ok": False carries "reason"."""
    if registry.is_revoked(own_asset_id):
        return {"ok": False, "reason": f"revoked: asset_id {own_asset_id} will not answer "
                                        f"challenges (asset has been revoked)"}
    try:
        frame = iff_challenge.verify_challenge(
            raw, registry.mission_master_secret, registry.mission_id, own_asset_id)
    except iff_challenge.IFFChallengeError as e:
        return {"ok": False, "reason": str(e)}

    asset_secret = iff.derive_asset_secret(registry.mission_master_secret, frame.mission_id,
                                            own_asset_id)
    response = iff_challenge.build_response(asset_secret, own_asset_id, frame.mission_id,
                                             frame.nonce)
    return {"ok": True, "response_frame": response}


def handle_response(raw: bytes, registry: AssetRegistry, store: NonceStore,
                     now: Optional[float] = None) -> dict:
    """Interrogator side: given a raw response frame and the NonceStore that
    tracked the outstanding challenge, consume the claimed nonce (exactly
    once, whatever the outcome) and verify. Returns a result dict shaped
    like handle_frame()'s, plus "reason" values distinguishing an unknown/
    already-consumed/expired nonce from a cryptographic failure, and a
    revocation check identical in spirit to handle_frame()'s."""
    now = time.time() if now is None else now
    try:
        pre = iff_challenge.parse_response(raw)
    except iff_challenge.IFFChallengeError as e:
        return {"ok": False, "reason": str(e)}

    consumed = store.consume(pre.nonce, now=now)
    if consumed is None:
        return {"ok": False,
                "reason": "nonce not outstanding -- unknown, already-consumed, or expired "
                          "(this is what rejects a replayed/relayed response)"}
    expected_asset_id, expected_mission_id = consumed

    if registry.is_revoked(pre.asset_id):
        return {"ok": False,
                "reason": f"revoked: asset_id {pre.asset_id} has been revoked "
                          f"and is no longer trusted (HMAC tag was valid)"}

    try:
        frame = iff_challenge.verify_response(
            raw, registry.mission_master_secret, expected_mission_id, expected_asset_id,
            pre.nonce)
    except iff_challenge.IFFChallengeError as e:
        return {"ok": False, "reason": str(e)}

    callsign = registry.callsign_for(frame.asset_id) or f"unknown-asset-{frame.asset_id}"
    return {
        "ok": True,
        "asset_id": frame.asset_id,
        "callsign": callsign,
        "mission_id": frame.mission_id,
        "verified_at": now,
        "mode": "challenge-response",
    }


class AssetRegistry:
    """Loads the out-of-band-provisioned mission secret + asset roster from a
    local JSON file. This file is deliberately NOT committed to git (see
    .gitignore entry added alongside this bridge) -- it contains
    MISSION_MASTER_SECRET, which must only ever move between the receiver
    and each asset via a physical/secure out-of-band channel, never over
    the network this codebase serves.

    Expected format:
        {
          "mission_id": 4660,
          "mission_master_secret_hex": "a1b2c3...  (32+ bytes hex)",
          "assets": {
            "66": "Falcon-1",
            "67": "Ground-Relay-A"
          },
          "revoked_asset_ids": [67]
        }

    revoked_asset_ids (per-asset revocation, hardening added on top of task
    #60): a physical asset that is captured, lost, or otherwise suspected
    compromised no longer needs the WHOLE mission's master secret rotated
    (which would also lock out every other still-trusted asset) -- it can
    instead be listed here. handle_frame() below rejects a frame from a
    revoked asset_id with a distinct "revoked" reason EVEN IF its HMAC tag
    verifies correctly, since the tag alone only proves the frame was built
    with that asset's correctly-derived key, not that the asset (or whoever
    now holds it) is still trusted. Operator workflow: edit this field in
    the registry file (out-of-band, same channel as the rest of this file)
    and/or call revoke()/unrevoke() on a live AssetRegistry instance (e.g.
    the backend-driven revoke/unrevoke endpoints in backend/server.py mirror
    this same intent server-side against already-ingested beacons; see that
    file's iff_revoke_asset()/iff_unrevoke_asset()). This registry's
    in-memory set is NOT automatically synced with the backend's revocation
    list today -- reloading this bridge process's registry file (or calling
    revoke() directly) is the mechanism until/unless a poll-the-backend
    sync loop is added; stated as a real limitation, not silently assumed.
    """

    def __init__(self, mission_id: int, mission_master_secret: bytes,
                 callsigns: Dict[int, str],
                 revoked_asset_ids: Optional[set] = None):
        self.mission_id = mission_id
        self.mission_master_secret = mission_master_secret
        self.callsigns = callsigns  # asset_id -> human callsign, roster only, not secret
        self.revoked_asset_ids: set = set(revoked_asset_ids or ())

    @classmethod
    def load(cls, path: str) -> "AssetRegistry":
        with open(path, "r") as f:
            data = json.load(f)
        secret = bytes.fromhex(data["mission_master_secret_hex"])
        callsigns = {int(k): v for k, v in data.get("assets", {}).items()}
        revoked = {int(a) for a in data.get("revoked_asset_ids", [])}
        return cls(mission_id=int(data["mission_id"]), mission_master_secret=secret,
                   callsigns=callsigns, revoked_asset_ids=revoked)

    def callsign_for(self, asset_id: int) -> Optional[str]:
        return self.callsigns.get(asset_id)

    def is_revoked(self, asset_id: int) -> bool:
        return asset_id in self.revoked_asset_ids

    def revoke(self, asset_id: int) -> None:
        """Revoke a single asset in-memory (does not rewrite the registry
        file). Callers that persist revocations across restarts should also
        write revoked_asset_ids back into the JSON file themselves."""
        self.revoked_asset_ids.add(asset_id)

    def unrevoke(self, asset_id: int) -> None:
        self.revoked_asset_ids.discard(asset_id)


def handle_frame(raw: bytes, registry: AssetRegistry, replay_cache: ReplayCache,
                  now: Optional[float] = None) -> dict:
    """Verify one raw LoRa payload. Returns a result dict; never raises for a
    normal verification failure (that's an expected outcome, not a bug) --
    only structurally-impossible input (wrong type) would raise.

    Result dict always has "ok": bool. On ok=True it additionally has
    asset_id, callsign, mission_id, geocell, distance-relevant fields left
    for the caller to fill in from whatever RF metadata (RSSI/bearing) the
    real hardware layer would provide alongside the payload once it exists.
    """
    now = time.time() if now is None else now
    now_slot = int(now // iff.INTERVAL_S)
    try:
        frame = iff.verify_frame(raw, registry.mission_master_secret,
                                  registry.mission_id, now_slot=now_slot)
    except iff.IFFVerifyError as e:
        return {"ok": False, "reason": str(e)}

    # Per-asset revocation check (hardening on top of task #60): deliberately
    # AFTER the HMAC/slot verification above, so the rejection reason below
    # is distinct from "bad HMAC" -- this frame's crypto is genuinely valid,
    # the asset itself is simply no longer trusted (captured/compromised),
    # without requiring a mission-wide master-secret rotation that would
    # also lock out every other still-good asset. See AssetRegistry's
    # revoked_asset_ids docstring above for the operator workflow.
    if registry.is_revoked(frame.asset_id):
        return {"ok": False,
                "reason": f"revoked: asset_id {frame.asset_id} has been revoked "
                          f"and is no longer trusted (HMAC tag was valid)"}

    if replay_cache.seen_before(frame.asset_id, frame.timestamp_slot, frame.counter):
        return {"ok": False, "reason": "replay: (timestamp_slot, counter) already accepted"}
    replay_cache.record(frame.asset_id, frame.timestamp_slot, frame.counter)

    callsign = registry.callsign_for(frame.asset_id)
    if callsign is None:
        # Tag verified (so this asset_id/mission share a valid derivation --
        # it IS provisioned with a correct key), but it's not on the local
        # roster file. Treat as ok=True but flag it: a correctly-provisioned
        # asset simply missing from this receiver's roster copy is a real
        # possibility (roster sync lag), not proof of hostility, but should
        # not be silently treated identically to a known, named asset.
        callsign = f"unknown-asset-{frame.asset_id}"

    return {
        "ok": True,
        "asset_id": frame.asset_id,
        "callsign": callsign,
        "mission_id": frame.mission_id,
        "geocell": frame.geocell,
        "geocell_known": frame.geocell != iff.GEOCELL_UNKNOWN,
        "timestamp_slot": frame.timestamp_slot,
        "counter": frame.counter,
        "verified_at": now,
    }


# =====================================================================
# Backend integration (auth pattern copied from hackrf_rx.py's
# login()/_post_with_reauth() -- see that file's docstring for why the
# reauth-on-401 behavior exists; duplicated here rather than imported so
# this bridge has no import-time dependency on hackrf_rx.py needing a
# HackRF device to be present).
# =====================================================================

def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                       json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5) -> "requests.Response":
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", BRIDGE_NAME)
    r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        print(f"[auth] 401 from POST {path} -- token expired, re-authenticating as {email}",
              file=sys.stderr)
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[auth] re-login failed ({e}) -- leaving stale token in place",
                  file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
        if r.status_code == 401:
            print(f"[auth] still 401 for POST {path} after re-authenticating -- real auth "
                  f"problem, check credentials for {email}", file=sys.stderr)
    return r


def post_verified_beacon(console_url: str, headers: dict, email: str, password: str,
                          result: dict, bearing_deg: Optional[float] = None,
                          distance_m: Optional[float] = None) -> None:
    """POST a verified beacon to /api/iff/beacons/ingest (see backend/server.py).

    bearing_deg/distance_m are left optional/None here because a one-way LoRa
    receiver with a single omni antenna has no bearing information of its own
    -- if/when this is paired with a directional antenna or an SDR-based
    angle-of-arrival estimate, those would be filled in from that real
    measurement, never fabricated.
    """
    body = {
        "asset_id": result["asset_id"],
        "callsign": result["callsign"],
        "mission_id": result["mission_id"],
        "geocell": result["geocell"],
        "geocell_known": result["geocell_known"],
        "bearing_deg": bearing_deg,
        "distance_m": distance_m,
    }
    r = _post_with_reauth(console_url, "/api/iff/beacons/ingest", body, headers, email, password)
    if r.status_code >= 300:
        print(f"[ingest] iff beacon POST failed: {r.status_code} {r.text[:200]}",
              file=sys.stderr)


# =====================================================================
# Live RX -- HARDWARE-BLOCKED, see module docstring.
# =====================================================================

def run_live(*_args, **_kwargs) -> int:
    print(
        "iff_beacon_bridge.py: no LoRa receive hardware is wired up yet -- "
        "this needs a real SX1276/RFM95-class LoRa module (SPI, e.g. Adafruit "
        "RFM95W or a HopeRF RFM95W board, ~$10-20) plus a driver "
        "(e.g. pySX127x, or an ESP32 front-end handing frames up over "
        "USB-serial). Nothing is fabricated here -- see this module's "
        "HARDWARE STATUS docstring section. Use --self-test to exercise the "
        "real verification logic against synthetic-but-cryptographically-"
        "real frames, or --verify-hex to check one captured frame offline.",
        file=sys.stderr,
    )
    return 1


# =====================================================================
# Self-test: build synthetic (but cryptographically real) beacons with
# iff_crypto.build_frame() using an in-memory registry, and confirm the full
# handle_frame() pipeline (verify + replay cache + roster lookup) behaves
# correctly -- exercises everything except the actual radio.
# =====================================================================

def self_test() -> None:
    import secrets as _secrets

    mission_id = 0xBEEF
    master_secret = _secrets.token_bytes(32)
    asset_id = 501
    registry = AssetRegistry(mission_id=mission_id, mission_master_secret=master_secret,
                              callsigns={asset_id: "Falcon-1"})
    replay_cache = ReplayCache()

    asset_secret = iff.derive_asset_secret(master_secret, mission_id, asset_id)
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    geocell = iff.quantize_geocell(28.6139, 77.2090)

    checks = []

    # 1. A genuine, fresh beacon verifies and is recognized by roster.
    frame1 = iff.build_frame(asset_secret, asset_id, mission_id, now_slot, geocell, counter=0)
    r1 = handle_frame(frame1, registry, replay_cache, now=now)
    checks.append(("fresh valid beacon accepted", r1["ok"] is True and r1["callsign"] == "Falcon-1"))

    # 2. Exact retransmission of the same frame is rejected as a replay.
    r2 = handle_frame(frame1, registry, replay_cache, now=now + 1)
    checks.append(("exact replay rejected", r2["ok"] is False and "replay" in r2["reason"]))

    # 3. A new beacon (next counter, same slot) from the same asset is accepted.
    frame2 = iff.build_frame(asset_secret, asset_id, mission_id, now_slot, geocell, counter=1)
    r3 = handle_frame(frame2, registry, replay_cache, now=now + 1)
    checks.append(("new counter same slot accepted", r3["ok"] is True))

    # 4. A forged frame (wrong secret) is rejected.
    forged_secret = iff.derive_asset_secret(_secrets.token_bytes(32), mission_id, asset_id)
    forged = iff.build_frame(forged_secret, asset_id, mission_id, now_slot, geocell, counter=9)
    r4 = handle_frame(forged, registry, replay_cache, now=now)
    checks.append(("forged frame rejected", r4["ok"] is False and "HMAC" in r4["reason"]))

    # 5. A stale frame (way outside the slot-skew window) is rejected.
    stale = iff.build_frame(asset_secret, asset_id, mission_id, now_slot - 100, geocell, counter=0)
    r5 = handle_frame(stale, registry, replay_cache, now=now)
    checks.append(("stale frame rejected", r5["ok"] is False and "tolerance" in r5["reason"]))

    # 6. An unrostered-but-correctly-signed asset is accepted but flagged unknown.
    other_asset_id = 999
    other_secret = iff.derive_asset_secret(master_secret, mission_id, other_asset_id)
    other_frame = iff.build_frame(other_secret, other_asset_id, mission_id, now_slot,
                                   iff.GEOCELL_UNKNOWN, counter=0)
    r6 = handle_frame(other_frame, registry, replay_cache, now=now)
    checks.append(("unrostered-but-valid asset flagged", r6["ok"] is True
                   and r6["callsign"] == "unknown-asset-999"))

    # 7. Revoked asset rejected even though HMAC is valid, distinct reason.
    registry.revoke(asset_id)
    frame3 = iff.build_frame(asset_secret, asset_id, mission_id, now_slot, geocell, counter=2)
    r7 = handle_frame(frame3, registry, replay_cache, now=now)
    checks.append(("revoked asset rejected distinctly", r7["ok"] is False
                   and "revoked" in r7["reason"] and "mismatch" not in r7["reason"]))
    registry.unrevoke(asset_id)

    # ---- Challenge-response (hardening on top of task #60) ----
    store = NonceStore()

    # 8. Genuine challenge -> genuine response round trip succeeds.
    challenge = build_and_track_challenge(store, master_secret, mission_id, asset_id)
    resp_result = handle_challenge(challenge, registry, own_asset_id=asset_id)
    r8a = resp_result["ok"] is True
    ir8 = handle_response(resp_result.get("response_frame", b""), registry, store, now=now) \
        if r8a else {"ok": False}
    checks.append(("challenge-response round trip succeeds", r8a and ir8["ok"] is True))

    # 9. Replaying an already-consumed nonce's response is rejected.
    r9 = handle_response(resp_result["response_frame"], registry, store, now=now)
    checks.append(("replayed/consumed nonce rejected", r9["ok"] is False
                   and "nonce" in r9["reason"]))

    # 10. An expired (stale) nonce is rejected.
    challenge2 = build_and_track_challenge(store, master_secret, mission_id, asset_id)
    resp2 = handle_challenge(challenge2, registry, own_asset_id=asset_id)
    r10 = handle_response(resp2["response_frame"], registry, store,
                          now=now + iff_challenge.NONCE_TTL_S + 1)
    checks.append(("expired nonce rejected", r10["ok"] is False
                   and "nonce" in r10["reason"]))

    # 11. A response claiming the wrong asset_id (wrong secret) is rejected.
    challenge3 = build_and_track_challenge(store, master_secret, mission_id, asset_id)
    other_secret2 = iff.derive_asset_secret(master_secret, mission_id, 777)
    frame_parsed = iff_challenge.parse_challenge(challenge3)
    forged_response = iff_challenge.build_response(other_secret2, asset_id, mission_id,
                                                     frame_parsed.nonce)
    r11 = handle_response(forged_response, registry, store, now=now)
    checks.append(("wrong-asset-secret response rejected", r11["ok"] is False))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}: {name}")
    if failed:
        print(f"\nSELF-TEST FAILED: {failed}", file=sys.stderr)
        sys.exit(1)
    print(f"\nSELF-TEST PASSED: {len(checks)}/{len(checks)} checks")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="Run the synthetic end-to-end verification self-test "
                          "(no hardware, no network) and exit.")
    ap.add_argument("--verify-hex", metavar="HEX",
                     help="Verify a single 23-byte captured frame given as hex, "
                          "using --registry, and print the result. Offline "
                          "utility -- does not touch any live radio or the backend.")
    ap.add_argument("--registry", default=os.environ.get("IFF_ASSET_REGISTRY"),
                     help="Path to the local asset registry JSON (see AssetRegistry "
                          "docstring). Also settable via IFF_ASSET_REGISTRY.")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.verify_hex:
        if not args.registry:
            print("--verify-hex requires --registry/IFF_ASSET_REGISTRY", file=sys.stderr)
            return 1
        registry = AssetRegistry.load(args.registry)
        raw = bytes.fromhex(args.verify_hex)
        result = handle_frame(raw, registry, ReplayCache())
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    return run_live()


if __name__ == "__main__":
    sys.exit(main())
