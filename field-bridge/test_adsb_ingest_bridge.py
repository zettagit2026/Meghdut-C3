#!/usr/bin/env python3
"""Unit tests for adsb_ingest_bridge.py -- the LIVE Beast-feed -> DF17 decode
wiring. No hardware, no radio, no real network/feed.

The DF17 frames used here are the SAME reference-verified, CRC-valid hex
vectors adsb_decode_bridge.py's own test suite uses (recalled from the
published "1090 Megahertz Riddle" / pyModeS worked examples), so a decode
success is genuine, not a self-consistent fabrication. Each is wrapped in real
Beast binary framing (0x1a '3' <6 ts><1 sig><14 msg>, with 0x1a escaped) to
exercise the feed parser end-to-end.
"""
import adsb_ingest_bridge as b

# Reference-verified, CRC-valid DF17 vectors (same as test_adsb_decode_bridge.py).
IDENT_HEX = "8D4840D6202CC371C32CE0576098"   # icao 4840D6, callsign KLM1023
VEL_HEX = "8D485020994409940838175B284F"     # icao 485020, ground speed 159.2 kt

_ESC = 0x1A


def _beast_long(msg: bytes, ts: bytes = b"\x00" * 6, sig: bytes = b"\x00") -> bytes:
    """Build one Beast Mode-S long frame (type '3'), escaping any 0x1a data
    byte as 0x1a 0x1a exactly as a real dump1090/readsb Beast feed does."""
    body = ts + sig + msg
    escaped = bytearray()
    for byte in body:
        escaped.append(byte)
        if byte == _ESC:
            escaped.append(_ESC)  # 0x1a -> 0x1a 0x1a
    return bytes([_ESC, 0x33]) + bytes(escaped)


class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status
        self.text = "ok"

    def json(self):
        return {"ok": True}


