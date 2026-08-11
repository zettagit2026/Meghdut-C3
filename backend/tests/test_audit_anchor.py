"""Unit tests for the lightweight external audit-chain anchor.

Context: the append-time hash-chain (test_audit_chain.py) only detects CASUAL
tampering on its own — a Mongo-write-capable adversary can recompute the whole
chain from genesis. server.py periodically emits the current chain head to an
APPEND-ONLY sink independent of the mission_log collection (a greppable
`AUDIT_ANCHOR seq=… head=… ts=…` line on stdout/journal AND an append-mode
on-disk file), so tampering between anchor points is detectable by comparing the
live head against the last anchored head.

These are true unit tests (no live server/Mongo, same style as
tests/test_audit_chain.py): they exercise the REAL pure anchor helpers from
server.py (_format_audit_anchor / _read_last_anchor) plus the anchor-file emit
path, against a temp file. _emit_audit_anchor's Mongo read is monkeypatched.

Run: pytest backend/tests/test_audit_anchor.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server as srv  # noqa: E402


def test_anchor_line_has_greppable_prefix_and_fields():
    line = srv._format_audit_anchor(42, "deadbeef" * 8, "2026-08-11T00:00:00+00:00")
    assert line.startswith("AUDIT_ANCHOR ")
    assert "seq=42" in line
    assert "head=" + "deadbeef" * 8 in line
    assert "ts=2026-08-11T00:00:00+00:00" in line


def test_read_last_anchor_parses_latest_line(tmp_path, monkeypatch):
    f = tmp_path / "anchor.log"
    f.write_text(
        srv._format_audit_anchor(0, "a" * 64, "2026-08-11T00:00:00+00:00") + "\n"
        + srv._format_audit_anchor(1, "b" * 64, "2026-08-11T00:01:00+00:00") + "\n"
    )
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", str(f))
    anchor = srv._read_last_anchor()
    assert anchor is not None
    assert anchor["seq"] == 1           # int-coerced, latest line
    assert anchor["head"] == "b" * 64
    assert anchor["ts"] == "2026-08-11T00:01:00+00:00"


def test_read_last_anchor_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", str(tmp_path / "nope.log"))
    assert srv._read_last_anchor() is None


def test_emit_audit_anchor_appends_greppable_line(tmp_path, monkeypatch):
    f = tmp_path / "anchor.log"
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", str(f))

    async def fake_head():
        return {"seq": 7, "entry_hash": "c" * 64}

    monkeypatch.setattr(srv, "_current_chain_head", fake_head)
    result = asyncio.run(srv._emit_audit_anchor(reason="unit"))
    assert result == {"seq": 7, "head": "c" * 64,
                      "ts": result["ts"]}  # ts is generated
    content = f.read_text()
    assert content.startswith("AUDIT_ANCHOR seq=7 head=" + "c" * 64)
    # round-trips through the reader
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", str(f))
    assert srv._read_last_anchor()["seq"] == 7


def test_emit_audit_anchor_empty_chain_is_noop(tmp_path, monkeypatch):
    f = tmp_path / "anchor.log"
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", str(f))

    async def empty_head():
        return None

    monkeypatch.setattr(srv, "_current_chain_head", empty_head)
    assert asyncio.run(srv._emit_audit_anchor()) is None
    assert not f.exists()


def test_emit_audit_anchor_file_failure_does_not_raise(monkeypatch):
    # Point the anchor file at an unwritable path; emit must swallow the error
    # (never crash the loop) but still return the head it tried to anchor.
    monkeypatch.setattr(srv, "AUDIT_ANCHOR_FILE", "/nonexistent-dir-xyz/anchor.log")

    async def fake_head():
        return {"seq": 3, "entry_hash": "d" * 64}

    monkeypatch.setattr(srv, "_current_chain_head", fake_head)
    result = asyncio.run(srv._emit_audit_anchor())
    assert result["seq"] == 3  # journal line still emitted, no exception raised
