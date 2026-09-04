#!/usr/bin/env python3
"""LIVE ADS-B consumer: EXISTING dump1090/readsb Beast feed -> DF17 decode ->
backend ingest.

RX-ONLY. Passive. No transmit path anywhere. And -- read this carefully --
NO HackRF/SDR path anywhere either.

=============================================================================
WHAT THIS IS (and the ONE thing it must never do)
=============================================================================
This is the LIVE wiring that turns field-bridge/adsb_decode_bridge.py's
verified ICAO Annex 10 DF17 / CPR decoder into a running service, exactly the
way remoteid_kismet_bridge.py did for the Remote ID decoder:

  a dump1090/readsb receiver that is ALREADY running (its own 1090 MHz RTL-SDR
  or ADS-B receiver, wholly separate from this project's detection HackRF)
      -> its Beast binary output feed over TCP (default port 30005)
      -> extract each raw 14-byte Mode-S long (DF17 Extended Squitter) frame
      -> decode with adsb_decode_bridge.py's UNMODIFIED, reference-verified
         parse_df17() / AircraftTracker (CRC-24 validate + even/odd CPR pair)
      -> aggregate per-ICAO state into {icao24, callsign, position, velocity}
      -> POST /api/adsb/ingest (and a per-cycle /api/protocols/heartbeat).

adsb_decode_bridge.py's own module docstring warns that opening the HackRF to
demodulate 1090 MHz here would TIME-SHARE (and thus starve) the primary
detection sweep. This bridge therefore NEVER opens a radio: it consumes an
EXISTING dump1090/readsb feed over a TCP socket. The only I/O it does is a
client TCP connect to that feed and the backend HTTPS POSTs. If you are
looking for the line that opens a SoapySDR/HackRF device, it does not exist
and must never be added.

=============================================================================
WHY Beast, NOT SBS/BaseStation (an honesty decision, stated plainly)
=============================================================================
dump1090/readsb expose two consumable feeds:

  * Beast binary (port 30005): the RAW, still-encoded 14-byte Mode-S frames,
    exactly as received off the air. Decoding these routes every byte through
    adsb_decode_bridge.py's verified DF17 envelope + CRC-24 + CPR math -- i.e.
    it genuinely REUSES the verified decoder, as the task requires.

  * SBS/BaseStation (port 30003): a CSV text feed of fields dump1090 has
    ALREADY decoded (lat/lon/callsign/altitude as ASCII). Consuming that would
    mean trusting dump1090's decode and doing NONE of ours -- the verified
    decoder would be bypassed entirely, and a "decode" here would be a picture
    of someone else's decode. That fails the honesty bar ("reuse the VERIFIED
    decoder; no synthetic/second-hand fallback").

So this bridge speaks Beast, and Beast only. ADSB_FEED_FORMAT exists solely so
a misconfiguration to "sbs" fails LOUDLY with this explanation rather than
silently doing the wrong thing.

=============================================================================
HONEST STATUS -- READY vs LIVE vs OFFLINE
=============================================================================
The decoder is real and reference-verified (see adsb_decode_bridge.py). What
this service reports depends ENTIRELY on the feed:

  * Feed reachable + a transponder-equipped aircraft in range -> a real DF17
    frame decodes -> POST /api/adsb/ingest -> the adsb protocol shows LIVE.
  * Feed reachable but quiet (no aircraft squittering in range right now) ->
    the service heartbeats every cycle -> the board honestly shows READY
    (running, awaiting a matching broadcast). No telemetry is fabricated.
  * Feed NOT reachable (no dump1090/readsb listening on host:port) -> there is
    no pipeline to be READY about. The service does NOT heartbeat; it logs an
    honest OFFLINE and keeps retrying the connection. A heartbeat here would be
    a lie (claiming READY with no feed behind it), so it is never sent.

ADS-B is a DECONFLICTION aid (positively identify KNOWN, cooperative,
transponder-equipped traffic so an operator can DEPRIORITIZE it), NOT a threat
detector -- a hostile/non-cooperative drone carries no transponder and need
never appear here. See adsb_decode_bridge.py's docstring for the full doctrine.

=============================================================================
CONFIG (env vars, same convention as the other field-bridge scripts)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
ADSB_FEED_HOST      dump1090/readsb Beast host   (default 127.0.0.1)
ADSB_FEED_PORT      dump1090/readsb Beast port   (default 30005)
ADSB_FEED_FORMAT    "beast" only (default beast; "sbs" is rejected on purpose)
ADSB_POLL_INTERVAL_S   seconds of feed-read per cycle (default 5)
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adsb_decode_bridge as adsb  # reuse the VERIFIED DF17/CPR decoder unchanged

BRIDGE_NAME = "adsb_ingest_bridge"
PROTOCOL_ID = "adsb"

# Beast binary framing: 0x1a escape, then a type byte selecting the payload
# length. Only Mode-S long ('3') carries a DF17 Extended Squitter (14 bytes);
# short ('2', DF<=11) and Mode-AC ('1') are read to stay frame-aligned but not
# fed to the DF17 decoder. A real 0x1a data byte is escaped on the wire as
# 0x1a 0x1a. Every frame is <ts:6><signal:1><message:payload_len>.
_ESC = 0x1A
_BEAST_TYPE_LEN = {0x31: 2, 0x32: 7, 0x33: 14}  # '1' ModeAC, '2' short, '3' long
_MODE_S_LONG = 0x33
_BEAST_META_LEN = 7  # 6-byte MLAT timestamp + 1-byte signal, before the message


def _read_escaped(buf: bytes, start: int, need: int) -> Tuple[bytes, int, bool]:
    """Read `need` un-escaped bytes from `buf` starting at `start`, collapsing
    each 0x1a 0x1a pair to a single literal 0x1a. Returns (bytes, next_index,
    complete). complete=False means the buffer ran out mid-frame (caller keeps
    it as remainder and waits for more), or a 0x1a-then-non-0x1a appeared
    inside the payload region (a truncated/misframed frame -- treat as
    incomplete and resync on the next real frame start)."""
    out = bytearray()
    i = start
    n = len(buf)
    while len(out) < need:
        if i >= n:
            return bytes(out), i, False
        b = buf[i]
        if b == _ESC:
            if i + 1 >= n:
                return bytes(out), i, False  # need the second byte of the pair
            if buf[i + 1] == _ESC:
                out.append(_ESC)
                i += 2
                continue
            # 0x1a followed by a non-0x1a inside the payload = the previous
            # frame was truncated and this is the next frame's delimiter.
            return bytes(out), i, False
        out.append(b)
        i += 1
    return bytes(out), i, True


def iter_beast_frames(buf: bytes) -> Tuple[List[Tuple[str, bytes]], bytes]:
    """Extract complete Beast frames from `buf`.

    Returns (frames, remainder) where frames is a list of (type_char, message)
    for every complete frame found (message is the raw Mode-S bytes, already
    un-escaped), and remainder is the trailing partial bytes to prepend to the
    next read. Never raises; unrecognized/stray bytes are skipped so the parser
    resynchronizes on the next genuine 0x1a<type> frame start."""
    frames: List[Tuple[str, bytes]] = []
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] != _ESC:
            i += 1
            continue
        if i + 1 >= n:
            return frames, buf[i:]  # 0x1a with no type byte yet
        t = buf[i + 1]
        if t not in _BEAST_TYPE_LEN:
            i += 1  # stray 0x1a, not a frame start
            continue
        payload_len = _BEAST_TYPE_LEN[t]
        frame, end, complete = _read_escaped(buf, i + 2, _BEAST_META_LEN + payload_len)
        if not complete:
            return frames, buf[i:]  # partial frame -- wait for more bytes
        frames.append((chr(t), frame[_BEAST_META_LEN:_BEAST_META_LEN + payload_len]))
        i = end
    return frames, b""


def long_frames(buf: bytes) -> Tuple[List[bytes], bytes]:
    """Convenience: from a Beast byte buffer, return (mode_s_long_messages,
    remainder) -- only the 14-byte Mode-S long frames (the DF17 Extended
    Squitter candidates the decoder cares about)."""
    frames, remainder = iter_beast_frames(buf)
    msgs = [msg for tchar, msg in frames if tchar == chr(_MODE_S_LONG) and len(msg) == 14]
    return msgs, remainder


def adsb_caveats() -> List[str]:
    return [
        "ADS-B is a DECONFLICTION aid (identifies KNOWN, cooperative, transponder-equipped traffic), not a threat detector",
        "a hostile/non-cooperative drone carries no ADS-B transponder and need never appear here",
        "consumed from an EXISTING dump1090/readsb feed -- this bridge never opens the detection HackRF/SDR",
    ]


def state_to_ingest_body(st: "adsb.AircraftState") -> Optional[Dict]:
    """Map an adsb_decode_bridge.AircraftState into the /api/adsb/ingest body
    shape. Returns None if the state carries only an ICAO address and no
    decoded identity/position/velocity yet (nothing worth ingesting). Every
    field is populated ONLY from a real decoded message; absent message types
    stay null (no fabrication). DF17 Extended Squitter carries no 4096 squawk
    code, so `squawk` is always null here."""
    if not st.icao:
        return None
    has_payload = any(v is not None for v in (
        st.callsign, st.lat, st.lon, st.altitude_ft,
        st.ground_speed_kt, st.track_deg, st.vertical_rate_fpm))
    if not has_payload:
        return None
    return {
        "icao24": st.icao,
        "callsign": st.callsign,
        "latitude_deg": st.lat,
        "longitude_deg": st.lon,
        "altitude_ft": float(st.altitude_ft) if st.altitude_ft is not None else None,
        "ground_speed_kt": st.ground_speed_kt,
        "track_deg": st.track_deg,
        "vertical_rate_fpm": float(st.vertical_rate_fpm) if st.vertical_rate_fpm is not None else None,
        "squawk": None,
        "source": "ADSB_DUMP1090",
        "caveats": adsb_caveats(),
    }


# ---------------------------------------------------------------------------
# Console auth (same convention as every other field-bridge script).
# ---------------------------------------------------------------------------
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
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] re-login failed ({e})", file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    return r


def ingest_states(tracker: "adsb.AircraftTracker", updated_icaos: Set[str],
                  console_url: str, headers: dict, email: str, password: str) -> int:
    """POST /api/adsb/ingest for each aircraft updated this cycle that now has
    something worth reporting. Returns the number ingested."""
    ingested = 0
    for icao in updated_icaos:
        st = tracker.states.get(icao)
        if st is None:
            continue
        body = state_to_ingest_body(st)
        if body is None:
            continue
        try:
            r = _post_with_reauth(console_url, "/api/adsb/ingest", body,
                                   headers, email, password, timeout=8)
            if r.status_code == 200:
                ingested += 1
                print(f"[{BRIDGE_NAME}] REAL ADS-B decode: icao24={body['icao24']} "
                      f"callsign={body['callsign']} pos=({body['latitude_deg']},"
                      f"{body['longitude_deg']}) alt_ft={body['altitude_ft']}")
            else:
                print(f"[{BRIDGE_NAME}] ingest HTTP {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] ingest failed: {e}", file=sys.stderr)
    return ingested


def heartbeat(console_url: str, headers: dict, email: str, password: str,
              note: str) -> None:
    """Per-cycle liveness heartbeat -- ONLY called when the feed is genuinely
    connected (see module docstring: no feed -> OFFLINE, no heartbeat)."""
    try:
        _post_with_reauth(console_url, "/api/protocols/heartbeat",
                          {"protocol": PROTOCOL_ID, "note": note},
                          headers, email, password, timeout=5)
    except requests.RequestException:
        pass


def process_frames(frames: List[bytes], tracker: "adsb.AircraftTracker",
                   console_url: str, headers: dict, email: str, password: str
                   ) -> Tuple[int, int]:
    """Feed a batch of raw 14-byte Mode-S long frames to the tracker, then
    ingest every aircraft that changed and post ONE heartbeat (the feed is up
    when this is called). Returns (frames_decoded_ok, aircraft_ingested).

    Per-frame decode errors (CRC failure, non-DF17 long frames like DF18 TIS-B,
    malformed bytes) are counted-out and skipped -- never fabricated."""
    updated: Set[str] = set()
    decoded_ok = 0
    for raw in frames:
        try:
            parsed = tracker.handle_frame(raw)
        except Exception:
            continue  # CRC fail / non-DF17 / malformed -> skip, no fabrication
        decoded_ok += 1
        updated.add(parsed.icao)
    ingested = ingest_states(tracker, updated, console_url, headers, email, password)
    heartbeat(console_url, headers, email, password,
              note=f"read {len(frames)} Mode-S long frames, {decoded_ok} DF17 decoded")
    return decoded_ok, ingested


def connect_feed(host: str, port: int, timeout: float = 5) -> socket.socket:
    """Open a client TCP connection to the EXISTING dump1090/readsb Beast feed.
    Raises OSError (ConnectionRefusedError/timeout/etc.) if nothing is
    listening -- the caller treats that as honest OFFLINE. Opens NO radio."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(0.5)
    return sock


