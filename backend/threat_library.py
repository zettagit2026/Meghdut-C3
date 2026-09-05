"""Drone threat library + matching/classification engine (RFI Northern Command
Sec 4.2.12).

This module is DELIBERATELY dependency-free (no FastAPI, no Mongo, no network)
so the matching/classification logic and the offline/online import-merge logic
are unit-testable in-process, exactly like protocol_status.py / swarm_classifier.py.

What it provides
----------------
1. An inbuilt, VERSIONED threat library (data/threat_library.json) of COTS +
   class-level military threat drones (Sec 4.2.12.1). Every entry is compiled
   from PUBLIC manufacturer specs / open-source RF observations; unknown fields
   are null, never invented; military entries are class-level placeholders only.

2. A matching + classification engine (Sec 4.2.12.2, feeding 4.2.5.4) that maps
   an observed detection's attributes to RANKED candidate threats with HONEST,
   tiered confidence:

     * BROADCAST-DECODED identity  (ASTM Remote ID serial, DJI DroneID make/model,
       distinctive Wi-Fi SSID) -> EXACT make/model/serial, HIGH confidence.
     * RF-SIGNATURE-only match     (band + occupied-bw + control-link family) ->
       threat CLASS / FAMILY candidates, LOWER confidence, RANKED. It NEVER
       promotes a signature-only match to a fabricated exact model.
     * Insufficient signature       -> honest "unknown", no guess.

3. An offline/online update mechanism (Sec 4.2.12.3/4.2.12.4): validate + merge
   an imported library file WITHOUT OEM intervention -- add/update entries, bump
   the revision, and keep an audit of exactly what changed; plus export of the
   current library.

Honest limits (surfaced, not hidden)
------------------------------------
  * An RF-signature (band/bw/family) match CANNOT yield a confirmed exact model
    or serial -- only a class/family candidate. Only a decoded broadcast id can.
  * Cyber-takeover is applicable ONLY to unencrypted MAVLink-over-RF links; every
    encrypted/FHSS COTS link (OcuSync, SkyLink, ELRS, Crossfire) is marked
    not-injectable in its countermeasures block.
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY_PATH = ROOT_DIR / "data" / "threat_library.json"
# The merged/imported library persists here so an update survives restart. If it
# does not exist yet, the seed (DEFAULT_LIBRARY_PATH) is the active library.
ACTIVE_LIBRARY_PATH = Path(
    os.environ.get("THREAT_LIBRARY_ACTIVE_PATH",
                   str(ROOT_DIR / "data" / "threat_library.active.json")))

SCHEMA_ID = "meghdut.threat_library.v1"
CLASS_VOCAB = {
    "multirotor", "fixed-wing", "FPV", "VTOL",
    "loitering-munition-class", "ISR-class", "unknown",
}
THREAT_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
_REQUIRED_ENTRY_KEYS = ("id", "make", "model", "class", "threat_level")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# Load / persist
# =====================================================================
def load_library(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate a library file from disk (defaults to active-or-seed)."""
    if path is None:
        path = ACTIVE_LIBRARY_PATH if ACTIVE_LIBRARY_PATH.exists() else DEFAULT_LIBRARY_PATH
    with open(path, "r", encoding="utf-8") as fh:
        lib = json.load(fh)
    validate_library(lib)
    return lib


def save_active_library(lib: Dict[str, Any]) -> Path:
    """Persist the merged/active library. Returns the path written."""
    validate_library(lib)
    ACTIVE_LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_LIBRARY_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(lib, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, ACTIVE_LIBRARY_PATH)
    return ACTIVE_LIBRARY_PATH


# =====================================================================
# Validation (Sec 4.2.12.3 -- reject a malformed import, never silently ingest)
# =====================================================================
class ThreatLibraryError(ValueError):
    """Raised when an imported library fails schema validation."""


