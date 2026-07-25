#!/usr/bin/env python3
"""pytest coverage for iff_challenge.py's challenge-response construction --
hardening added on top of task #60 to close the relay/wormhole gap the
one-way periodic beacon (iff_crypto.py) cannot fully defeat (see that
module's REPLAY DEFENSE section, point 3, and iff_challenge.py's module
docstring for the full rationale).

Pure protocol-layer tests here (build/verify challenge and response frames
directly, no NonceStore/bridge orchestration) -- the bounded, replay-closing
NonceStore state lives in iff_beacon_bridge.py and is covered in
test_iff_beacon_bridge.py alongside handle_frame()'s existing tests. No LoRa
hardware, no network -- pure synthetic-but-cryptographically-real frames,
same convention as test_iff_crypto.py/test_iff_beacon_bridge.py.
"""
from __future__ import annotations

import secrets
import time

import pytest

import iff_challenge as ch
import iff_crypto as iff


@pytest.fixture
def mission():
    mission_id = 0xBEEF
    master_secret = secrets.token_bytes(32)
    asset_id = 501
    asset_secret = iff.derive_asset_secret(master_secret, mission_id, asset_id)
    return {
        "mission_id": mission_id,
        "master_secret": master_secret,
        "asset_id": asset_id,
        "asset_secret": asset_secret,
    }


def test_frame_len_constants():
    assert ch.CHALLENGE_FRAME_LEN == 28
    assert ch.RESPONSE_FRAME_LEN == 24


def test_challenge_round_trip_verifies(mission):
    raw, nonce = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                     mission["asset_id"])
    frame = ch.verify_challenge(raw, mission["master_secret"], mission["mission_id"],
                                 own_asset_id=mission["asset_id"])
    assert frame.nonce == nonce
    assert frame.asset_id == mission["asset_id"]


def test_response_round_trip_succeeds(mission):
    raw, nonce = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                     mission["asset_id"])
    ch.verify_challenge(raw, mission["master_secret"], mission["mission_id"],
                         own_asset_id=mission["asset_id"])
    response = ch.build_response(mission["asset_secret"], mission["asset_id"],
                                  mission["mission_id"], nonce)
    verified = ch.verify_response(response, mission["master_secret"], mission["mission_id"],
                                   expected_asset_id=mission["asset_id"], expected_nonce=nonce)
    assert verified.nonce == nonce
    assert verified.asset_id == mission["asset_id"]


def test_challenge_wrong_target_asset_rejected(mission):
    raw, _ = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                 mission["asset_id"])
    with pytest.raises(ch.IFFChallengeError, match="not this asset"):
        ch.verify_challenge(raw, mission["master_secret"], mission["mission_id"],
                             own_asset_id=999)


def test_challenge_forged_rejected(mission):
    forged_secret = iff.derive_asset_secret(secrets.token_bytes(32), mission["mission_id"],
                                             mission["asset_id"])
    header_raw, nonce = ch.build_challenge(forged_secret, mission["mission_id"],
                                            mission["asset_id"])
    with pytest.raises(ch.IFFChallengeError, match="HMAC"):
        ch.verify_challenge(header_raw, mission["master_secret"], mission["mission_id"],
                             own_asset_id=mission["asset_id"])


def test_challenge_stale_rejected(mission):
    raw, _ = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                 mission["asset_id"], issued_time=int(time.time()) - 3600)
    with pytest.raises(ch.IFFChallengeError, match="stale"):
        ch.verify_challenge(raw, mission["master_secret"], mission["mission_id"],
                             own_asset_id=mission["asset_id"])


def test_response_wrong_nonce_rejected(mission):
    raw, nonce = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                     mission["asset_id"])
    response = ch.build_response(mission["asset_secret"], mission["asset_id"],
                                  mission["mission_id"], nonce)
    wrong_nonce = secrets.token_bytes(ch.NONCE_LEN)
    with pytest.raises(ch.IFFChallengeError, match="nonce mismatch"):
        ch.verify_response(response, mission["master_secret"], mission["mission_id"],
                            expected_asset_id=mission["asset_id"], expected_nonce=wrong_nonce)


def test_response_wrong_asset_secret_rejected(mission):
    """A response built with a DIFFERENT asset's derived secret (e.g. a
    compromised device trying to answer on behalf of another asset_id) is
    rejected -- this is the "wrong asset's HMAC is rejected" case."""
    raw, nonce = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                     mission["asset_id"])
    other_secret = iff.derive_asset_secret(mission["master_secret"], mission["mission_id"], 777)
    forged = ch.build_response(other_secret, mission["asset_id"], mission["mission_id"], nonce)
    with pytest.raises(ch.IFFChallengeError, match="HMAC"):
        ch.verify_response(forged, mission["master_secret"], mission["mission_id"],
                            expected_asset_id=mission["asset_id"], expected_nonce=nonce)


def test_response_wrong_mission_id_rejected(mission):
    raw, nonce = ch.build_challenge(mission["master_secret"], mission["mission_id"],
                                     mission["asset_id"])
    response = ch.build_response(mission["asset_secret"], mission["asset_id"],
                                  mission["mission_id"], nonce)
    with pytest.raises(ch.IFFChallengeError, match="mission_id"):
        ch.verify_response(response, mission["master_secret"], mission["mission_id"] + 1,
                            expected_asset_id=mission["asset_id"], expected_nonce=nonce)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