def _install_capture(monkeypatch):
    """Patch adsb_ingest_bridge.requests.post to record (url, json) and return
    200, so process_frames never touches a real network."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return _FakeResp(200)

    monkeypatch.setattr(b.requests, "post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# Beast feed framing
# ---------------------------------------------------------------------------
def test_iter_beast_extracts_long_frame():
    frame = _beast_long(bytes.fromhex(IDENT_HEX))
    msgs, remainder = b.long_frames(frame)
    assert msgs == [bytes.fromhex(IDENT_HEX)]
    assert remainder == b""


def test_beast_escaped_0x1a_in_payload_round_trips():
    # A signal byte of 0x1a must be un-escaped back to a single 0x1a and the
    # 14-byte message recovered intact.
    frame = _beast_long(bytes.fromhex(IDENT_HEX), sig=bytes([0x1A]))
    msgs, remainder = b.long_frames(frame)
    assert msgs == [bytes.fromhex(IDENT_HEX)]
    assert remainder == b""


def test_partial_beast_frame_is_buffered_not_fabricated():
    frame = _beast_long(bytes.fromhex(IDENT_HEX))
    truncated = frame[:-4]  # drop the tail of the message
    msgs, remainder = b.long_frames(truncated)
    assert msgs == []
    assert remainder == truncated  # kept whole to prepend to the next read


def test_two_back_to_back_frames():
    stream = _beast_long(bytes.fromhex(IDENT_HEX)) + _beast_long(bytes.fromhex(VEL_HEX))
    msgs, remainder = b.long_frames(stream)
    assert msgs == [bytes.fromhex(IDENT_HEX), bytes.fromhex(VEL_HEX)]
    assert remainder == b""


# ---------------------------------------------------------------------------
# Ingest body mapping
# ---------------------------------------------------------------------------
def test_state_to_body_none_when_only_icao():
    st = b.adsb.AircraftState(icao="ABCDEF")
    assert b.state_to_ingest_body(st) is None


def test_state_to_body_populated_from_real_decode():
    tracker = b.adsb.AircraftTracker()
    tracker.handle_frame(bytes.fromhex(IDENT_HEX))
    body = b.state_to_ingest_body(tracker.states["4840D6"])
    assert body is not None
    assert body["icao24"] == "4840D6"
    assert body["callsign"] == "KLM1023"
    assert body["squawk"] is None            # DF17 ES carries no 4096 squawk
    assert body["source"] == "ADSB_DUMP1090"
    assert body["caveats"]                    # honesty caveats always attached


# ---------------------------------------------------------------------------
# process_frames: a real decode POSTs an ingest AND a heartbeat
# ---------------------------------------------------------------------------
def test_process_frames_ingests_and_heartbeats(monkeypatch):
    calls = _install_capture(monkeypatch)
    tracker = b.adsb.AircraftTracker()
    frames = [bytes.fromhex(IDENT_HEX), bytes.fromhex(VEL_HEX)]
    decoded_ok, ingested = b.process_frames(
        frames, tracker, "http://x", {"Authorization": "Bearer t"},
        "e@x", "pw")

    assert decoded_ok == 2
    assert ingested >= 1

    ingest_calls = [c for c in calls if c[0].endswith("/api/adsb/ingest")]
    heartbeat_calls = [c for c in calls if c[0].endswith("/api/protocols/heartbeat")]
    assert ingest_calls, "a real DF17 decode must POST /api/adsb/ingest"
    assert heartbeat_calls, "every up-feed cycle must POST a heartbeat"

    # The heartbeat honestly carries the adsb protocol id (backend rejects any
    # other, so a wrong id would never go LIVE).
    assert heartbeat_calls[0][1]["protocol"] == "adsb"

    icaos = {c[1]["icao24"] for c in ingest_calls}
    assert "4840D6" in icaos
    ident_body = next(c[1] for c in ingest_calls if c[1]["icao24"] == "4840D6")
    assert ident_body["callsign"] == "KLM1023"


def test_process_frames_quiet_feed_still_heartbeats(monkeypatch):
    # No frames this cycle (feed up but no aircraft in range) -> zero ingests
    # but the heartbeat still fires (honest READY, never OFFLINE, never faked).
    calls = _install_capture(monkeypatch)
    tracker = b.adsb.AircraftTracker()
    decoded_ok, ingested = b.process_frames(
        [], tracker, "http://x", {"Authorization": "Bearer t"}, "e@x", "pw")
    assert decoded_ok == 0 and ingested == 0
    assert any(c[0].endswith("/api/protocols/heartbeat") for c in calls)
    assert not any(c[0].endswith("/api/adsb/ingest") for c in calls)


def test_bad_crc_frame_never_fabricates(monkeypatch):
    calls = _install_capture(monkeypatch)
    tracker = b.adsb.AircraftTracker()
    corrupt = bytearray(bytes.fromhex(IDENT_HEX))
    corrupt[5] ^= 0x01  # flip a bit -> CRC fails
    decoded_ok, ingested = b.process_frames(
        [bytes(corrupt)], tracker, "http://x", {"Authorization": "Bearer t"},
        "e@x", "pw")
    assert decoded_ok == 0 and ingested == 0
    # No ingest fired, but the cycle still heartbeats (feed was up).
    assert not any(c[0].endswith("/api/adsb/ingest") for c in calls)
    assert any(c[0].endswith("/api/protocols/heartbeat") for c in calls)


# ---------------------------------------------------------------------------
# Honest OFFLINE: no reachable feed -> connect raises (the main loop then
# skips the heartbeat, per the module docstring).
# ---------------------------------------------------------------------------
def test_connect_feed_refused_is_offline():
    import socket
    # Bind then close a socket to obtain a definitely-closed local port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        b.connect_feed("127.0.0.1", port, timeout=1)
        assert False, "connect to a closed port should raise (honest OFFLINE)"
    except OSError:
        pass  # expected: caller treats this as OFFLINE and does NOT heartbeat


def test_sbs_format_is_rejected():
    # SBS would bypass the verified decoder; the bridge must refuse it loudly
    # rather than silently doing second-hand decoding.
    import subprocess
    import sys
    r = subprocess.run(
        [sys.executable, "adsb_ingest_bridge.py", "--feed-format", "sbs",
         "--console-url", "http://x", "--email", "e", "--password", "p"],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "not supported" in (r.stderr + r.stdout).lower()
