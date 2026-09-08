#!/usr/bin/env python3
"""SAFETY-CRITICAL tests for the active Wi-Fi-defeat TX primitives.

These prove the fail-closed spine of wifi_defeat_primitives.py WITHOUT a real
NIC: every real-TX call (scapy frame build/send, UDP socket send) is injected as
a recording fake, so NO 802.11 frame and NO UDP datagram is ever transmitted.

Coverage:
  * unpinned (WIFI_TX_IFACE unset, opt-out unset) -> REFUSE, no TX call made.
  * dev opt-out (WIFI_ALLOW_UNPINNED_TX=1) -> proceeds, WARNING emitted.
  * broadcast / empty / None / malformed target_bssid -> REFUSE, no TX (the
    fratricide guard).
  * a valid pinned deauth -> the isolated TX sender is invoked with the correct
    BSSID / channel.
  * tx_halt_check truthy -> the loop stops promptly, no further TX.
  * the no-raise contract (a raising sender / a raising tx_halt probe).
  * the ARSDK / Tello UDP primitives: pin gate, pre-send abort, happy path.

conftest.py sets WIFI-agnostic opt-outs? No — it sets HACKRF_ALLOW_UNPINNED_TX
only. For the Wi-Fi module the tests set/clear WIFI_TX_IFACE / WIFI_ALLOW_
UNPINNED_TX explicitly per test; the pin gate reads them LIVE from the env.

Run: pytest field-bridge/test_wifi_defeat_primitives.py -q
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wifi_defeat_primitives as wdp  # noqa: E402

TX_IFACE = "wlan1mon"
PIN_ENV = "WIFI_TX_IFACE"
ALLOW_ENV = "WIFI_ALLOW_UNPINNED_TX"
GOOD_BSSID = "AA:BB:CC:11:22:33"
# Associated-client STA MACs — the per-STA (unicast) deauth targets. A deauth now
# REQUIRES a concrete STA list (no broadcast fallback), so valid-deauth tests must
# supply one.
GOOD_STA = "DE:AD:BE:EF:00:01"
GOOD_STA2 = "DE:AD:BE:EF:00:02"
GOOD_STAS = [GOOD_STA]


@pytest.fixture
def pinned(monkeypatch):
    """Pin the TX iface and clear the dev opt-out — the governed/production
    posture. The pin gate reads env live, so this needs no re-import."""
    monkeypatch.setenv(PIN_ENV, TX_IFACE)
    monkeypatch.delenv(ALLOW_ENV, raising=False)


@pytest.fixture
def unpinned_no_optout(monkeypatch):
    """Neither pinned nor opted-out -> the fail-closed REFUSE posture."""
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.delenv(ALLOW_ENV, raising=False)


class _RecordingDeauthSender:
    """Records every (iface, bssid, client_macs, channel) burst it is asked to
    send. client_macs is the resolved unicast STA LIST (never a broadcast)."""
    def __init__(self):
        self.calls = []

    def __call__(self, iface, target_bssid, client_macs, channel):
        self.calls.append((iface, target_bssid, client_macs, channel))


class _RecordingUdpSender:
    def __init__(self):
        self.calls = []

    def __call__(self, target, payload):
        self.calls.append((target, payload))


# --- Fake scapy so the REAL _tx_deauth_frames can be exercised without scapy ---
# scapy is intentionally absent in this test env (see the module docstring). To
# assert the ACTUAL frame addressing the primitive builds (bidirectional per STA)
# we inject a tiny fake `scapy.all` into sys.modules; the primitive's lazy
# `from scapy.all import ...` then picks up these fakes. No frame is transmitted.
class _FakeLayer:
    def __init__(self, _name, **kw):
        self._name = _name
        self._kw = kw
        self.layers = [self]

    def __truediv__(self, other):
        comp = _FakeLayer("__composite__")
        comp.layers = self.layers + other.layers
        return comp


def _dot11_of(frame):
    """Return the addr1/addr2/addr3 of the Dot11 layer in a composite fake frame."""
    for layer in frame.layers:
        if layer._name == "Dot11":
            return layer._kw
    raise AssertionError("no Dot11 layer in frame")


def _install_fake_scapy(monkeypatch):
    """Install a fake scapy.all and return a capture dict of sendp() calls."""
    import types
    sent = {"sendp": []}
    mod = types.ModuleType("scapy.all")

    def _mk(name):
        return lambda **kw: _FakeLayer(name, **kw)

    mod.RadioTap = _mk("RadioTap")
    mod.Dot11 = _mk("Dot11")
    mod.Dot11Deauth = _mk("Dot11Deauth")
    mod.Dot11Disas = _mk("Dot11Disas")

    def _sendp(frames, iface=None, verbose=None):
        sent["sendp"].append({"frames": list(frames), "iface": iface})

    mod.sendp = _sendp
    scapy_pkg = types.ModuleType("scapy")
    monkeypatch.setitem(sys.modules, "scapy", scapy_pkg)
    monkeypatch.setitem(sys.modules, "scapy.all", mod)
    return sent


# ---------------------------------------------------------------------------
# 1) unpinned + no opt-out -> REFUSE, no TX call made
# ---------------------------------------------------------------------------
def test_send_deauth_refuses_when_unpinned(unpinned_no_optout):
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, None, 6, 3,
                             frame_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert result["frames_sent"] == 0
    assert sender.calls == [], "REFUSE path must inject no frame"


def test_arsdk_refuses_when_unpinned(unpinned_no_optout):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1:54321", b"\x01\x02",
                                      udp_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert sender.calls == []


def test_tello_refuses_when_unpinned(unpinned_no_optout):
    sender = _RecordingUdpSender()
    result = wdp.tello_command(TX_IFACE, "192.168.10.1", "land",
                               udp_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert sender.calls == []


# ---------------------------------------------------------------------------
# 2) dev opt-out set -> proceeds with a WARNING
# ---------------------------------------------------------------------------
def test_optout_proceeds_with_warning(monkeypatch, capsys):
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.setenv(ALLOW_ENV, "1")
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 1,
                             frame_sender=sender)
    assert result["ok"] is True
    assert result["frames_sent"] == 1
    assert len(sender.calls) == 1
    err = capsys.readouterr().err
    assert "WARNING" in err and "WIFI_TX_IFACE" in err


def test_pinning_gate_returns_none_when_pinned(pinned):
    assert wdp._wifi_tx_pinning_error() is None


# ---------------------------------------------------------------------------
# 3) FRATRICIDE guard: broadcast / empty / None / malformed BSSID -> REFUSE
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_bssid", [
    "FF:FF:FF:FF:FF:FF",
    "ff:ff:ff:ff:ff:ff",
    "ff-ff-ff-ff-ff-ff",
    "",
    "   ",
    None,
    "not-a-mac",
    "AA:BB:CC:DD:EE",       # too short
    "AA:BB:CC:DD:EE:FF:00",  # too long
    "GG:BB:CC:DD:EE:FF",     # non-hex
])
def test_send_deauth_refuses_bad_bssid(pinned, bad_bssid):
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, bad_bssid, None, 6, 3,
                             frame_sender=sender)
    assert result["ok"] is False
    assert "fratricide" in result["error"].lower()
    assert result["frames_sent"] == 0
    assert sender.calls == [], "fratricide REFUSE must inject no frame"


# ---------------------------------------------------------------------------
# 4) valid pinned deauth -> the isolated TX sender gets the right BSSID/channel
# ---------------------------------------------------------------------------
def test_valid_deauth_invokes_sender_with_bssid_and_channel(pinned):
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, [GOOD_STA], 11, 4,
                             frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is False
    assert result["frames_sent"] == 4
    assert len(sender.calls) == 4
    for iface, bssid, client_macs, channel in sender.calls:
        assert iface == TX_IFACE
        assert bssid == GOOD_BSSID
        # The resolved STA LIST is forwarded to the sender (never a bare MAC and
        # never a broadcast).
        assert client_macs == [GOOD_STA]
        assert channel == 11


def test_empty_sta_list_refused_no_broadcast_fallback(pinned):
    """HONESTY GATE / no-broadcast-fallback: an empty STA list is REFUSED, never
    silently turned into an FF:FF:FF:FF:FF:FF broadcast client. Proves the old
    broadcast default is GONE."""
    for empty in (None, [], (), "", "   ", ["  "], ["not-a-mac"], ["FF:FF:FF:FF:FF:FF"]):
        sender = _RecordingDeauthSender()
        result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, empty, 6, 3,
                                 frame_sender=sender)
        assert result["ok"] is False, f"empty STA {empty!r} must refuse"
        assert "no associated clients" in result["error"]
        assert result["frames_sent"] == 0
        assert sender.calls == [], "no frame may be sent when there is no STA"


def test_deauth_normalizes_dedups_and_drops_broadcast_and_malformed(pinned):
    """A mixed STA list is normalized: valid MACs upper-cased + de-duped (order
    stable), a broadcast MAC and a malformed MAC dropped. The surviving unicast
    list is what reaches the sender."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(
        TX_IFACE, GOOD_BSSID,
        ["de:ad:be:ef:00:01", "FF:FF:FF:FF:FF:FF", "DE:AD:BE:EF:00:01",
         "garbage", GOOD_STA2],
        6, 1, frame_sender=sender)
    assert result["ok"] is True
    assert sender.calls[0][2] == [GOOD_STA, GOOD_STA2]