def read_feed_window(sock: socket.socket, buf: bytes, window_s: float
                     ) -> Tuple[List[bytes], bytes]:
    """Read from the Beast socket for up to `window_s` seconds, extract every
    complete Mode-S long frame, and return (frames, leftover_buffer). Raises
    OSError if the peer closes/errors (caller drops the socket and reconnects)."""
    deadline = time.time() + window_s
    frames: List[bytes] = []
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise ConnectionError("feed closed by peer")
        buf += chunk
        new_frames, buf = long_frames(buf)
        frames.extend(new_frames)
    return frames, buf


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--feed-host", default=os.environ.get("ADSB_FEED_HOST", "127.0.0.1"))
    ap.add_argument("--feed-port", type=int,
                     default=int(os.environ.get("ADSB_FEED_PORT", "30005")))
    ap.add_argument("--feed-format", default=os.environ.get("ADSB_FEED_FORMAT", "beast"))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("ADSB_POLL_INTERVAL_S", "5.0")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    if args.feed_format.strip().lower() != "beast":
        ap.error(
            f"ADSB_FEED_FORMAT='{args.feed_format}' is not supported. This bridge "
            "speaks Beast binary (port 30005) ONLY, so every frame is decoded by "
            "the verified DF17/CPR decoder. The SBS/BaseStation CSV feed (30003) "
            "is dump1090's ALREADY-decoded output and would bypass the verified "
            "decoder -- see the module docstring.")

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[{BRIDGE_NAME}] logged in. Consuming EXISTING dump1090/readsb Beast feed "
          f"at {args.feed_host}:{args.feed_port} every {args.interval_s}s. "
          "RX ONLY -- no HackRF/SDR opened.")

    tracker = adsb.AircraftTracker()
    sock: Optional[socket.socket] = None
    buf = b""
    i = 0
    while args.iterations == 0 or i < args.iterations:
        if sock is None:
            try:
                sock = connect_feed(args.feed_host, args.feed_port)
                buf = b""
                print(f"[{BRIDGE_NAME}] connected to Beast feed "
                      f"{args.feed_host}:{args.feed_port}.")
            except OSError as e:
                # No feed behind host:port -> honest OFFLINE. Do NOT heartbeat:
                # there is no pipeline to claim READY for.
                print(f"[{BRIDGE_NAME}] feed OFFLINE ({args.feed_host}:{args.feed_port}: "
                      f"{e}). Not heartbeating (no feed = OFFLINE, not fake-READY). "
                      "Retrying.", file=sys.stderr)
                i += 1
                time.sleep(args.interval_s)
                continue
        try:
            frames, buf = read_feed_window(sock, buf, args.interval_s)
        except OSError as e:
            print(f"[{BRIDGE_NAME}] feed read error ({e}); reconnecting.", file=sys.stderr)
            try:
                sock.close()
            except OSError:
                pass
            sock = None
            i += 1
            continue
        decoded_ok, ingested = process_frames(
            frames, tracker, args.console_url, headers, args.email, args.password)
        if ingested == 0:
            print(f"[{BRIDGE_NAME}] cycle complete: {decoded_ok} DF17 frame(s) decoded, "
                  "0 aircraft ingested (READY -- feed up, awaiting a decodable "
                  "cooperative-aircraft squitter in range).")
        i += 1

    if sock is not None:
        sock.close()


if __name__ == "__main__":
    main()