def validate_library(lib: Any) -> None:
    """Structural + value validation. Raises ThreatLibraryError on any problem so
    a bad import can NEVER corrupt the active library."""
    if not isinstance(lib, dict):
        raise ThreatLibraryError("library must be a JSON object")
    if lib.get("schema") not in (None, SCHEMA_ID):
        raise ThreatLibraryError(
            f"unsupported schema '{lib.get('schema')}' (expected '{SCHEMA_ID}')")
    entries = lib.get("entries")
    if not isinstance(entries, list):
        raise ThreatLibraryError("library.entries must be a list")
    if "version" in lib and not isinstance(lib["version"], str):
        raise ThreatLibraryError("library.version must be a string")
    seen_ids: set = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ThreatLibraryError(f"entries[{i}] must be an object")
        for k in _REQUIRED_ENTRY_KEYS:
            if not e.get(k):
                raise ThreatLibraryError(f"entries[{i}] missing required field '{k}'")
        eid = e["id"]
        if not isinstance(eid, str):
            raise ThreatLibraryError(f"entries[{i}].id must be a string")
        if eid in seen_ids:
            raise ThreatLibraryError(f"duplicate entry id '{eid}'")
        seen_ids.add(eid)
        if e["class"] not in CLASS_VOCAB:
            raise ThreatLibraryError(
                f"entries[{i}] ('{eid}') invalid class '{e['class']}'")
        if e["threat_level"] not in THREAT_LEVELS:
            raise ThreatLibraryError(
                f"entries[{i}] ('{eid}') invalid threat_level '{e['threat_level']}'")