def test_deauth_drops_multicast_and_ig_bit_macs(pinned):
    """R1: ANY MAC whose first octet has the I/G (least-significant) bit set is
    dropped, not just the all-FF broadcast — e.g. the well-known IPv4-multicast
    OUI 01:00:5E. A real associated client STA is always unicast."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(
        TX_IFACE, GOOD_BSSID,
        ["01:00:5E:00:00:01", "03:00:00:11:22:33", GOOD_STA],
        6, 1, frame_sender=sender)
    assert result["ok"] is True
    assert sender.calls[0][2] == [GOOD_STA], "multicast/I-G-bit MACs must be dropped"


def test_deauth_all_multicast_list_refused_no_send(pinned):
    """An all-multicast/I-G-bit STA list has nothing surviving -> REFUSE, same as
    an all-broadcast or all-malformed list; no sendp/TX call made."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(
        TX_IFACE, GOOD_BSSID,
        ["01:00:5E:00:00:01", "33:33:00:00:00:01"],
        6, 3, frame_sender=sender)
    assert result["ok"] is False
    assert "no associated clients" in result["error"]
    assert sender.calls == [], "all-multicast STA list must inject no frame"


def test_deauth_canonicalizes_dash_form_mac_to_colon_upper(pinned):
    """R3: a valid dash-separated MAC is canonicalized to colon-separated UPPER
    form (not rejected, not forwarded with dashes) so scapy accepts it at TX."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, ["de-ad-be-ef-00-01"],
                             6, 1, frame_sender=sender)
    assert result["ok"] is True
    assert sender.calls[0][2] == ["DE:AD:BE:EF:00:01"]


def test_deauth_caps_oversized_sta_list_with_warning(pinned, capsys):
    """R2: a STA list larger than MAX_DEAUTH_STA_TARGETS is truncated to the
    first MAX_DEAUTH_STA_TARGETS (stable order), and a WARNING is logged — the
    request is NOT refused, the surviving capped list is still transmitted."""
    over = wdp.MAX_DEAUTH_STA_TARGETS + 10
    stas = [f"DE:AD:BE:EF:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}" for i in range(over)]
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, stas, 6, 1, frame_sender=sender)
    assert result["ok"] is True
    sent_targets = sender.calls[0][2]
    assert len(sent_targets) == wdp.MAX_DEAUTH_STA_TARGETS
    # stable order: the first MAX_DEAUTH_STA_TARGETS survivors, unchanged order.
    assert sent_targets == stas[: wdp.MAX_DEAUTH_STA_TARGETS]
    err = capsys.readouterr().err
    assert "WARNING" in err and "MAX_DEAUTH_STA_TARGETS" in err


def test_deauth_cap_reflected_in_real_frame_count(monkeypatch):
    """The cap is enforced BEFORE frames are built: with the real (fake-scapy)
    _tx_deauth_frames, an oversized STA list yields exactly
    MAX_DEAUTH_STA_TARGETS * 4 frames (2 directions x {deauth,disas}) per
    burst, never one frame more."""
    monkeypatch.setenv(PIN_ENV, TX_IFACE)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    sent = _install_fake_scapy(monkeypatch)
    over = wdp.MAX_DEAUTH_STA_TARGETS + 5
    stas = [f"DE:AD:BE:EF:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}" for i in range(over)]
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, stas, 6, 1)
    assert result["ok"] is True
    assert len(sent["sendp"]) == 1
    assert len(sent["sendp"][0]["frames"]) == wdp.MAX_DEAUTH_STA_TARGETS * 4


def test_tx_deauth_frames_emits_both_directions_per_sta(monkeypatch):
    """BIDIRECTIONAL per-STA: the REAL _tx_deauth_frames (scapy mocked) must emit,
    for EACH STA, an AP->STA frame (addr1=STA,addr2=BSSID) AND a STA->AP frame
    (addr1=BSSID,addr2=STA), with addr2/addr3 always pinned to the ONE BSSID."""
    sent = _install_fake_scapy(monkeypatch)
    stas = [GOOD_STA, GOOD_STA2]
    wdp._tx_deauth_frames(TX_IFACE, GOOD_BSSID, stas, 6)
    # One sendp() call carrying every frame for this burst.
    assert len(sent["sendp"]) == 1
    call = sent["sendp"][0]
    assert call["iface"] == TX_IFACE
    dot11s = [_dot11_of(f) for f in call["frames"]]
    for sta in stas:
        ap_to_sta = [d for d in dot11s if d["addr1"] == sta and d["addr2"] == GOOD_BSSID]
        sta_to_ap = [d for d in dot11s if d["addr1"] == GOOD_BSSID and d["addr2"] == sta]
        assert ap_to_sta, f"missing AP->STA frame for {sta}"
        assert sta_to_ap, f"missing STA->AP frame for {sta}"
        # addr3 (the BSS) is always the ONE target BSSID, never the band.
        for d in ap_to_sta + sta_to_ap:
            assert d["addr3"] == GOOD_BSSID
    # NEVER a broadcast client address anywhere.
    assert all(d["addr1"] != "FF:FF:FF:FF:FF:FF" for d in dot11s)


# ---------------------------------------------------------------------------
# 5) tx_halt_check truthy -> loop stops promptly, no further TX
# ---------------------------------------------------------------------------
def test_deauth_stops_immediately_when_halted_before_first_burst(pinned):
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 100,
                             tx_halt_check=lambda: True, frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert result["frames_sent"] == 0
    assert sender.calls == [], "halted-before-start must inject nothing"


def test_deauth_stops_mid_stream_on_halt(pinned):
    """Halt after N bursts: the loop must poll BEFORE each burst and stop."""
    state = {"n": 0}

    def halt():
        return state["n"] >= 3

    def sender(iface, bssid, client, channel):
        state["n"] += 1

    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, None,  # continuous
                             tx_halt_check=halt, frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    # Polled before each burst -> exactly 3 sent, then the 4th poll halts.
    assert state["n"] == 3
    assert result["frames_sent"] == 3


def test_deauth_stops_on_stop_event(pinned):
    ev = threading.Event()
    ev.set()
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 50,
                             stop_event=ev, frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert sender.calls == []


def test_deauth_halt_probe_that_raises_fails_safe(pinned):
    """A raising tx_halt probe fails SAFE (treated as stop), never keeps TX up."""
    def boom():
        raise RuntimeError("probe broke")
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 100,
                             tx_halt_check=boom, frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert sender.calls == []


# ---------------------------------------------------------------------------
# 6) no-raise contract: a raising real-TX sender comes back as ok=False
# ---------------------------------------------------------------------------
def test_deauth_sender_exception_is_not_raised(pinned):
    def boom(iface, bssid, client_macs, channel):
        raise OSError("nic gone")
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 3, frame_sender=boom)
    assert result["ok"] is False
    assert "failed" in result["error"].lower()
    assert result["stopped_early"] is False


# ---------------------------------------------------------------------------
# 7) ARSDK / Tello UDP primitives
# ---------------------------------------------------------------------------
def test_arsdk_valid_send(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1:9988",
                                      b"\x02\x0b\x00", udp_sender=sender)
    assert result["ok"] is True
    assert result["bytes_sent"] == 3
    assert sender.calls == [(("192.168.42.1", 9988), b"\x02\x0b\x00")]


def test_arsdk_default_port_when_bare_host(pinned):
    sender = _RecordingUdpSender()
    wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1", b"\x01", udp_sender=sender)
    assert sender.calls[0][0] == ("192.168.42.1", wdp.ARSDK_DEFAULT_C2D_PORT)


def test_arsdk_refuses_non_bytes_payload(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1:1", "not-bytes",  # type: ignore[arg-type]
                                      udp_sender=sender)
    assert result["ok"] is False
    assert sender.calls == []


def test_arsdk_refuses_empty_softap(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "", b"\x01", udp_sender=sender)
    assert result["ok"] is False
    assert sender.calls == []


def test_arsdk_pre_send_abort(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1:1", b"\x01",
                                      tx_halt_check=lambda: True, udp_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert result["bytes_sent"] == 0
    assert sender.calls == [], "aborted before send must transmit nothing"


def test_tello_valid_send_encodes_ascii(pinned):
    sender = _RecordingUdpSender()
    result = wdp.tello_command(TX_IFACE, "192.168.10.1", "land", udp_sender=sender)
    assert result["ok"] is True
    assert sender.calls == [(("192.168.10.1", wdp.TELLO_CMD_PORT), b"land")]


def test_tello_accepts_bytes(pinned):
    sender = _RecordingUdpSender()
    wdp.tello_command(TX_IFACE, "192.168.10.1", b"emergency", udp_sender=sender)
    assert sender.calls[0][1] == b"emergency"


def test_tello_refuses_empty_command(pinned):
    sender = _RecordingUdpSender()
    result = wdp.tello_command(TX_IFACE, "192.168.10.1", "   ", udp_sender=sender)
    assert result["ok"] is False
    assert sender.calls == []


def test_tello_udp_send_exception_not_raised(pinned):
    def boom(target, payload):
        raise OSError("socket gone")
    result = wdp.tello_command(TX_IFACE, "192.168.10.1", "land", udp_sender=boom)
    assert result["ok"] is False
    assert "failed" in result["error"].lower()


def test_no_real_scapy_or_socket_needed_to_import():
    """The module imported at top of this file with scapy absent — proving no
    radio dependency at import time."""
    assert hasattr(wdp, "send_deauth")
    assert hasattr(wdp, "inject_arsdk_command")
    assert hasattr(wdp, "tello_command")


# ---------------------------------------------------------------------------
# 8) M1: transmit is BOUND to the pinned NIC — a caller-supplied iface that
#    does NOT equal the pinned WIFI_TX_IFACE is refused, no TX (the Wi-Fi
#    analogue of HackRF's -d <serial> binding). A matching iface proceeds.
# ---------------------------------------------------------------------------
WRONG_IFACE = "wlan0mon"  # e.g. the detection/RX NIC, not the pinned TX NIC


def test_send_deauth_pinned_but_wrong_iface_refused(pinned):
    """pinned fixture sets WIFI_TX_IFACE=TX_IFACE; passing a DIFFERENT iface
    to send_deauth must be refused with no frame sent."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(WRONG_IFACE, GOOD_BSSID, None, 6, 3,
                             frame_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert result["frames_sent"] == 0
    assert sender.calls == [], "iface-mismatch REFUSE must inject no frame"


