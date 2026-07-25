#!/usr/bin/env python3
"""IFF (Identification Friend-or-Foe) LoRa beacon: framing + HMAC construction.

PURE CRYPTO/FRAMING MODULE -- no radio I/O, no network I/O, no hardware
dependency of any kind. This is deliberately separated from
iff_beacon_bridge.py (the RX/verify bridge that talks to LoRa hardware and
the backend) so that the cryptographic construction can be built, tested,
and verified with real test vectors TODAY, independent of LoRa module
procurement (see iff_beacon_bridge.py docstring, HARDWARE STATUS section).

=============================================================================
WHY THIS EXISTS (task #103 fratricide-mitigation dependency)
=============================================================================
Task #103 (GNSS/GPS spoofing) and the still-deferred CRSF/ELRS/DJI
control-link injection line of work both let an operator aim an aggressive,
broadcast-radius RF effect (a fake GNSS constellation, a jammer, in the
control-link case, an injected command) at "the hostile drone" identified by
a bearing/frequency from the RF-heuristic detection pipeline
(field-bridge/hackrf_rx.py, ml_classify_bridge.py, gamutrf_backend_adapter.py).
None of those detectors have any concept of "friendly" -- they infer
"something is transmitting energy in this band, in this direction" and nothing
more. A friendly asset (recon drone, relay, ground unit) sitting in or near
the effect's footprint has no way today to say "that's me, don't." This
module is the cryptographic core of that "that's me" signal: a periodic LoRa
beacon a friendly asset transmits, verifiable by this system's receive side,
that task #103's attestation gate (and the general detection pipeline) can
check against BEFORE authorizing or auto-classifying an effect/detection.

=============================================================================
THE CONCRETE CONSTRUCTION (no hand-waving -- this is the actual scheme)
=============================================================================
Per-mission provisioning (done ONCE, out-of-band, before the mission, e.g.
loading a value onto each asset's LoRa module and onto this system's registry
file via a secure USB/QR/direct-connect step -- never over the air):

  - MISSION_MASTER_SECRET: 32 random bytes, generated once per mission
    (`secrets.token_bytes(32)`), shared only between the receiver's asset
    registry and the assets themselves. Never transmitted.
  - MISSION_ID: a 16-bit mission identifier, also provisioned out-of-band.
  - Each friendly asset gets a 32-bit ASSET_ID (a plain roster number, not
    secret) and its own per-asset key derived (never transmitted, never
    reused across missions) via HKDF-SHA256:

        asset_secret = HKDF-SHA256(
            ikm  = MISSION_MASTER_SECRET,
            salt = mission_id (2 bytes, big-endian),
            info = b"IFF-LoRa-v1|" + asset_id (4 bytes, big-endian),
            length = 32,
        )

    This means the receiver only needs to store ONE mission secret + a
    public roster of asset_ids, and can derive every asset's key on demand
    -- it does not need N separately-provisioned per-asset secrets. Losing
    one asset's device does not expose any other asset's derivation (HKDF's
    `info` binds the derived key to that asset_id only), though it DOES
    expose the mission master secret if the device is captured with it
    extracted -- per-mission secret rotation (a new mission = a new master
    secret) is the mitigation for that, not attempted key-per-asset
    distribution complexity.

  - Beacon transmit interval: every INTERVAL_S seconds (default 30s), the
    asset transmits ONE beacon frame (see wire format below). No key
    material changes between beacons within a mission; what changes, and
    is what actually defeats simple indefinite-replay, is the ENVIRONMENTAL
    ENTROPY folded into every frame's HMAC input:

      1. TIME entropy: `timestamp_slot = unix_time // INTERVAL_S`, a
         monotonically increasing 32-bit counter both sides can compute
         independently from their own (GPS-disciplined, on a drone) clock.
         A captured frame's HMAC is only valid for the slot(s) it names,
         checked against the verifier's own current slot with a small
         tolerance window (see REPLAY DEFENSE below) -- so replaying a
         captured frame is only useful for a short window after capture,
         not indefinitely, unlike a static pre-shared token.
      2. LOCATION entropy: `geocell`, a 16-bit value derived by quantizing
         the asset's own GPS fix to a coarse grid (see quantize_geocell(),
         ~0.01 degree cells, ~1.1km at the equator) and folding lat/lon
         grid indices together. This is "locally observable" entropy in
         the same sense timestamp is: the asset doesn't need any extra
         provisioning to obtain it (it already has GPS, being a drone/
         ground asset), and it changes automatically as the asset moves,
         without any operator action. It is NOT cryptographically load-
         bearing on its own (a 16-bit space is far too small to resist
         brute force alone) -- it is one more input mixed into the same
         real HMAC-SHA256 computation below, so it costs nothing extra to
         include, and it gives the verifier an additional SANITY check
         (not a security guarantee): a verified beacon whose claimed
         geocell doesn't match a plausible position for that asset is
         flagged for operator attention even though the HMAC itself
         checked out. Assets with no GPS fix set geocell = 0xFFFF
         ("unknown"), which is accepted by verification (no location
         cross-check performed) but never treated as-good-as a real fix
         for the friendly-footprint check in the detection/attestation
         integration.

  - Wire format (23 bytes total -- small enough for a single LoRa frame at
    any spreading factor, no fragmentation needed):

        offset  size  field
        0       1     magic        = 0x49 ('I')
        1       1     version      = 0x01
        2       4     asset_id     (uint32, big-endian)
        6       2     mission_id   (uint16, big-endian)
        8       4     timestamp_slot (uint32, big-endian)
        12      2     geocell      (uint16, big-endian; 0xFFFF = unknown)
        14      1     counter      (uint8; increments per beacon within the
                                     same timestamp_slot, wraps at 256; lets
                                     the verifier's replay-cache distinguish
                                     two genuinely-different beacons that
                                     happen to fall in the same slot from a
                                     literal retransmission of one of them)
        15      8     tag          = HMAC-SHA256(asset_secret, bytes[0:15])[:8]

  - Verification (see verify_frame()): the verifier re-derives asset_secret
    for the claimed asset_id via the same HKDF, recomputes the HMAC over
    bytes[0:15], and compares the 8-byte tag using constant-time comparison
    (hmac.compare_digest -- NOT `==`, which would leak timing information
    byte-by-byte). Only THEN are timestamp_slot skew and the replay cache
    checked -- an attacker who cannot compute a valid tag learns nothing
    from a MAC failure regardless of what the other fields say.

=============================================================================
REPLAY DEFENSE (concrete, not hand-waved)
=============================================================================
  1. Slot skew tolerance: MAX_SLOT_SKEW slots either side of the verifier's
     own current timestamp_slot (default 1, i.e. +/- INTERVAL_S seconds,
     accommodating clock drift/GPS-time jitter between asset and receiver).
     A frame outside that window is rejected outright regardless of tag
     validity.
  2. Replay cache: the verifier (iff_beacon_bridge.py, not this module)
     keeps a bounded per-asset set of (timestamp_slot, counter) pairs it has
     already accepted; an exact repeat of a previously-accepted frame within
     the skew window is rejected as a replay even though its tag is valid,
     since a legitimate asset never emits the same (slot, counter) twice.
  3. What this does NOT defend against, stated plainly: an attacker who can
     both jam the real beacon and instantly retransmit ("relay") a captured
     frame from elsewhere within the same +/-INTERVAL_S window would pass
     verification -- this is a standard relay/wormhole attack against any
     interval-based scheme without a challenge-response round trip, which a
     one-way LoRa beacon (by design, for range/simplicity/no reverse-link
     dependency) cannot fully close. Mitigations available if this becomes
     an operational concern: shorten INTERVAL_S, cross-check geocell against
     an independently-observed bearing/RSSI for that asset, or move to a
     challenge-response scheme at the cost of a return RF link. Not
     implemented here; flagged as a known limitation, not silently ignored.

=============================================================================
WHAT WAS ACTUALLY VERIFIED IN THIS SESSION
=============================================================================
Every function in this module (HKDF derivation, frame packing/unpacking,
HMAC construction, verification, replay-window arithmetic, geocell
quantization) is exercised by field-bridge/test_iff_crypto.py against
concrete, computed-and-checked-by-hand test vectors -- real HMAC-SHA256 and
HKDF-SHA256 (via Python's stdlib `hmac`/`hashlib`, the same primitives used
elsewhere in this codebase), no mocked crypto, no fabricated "it would pass"
claims. See that file for the vectors and to reproduce them yourself.
NOT verified: this has never been carried over an actual LoRa radio link --
see iff_beacon_bridge.py's HARDWARE STATUS section.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Optional

MAGIC = 0x49
VERSION = 1
INTERVAL_S = 30
MAX_SLOT_SKEW = 1
TAG_LEN = 8
GEOCELL_UNKNOWN = 0xFFFF

# offset 0..14 (15 bytes) is the HMAC input; offset 15..22 (8 bytes) is the tag.
_HEADER_FMT = ">BBIHIH B"  # magic, version, asset_id, mission_id, ts_slot, geocell, counter
_HEADER_LEN = struct.calcsize(_HEADER_FMT)  # = 15
FRAME_LEN = _HEADER_LEN + TAG_LEN  # = 23

assert _HEADER_LEN == 15, _HEADER_LEN


class IFFVerifyError(ValueError):
    """Raised when a beacon frame fails structural or cryptographic checks."""


@dataclass(frozen=True)
class IFFBeaconFrame:
    asset_id: int
    mission_id: int
    timestamp_slot: int
    geocell: int
    counter: int
    tag: bytes


def derive_asset_secret(mission_master_secret: bytes, mission_id: int, asset_id: int) -> bytes:
    """HKDF-SHA256(ikm=mission_master_secret, salt=mission_id, info=asset_id) -> 32 bytes.

    Real HKDF-SHA256 (RFC 5869), implemented directly on top of stdlib hmac
    (Python has no hkdf in the standard library) -- extract-then-expand,
    single 32-byte output so only one expand round is needed.
    """
    if len(mission_master_secret) < 16:
        raise ValueError("mission_master_secret must be at least 16 bytes")
    salt = struct.pack(">H", mission_id & 0xFFFF)
    info = b"IFF-LoRa-v1|" + struct.pack(">I", asset_id & 0xFFFFFFFF)
    # HKDF-Extract
    prk = hmac.new(salt, mission_master_secret, hashlib.sha256).digest()
    # HKDF-Expand, single block (L=32=hash output length, so T(1) alone suffices)
    t1 = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return t1  # 32 bytes


def quantize_geocell(lat_deg: Optional[float], lon_deg: Optional[float],
                      cell_deg: float = 0.01) -> int:
    """Quantize a GPS fix to a coarse 16-bit grid cell, or GEOCELL_UNKNOWN.

    ~0.01 degree cells are ~1.1km latitude / ~0.7-1.1km longitude depending on
    latitude -- coarse enough that this alone is not a location leak useful
    beyond "roughly which grid square", fine enough to be a real sanity
    signal for the friendly-footprint check in the integration layer.
    """
    if lat_deg is None or lon_deg is None:
        return GEOCELL_UNKNOWN
    lat_idx = int((lat_deg + 90.0) / cell_deg)
    lon_idx = int((lon_deg + 180.0) / cell_deg)
    # Fold two potentially-large indices into 16 bits. Collisions are
    # expected and fine -- this is a sanity check, not a unique identifier.
    cell = (lat_idx * 73856093) ^ (lon_idx * 19349663)
    cell &= 0xFFFE  # keep 0xFFFF reserved for "unknown"
    return cell


def build_frame(asset_secret: bytes, asset_id: int, mission_id: int,
                 timestamp_slot: int, geocell: int, counter: int) -> bytes:
    """Pack + HMAC-tag a beacon frame. Returns the raw FRAME_LEN-byte wire frame."""
    header = struct.pack(_HEADER_FMT, MAGIC, VERSION, asset_id & 0xFFFFFFFF,
                          mission_id & 0xFFFF, timestamp_slot & 0xFFFFFFFF,
                          geocell & 0xFFFF, counter & 0xFF)
    tag = hmac.new(asset_secret, header, hashlib.sha256).digest()[:TAG_LEN]
    return header + tag


def parse_frame(raw: bytes) -> IFFBeaconFrame:
    """Structural parse only -- does NOT verify the HMAC. See verify_frame()."""
    if len(raw) != FRAME_LEN:
        raise IFFVerifyError(f"expected {FRAME_LEN}-byte frame, got {len(raw)}")
    magic, version, asset_id, mission_id, ts_slot, geocell, counter = struct.unpack(
        _HEADER_FMT, raw[:_HEADER_LEN])
    if magic != MAGIC:
        raise IFFVerifyError(f"bad magic byte 0x{magic:02x} (expected 0x{MAGIC:02x})")
    if version != VERSION:
        raise IFFVerifyError(f"unsupported version {version} (expected {VERSION})")
    tag = raw[_HEADER_LEN:_HEADER_LEN + TAG_LEN]
    return IFFBeaconFrame(asset_id=asset_id, mission_id=mission_id,
                           timestamp_slot=ts_slot, geocell=geocell,
                           counter=counter, tag=tag)


def verify_frame(raw: bytes, mission_master_secret: bytes, expected_mission_id: int,
                  now_slot: int, max_slot_skew: int = MAX_SLOT_SKEW) -> IFFBeaconFrame:
    """Full verification: structure, HMAC (constant-time), mission_id, slot skew.

    Does NOT check the replay cache -- that is per-deployment mutable state
    that belongs to the caller (iff_beacon_bridge.py), not this stateless
    crypto module. Raises IFFVerifyError with a specific reason on any
    failure; returns the parsed frame only if every check passes.
    """
    frame = parse_frame(raw)
    if frame.mission_id != expected_mission_id:
        raise IFFVerifyError(
            f"mission_id mismatch: frame={frame.mission_id} expected={expected_mission_id}")

    asset_secret = derive_asset_secret(mission_master_secret, frame.mission_id, frame.asset_id)
    expected = build_frame(asset_secret, frame.asset_id, frame.mission_id,
                            frame.timestamp_slot, frame.geocell, frame.counter)
    expected_tag = expected[_HEADER_LEN:_HEADER_LEN + TAG_LEN]
    if not hmac.compare_digest(expected_tag, frame.tag):
        raise IFFVerifyError("HMAC tag mismatch -- forged, corrupted, or unknown asset/secret")

    skew = frame.timestamp_slot - now_slot
    if abs(skew) > max_slot_skew:
        raise IFFVerifyError(
            f"timestamp_slot out of tolerance: frame_slot={frame.timestamp_slot} "
            f"now_slot={now_slot} skew={skew} max_skew={max_slot_skew}")

    return frame
