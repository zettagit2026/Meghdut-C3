"""Unit tests for the tamper-evident (hash-chained) mission_log audit trail.

Context: the Operational Requirement Doc (Section 6) claims the mission log is a
"hash-chain ... tamper-evident" audit trail. Previously the SHA-256 chain was
only RECOMPUTED at PDF-report time over the mutable Mongo rows, so a row edited
or deleted directly in the DB simply changed the recomputed hash with nothing
authoritative to compare against — NOT actually tamper-evident. server.py now
computes and STORES the chain at append time (log_event) and exposes
verify_audit_chain() + GET /api/audit/verify to prove integrity.

These are true unit tests (no live server/Mongo, same style as
tests/test_gnss_spoof_geodesic.py): they exercise the REAL pure chaining
helpers from server.py (_next_chained_entry / _compute_entry_hash /
verify_audit_chain) against in-memory entry lists. Importing server.py only
requires the env vars to be SET (motor's client is lazy and never connects).

Run: pytest backend/tests/test_audit_chain.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server as srv  # noqa: E402


def _base_entry(i: int) -> dict:
    """A base mission_log entry (semantic fields only), as log_event builds it
    before stamping seq/prev_hash/entry_hash."""
    return {
        "id": f"id-{i}",
        "ts": datetime(2026, 8, 6, 12, 0, i, tzinfo=timezone.utc).isoformat(),
        "kind": "TEST",
        "message": f"event number {i}",
        "actor": "SYSTEM",
        "meta": {"n": i},
    }


def _build_chain(count: int) -> list[dict]:
    """Append `count` entries the same way log_event does under its lock:
    head = last chained entry, stamp the next one via the REAL helper."""
    entries: list[dict] = []
    head = None
    for i in range(count):
        e = srv._next_chained_entry(_base_entry(i), head)
        entries.append(e)
        head = e
    return entries


# ---------------------------------------------------------------------
# Happy path: a freshly built chain verifies.
# ---------------------------------------------------------------------
def test_intact_chain_verifies():
    entries = _build_chain(5)

    # Structural sanity: genesis seeds from all-zeros, seq is monotonic 0..n-1,
    # each prev_hash links to the previous entry_hash.
    assert entries[0]["prev_hash"] == srv.AUDIT_GENESIS_PREV_HASH
    assert entries[0]["seq"] == 0
    for prev, cur in zip(entries, entries[1:]):
        assert cur["seq"] == prev["seq"] + 1
        assert cur["prev_hash"] == prev["entry_hash"]

    result = srv.verify_audit_chain(entries)
    assert result["valid"] is True, result
    assert result["broken_seq"] is None
    assert result["chained_entries"] == 5
    assert result["legacy_unchained_entries"] == 0
    assert result["head_hash"] == entries[-1]["entry_hash"]


def test_verification_is_order_independent():
    # verify_audit_chain sorts by seq internally, so a shuffled input still
    # verifies (the stored seq/prev_hash define the authoritative order).
    entries = _build_chain(6)
    shuffled = [entries[i] for i in (3, 0, 5, 1, 4, 2)]
    assert srv.verify_audit_chain(shuffled)["valid"] is True


# ---------------------------------------------------------------------
# Tampering: mutating a row's CONTENT breaks verification at that seq.
# ---------------------------------------------------------------------
def test_tampered_content_fails_at_right_seq():
    entries = _build_chain(5)

    # Simulate an attacker editing the message of the entry at seq 2 directly in
    # Mongo, WITHOUT recomputing the stored entry_hash (they can't — the chain
    # forward would also have to be reforged, and the head no longer matches any
    # external attestation). The recomputed hash now diverges from the stored one.
    entries[2]["message"] = "TAMPERED — original event erased"

    result = srv.verify_audit_chain(entries)
    assert result["valid"] is False
    assert result["broken_seq"] == 2, result
    assert "tampered" in result["reason"].lower()


def test_deleted_link_fails():
    # Removing entry seq 2 leaves seq 3's prev_hash pointing at seq 2's (now
    # absent) entry_hash; the walk expects seq 1's hash there -> prev_hash mismatch.
    entries = _build_chain(5)
    del entries[2]
    result = srv.verify_audit_chain(entries)
    assert result["valid"] is False
    assert result["broken_seq"] == 3, result
    assert "prev_hash" in result["reason"]


def test_meta_tamper_detected():
    # The hash covers `meta` too, so quietly rewriting a meta field is caught.
    entries = _build_chain(4)
    entries[1]["meta"]["n"] = 9999
    result = srv.verify_audit_chain(entries)
    assert result["valid"] is False
    assert result["broken_seq"] == 1


# ---------------------------------------------------------------------
# Migration: pre-feature rows (no seq/entry_hash) are an honest, explicitly
# reported unchained legacy prefix — not silently pretended to be chained.
# ---------------------------------------------------------------------
def test_legacy_unchained_prefix_reported_and_chain_still_enforced():
    legacy = [
        {"id": "L1", "ts": "2026-08-01T00:00:00+00:00", "kind": "OLD",
         "message": "pre-migration row", "actor": "SYSTEM", "meta": {}},
        {"id": "L2", "ts": "2026-08-01T00:00:01+00:00", "kind": "OLD",
         "message": "another old row", "actor": "SYSTEM", "meta": {}},
    ]
    chained = _build_chain(3)
    result = srv.verify_audit_chain(legacy + chained)
    assert result["valid"] is True, result
    assert result["legacy_unchained_entries"] == 2
    assert result["chained_entries"] == 3
    assert result["head_hash"] == chained[-1]["entry_hash"]

    # And tampering within the chained portion is still caught despite the
    # legacy prefix being present.
    chained[1]["message"] = "tampered"
    bad = srv.verify_audit_chain(legacy + chained)
    assert bad["valid"] is False
    assert bad["broken_seq"] == 1


def test_empty_log_is_trivially_valid():
    result = srv.verify_audit_chain([])
    assert result["valid"] is True
    assert result["head_hash"] is None
    assert result["chained_entries"] == 0


def test_canonical_payload_excludes_chain_fields():
    # No circular dependency: the hashed payload must NOT include _id/seq/
    # prev_hash/entry_hash, only the semantic fields.
    e = srv._next_chained_entry(_base_entry(0), None)
    payload = srv._canonical_audit_payload(e)
    for forbidden in ("entry_hash", "prev_hash", "seq", "_id"):
        assert forbidden not in payload, f"{forbidden} leaked into hashed payload"
    for semantic in ("ts", "kind", "message", "actor", "meta"):
        assert semantic in payload
