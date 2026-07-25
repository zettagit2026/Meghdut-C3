#!/usr/bin/env python3
"""IFF challenge-response (interrogate-on-demand): framing + HMAC construction.

PURE CRYPTO/FRAMING MODULE -- same split as iff_crypto.py (no radio I/O, no
network I/O, no hardware dependency). This is the hardening companion to
iff_crypto.py's one-way periodic beacon, added on top of task #60 to close a
gap a Security Architect's gap analysis found against the Army's exact
requirement wording ("continuous authentication -- zero trust architecture")
and against how real military IFF (Mode 4/5, NATO STANAG 4193) actually
works: real Mode 4/5 is fundamentally interrogate-on-demand challenge-
response -- "prove it right now, for this specific query" -- not a trust
window. iff_crypto.py's one-way beacon says "trust the last transmission
until the next one, within a timing-skew window"; an attacker who jams the
real beacon and instantly relays a captured frame within that window still
passes verification (stated plainly in iff_crypto.py's REPLAY DEFENSE
section, point 3). This module closes that window: every challenge carries a
single-use random nonce, the asset must answer THAT exact nonce, and a reply
is only ever valid once -- there is no interval to relay into.

=============================================================================
RELATIONSHIP TO iff_crypto.py / iff_beacon_bridge.py -- NOTHING IS DELETED
=============================================================================
The one-way periodic beacon (iff_crypto.py, iff_beacon_bridge.py) is kept
alongside this, NOT removed: it remains a valid lower-latency, lower-power,
no-reverse-link-needed "freshness" signal (useful when the interrogator side
doesn't have LoRa TX capability, or when constant low-rate presence is more
useful than an on-demand check), per the recommendation in this task's
resulting design: challenge-response should be the PRIMARY/PREFERRED
verification path when a positive, current, wormhole-resistant answer is
needed (e.g. immediately before authorizing an RF effect near a claimed
friendly position -- task #103's attestation gate), while the periodic
beacon remains a SECONDARY signal (e.g. "is this asset even in the area at
all, roughly"). Nothing about iff_crypto.py's wire format, HMAC construction,
or iff_beacon_bridge.py's handle_frame()/ReplayCache/AssetRegistry changed --
this module is purely additive, uses the SAME per-asset key derivation
(iff_crypto.derive_asset_secret(), same HKDF-SHA256 construction, same
MISSION_MASTER_SECRET provisioning) so no separate secret material or
provisioning step is needed to support both modes on the same asset.

=============================================================================
THE CONCRETE CONSTRUCTION
=============================================================================
Roles: an INTERROGATOR (the backend, or another field asset -- anything that
holds the mission master secret, exactly as iff_beacon_bridge.py's receiver
already must) sends a CHALLENGE naming one target asset_id and a fresh random
nonce. The named asset computes an HMAC over (nonce + asset_id + mission_id +
its own derived key) and sends back a RESPONSE. The interrogator verifies the
response against the *exact* nonce it just issued -- one that has not already
been answered and has not expired.

Challenge frame (interrogator -> asset), 28 bytes:
    offset  size  field
    0       1     magic        = 0x43 ('C')
    1       1     version      = 0x01
    2       4     asset_id     (uint32, big-endian; the ONE asset being asked)
    6       2     mission_id   (uint16, big-endian)
    8       8     nonce        (8 random bytes, secrets.token_bytes(8))
    16      4     issued_time  (uint32, unix seconds, big-endian)
    20      8     tag          = HMAC-SHA256(asset_secret, bytes[0:20])[:8]

The challenge itself is HMAC-tagged (using the SAME derived asset_secret the
one-way beacon uses) so a rogue node without the mission master secret cannot
forge challenges to elicit responses from a friendly asset -- the asset
verifies the challenge before ever replying, exactly as the receiver verifies
beacons in the one-way scheme.

Response frame (asset -> interrogator), 24 bytes:
    offset  size  field
    0       1     magic        = 0x52 ('R')
    1       1     version      = 0x01
    2       4     asset_id     (uint32, big-endian)
    6       2     mission_id   (uint16, big-endian)
    8       8     nonce        (echoed back verbatim from the challenge)
    16      8     tag          = HMAC-SHA256(asset_secret, bytes[0:16])[:8]

Note the response's tag input differs from the challenge's tag input (fewer
bytes, different magic byte at offset 0) -- a reflection attack that simply
replays the challenge frame back as a "response" fails immediately at
parse_response() (wrong magic/length), and even a hypothetical same-length
forgery would still fail hmac.compare_digest() because the byte layouts (and
therefore the HMAC input) differ. No cross-context tag reuse is possible.

=============================================================================
WHY THIS ACTUALLY CLOSES THE RELAY/WORMHOLE GAP
=============================================================================
Every challenge's nonce is:
  1. Randomly generated per-challenge (secrets.token_bytes(8) -- 64 bits of
     entropy, not guessable/predictable).
  2. Tracked by the interrogator as "outstanding" until answered or expired.
  3. Consumed (removed from the outstanding set) the FIRST time any response
     naming it is processed, successfully or not -- so an attacker capturing
     a genuine response and replaying it a second time (even a fraction of a
     second later) is rejected: the nonce is no longer outstanding.
  4. Bounded by NONCE_TTL_S (default 5s -- a single LoRa round trip, not an
     interval an attacker can plan a relay around). There is no "trust
     window" here in the sense iff_crypto.py's beacon has one: a jam-and-
     relay attempt would need to intercept, relay, and return a forged (or
     the genuine, but now-consumed-once-legitimately) reply inside a single
     短 round trip for ONE specific, unpredictable nonce chosen fresh for
     that exact query -- structurally the same defense real Mode 4/5
     interrogation gets from "prove it right now, for this specific query."

=============================================================================
WHAT WAS ACTUALLY VERIFIED IN THIS SESSION
=============================================================================
Every function here (challenge build/verify, response build/verify, nonce
round-trip, replay-of-consumed-nonce rejection, expired-nonce rejection,
wrong-asset rejection) is exercised by field-bridge/test_iff_challenge.py and
field-bridge/test_iff_beacon_bridge.py against concrete, real HMAC-SHA256
computations (same stdlib hmac/hashlib primitives as iff_crypto.py) -- no
mocked crypto. NOT verified, same as the rest of this project's IFF work:
this has never been carried over an actual LoRa (or any) radio link -- there
is no LoRa transceiver on either side of this project yet (see
iff_beacon_bridge.py's HARDWARE STATUS section, unchanged and still
accurate). This module and the NonceStore-based orchestration added to
iff_beacon_bridge.py are synthetic/self-test-validated only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Optional

import iff_crypto as iff

CHALLENGE_MAGIC = 0x43  # 'C'
RESPONSE_MAGIC = 0x52  # 'R'
VERSION = 1
NONCE_LEN = 8
TAG_LEN = 8

# offset 0..19 (20 bytes) is the challenge HMAC input; offset 20..27 (8) is the tag.
_CHALLENGE_FMT = ">BBIH8sI"  # magic, version, asset_id, mission_id, nonce, issued_time
_CHALLENGE_HDR_LEN = struct.calcsize(_CHALLENGE_FMT)  # = 20
CHALLENGE_FRAME_LEN = _CHALLENGE_HDR_LEN + TAG_LEN  # = 28

# offset 0..15 (16 bytes) is the response HMAC input; offset 16..23 (8) is the tag.
_RESPONSE_FMT = ">BBIH8s"  # magic, version, asset_id, mission_id, nonce
_RESPONSE_HDR_LEN = struct.calcsize(_RESPONSE_FMT)  # = 16
RESPONSE_FRAME_LEN = _RESPONSE_HDR_LEN + TAG_LEN  # = 24

assert _CHALLENGE_HDR_LEN == 20, _CHALLENGE_HDR_LEN
assert _RESPONSE_HDR_LEN == 16, _RESPONSE_HDR_LEN

# Single round-trip TTL: a nonce not answered within this many seconds of
# issuance is expired and rejected regardless of tag validity. Deliberately
# short -- this is "prove it right now", not another trust window.
NONCE_TTL_S = 5


class IFFChallengeError(ValueError):
    """Raised when a challenge or response frame fails structural or
    cryptographic checks, or reuses/expires a nonce."""


@dataclass(frozen=True)
class ChallengeFrame:
    asset_id: int
    mission_id: int
    nonce: bytes
    issued_time: int
    tag: bytes


@dataclass(frozen=True)
class ResponseFrame:
    asset_id: int
    mission_id: int
    nonce: bytes
    tag: bytes


# ---------------------------------------------------------------------
# Challenge: interrogator -> asset
# ---------------------------------------------------------------------

def build_challenge(mission_master_secret: bytes, mission_id: int, asset_id: int,
                     nonce: Optional[bytes] = None, issued_time: Optional[int] = None
                     ) -> tuple[bytes, bytes]:
    """Build a HMAC-tagged challenge frame for `asset_id`. Returns
    (raw_frame, nonce) -- the caller (interrogator) must remember `nonce` as
    outstanding (see iff_beacon_bridge.NonceStore) so it can match the
    eventual response.
    """
    nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes")
    issued_time = int(time.time()) if issued_time is None else issued_time
    asset_secret = iff.derive_asset_secret(mission_master_secret, mission_id, asset_id)
    header = struct.pack(_CHALLENGE_FMT, CHALLENGE_MAGIC, VERSION, asset_id & 0xFFFFFFFF,
                          mission_id & 0xFFFF, nonce, issued_time & 0xFFFFFFFF)
    tag = hmac.new(asset_secret, header, hashlib.sha256).digest()[:TAG_LEN]
    return header + tag, nonce


def parse_challenge(raw: bytes) -> ChallengeFrame:
    """Structural parse only -- does NOT verify the HMAC. See verify_challenge()."""
    if len(raw) != CHALLENGE_FRAME_LEN:
        raise IFFChallengeError(f"expected {CHALLENGE_FRAME_LEN}-byte challenge, got {len(raw)}")
    magic, version, asset_id, mission_id, nonce, issued_time = struct.unpack(
        _CHALLENGE_FMT, raw[:_CHALLENGE_HDR_LEN])
    if magic != CHALLENGE_MAGIC:
        raise IFFChallengeError(f"bad magic byte 0x{magic:02x} (expected 0x{CHALLENGE_MAGIC:02x})")
    if version != VERSION:
        raise IFFChallengeError(f"unsupported version {version} (expected {VERSION})")
    tag = raw[_CHALLENGE_HDR_LEN:_CHALLENGE_HDR_LEN + TAG_LEN]
    return ChallengeFrame(asset_id=asset_id, mission_id=mission_id, nonce=nonce,
                           issued_time=issued_time, tag=tag)


def verify_challenge(raw: bytes, mission_master_secret: bytes, expected_mission_id: int,
                      own_asset_id: int, now: Optional[float] = None,
                      max_age_s: int = NONCE_TTL_S) -> ChallengeFrame:
    """Asset-side verification of an incoming challenge, BEFORE replying.
    Confirms the challenge was built by someone holding the mission master
    secret (not a rogue node), targets THIS asset, names the right mission,
    and is not stale. Raises IFFChallengeError with a specific reason on any
    failure; returns the parsed frame only if every check passes."""
    frame = parse_challenge(raw)
    if frame.mission_id != expected_mission_id:
        raise IFFChallengeError(
            f"mission_id mismatch: frame={frame.mission_id} expected={expected_mission_id}")
    if frame.asset_id != own_asset_id:
        raise IFFChallengeError(
            f"challenge targets asset_id={frame.asset_id}, not this asset ({own_asset_id})")

    asset_secret = iff.derive_asset_secret(mission_master_secret, frame.mission_id, frame.asset_id)
    header = struct.pack(_CHALLENGE_FMT, CHALLENGE_MAGIC, VERSION, frame.asset_id,
                          frame.mission_id, frame.nonce, frame.issued_time)
    expected_tag = hmac.new(asset_secret, header, hashlib.sha256).digest()[:TAG_LEN]
    if not hmac.compare_digest(expected_tag, frame.tag):
        raise IFFChallengeError("challenge HMAC tag mismatch -- forged or corrupted challenge")

    now = time.time() if now is None else now
    age = now - frame.issued_time
    if age > max_age_s or age < -max_age_s:
        raise IFFChallengeError(
            f"challenge stale/out-of-tolerance: age={age:.1f}s max_age_s={max_age_s}")

    return frame


# ---------------------------------------------------------------------
# Response: asset -> interrogator
# ---------------------------------------------------------------------

def build_response(asset_secret: bytes, asset_id: int, mission_id: int, nonce: bytes) -> bytes:
    """Build a HMAC-tagged response echoing `nonce` back. `asset_secret` is
    the SAME per-asset key iff_crypto.derive_asset_secret() produces for the
    one-way beacon -- one key, two uses, no extra provisioning."""
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes")
    header = struct.pack(_RESPONSE_FMT, RESPONSE_MAGIC, VERSION, asset_id & 0xFFFFFFFF,
                          mission_id & 0xFFFF, nonce)
    tag = hmac.new(asset_secret, header, hashlib.sha256).digest()[:TAG_LEN]
    return header + tag


def parse_response(raw: bytes) -> ResponseFrame:
    """Structural parse only -- does NOT verify the HMAC. See verify_response()."""
    if len(raw) != RESPONSE_FRAME_LEN:
        raise IFFChallengeError(f"expected {RESPONSE_FRAME_LEN}-byte response, got {len(raw)}")
    magic, version, asset_id, mission_id, nonce = struct.unpack(
        _RESPONSE_FMT, raw[:_RESPONSE_HDR_LEN])
    if magic != RESPONSE_MAGIC:
        raise IFFChallengeError(f"bad magic byte 0x{magic:02x} (expected 0x{RESPONSE_MAGIC:02x})")
    if version != VERSION:
        raise IFFChallengeError(f"unsupported version {version} (expected {VERSION})")
    tag = raw[_RESPONSE_HDR_LEN:_RESPONSE_HDR_LEN + TAG_LEN]
    return ResponseFrame(asset_id=asset_id, mission_id=mission_id, nonce=nonce, tag=tag)


def verify_response(raw: bytes, mission_master_secret: bytes, expected_mission_id: int,
                     expected_asset_id: int, expected_nonce: bytes) -> ResponseFrame:
    """Interrogator-side verification of an incoming response. The caller
    (iff_beacon_bridge.NonceStore-based orchestration) is responsible for
    having already confirmed `expected_nonce` is still outstanding (issued,
    not yet consumed, not expired) and for consuming it -- exactly once --
    regardless of whether this verification ultimately succeeds; that is
    what actually closes the replay/wormhole window, not this function
    alone. This function only checks the cryptographic/structural
    correctness of one specific response against one specific expected
    (mission_id, asset_id, nonce) triple. Raises IFFChallengeError with a
    specific reason on any failure."""
    frame = parse_response(raw)
    if frame.mission_id != expected_mission_id:
        raise IFFChallengeError(
            f"mission_id mismatch: frame={frame.mission_id} expected={expected_mission_id}")
    if frame.asset_id != expected_asset_id:
        raise IFFChallengeError(
            f"asset_id mismatch: frame={frame.asset_id} expected={expected_asset_id}")
    if not hmac.compare_digest(frame.nonce, expected_nonce):
        raise IFFChallengeError("nonce mismatch -- response does not answer the expected challenge")

    asset_secret = iff.derive_asset_secret(mission_master_secret, frame.mission_id, frame.asset_id)
    expected = build_response(asset_secret, frame.asset_id, frame.mission_id, frame.nonce)
    expected_tag = expected[_RESPONSE_HDR_LEN:_RESPONSE_HDR_LEN + TAG_LEN]
    if not hmac.compare_digest(expected_tag, frame.tag):
        raise IFFChallengeError("response HMAC tag mismatch -- forged, corrupted, or unknown asset/secret")

    return frame
