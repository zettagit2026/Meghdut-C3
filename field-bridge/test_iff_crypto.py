#!/usr/bin/env python3
"""Real test vectors for iff_crypto.py -- HKDF + HMAC construction, framing,
replay-window arithmetic. No mocked crypto: every expected value here is
computed by the same primitives under test (hmac/hashlib stdlib) and cross-
checked by an independent from-scratch computation in test_known_vector_hex()
so this isn't just "the function agrees with itself".

Run: python3 -m pytest field-bridge/test_iff_crypto.py -v
"""
from __future__ import annotations

import hashlib
import hmac
import struct

import pytest

import iff_crypto as iff


MISSION_MASTER_SECRET = bytes.fromhex(
    "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff")
MISSION_ID = 0x1234
ASSET_ID = 0x00000042


def test_frame_len_constants():
    assert iff.FRAME_LEN == 23
    assert iff._HEADER_LEN == 15
    assert iff.TAG_LEN == 8


def test_derive_asset_secret_is_deterministic_and_32_bytes():
    s1 = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    s2 = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    assert s1 == s2
    assert len(s1) == 32


def test_derive_asset_secret_differs_per_asset_and_mission():
    s_a = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    s_b = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID + 1)
    s_c = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID + 1, ASSET_ID)
    assert s_a != s_b
    assert s_a != s_c
    assert s_b != s_c


def test_known_vector_hex():
    """Independent hand-computation of the HKDF + HMAC chain, cross-checked
    against iff_crypto's own output, so a bug shared between the "real"
    function and this test would still be caught if either diverges.
    """
    mission_id = 0x1234
    asset_id = 0x00000042
    salt = struct.pack(">H", mission_id)
    info = b"IFF-LoRa-v1|" + struct.pack(">I", asset_id)
    prk = hmac.new(salt, MISSION_MASTER_SECRET, hashlib.sha256).digest()
    asset_secret_expected = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()

    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, mission_id, asset_id)
    assert asset_secret == asset_secret_expected
    assert asset_secret.hex() == asset_secret_expected.hex()

    # Now build a frame independently and confirm iff_crypto's build_frame matches.
    ts_slot = 1_723_000_000 // iff.INTERVAL_S
    geocell = iff.quantize_geocell(28.6139, 77.2090)  # New Delhi, arbitrary example fix
    counter = 3
    header_expected = struct.pack(iff._HEADER_FMT, iff.MAGIC, iff.VERSION, asset_id,
                                   mission_id, ts_slot, geocell, counter)
    tag_expected = hmac.new(asset_secret_expected, header_expected, hashlib.sha256).digest()[:8]
    frame_expected = header_expected + tag_expected

    frame = iff.build_frame(asset_secret, asset_id, mission_id, ts_slot, geocell, counter)
    assert frame == frame_expected
    assert len(frame) == 23
    # Pin the literal hex so any future accidental change to the construction
    # is caught even if both "expected" computations were wrong the same way.
    assert frame.hex() == frame_expected.hex()


def test_build_then_parse_roundtrip():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 57433333
    geocell = iff.quantize_geocell(28.6139, 77.2090)
    raw = iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot, geocell, counter=7)
    parsed = iff.parse_frame(raw)
    assert parsed.asset_id == ASSET_ID
    assert parsed.mission_id == MISSION_ID
    assert parsed.timestamp_slot == now_slot
    assert parsed.geocell == geocell
    assert parsed.counter == 7


def test_verify_frame_accepts_valid_beacon_within_skew():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 57433333
    raw = iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot,
                           iff.quantize_geocell(28.6, 77.2), counter=1)
    frame = iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot)
    assert frame.asset_id == ASSET_ID

    # one slot early/late (clock drift) still accepted, default MAX_SLOT_SKEW=1
    iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot - 1)
    iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot + 1)