def test_send_deauth_pinned_matching_iface_proceeds(pinned):
    """iface == the pinned WIFI_TX_IFACE -> transmit proceeds normally."""
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 2,
                             frame_sender=sender)
    assert result["ok"] is True
    assert result["frames_sent"] == 2
    assert len(sender.calls) == 2


def test_arsdk_pinned_but_wrong_iface_refused(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(WRONG_IFACE, "192.168.42.1:54321",
                                      b"\x01\x02", udp_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert sender.calls == []


def test_arsdk_pinned_matching_iface_proceeds(pinned):
    sender = _RecordingUdpSender()
    result = wdp.inject_arsdk_command(TX_IFACE, "192.168.42.1:54321",
                                      b"\x01\x02", udp_sender=sender)
    assert result["ok"] is True
    assert sender.calls == [(("192.168.42.1", 54321), b"\x01\x02")]


def test_tello_pinned_but_wrong_iface_refused(pinned):
    sender = _RecordingUdpSender()
    result = wdp.tello_command(WRONG_IFACE, "192.168.10.1", "land",
                               udp_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert sender.calls == []


def test_tello_pinned_matching_iface_proceeds(pinned):
    sender = _RecordingUdpSender()
    result = wdp.tello_command(TX_IFACE, "192.168.10.1", "land",
                               udp_sender=sender)
    assert result["ok"] is True
    assert sender.calls == [(("192.168.10.1", wdp.TELLO_CMD_PORT), b"land")]


def test_dev_optout_allows_iface_mismatch_with_warning(monkeypatch, capsys):
    """Under the explicit WIFI_ALLOW_UNPINNED_TX=1 dev opt-out (no real pin
    set), ANY iface is allowed — that IS the dev escape hatch — but the
    WARNING is still emitted."""
    monkeypatch.delenv(PIN_ENV, raising=False)
    monkeypatch.setenv(ALLOW_ENV, "1")
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(WRONG_IFACE, GOOD_BSSID, GOOD_STAS, 6, 1,
                             frame_sender=sender)
    assert result["ok"] is True
    assert result["frames_sent"] == 1
    assert len(sender.calls) == 1
    err = capsys.readouterr().err
    assert "WARNING" in err and "WIFI_TX_IFACE" in err


def test_pinning_gate_refuses_mismatched_iface_directly(pinned):
    err = wdp._wifi_tx_pinning_error(WRONG_IFACE)
    assert err is not None
    assert "WIFI_TX_IFACE" in err


def test_pinning_gate_permits_matching_iface_directly(pinned):
    assert wdp._wifi_tx_pinning_error(TX_IFACE) is None


# ---------------------------------------------------------------------------
# 9) M2: a whitespace-only WIFI_TX_IFACE must be treated as UNSET -> REFUSE
#    (unless the dev opt-out is also set).
# ---------------------------------------------------------------------------
def test_blank_pin_treated_as_unset_refuses(monkeypatch):
    monkeypatch.setenv(PIN_ENV, "   ")
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, None, 6, 3,
                             frame_sender=sender)
    assert result["ok"] is False
    assert "WIFI_TX_IFACE" in result["error"]
    assert sender.calls == []


def test_blank_pin_pinning_gate_returns_error_directly(monkeypatch):
    monkeypatch.setenv(PIN_ENV, "   ")
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert wdp._wifi_tx_pinning_error() is not None


def test_blank_pin_with_optout_still_proceeds(monkeypatch, capsys):
    """A blank pin counts as unset, so the dev opt-out still applies normally
    (any iface allowed, WARNING emitted)."""
    monkeypatch.setenv(PIN_ENV, "  \t  ")
    monkeypatch.setenv(ALLOW_ENV, "1")
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 1,
                             frame_sender=sender)
    assert result["ok"] is True
    err = capsys.readouterr().err
    assert "WARNING" in err


# ---------------------------------------------------------------------------
# 10) L1: a stop_event whose is_set() raises fails SAFE (symmetric with the
#     already-tested raising tx_halt_check probe).
# ---------------------------------------------------------------------------
class _RaisingStopEvent:
    def is_set(self):
        raise RuntimeError("stop_event broke")


def test_deauth_stop_event_that_raises_fails_safe(pinned):
    sender = _RecordingDeauthSender()
    result = wdp.send_deauth(TX_IFACE, GOOD_BSSID, GOOD_STAS, 6, 100,
                             stop_event=_RaisingStopEvent(), frame_sender=sender)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert result["frames_sent"] == 0
    assert sender.calls == [], "raising stop_event must inject nothing"


def test_stop_requested_helper_fails_safe_on_raising_stop_event():
    assert wdp._stop_requested(_RaisingStopEvent(), None) is True


def test_stop_requested_helper_fails_safe_on_raising_tx_halt_check():
    def boom():
        raise RuntimeError("tx_halt broke")
    assert wdp._stop_requested(None, boom) is True
