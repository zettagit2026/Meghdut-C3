"""Tests for task #117 "Export IQ for RE analysis": the detection <-> SigMF
IQ capture association endpoints (POST /detections/{id}/iq-capture,
GET /detections/{id}/iq-export).

Per iq_capture.py's own docstring, capture is a manually-invoked, standalone
tool with NO existing wiring into the detection pipeline -- so there is no
automatic association to test. These tests build a REAL fixture SigMF pair
(same field names the real iq_capture.py writes: core:datatype/core:sample_rate/
capture core:frequency) on disk in IQ_CAPTURE_DIR, attach it to a detection
created via the real /detections/ingest endpoint, and verify the export
endpoint serves exactly that pair as a zip -- plus the honest-failure paths
(no capture attached, capture files missing, unsafe basename).

Same convention as the rest of backend/tests: live integration tests against
a running backend (REACT_APP_BACKEND_URL), not app-import unit tests -- this
matches how test_backend.py / test_new_endpoints.py are written in this repo.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import time
import uuid
import zipfile
from pathlib import Path

import pytest
import requests

if "REACT_APP_BACKEND_URL" in os.environ:
    BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
else:
    env_txt = Path("/app/frontend/.env").read_text()
    BASE_URL = None
    for line in env_txt.splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL not resolvable"

API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@meghaduta.mil")
# Task #127: never hardcode a real password here -- server.py's
# _PLACEHOLDER_SECRETS blocklist refuses to boot with known placeholder
# values (the old "cema@2026" literal that used to live here included), so a
# fixed test constant can never authenticate against a correctly-configured
# backend. Reuse whatever ADMIN_PASSWORD the backend was actually booted
# with (same env var, set once per test session by the harness/docker-compose
# invocation) and only fall back to a random session-only value -- generated
# fresh each run, never written to disk -- if the harness didn't export one.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)

# Must match backend/server.py's IQ_CAPTURE_DIR default resolution
# (ROOT_DIR.parent / "field-bridge" / "iq_captures") unless overridden by the
# IQ_CAPTURE_DIR env var on the server process -- tests write fixtures to
# whichever directory the server is actually configured for.
IQ_CAPTURE_DIR = Path(
    os.environ.get("IQ_CAPTURE_DIR", str(Path(__file__).resolve().parents[2] / "field-bridge" / "iq_captures"))
)


@pytest.fixture(scope="module")
def auth_headers() -> dict:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _ingest_detection(auth_headers, confidence_type="unclassified_signal"):
    body = {
        "callsign": f"IQTEST-{uuid.uuid4().hex[:6]}",
        "model": "Unknown UAV",
        "protocol": "Unknown",
        "center_freq_ghz": 2.44,
        "source": "ML_CLASSIFY_BRIDGE",
        "ml_label": "drone",
        "ml_confidence": 0.31,
        "confidence_type": confidence_type,
    }
    r = requests.post(f"{API}/detections/ingest", json=body, headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _write_fixture_sigmf(basename: str, num_samples: int = 256):
    IQ_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    data_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-data"
    meta_path = IQ_CAPTURE_DIR / f"{basename}.sigmf-meta"
    data_path.write_bytes(bytes([1, 2]) * num_samples)  # fake ci8 interleaved I/Q
    meta = {
        "global": {
            "core:datatype": "ci8",
            "core:sample_rate": 20000000.0,
            "core:version": "1.0.0",
            "core:hw": "HackRF One (test fixture)",
            "core:sha512": "deadbeef",
        },
        "captures": [{"core:sample_start": 0, "core:frequency": 2440000000.0,
                      "core:datetime": "2026-07-25T00:00:00.000000Z"}],
        "annotations": [{"core:sample_start": 0, "core:sample_count": num_samples}],
    }
    meta_path.write_text(json.dumps(meta))
    return data_path, meta_path


class TestIqExport:
    def test_export_404_when_no_capture_attached(self, auth_headers):
        det = _ingest_detection(auth_headers)
        r = requests.get(f"{API}/detections/{det['id']}/iq-export", headers=auth_headers, timeout=15)
        assert r.status_code == 404, r.text
        assert "no iq capture" in r.json()["detail"].lower()

    def test_export_404_for_nonexistent_detection(self, auth_headers):
        r = requests.get(f"{API}/detections/{uuid.uuid4()}/iq-export", headers=auth_headers, timeout=15)
        assert r.status_code == 404

    def test_attach_rejects_unsafe_basename(self, auth_headers):
        det = _ingest_detection(auth_headers)
        for bad in ["../../etc/passwd", "a/b", "", "  "]:
            r = requests.post(f"{API}/detections/{det['id']}/iq-capture",
                              json={"basename": bad}, headers=auth_headers, timeout=15)
            assert r.status_code == 400, (bad, r.text)

    def test_attach_404_when_files_missing(self, auth_headers):
        det = _ingest_detection(auth_headers)
        r = requests.post(f"{API}/detections/{det['id']}/iq-capture",
                          json={"basename": f"nonexistent-{uuid.uuid4().hex}"},
                          headers=auth_headers, timeout=15)
        assert r.status_code == 404

    def test_attach_and_export_roundtrip(self, auth_headers):
        det = _ingest_detection(auth_headers, confidence_type="unclassified_signal")
        basename = f"iqtest-{uuid.uuid4().hex[:10]}"
        _write_fixture_sigmf(basename)

        r = requests.post(f"{API}/detections/{det['id']}/iq-capture",
                          json={"basename": basename}, headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["iq_capture_basename"] == basename

        # Detection record now carries the association
        r2 = requests.get(f"{API}/detections/{det['id']}", headers=auth_headers, timeout=15)
        assert r2.json()["iq_capture_basename"] == basename

        r3 = requests.get(f"{API}/detections/{det['id']}/iq-export", headers=auth_headers, timeout=15)
        assert r3.status_code == 200, r3.text
        assert r3.headers["content-type"] == "application/zip"
        assert basename in r3.headers.get("content-disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(r3.content))
        names = set(zf.namelist())
        assert names == {f"{basename}.sigmf-data", f"{basename}.sigmf-meta"}

        meta = json.loads(zf.read(f"{basename}.sigmf-meta"))
        # These are exactly the fields URH's native SigMF importer needs
        # (center frequency, sample rate, ci8 datatype) -- confirmed present,
        # not assumed, both here and by re-reading iq_capture.py's
        # build_sigmf_meta() directly.
        assert meta["global"]["core:datatype"] == "ci8"
        assert meta["global"]["core:sample_rate"] == 20000000.0
        assert meta["captures"][0]["core:frequency"] == 2440000000.0

        data_bytes = zf.read(f"{basename}.sigmf-data")
        assert len(data_bytes) == 256 * 2  # ci8 = 2 bytes/sample (I+Q)

    def test_export_works_for_non_unclassified_detection_too(self, auth_headers):
        # Design intent (task #108/#117): don't over-restrict the underlying
        # capability just because the UI surfaces it primarily for
        # unclassified_signal detections.
        det = _ingest_detection(auth_headers, confidence_type="heuristic_binary")
        basename = f"iqtest-hb-{uuid.uuid4().hex[:10]}"
        _write_fixture_sigmf(basename, num_samples=64)
        r = requests.post(f"{API}/detections/{det['id']}/iq-capture",
                          json={"basename": basename}, headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        r2 = requests.get(f"{API}/detections/{det['id']}/iq-export", headers=auth_headers, timeout=15)
        assert r2.status_code == 200, r2.text