# =====================================================================
# Import / merge (Sec 4.2.12.3 / 4.2.12.4 -- update without OEM intervention)
# =====================================================================
def merge_library(base: Dict[str, Any],
                  incoming: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Merge `incoming` into `base` by entry id (add new, update existing).

    Returns (merged_library, audit). The merged library gets a bumped `revision`
    (monotonic int) and a refreshed `updated` timestamp; `version` follows the
    incoming file's version when it supplies one, else the base version is kept.
    The audit records exactly which ids were added vs updated -- kept so an
    operator can see what a 3-monthly / on-request update actually changed.

    Neither argument is mutated.
    """
    validate_library(base)
    validate_library(incoming)

    merged = copy.deepcopy(base)
    by_id: Dict[str, int] = {e["id"]: idx for idx, e in enumerate(merged["entries"])}

    added: List[str] = []
    updated: List[str] = []
    for e in incoming.get("entries", []):
        eid = e["id"]
        if eid in by_id:
            # Only count it as an update if the content actually differs.
            if merged["entries"][by_id[eid]] != e:
                merged["entries"][by_id[eid]] = copy.deepcopy(e)
                updated.append(eid)
        else:
            merged["entries"].append(copy.deepcopy(e))
            by_id[eid] = len(merged["entries"]) - 1
            added.append(eid)

    from_revision = int(base.get("revision", 1))
    to_revision = from_revision + 1
    from_version = base.get("version")
    to_version = incoming.get("version") or from_version

    merged["schema"] = SCHEMA_ID
    merged["revision"] = to_revision
    merged["version"] = to_version
    merged["updated"] = _now_iso()

    audit_entry = {
        "timestamp": merged["updated"],
        "from_revision": from_revision,
        "to_revision": to_revision,
        "from_version": from_version,
        "to_version": to_version,
        "added": added,
        "updated": updated,
        "added_count": len(added),
        "updated_count": len(updated),
        "incoming_source": incoming.get("source"),
    }
    history = list(merged.get("import_history", []))
    history.append(audit_entry)
    merged["import_history"] = history

    validate_library(merged)
    return merged, audit_entry


# =====================================================================
# Matching + classification engine (Sec 4.2.12.2)
# =====================================================================
# Coarse band vocabulary: maps a center frequency (GHz) to a band label so an
# observation carrying only a raw center frequency can still be matched.
_BAND_RANGES = [
    ("433MHz", 0.40, 0.46),
    ("868MHz", 0.86, 0.88),
    ("900MHz", 0.90, 0.93),
    ("915MHz", 0.90, 0.93),
    ("1.2GHz", 1.1, 1.3),
    ("2.4GHz", 2.40, 2.4835),
    ("5.2GHz", 5.15, 5.35),
    ("5GHz-WiFi", 5.15, 5.85),
    ("5.8GHz", 5.65, 5.925),
]

# Confidence tiers -- deliberately CATEGORICAL, not a fake blended 0-1 score
# (same doctrine as backend/CONFIDENCE_MODEL.md).
CONF_HIGH = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW = "LOW"
CONF_NONE = None

BASIS_BROADCAST = "broadcast_decode"   # decoded RID serial / DJI DroneID
BASIS_WIFI_IDENTITY = "wifi_identity"   # distinctive SSID / drone OUI beacon
BASIS_RF_SIGNATURE = "rf_signature"     # band + bw + control-link family only
BASIS_NONE = "none"                     # insufficient signature -> honest fail


def band_for_freq_ghz(freq_ghz: Optional[float]) -> Optional[str]:
    if freq_ghz is None:
        return None
    for label, lo, hi in _BAND_RANGES:
        if lo <= freq_ghz <= hi:
            return label
    return None


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _observation_bands(obs: Dict[str, Any]) -> List[str]:
    bands: List[str] = []
    for b in (obs.get("bands") or []):
        if b:
            bands.append(str(b))
    if obs.get("band"):
        bands.append(str(obs["band"]))
    fb = band_for_freq_ghz(obs.get("center_freq_ghz"))
    if fb:
        bands.append(fb)
    # normalize/dedupe
    out: List[str] = []
    for b in bands:
        if b not in out:
            out.append(b)
    return out


def _bands_overlap(entry_bands: Any, obs_bands: List[str]) -> bool:
    if not entry_bands or not obs_bands:
        return False
    es = {_norm(b) for b in entry_bands}
    os_ = {_norm(b) for b in obs_bands}
    if es & os_:
        return True
    # treat the two 5 GHz labels as compatible
    fuzzy = {"5ghz-wifi", "5.2ghz", "5.8ghz"}
    if (es & fuzzy) and (os_ & fuzzy):
        return True
    return False


def _lookup_by_id(lib: Dict[str, Any], eid: str) -> Optional[Dict[str, Any]]:
    return next((e for e in lib.get("entries", []) if e.get("id") == eid), None)


def _candidate(entry: Dict[str, Any], confidence: str, basis: str,
               reasons: List[str], score: Optional[float] = None,
               exact_model_confirmed: bool = False) -> Dict[str, Any]:
    """Shape one ranked candidate. `exact_model_confirmed` is TRUE only for a
    decoded-broadcast identity; an RF-signature candidate is ALWAYS False so the
    UI/consumer can never present a signature guess as a confirmed model."""
    return {
        "id": entry.get("id"),
        "make": entry.get("make"),
        "model": entry.get("model"),
        "class": entry.get("class"),
        "threat_level": entry.get("threat_level"),
        "match_basis": basis,
        "confidence": confidence,
        "exact_model_confirmed": bool(exact_model_confirmed),
        "identification_level": "make_model_serial" if exact_model_confirmed else "class_family",
        "score": score,
        "reasons": reasons,
        "countermeasures": entry.get("countermeasures"),
    }


def _match_droneid(lib: Dict[str, Any], droneid: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    make = _norm(droneid.get("make"))
    model = _norm(droneid.get("model"))
    if not make:
        return None
    best = None
    for e in lib.get("entries", []):
        marker = (e.get("signatures") or {}).get("droneid_marker")
        if not marker or _norm(marker.get("make")) != make:
            continue
        models = [_norm(m) for m in (marker.get("models") or [])]
        reasons = [f"DJI DroneID broadcast: make='{droneid.get('make')}'"]
        if model and model in models:
            reasons.append(f"model='{droneid.get('model')}' matched DroneID marker")
            return _candidate(e, CONF_HIGH, BASIS_BROADCAST, reasons,
                              exact_model_confirmed=True)
        # make matched but model not (yet) in the marker list -- still an exact
        # DECODED make; keep as the best make-level broadcast hit.
        if best is None:
            best = _candidate(e, CONF_HIGH, BASIS_BROADCAST, reasons,
                              exact_model_confirmed=bool(model == "" ))
    return best


def _match_wifi(lib: Dict[str, Any], wifi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ssid = str(wifi.get("ssid") or "")
    oui = _norm(wifi.get("oui"))
    for e in lib.get("entries", []):
        sig = e.get("signatures") or {}
        pat = sig.get("wifi_ssid_pattern")
        if ssid and pat:
            try:
                if re.search(pat, ssid, re.IGNORECASE):
                    return _candidate(
                        e, CONF_MEDIUM, BASIS_WIFI_IDENTITY,
                        [f"Wi-Fi SSID '{ssid}' matched pattern '{pat}' (beacon can be spoofed -> MEDIUM, not HIGH)"],
                        exact_model_confirmed=False)
            except re.error:
                pass
    if oui:
        for e in lib.get("entries", []):
            ouis = [_norm(o) for o in ((e.get("signatures") or {}).get("wifi_oui") or [])]
            if oui in ouis:
                return _candidate(
                    e, CONF_MEDIUM, BASIS_WIFI_IDENTITY,
                    [f"Wi-Fi OUI '{wifi.get('oui')}' matched a known drone OUI"],
                    exact_model_confirmed=False)
    return None


def _score_rf_signature(entry: Dict[str, Any], obs: Dict[str, Any],
                        obs_bands: List[str]) -> Tuple[float, List[str]]:
    """Score an entry against band + occupied-bw + control-link family + video.
    Returns (score, reasons). A class-level placeholder with null signatures
    scores ~0 so it is never surfaced from a bare RF signature."""
    sig = entry.get("signatures") or {}
    rf = entry.get("rf_profile") or {}
    score = 0.0
    reasons: List[str] = []

    if _bands_overlap(rf.get("bands"), obs_bands):
        score += 2.0
        reasons.append("band overlap")

    fam = _norm(obs.get("control_link_family") or obs.get("control_link_label"))
    entry_fam = _norm(rf.get("control_link_family"))
    if fam and entry_fam:
        if fam == entry_fam or fam in entry_fam or entry_fam in fam:
            score += 3.0
            reasons.append(f"control-link family '{rf.get('control_link_family')}'")

    bw = obs.get("occupied_bw_mhz")
    rng = sig.get("occupied_bw_mhz_range")
    if bw is not None and isinstance(rng, list) and len(rng) == 2 and rng[0] is not None:
        if rng[0] <= bw <= rng[1]:
            score += 1.5
            reasons.append(f"occupied BW {bw} MHz within [{rng[0]},{rng[1]}]")

    vt = _norm(obs.get("video_type"))
    if vt and _norm(rf.get("video_type")).startswith(vt.split()[0] if vt else ""):
        if vt.split()[0] in _norm(rf.get("video_type")):
            score += 1.0
            reasons.append(f"video type '{obs.get('video_type')}'")

    fhss = obs.get("fhss")
    if fhss is not None and sig.get("fhss") is not None and bool(fhss) == bool(sig.get("fhss")):
        score += 0.5
        reasons.append(f"FHSS={bool(fhss)}")

    return score, reasons


def match_detection(observation: Dict[str, Any],
                    lib: Optional[Dict[str, Any]] = None,
                    max_candidates: int = 5) -> Dict[str, Any]:
    """Match one observed detection's attributes against the library.

    `observation` accepts any of (all optional):
      band / bands / center_freq_ghz, occupied_bw_mhz, fhss (bool),
      control_link_family (or control_link_label), video_type,
      remoteid={uas_id, ua_type, make}, droneid={make, model, serial},
      wifi={ssid, oui}

    Returns a result dict:
      {
        match_basis, confidence, threat_level, exact_model_confirmed,
        best (candidate | None), candidates [ranked], message
      }

    Doctrine: a broadcast-decoded id yields an EXACT make/model/serial at HIGH
    confidence; an RF-signature-only match yields RANKED CLASS/FAMILY candidates
    at LOWER confidence and NEVER a confirmed exact model; nothing usable yields
    an honest 'unknown'.
    """
    if lib is None:
        lib = load_library()
    obs = observation or {}

    # ---- Tier 1: broadcast-decoded identity (HIGH, exact) ----
    droneid = obs.get("droneid") or {}
    if droneid.get("make"):
        hit = _match_droneid(lib, droneid)
        if hit:
            if droneid.get("serial"):
                hit["serial"] = droneid.get("serial")
                hit["reasons"].append(f"DroneID serial '{droneid.get('serial')}'")
            return _finalize(hit, [hit],
                             "Exact identification from decoded DJI DroneID broadcast.")

    remoteid = obs.get("remoteid") or {}
    rid_serial = remoteid.get("uas_id") or remoteid.get("serial")
    if rid_serial:
        # A decoded ASTM Remote ID serial IS the drone's real broadcast identity.
        make_hint = _norm(remoteid.get("make"))
        entry = None
        if make_hint:
            entry = next((e for e in lib.get("entries", [])
                          if _norm(e.get("make")).startswith(make_hint) or make_hint in _norm(e.get("make"))), None)
        base_entry = entry or {
            "id": None, "make": remoteid.get("make"),
            "model": remoteid.get("ua_type") or "per broadcast identity",
            "class": "unknown", "threat_level": "MEDIUM", "countermeasures": None,
        }
        cand = _candidate(base_entry, CONF_HIGH, BASIS_BROADCAST,
                          [f"ASTM Remote ID broadcast serial '{rid_serial}' decoded"],
                          exact_model_confirmed=True)
        cand["serial"] = rid_serial
        if remoteid.get("operator_id"):
            cand["operator_id"] = remoteid.get("operator_id")
            cand["reasons"].append(f"operator id '{remoteid.get('operator_id')}'")
        return _finalize(cand, [cand],
                         "Exact identification from decoded ASTM Remote ID broadcast serial.")

    # ---- Tier 2: distinctive Wi-Fi identity (MEDIUM) ----
    wifi = obs.get("wifi") or {}
    if wifi.get("ssid") or wifi.get("oui"):
        hit = _match_wifi(lib, wifi)
        if hit:
            return _finalize(hit, [hit],
                             "Identification from a distinctive Wi-Fi beacon (SSID/OUI) -- "
                             "beacons can be spoofed, so MEDIUM confidence, not exact-confirmed.")

    # ---- Tier 3: RF-signature-only -> ranked CLASS/FAMILY candidates (LOWER) ----
    obs_bands = _observation_bands(obs)
    have_signature = bool(
        obs_bands or obs.get("occupied_bw_mhz") is not None
        or obs.get("control_link_family") or obs.get("control_link_label"))
    scored: List[Tuple[float, Dict[str, Any], List[str]]] = []
    if have_signature:
        for e in lib.get("entries", []):
            s, reasons = _score_rf_signature(e, obs, obs_bands)
            if s > 0:
                scored.append((s, e, reasons))
        scored.sort(key=lambda t: t[0], reverse=True)

    if scored:
        top = scored[0][0]
        # MEDIUM only when the leader is strong (family + band aligned) AND clearly
        # ahead of the runner-up; otherwise LOW. Signature matches are NEVER HIGH
        # and NEVER exact-model-confirmed.
        runner = scored[1][0] if len(scored) > 1 else 0.0
        strong = top >= 4.5 and (top - runner) >= 1.5
        candidates = []
        for s, e, reasons in scored[:max_candidates]:
            conf = CONF_MEDIUM if (strong and s == top) else CONF_LOW
            candidates.append(_candidate(e, conf, BASIS_RF_SIGNATURE, reasons,
                                         score=round(s, 2), exact_model_confirmed=False))
        best = candidates[0]
        n = len(scored)
        msg = (f"RF-signature match: {n} class/family candidate(s) ranked by signature "
               f"overlap. This is a CLASS/FAMILY identification, not a confirmed exact "
               f"model -- only a decoded broadcast id (Remote ID / DroneID) can confirm "
               f"make/model/serial.")
        return _finalize(best, candidates, msg)

    # ---- Tier 4: honest fail ----
    return {
        "match_basis": BASIS_NONE,
        "confidence": CONF_NONE,
        "threat_level": "UNKNOWN",
        "exact_model_confirmed": False,
        "best": None,
        "candidates": [],
        "message": "Unknown -- insufficient signature to identify or classify this "
                   "contact. No guess is made (honest fail).",
    }


def _finalize(best: Dict[str, Any], candidates: List[Dict[str, Any]],
              message: str) -> Dict[str, Any]:
    return {
        "match_basis": best["match_basis"],
        "confidence": best["confidence"],
        "threat_level": classify_threat_level(best),
        "exact_model_confirmed": best.get("exact_model_confirmed", False),
        "best": best,
        "candidates": candidates,
        "message": message,
    }


def classify_threat_level(candidate: Optional[Dict[str, Any]]) -> str:
    """Threat level of the best match, or UNKNOWN when there is no usable match."""
    if not candidate:
        return "UNKNOWN"
    return candidate.get("threat_level") or "UNKNOWN"


# =====================================================================
# Summaries for the API/UI
# =====================================================================
def library_summary(lib: Dict[str, Any]) -> Dict[str, Any]:
    entries = lib.get("entries", [])
    by_class: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    for e in entries:
        by_class[e.get("class", "unknown")] = by_class.get(e.get("class", "unknown"), 0) + 1
        by_level[e.get("threat_level", "?")] = by_level.get(e.get("threat_level", "?"), 0) + 1
    return {
        "schema": lib.get("schema"),
        "version": lib.get("version"),
        "revision": lib.get("revision"),
        "updated": lib.get("updated"),
        "source": lib.get("source"),
        "provenance_note": lib.get("provenance_note"),
        "entry_count": len(entries),
        "by_class": by_class,
        "by_threat_level": by_level,
        "class_vocab": sorted(CLASS_VOCAB),
        "import_history_count": len(lib.get("import_history", [])),
    }
