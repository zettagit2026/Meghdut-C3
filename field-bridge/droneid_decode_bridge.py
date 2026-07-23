#!/usr/bin/env python3
"""Real-IQ -> DroneSecurity offline decoder -> detection-ingest bridge.

RECEIVE ONLY. No transmission happens anywhere in this script.

=============================================================================
WHAT THIS IS
=============================================================================
hackrf_rx.py posts RSSI-heuristic "DJI Mini (candidate)" detections purely
from energy in the 2.4/5.8 GHz bands -- it has no idea whether the emitter
is actually a DJI drone using OcuSync, or some other 2.4/5.8GHz emitter that
happens to be energetic in that band. ml_classify_bridge.py adds a 3-class
RF-fingerprint ML label on top of that, still RSSI/spectrogram based, still
a guess.

This script is a THIRD, INDEPENDENT layer: it periodically captures REAL IQ
via iq_capture.py's capture_iq() (the same real hackrf_transfer wrapper the
other bridges use -- not duplicated), and feeds that capture, IN PROCESS,
into the actual DJI DroneID protocol decoder from the DroneSecurity project
(NDSS'23 "Drone Security and the Mysterious Case of DJI's DroneID",
https://github.com/RUB-SysSec/DroneSecurity, AGPLv3 -- permitted for direct
reuse here under this project's internal-defense licensing override; see
project memory note on open-source sovereignty/licensing).

If -- and only if -- a DroneID frame is found, demodulated (OFDM/QPSK),
descrambled/turbo-decoded, unpacked into the DroneID struct, AND passes its
CRC check, this is a genuine PROTOCOL-LEVEL confirmation that a real DJI
OcuSync 2.0 transmission was received and decoded -- not an RF-signature
guess. That is categorically stronger evidence than the RSSI heuristic or
the closed-world ML classifier, so it is posted with a distinct, higher
label/threat_level: model="DJI DroneID (decoded)", protocol="OcuSync 2.0",
threat_level="HIGH" (vs "MEDIUM" for the RSSI/ML candidates), and includes
the decoded serial_number/device_type/GPS fields when present.

=============================================================================
WHAT WAS ACTUALLY VERIFIED, AND WHAT WAS NOT (READ THIS BEFORE TRUSTING IT)
=============================================================================
VERIFIED, with real data:
  - DroneSecurity's own offline decoder (droneid_receiver_offline.py) was
    run, unmodified, against its own bundled real-hardware sample captures
    (samples/mini2_sm, samples/mavic_air_2 -- real DJI Mini 2 / Mavic Air 2
    RF captured with a USRP B205-mini per the project's README). Output
    matched the README's documented result exactly: "Frame detection: 10
    candidates", "Decoder: 9 total, CRC OK: 7 (2 CRC errors)", with decoded
    serial_number "SysSecWasHere" and app-side GPS coordinates. This
    confirms the decode chain itself (packetizer -> Packet/ZC sync -> QPSK
    Decoder -> DroneIDPacket -> CRC) is real and works as documented.
  - iq_capture.py's capture_iq() is an already-deployed, real
    hackrf_transfer wrapper (RX only) used unmodified here.

NOT VERIFIED -- no live DJI hardware was available in this session:
  - This script has NEVER been run against a real live DJI OcuSync 2.0
    transmission. No DJI drone or DJI remote controller was available to
    test with. No detection this script would produce has been observed;
    none is fabricated here.
  - DroneSecurity's packet DETECTOR (packetizer.find_packet_candidate_time)
    thresholds signal energy above a locally-computed STFT noise floor --
    the threshold itself is relative (1.15x local noise floor), not an
    absolute level, which is a good sign for portability, but the detector
    was tuned/validated against a USRP B205-mini's ADC noise characteristics
    at 50 Msps, not a HackRF One's noisier 8-bit ADC at ~16-20 Msps. It is
    plausible the detector's SNR assumptions do not hold on HackRF-quality
    captures. This is a real, stated risk, not a solved problem.
  - Sample-rate: DroneSecurity's SpectrumCapture.get_packet_samples()
    resamples down to 15.36 MHz for packet_type="droneid" and RAISES if the
    input sample rate is below that. The HackRF One's practical ceiling is
    ~20 Msps. So the only usable capture rate is a narrow window,
    ~15.36-20 Msps (this script defaults to 16 Msps for headroom below the
    HackRF's advertised ceiling) -- untested at that specific rate against
    a real DJI signal.
  - Center frequency: DJI OcuSync 2.0 hops within 2.4 GHz and 5.8 GHz ISM
    bands. DroneSecurity's own live receiver (droneid_receiver_live.py)
    hops across a fixed list of frequencies within those bands (see
    CANDIDATE_FREQS_MHZ below, taken verbatim from that file) -- this
    script reuses that same candidate list rather than inventing new
    frequencies, but has not observed a real DJI transmission on any of
    them.

If you have real DJI hardware in range: run this script, then separately
confirm with a spectrum analyzer / SDR waterfall that the DJI controller is
actually transmitting during the capture window, before trusting either a
"decoded" or "no packets found" result.

=============================================================================
CONFIG (env vars, same convention as the other field-bridge scripts)
=============================================================================
CEMA_API_URL              backend base URL (e.g. http://localhost:8001)
CEMA_EMAIL / CEMA_PASSWORD  operator login
DRONEID_SRC_DIR           path to DroneSecurity's src/ dir (default: sibling
                          checkout at ../../DroneSecurity/src relative to
                          this file's Desktop/Zettawise/... layout; override
                          if DroneSecurity lives elsewhere)
CEMA_DRONEID_SAMPLE_RATE_HZ  IQ capture sample rate (default 16e6 -- must
                          stay in the ~15.36e6-20e6 window, see above)
CEMA_DRONEID_CAPTURE_S    capture duration per frequency, seconds (default 1.3,
                          matches DroneSecurity's own live-receiver default)
CEMA_DRONEID_INTERVAL_S   seconds between full hop-list sweeps (default 30)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from iq_capture import capture_iq  # real hackrf_transfer capture, RX only -- reused, not duplicated
from hackrf_rx import login
from gamutrf_infer import load_sigmf  # generic SigMF -> complex64 loader, reused not duplicated

# Candidate center frequencies, taken verbatim from DroneSecurity's
# src/droneid_receiver_live.py `frequencies` list (its own hop list for
# locating DJI OcuSync 2.0 transmissions), restricted to nothing invented.
CANDIDATE_FREQS_MHZ = [
    2414.5, 2429.502441, 2434.5, 2444.5, 2459.5, 2474.5,
    5721.5, 5731.5, 5741.5, 5756.5, 5761.5, 5771.5, 5786.5, 5801.5, 5816.5, 5831.5,
]

DEFAULT_SRC_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "DroneSecurity", "src")
)

MIN_SAMPLE_RATE_HZ = 15.36e6 + 0.1e6  # DroneSecurity raises below this for packet_type="droneid"
MAX_SAMPLE_RATE_HZ = 20e6  # HackRF One practical ceiling


def _load_droneid_modules(src_dir: str):
    """Import DroneSecurity's decode-chain classes from its own src/ dir.

    Done as a runtime sys.path insert (not vendored/copied) so this bridge
    always uses whatever DroneSecurity checkout is present, unmodified.
    """
    if not os.path.isdir(src_dir):
        raise FileNotFoundError(
            f"DroneSecurity src dir not found: {src_dir} (set DRONEID_SRC_DIR)"
        )
    sys.path.insert(0, src_dir)
    from SpectrumCapture import SpectrumCapture  # noqa: E402
    from Packet import Packet  # noqa: E402
    from qpsk import Decoder  # noqa: E402
    from droneid_packet import DroneIDPacket  # noqa: E402

    return SpectrumCapture, Packet, Decoder, DroneIDPacket


def decode_capture(samples: np.ndarray, sample_rate_hz: float, modules, legacy: bool = False):
    """Run DroneSecurity's real decode chain over one capture, in-process.

    Returns a list of decoded payload dicts (CRC-OK only). Never fabricates
    a result: an empty list means no valid DroneID frame was found/decoded,
    which is the expected, honest outcome absent a real DJI transmission.
    """
    SpectrumCapture, Packet, Decoder, DroneIDPacket = modules
    decoded = []

    capture = SpectrumCapture(samples, Fs=sample_rate_hz, p_type="droneid", legacy=legacy)
    if not capture.packets:
        return decoded

    for pktnum in range(len(capture.packets)):
        try:
            packet_data = capture.get_packet_samples(pktnum=pktnum)
            packet = Packet(packet_data, legacy=legacy)
        except Exception as e:
            print(f"[droneid_decode_bridge] frame {pktnum}: demod failed: {e}", file=sys.stderr)
            continue

        symbols = packet.get_symbol_data(skip_zc=True)
        decoder = Decoder(symbols)

        for phase_corr in range(4):
            decoder.raw_data_to_symbol_bits(phase_corr)
            droneid_duml = decoder.magic()
            try:
                payload = DroneIDPacket(droneid_duml)
            except Exception:
                continue

            if not payload.check_crc():
                continue  # CRC fail -- not a genuine confirmed decode, do not report

            drone_lat, drone_lon, app_lat, app_lon, height = payload.get_coords()
            # DroneIDPacket stores decoded fields in its `.droneid` dict
            # (see DroneSecurity's src/droneid_packet.py), not as bare attrs.
            fields = getattr(payload, "droneid", {})
            decoded.append({
                "serial_number": fields.get("serial_number", ""),
                "device_type": fields.get("device_type", ""),
                "drone_lat": drone_lat,
                "drone_lon": drone_lon,
                "app_lat": app_lat,
                "app_lon": app_lon,
                "height": height,
                "gps_time": fields.get("gps_time", None),
            })
            break  # this frame is done, correct CRC found

    return decoded


def sweep_and_ingest(console_url: str, headers: dict, sample_rate_hz: float,
                      capture_s: float, modules, tmp_dir: str):
    any_decoded = False
    for freq_mhz in CANDIDATE_FREQS_MHZ:
        center_hz = freq_mhz * 1e6
        out_path = os.path.join(tmp_dir, f"droneid_{freq_mhz:.1f}.sigmf-data")
        try:
            meta_path = capture_iq(
                center_freq_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                duration_s=capture_s,
                out_path=out_path,
                description=f"droneid_decode_bridge capture @ {freq_mhz} MHz",
            )
            samples, sr, freq, dtype = load_sigmf(meta_path, out_path)
        except Exception as e:
            print(f"[droneid_decode_bridge] capture failed @ {freq_mhz} MHz: {e}", file=sys.stderr)
            continue
        finally:
            for p in (out_path, out_path.replace(".sigmf-data", ".sigmf-meta")):
                try:
                    os.remove(p)
                except OSError:
                    pass

        try:
            results = decode_capture(samples, sr, modules)
        except Exception as e:
            print(f"[droneid_decode_bridge] decode failed @ {freq_mhz} MHz: {e}", file=sys.stderr)
            continue

        if not results:
            continue

        any_decoded = True
        for r in results:
            print(f"[droneid_decode_bridge] REAL DroneID decode @ {freq_mhz} MHz, CRC OK: {r}")
            det = {
                "model": "DJI DroneID (decoded)",
                "protocol": "OcuSync 2.0",
                "threat_level": "HIGH",
                "center_freq_ghz": freq_mhz / 1000.0,
                "bandwidth_mhz": sample_rate_hz / 1e6,
                "source": "HACKRF",
                "decoded_serial_number": r["serial_number"],
                "decoded_device_type": r["device_type"],
                "drone_lat": r["drone_lat"],
                "drone_lon": r["drone_lon"],
                "app_lat": r["app_lat"],
                "app_lon": r["app_lon"],
                "height_m": r["height"],
                "gps_time_ms": r["gps_time"],
                "distance_estimated": False,
                "notes": "Protocol-level DroneID CRC-verified decode, not an RF-signature heuristic.",
            }
            try:
                requests.post(f"{console_url}/api/detections/ingest", json=det,
                               headers=headers, timeout=10)
            except requests.RequestException as e:
                print(f"[droneid_decode_bridge] ingest failed: {e}", file=sys.stderr)

    return any_decoded


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--droneid-src-dir", default=os.environ.get("DRONEID_SRC_DIR", DEFAULT_SRC_DIR))
    ap.add_argument("--sample-rate-hz", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_SAMPLE_RATE_HZ", "16e6")))
    ap.add_argument("--capture-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_CAPTURE_S", "1.3")))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_INTERVAL_S", "30.0")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    if not (MIN_SAMPLE_RATE_HZ <= args.sample_rate_hz <= MAX_SAMPLE_RATE_HZ):
        ap.error(f"--sample-rate-hz must be in [{MIN_SAMPLE_RATE_HZ/1e6:.2f}e6, "
                  f"{MAX_SAMPLE_RATE_HZ/1e6:.0f}e6] Hz (DroneSecurity decode floor vs "
                  f"HackRF ceiling); got {args.sample_rate_hz}")

    modules = _load_droneid_modules(args.droneid_src_dir)

    print("[droneid_decode_bridge] KNOWN LIMITATION: untested against a live DJI "
          "transmission (no DJI hardware available in this session). Packet detector "
          "was tuned against USRP B205-mini captures at 50 Msps; behavior against "
          f"HackRF captures at {args.sample_rate_hz/1e6:.1f} Msps is unverified. "
          "See module docstring.")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[droneid_decode_bridge] logged in. Sweeping {len(CANDIDATE_FREQS_MHZ)} "
          f"candidate DJI OcuSync frequencies every {args.interval_s}s. RX ONLY.")

    i = 0
    with tempfile.TemporaryDirectory(prefix="cema_droneid_bridge_") as tmp_dir:
        while args.iterations == 0 or i < args.iterations:
            found = sweep_and_ingest(args.console_url, headers, args.sample_rate_hz,
                                      args.capture_s, modules, tmp_dir)
            if not found:
                print("[droneid_decode_bridge] sweep complete: no CRC-valid DroneID "
                      "frame decoded this cycle (expected/honest result if no real "
                      "DJI OcuSync transmitter was in range and transmitting).")
            i += 1
            time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