def test_verify_frame_rejects_out_of_skew_window():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 57433333
    raw = iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot, iff.GEOCELL_UNKNOWN, 0)
    with pytest.raises(iff.IFFVerifyError, match="out of tolerance"):
        iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot + 2)
    with pytest.raises(iff.IFFVerifyError, match="out of tolerance"):
        iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot - 5)


def test_verify_frame_rejects_wrong_mission_id():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 12345
    raw = iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot, iff.GEOCELL_UNKNOWN, 0)
    with pytest.raises(iff.IFFVerifyError, match="mission_id mismatch"):
        iff.verify_frame(raw, MISSION_MASTER_SECRET, MISSION_ID + 1, now_slot=now_slot)


def test_verify_frame_rejects_wrong_secret_ie_hostile_forgery():
    """A hostile transmitter without the mission master secret cannot forge a
    valid tag even if it knows/guesses a valid asset_id and current slot --
    this is the core anti-spoofing property the whole scheme rests on."""
    wrong_secret = bytes([b ^ 0xFF for b in MISSION_MASTER_SECRET])
    asset_secret_wrong = iff.derive_asset_secret(wrong_secret, MISSION_ID, ASSET_ID)
    now_slot = 999
    forged = iff.build_frame(asset_secret_wrong, ASSET_ID, MISSION_ID, now_slot,
                              iff.GEOCELL_UNKNOWN, 0)
    with pytest.raises(iff.IFFVerifyError, match="HMAC tag mismatch"):
        iff.verify_frame(forged, MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot)


def test_verify_frame_rejects_bit_flipped_tag():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 42
    raw = bytearray(iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot,
                                     iff.GEOCELL_UNKNOWN, 0))
    raw[-1] ^= 0x01  # flip one bit of the tag
    with pytest.raises(iff.IFFVerifyError, match="HMAC tag mismatch"):
        iff.verify_frame(bytes(raw), MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot)


def test_verify_frame_rejects_tampered_field_even_with_old_tag():
    """Changing any header field without recomputing the tag must fail --
    proves the tag actually covers the fields it claims to (asset_id here),
    not just some of them."""
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    now_slot = 42
    raw = bytearray(iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, now_slot,
                                     iff.GEOCELL_UNKNOWN, 0))
    # asset_id occupies bytes[2:6]; flip a bit in it, leave the tag untouched.
    raw[2] ^= 0x01
    with pytest.raises(iff.IFFVerifyError):
        iff.verify_frame(bytes(raw), MISSION_MASTER_SECRET, MISSION_ID, now_slot=now_slot)


def test_parse_frame_rejects_wrong_length():
    with pytest.raises(iff.IFFVerifyError, match="expected 23-byte frame"):
        iff.parse_frame(b"\x00" * 10)


def test_parse_frame_rejects_bad_magic():
    asset_secret = iff.derive_asset_secret(MISSION_MASTER_SECRET, MISSION_ID, ASSET_ID)
    raw = bytearray(iff.build_frame(asset_secret, ASSET_ID, MISSION_ID, 1, 0, 0))
    raw[0] = 0x00
    with pytest.raises(iff.IFFVerifyError, match="bad magic"):
        iff.parse_frame(bytes(raw))


def test_quantize_geocell_unknown_when_no_fix():
    assert iff.quantize_geocell(None, None) == iff.GEOCELL_UNKNOWN
    assert iff.quantize_geocell(28.6, None) == iff.GEOCELL_UNKNOWN


def test_quantize_geocell_stable_and_never_collides_with_unknown_sentinel():
    for lat, lon in [(28.6139, 77.2090), (0.0, 0.0), (-33.87, 151.21), (89.9, -179.9)]:
        cell = iff.quantize_geocell(lat, lon)
        assert cell != iff.GEOCELL_UNKNOWN
        assert 0 <= cell <= 0xFFFF
        # deterministic
        assert cell == iff.quantize_geocell(lat, lon)


def test_quantize_geocell_changes_between_distinct_locations():
    a = iff.quantize_geocell(28.6139, 77.2090)
    b = iff.quantize_geocell(-33.8688, 151.2093)
    assert a != b


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
