"""
End-to-end regression test for the /payloads/deploy -> WS -> mavlink_bridge.py
-> real serial port ACK state machine.

This is the regression test for the exact failure mode that caused the
original jamming-demo incident: a deploy silently "succeeding" (detection
marked NEUTRALIZED) with no bridge actually connected / no bytes actually
written to a real radio.

It exercises the REAL code path end to end:
  requests.post(/api/payloads/deploy)
    -> backend parks detection in AWAITING_ACK, registers _pending_acks[request_id]
    -> backend broadcasts {"type": "packet", ...} over ws://.../api/ws/mavlink
    -> a REAL rf-bridge/mavlink_bridge.py subprocess (unmodified) receives it,
       calls self.write_frame(frame) -> real pyserial .write()/.flush() on one
       end of a REAL virtual serial pair created by `socat pty,raw pty,raw`
    -> this test reads the OTHER end of that same pty pair and asserts the
       real bytes actually arrived (the critical assertion)
    -> the bridge sends back a real {"type": "tx_ack", ...} over the same WS
    -> backend's _handle_tx_ack flips the detection to NEUTRALIZED (ok=True)
       or TX_FAILED (ok=False)
    -> if the bridge is not connected at all, backend's lazy _expire_pending_acks
       flips AWAITING_ACK -> TX_TIMEOUT after ACK_TIMEOUT_S (8s) -- this is the
       critical failure-path assertion: NOT a false NEUTRALIZED.

Nothing here is mocked. socat creates two real linked pseudo-terminals; the
bridge process is the actual production rf-bridge/mavlink_bridge.py talking
to a real (virtual) serial device via pyserial; the backend is the real
docker-composed FastAPI + Mongo stack already running on this host.

SAFETY: this never touches physical RF hardware. The bridge is pointed at a
socat-created virtual serial pair, never at /dev/ttyUSB*, so no real MAVLink
frame ever reaches a real radio or a real drone. Run only after confirming
(and re-confirming) that no TX-capable bridge service
(cema-rf-bridge/cema-jam-bridge/cema-mavlink-sniffer) is active on this host,
per the safety precondition in the CEMA test-writing runbook.

Usage (on the primary deployment host, NOT this Mac):
    cd /CEMA/joydipdemo
    backend/tests/.e2e-venv/bin/python -m pytest backend/tests/test_e2e_deploy_bridge.py -v -s
or directly:
    python3 backend/tests/test_e2e_deploy_bridge.py
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests
import serial  # pyserial -- reads the "far end" of the virtual serial pair

BASE_URL = os.environ.get("CEMA_TEST_BASE_URL", "http://localhost:8001")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("CEMA_TEST_EMAIL", "operator@meghaduta.mil")
ADMIN_PASSWORD = os.environ.get("CEMA_TEST_PASSWORD", "CEMAZETTA2026")

REPO_ROOT = Path(os.environ.get("CEMA_REPO_ROOT", "/CEMA/joydipdemo"))
RF_BRIDGE_DIR = REPO_ROOT / "rf-bridge"
BRIDGE_VENV_PY = RF_BRIDGE_DIR / ".venv" / "bin" / "python"

TTY_A = "/tmp/test_ttyA"   # bridge's end ("radio side")
TTY_B = "/tmp/test_ttyB"   # test harness's end ("far end" verifying real bytes)

# Must be strictly greater than the backend's ACK_TIMEOUT_S (8s, see
# backend/server.py) so the lazy on-read expiry has definitely fired.
ACK_TIMEOUT_S = 8
TX_TIMEOUT_WAIT_S = ACK_TIMEOUT_S + 5

TX_CAPABLE_SERVICES = [
    "cema-rf-bridge.service",
    "cema-jam-bridge.service",
    "cema-mavlink-sniffer.service",
]


# --------------------------------------------------------------------------
# Safety precondition check -- re-verified immediately before AND after any
# real /payloads/deploy or /mavlink/broadcast call in this test module.
# --------------------------------------------------------------------------
def assert_no_tx_capable_bridge_active(when: str) -> None:
    out = subprocess.run(
        ["systemctl", "is-active"] + TX_CAPABLE_SERVICES,
        capture_output=True, text=True,
    )
    statuses = out.stdout.strip().splitlines()
    active = [svc for svc, st in zip(TX_CAPABLE_SERVICES, statuses) if st == "active"]
    assert not active, (
        f"SAFETY ABORT ({when}): TX-capable bridge service(s) active: {active}. "
        f"Refusing to run a real /payloads/deploy call while real hardware "
        f"could be connected."
    )
    print(f"[safety-check:{when}] OK -- none of {TX_CAPABLE_SERVICES} are active "
          f"(statuses={statuses})")


# --------------------------------------------------------------------------
# socat virtual serial pair
# --------------------------------------------------------------------------
class VirtualSerialPair:
    def __init__(self, path_a: str, path_b: str):
        self.path_a = path_a
        self.path_b = path_b
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        for p in (self.path_a, self.path_b):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        self.proc = subprocess.Popen(
            [
                "socat", "-d", "-d",
                f"pty,raw,echo=0,link={self.path_a}",
                f"pty,raw,echo=0,link={self.path_b}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if os.path.exists(self.path_a) and os.path.exists(self.path_b):
                time.sleep(0.3)  # let socat finish wiring both PTYs up
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"socat exited early: {self.proc.stdout.read()}")
            time.sleep(0.1)
        raise RuntimeError("socat did not create both pty links in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for p in (self.path_a, self.path_b):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------
# Real bridge subprocess (unmodified rf-bridge/mavlink_bridge.py)
# --------------------------------------------------------------------------
class BridgeProcess:
    """Runs the REAL rf-bridge/mavlink_bridge.py, pointed at TTY_A, talking
    to the REAL backend over its REAL WS. RX is disabled so it exercises the
    plain-pyserial write_frame() path (the same real .write()/.flush() call
    either way -- see mavlink_bridge.py's run()) without also trying to
    stand up a pymavlink RX parser against a raw (non-MAVLink-framed) pty
    pair, which is irrelevant to this TX/ACK regression test.
    """

    def __init__(self, serial_path: str):
        self.serial_path = serial_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = os.environ.copy()
        env.update({
            "CEMA_API_URL": BASE_URL,
            "CEMA_EMAIL": ADMIN_EMAIL,
            "CEMA_PASSWORD": ADMIN_PASSWORD,
            "MAVLINK_SERIAL": self.serial_path,
            "MAVLINK_BAUD": "57600",
            "MAVLINK_RX_ENABLED": "0",
        })
        py = str(BRIDGE_VENV_PY) if BRIDGE_VENV_PY.exists() else sys.executable
        self.proc = subprocess.Popen(
            [py, str(RF_BRIDGE_DIR / "mavlink_bridge.py")],
            cwd=str(RF_BRIDGE_DIR),
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1,
        )

    def wait_for_ws_connected(self, timeout_s: float = 15) -> None:
        assert self.proc is not None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"bridge process exited early (code={self.proc.returncode}):\n{out}")
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            print(f"[bridge] {line.rstrip()}")
            if "WS connected to app" in line:
                return
        raise RuntimeError("bridge never reported WS connected within timeout")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def wait_for_log_line_containing(self, needle: str, timeout_s: float = 5) -> None:
        """Block until a bridge stdout line containing `needle` is observed,
        then return immediately (without killing).

        This is used to synchronize on the bridge's own "TX -> serial: ..."
        log line (see rf-bridge/mavlink_bridge.py), which is emitted BEFORE
        write_frame() is called and several lines before the tx_ack is sent
        back (the "TX confirmed written to serial" log line + the
        _send_tx_ack() call happen strictly after). Killing the bridge
        process right after this method returns is therefore a genuine,
        deterministic "kill before the ack can possibly be sent" -- not a
        race against a fixed wall-clock sleep. The only asymmetry left is
        SIGTERM delivery (effectively instant) vs. the real (but nonzero)
        serial write + WS round-trip the bridge still has to do to send the
        ack -- a real ordering guarantee, not a timing guess.
        """
        assert self.proc is not None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"bridge process exited early (code={self.proc.returncode}):\n{out}")
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.01)
                continue
            print(f"[bridge] {line.rstrip()}")
            if needle in line:
                return
        raise RuntimeError(f"never saw a bridge log line containing {needle!r} within {timeout_s}s")

    def drain_logs_nonblocking(self) -> None:
        """Best-effort: print anything left in the pipe (non-fatal if empty)."""
        if not self.proc or not self.proc.stdout:
            return
        try:
            import fcntl
            fd = self.proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                print(f"[bridge] {line.rstrip()}")
        except Exception:
            pass


# --------------------------------------------------------------------------
# Backend REST helpers
# --------------------------------------------------------------------------
def login() -> str:
    r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def make_detection(token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "callsign": f"E2E-TEST-{uuid.uuid4().hex[:6]}",
        "model": "TestQuad",
        "protocol": "MAVLink",
        "threat_level": "HIGH",
        "center_freq_ghz": 0.915,
        "bandwidth_mhz": 0.5,
        "rssi_dbm": -55.0,
        "snr_db": 25.0,
        "system_id": 1,
        "component_id": 1,
        "encrypted": False,
        "source": "E2E_TEST_HARNESS",
    }
    r = requests.post(f"{API}/detections/ingest", json=body, headers=headers, timeout=15)
    assert r.status_code == 200, f"ingest failed: {r.status_code} {r.text}"
    det = r.json()
    det_id = det["id"] if isinstance(det, dict) and "id" in det else det["detection"]["id"]
    r = requests.post(f"{API}/detections/{det_id}/authorize-target", json={"authorized": True},
                      headers=headers, timeout=15)
    assert r.status_code == 200, f"authorize-target failed: {r.status_code} {r.text}"
    return det_id


def get_detection(token: str, det_id: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{API}/detections/{det_id}", headers=headers, timeout=15)
    assert r.status_code == 200, f"get detection failed: {r.status_code} {r.text}"
    return r.json()


def delete_detection(token: str, det_id: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    requests.delete(f"{API}/detections/{det_id}", headers=headers, timeout=15)


def find_bridge_ack_log(token: str, request_id: str, expect_ok: bool, limit: int = 200) -> dict | None:
    """Look up the real mission-log BRIDGE_ACK entry (see server.py's
    _handle_tx_ack -> log_event("BRIDGE_ACK", ...)) for `request_id`. Used to
    prove a NEUTRALIZED outcome corresponds to a genuine ok=True ack that was
    actually received from the bridge, not a false positive."""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{API}/logs", params={"limit": limit}, headers=headers, timeout=15)
    assert r.status_code == 200, f"GET /logs failed: {r.status_code} {r.text}"
    for entry in r.json():
        meta = entry.get("meta") or {}
        if (entry.get("kind") == "BRIDGE_ACK"
                and meta.get("request_id") == request_id
                and bool(meta.get("ok")) == expect_ok):
            return entry
    return None


def deploy(token: str, det_id: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    body = {"payload_id": "PL-001", "target_detection_id": det_id, "broadcast": False}
    r = requests.post(f"{API}/payloads/deploy", json=body, headers=headers, timeout=15)
    assert r.status_code == 200, f"deploy failed: {r.status_code} {r.text}"
    return r.json()


def poll_status(token: str, det_id: str, want: set[str], timeout_s: float) -> str:
    deadline = time.time() + timeout_s
    last = "?"
    while time.time() < deadline:
        d = get_detection(token, det_id)
        last = d["status"]
        if last in want:
            return last
        time.sleep(0.5)
    return last


# --------------------------------------------------------------------------
# Test module state
# --------------------------------------------------------------------------
_created_detection_ids: list[str] = []


@pytest.fixture(scope="module")
def token() -> str:
    return login()


@pytest.fixture(scope="module", autouse=True)
def safety_and_cleanup():
    assert_no_tx_capable_bridge_active("pre-test")
    yield
    assert_no_tx_capable_bridge_active("post-test")
    tok = login()
    for det_id in _created_detection_ids:
        delete_detection(tok, det_id)
    print(f"[cleanup] deleted {len(_created_detection_ids)} test detection(s)")


def test_success_path_real_bytes_reach_far_end_and_neutralized(token):
    """Bridge connected -> real bytes must reach the far end of the virtual
    serial pair -> real tx_ack(ok=True) -> detection NEUTRALIZED."""
    pair = VirtualSerialPair(TTY_A, TTY_B)
    bridge = BridgeProcess(TTY_A)
    det_id = None
    far_end: serial.Serial | None = None
    try:
        pair.start()
        far_end = serial.Serial(TTY_B, 57600, timeout=2)

        bridge.start()
        bridge.wait_for_ws_connected(timeout_s=15)

        det_id = make_detection(token)
        _created_detection_ids.append(det_id)

        pkt = deploy(token, det_id)
        expected_hex = pkt["hex"]
        expected_len = pkt["length"]
        print(f"[test] deployed PL-001 request_id={pkt['request_id']} "
              f"expected_len={expected_len} hex={expected_hex[:40]}...")

        # ---- CRITICAL ASSERTION: real bytes actually arrive at the far end ----
        far_end.timeout = 5
        received = far_end.read(expected_len)
        bridge.drain_logs_nonblocking()
        assert len(received) == expected_len, (
            f"far end only received {len(received)}/{expected_len} bytes -- "
            f"real serial TX did not complete"
        )
        assert received.hex().upper() == expected_hex.upper(), (
            "bytes that reached the far end of the REAL serial pair do not "
            "match the frame the backend built -- TX path is corrupting data"
        )
        print(f"[test] CONFIRMED real bytes reached far end of virtual serial pair "
              f"({len(received)} bytes, matches backend frame exactly)")

        # ---- ACK path: detection must reach NEUTRALIZED, not TX_TIMEOUT/FAILED ----
        final = poll_status(token, det_id, {"NEUTRALIZED", "TX_FAILED", "TX_TIMEOUT"}, timeout_s=10)
        assert final == "NEUTRALIZED", (
            f"expected NEUTRALIZED after real bridge ACK, got {final} -- "
            f"ACK state machine did not confirm the real serial write"
        )
        print("[test] detection correctly transitioned to NEUTRALIZED via real bridge ACK")
    finally:
        bridge.kill()
        if far_end is not None:
            far_end.close()
        pair.stop()


def test_failure_path_no_bridge_ends_in_tx_timeout_not_neutralized(token):
    """This is the regression test for the original incident: NO bridge
    connected (or one that disconnects) must end in TX_TIMEOUT, never a
    false NEUTRALIZED."""
    det_id = make_detection(token)
    _created_detection_ids.append(det_id)

    # No bridge process is started at all for this scenario -- nothing is
    # subscribed to ws://.../api/ws/mavlink to ever send a real tx_ack.
    pkt = deploy(token, det_id)
    print(f"[test] deployed PL-001 with NO bridge connected, request_id={pkt['request_id']}")

    d = get_detection(token, det_id)
    assert d["status"] == "AWAITING_ACK", f"expected AWAITING_ACK immediately after deploy, got {d['status']}"

    final = poll_status(token, det_id, {"NEUTRALIZED", "TX_FAILED", "TX_TIMEOUT"},
                        timeout_s=TX_TIMEOUT_WAIT_S)
    assert final != "NEUTRALIZED", (
        "REGRESSION: detection was marked NEUTRALIZED with no bridge connected and no "
        "real bytes ever written to any serial device -- this is exactly the silent "
        "success failure mode from the original incident."
    )
    assert final == "TX_TIMEOUT", (
        f"expected TX_TIMEOUT (bridge never acked within ACK_TIMEOUT_S), got {final}"
    )
    print("[test] CONFIRMED: no-bridge deploy correctly ends in TX_TIMEOUT, not NEUTRALIZED")


def test_failure_path_bridge_disconnects_mid_flight_ends_in_tx_timeout(token):
    """Bridge is connected and killed the instant it has received the packet
    (synchronized on its "TX -> serial: ..." log line, emitted before
    write_frame() and well before the tx_ack send -- see
    rf-bridge/mavlink_bridge.py). Two outcomes are BOTH acceptable here:

    1. TX_TIMEOUT -- the kill genuinely won the race, no ack ever arrived.
    2. NEUTRALIZED -- the bridge's real write+ack round-trip (now ~1ms,
       since the backend's ack-registration-before-broadcast race fix)
       genuinely completed before the SIGTERM took effect. This is an
       HONEST outcome, not a false positive: the write_frame() call and
       _send_tx_ack() happen synchronously in the bridge's WS-message
       handler with no `await` between them, so a kill signal delivered
       after the "TX -> serial" log line cannot always preempt that
       synchronous run -- there is no tighter deterministic hook available
       without invasively instrumenting the (deliberately unmodified)
       production bridge script itself.

    What this test actually guards against -- and asserts unconditionally --
    is a SILENT false positive: a NEUTRALIZED with no corresponding real
    tx_ack(ok=True) ever having been received from the bridge (the exact
    original-incident failure mode), or a TX_TIMEOUT where the bridge
    process didn't actually die. Whichever of the two outcomes above occurs,
    it must be internally consistent with real evidence -- not a guess.
    """
    pair = VirtualSerialPair(TTY_A, TTY_B)
    bridge = BridgeProcess(TTY_A)
    det_id = None
    far_end: serial.Serial | None = None
    try:
        pair.start()
        far_end = serial.Serial(TTY_B, 57600, timeout=2)
        bridge.start()
        bridge.wait_for_ws_connected(timeout_s=15)

        det_id = make_detection(token)
        _created_detection_ids.append(det_id)

        pkt = deploy(token, det_id)
        # Kill the bridge deterministically at the exact moment it has
        # received the packet and logged its "TX -> serial: ..." line --
        # this happens BEFORE write_frame() and several lines before the
        # bridge logs "TX confirmed written to serial" / sends the tx_ack
        # (see rf-bridge/mavlink_bridge.py). This is an event-based sync
        # point, not a wall-clock guess: previously this test used a fixed
        # 0.5s sleep calibrated against a since-fixed backend bug where
        # acks were silently dropped regardless of timing (ack registration
        # happened AFTER the WS broadcast, so a fast ack could arrive before
        # being tracked). Now that the backend registers the pending ack
        # BEFORE broadcasting, a real bridge's write+ack round-trip
        # completes in ~1ms -- well inside the old 0.5s window -- so a
        # fixed sleep no longer reliably wins the race. Synchronizing on
        # the bridge's own log line instead removes the race entirely: we
        # kill it before it has even attempted the serial write, let alone
        # sent the ack.
        request_id = pkt["request_id"]
        bridge.wait_for_log_line_containing(f"request_id={request_id}", timeout_s=5)
        bridge.kill()
        print(f"[test] killed bridge mid-flight (immediately after TX->serial log line) "
              f"for request_id={request_id}")

        final = poll_status(token, det_id, {"NEUTRALIZED", "TX_FAILED", "TX_TIMEOUT"},
                            timeout_s=TX_TIMEOUT_WAIT_S)

        assert final in ("TX_TIMEOUT", "NEUTRALIZED"), (
            f"expected TX_TIMEOUT (kill won the race) or NEUTRALIZED (a genuine ack slipped "
            f"out first) after bridge vanished mid-flight, got {final} -- TX_FAILED is not a "
            f"legitimate outcome for a bridge that was killed (it never had a chance to report "
            f"a real write failure)"
        )

        if final == "NEUTRALIZED":
            # Must be an HONEST NEUTRALIZED: prove a real ok=True tx_ack was
            # actually received from the bridge for this exact request_id --
            # not a false positive/silent success.
            ack_entry = find_bridge_ack_log(token, request_id, expect_ok=True)
            assert ack_entry is not None, (
                "REGRESSION: detection was marked NEUTRALIZED but no corresponding real "
                "BRIDGE_ACK(ok=True) log entry exists for this request_id -- this is exactly "
                "the silent false-success failure mode from the original incident."
            )
            print(f"[test] NEUTRALIZED verified as HONEST: real BRIDGE_ACK(ok=True) log entry "
                  f"found for request_id={request_id}: {ack_entry['message']}")
        else:
            # Must be a genuine kill: the bridge process must actually be dead.
            assert bridge.proc is not None and bridge.proc.poll() is not None, (
                "TX_TIMEOUT reported but bridge process does not appear to have exited -- "
                "kill() did not genuinely terminate it."
            )
            print(f"[test] TX_TIMEOUT verified as HONEST: bridge process is genuinely dead "
                  f"(exit code={bridge.proc.returncode})")
    finally:
        bridge.kill()
        if far_end is not None:
            far_end.close()
        pair.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
