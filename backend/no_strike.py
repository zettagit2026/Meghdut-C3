"""No-strike / civilian-protection registry -- pure matcher core (P1 of
no-strike-registry.md).

WHAT THIS IS
------------
`match(identity, entries) -> dict`. Given ONE contact's identity (a plain dict
carrying whatever the contact has: `ssid`, `bssid`, `oui`, `manuf`) and the
commander-managed no-strike registry entries (plain dicts), decide whether the
contact matches a protected civilian/friendly/neutral entry, and if so which.
Plain dicts/lists in, one plain dict out. No Mongo, no websocket, no FastAPI,
no threading. The ONLY non-stdlib dependency is `re` (stdlib). This module is a
pure leaf, exactly like `sop_engine.py` and `geo_zone.py`.

WHAT THIS IS NOT -- CRITICAL SAFETY BOUNDARY
--------------------------------------------
This module has NO capability to transmit, jam, spoof, arm, key TX, clear a
TX-halt, mint any token, or mutate a detection/track. It imports NOTHING from
the transmit spine. Its strongest possible output is a DISPLAY-LABEL match
verdict (`{matched, entry_id, category, hard, label, basis, randomized}`). This
is P1 -- the safe foundation. There is deliberately NO fire-path, ROE, or
classification coupling in P1; the classification-demote (P2) and the fire-time
hard block (P3) both consume THIS matcher so the two decisions stay in sync,
but that wiring lives in server.py, not here.

MATCH PRECEDENCE (most-specific-first, first hit wins)
------------------------------------------------------
  bssid (48-bit exact) -> ssid_exact -> ssid_prefix (case-insensitive)
  -> oui (24-bit) -> vendor_regex (vs manuf)
`basis` in the returned dict names the field that matched.

MAC-RANDOMIZATION CAVEAT (honesty -- never silently trust a randomized MAC)
---------------------------------------------------------------------------
A modern client randomizes its MAC (sets the locally-administered bit). An
OUI/BSSID entry protects a STABLE infrastructure AP, not a roaming randomized
client. When the input MAC is locally-administered, a MAC-based match
(bssid/oui) is flagged `randomized: True` so a caller (P2/P3) can treat it as
"possible civilian, unverified" rather than a hard, trusted infra match.
`is_locally_administered(mac)` is exposed for callers that need the flag
independently.

NEVER RAISES
------------
Malformed input (non-dict identity, junk MACs, a bad stored entry, a broken
vendor_regex) yields `{"matched": False}` -- never an exception.
"""
from __future__ import annotations

import re
from typing import Any

# The only categories a registry entry may carry. Mirrored by the server-side
# Pydantic pattern; duplicated here as data (no import) so the pure matcher has
# no server dependency.
CATEGORIES = ("CIVILIAN_INFRASTRUCTURE", "FRIENDLY_OWN_FORCE", "NEUTRAL")

# Cap the manuf string a vendor_regex is run against, to bound the work a
# (commander-authored) regex can do on any single contact -- defence in depth
# against a pathological pattern/input pair. Kept SMALL (a manufacturer string
# is short) so even a poorly-shaped pattern has little input to chew on.
_MAX_MANUF_LEN = 64

# Hard cap on a stored vendor_regex's SOURCE length. A no-strike vendor match
# is a short manufacturer token ("Apple", "DJI"); a long pattern is the shape a
# ReDoS payload takes, so we reject it at write time and refuse to compile it at
# load time. Commander-only input, but this is a single-threaded asyncio event
# loop -- a catastrophic-backtracking regex would freeze the whole C2 console /
# WS / TX-gate, so the guard is defence-in-depth, not a trust boundary.
_VENDOR_REGEX_MAX_LEN = 64

_SEP_RE = re.compile(r"[:\-.\s]")
_HEX_RE = re.compile(r"^[0-9A-F]+$")

# Adjacent unbounded-quantifier runs (".*.*", ".+.+", ".*.+", ".+.*") -- a
# classic catastrophic-backtracking shape independent of any grouping.
_ADJACENT_QUANT_RE = re.compile(r"\.[*+]\.[*+]")

_NO_MATCH = {"matched": False}


def has_catastrophic_backtracking(pattern: Any) -> bool:
    """Pragmatic heuristic: True when `pattern` carries a catastrophic
    (exponential) backtracking shape. Deliberately conservative -- a heuristic
    reject is acceptable for the commander-only vendor_regex, and a rejected
    legitimate pattern is a clear 422 the commander can rephrase, whereas an
    accepted pathological one freezes the event loop.

    Detects two shapes with NO third-party dependency (sovereign/offline
    appliance -- no `regex`/`re2`):
      1. A nested quantifier -- a quantified group that is itself quantified,
         e.g. ``(a+)+`` / ``(a*)*`` / ``(a+)*`` / ``(.*)+`` / ``(a{1,9})+``.
      2. Adjacent unbounded-quantifier runs, e.g. ``.*.*`` / ``.+.+``.
    """
    if not isinstance(pattern, str) or pattern == "":
        return False
    if _ADJACENT_QUANT_RE.search(pattern):
        return True
    # Walk the source tracking group nesting. For each group, remember whether
    # its body contains an unbounded quantifier; when the group closes and is
    # itself immediately followed by a quantifier, that is a nested-quantifier
    # (exponential) shape.
    stack: list[bool] = []  # per open group: has its body seen a quantifier?
    i = 0
    n = len(pattern)
    escaped = False
    while i < n:
        c = pattern[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if c == "\\":
            escaped = True
        elif c == "(":
            stack.append(False)
        elif c == ")":
            body_had_quant = stack.pop() if stack else False
            nxt = pattern[i + 1] if i + 1 < n else ""
            if body_had_quant and nxt in "*+{":
                return True
            # A group quantified with * or + is itself an unbounded quantifier
            # from the PARENT group's point of view (so ``((a+)+)+`` still trips).
            if nxt in "*+" and stack:
                stack[-1] = True
        elif c in "*+":
            if stack:
                stack[-1] = True
        elif c == "{":
            # Treat any `{...}` as an unbounded-ish quantifier for nesting
            # purposes (conservative -- a rare literal `{` is a cheap false
            # positive on commander input).
            if stack:
                stack[-1] = True
        i += 1
    return False


def compile_vendor_regex(pattern: Any) -> "re.Pattern[str] | None":
    """Safely compile a stored vendor_regex to a precompiled pattern, or return
    None when it must never reach the matcher (empty, over the length cap, a
    catastrophic-backtracking shape, or uncompilable). Callers precompile ONCE
    at cache-load so the hot classification path never re-compiles per tick and
    a broken/dangerous pattern can never be run. Never raises."""
    if not isinstance(pattern, str) or pattern.strip() == "":
        return None
    if len(pattern) > _VENDOR_REGEX_MAX_LEN:
        return None
    if has_catastrophic_backtracking(pattern):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def normalize_mac(mac: Any, octets: int = 6) -> str | None:
    """Normalize `mac` to canonical upper/colon form of exactly `octets` bytes
    (6 for a BSSID, 3 for an OUI), e.g. "aa-bb-cc-dd-ee-ff" -> "AA:BB:CC:DD:EE:FF".

    Returns None for anything that is not exactly `octets` bytes of hex once
    separators are stripped (so junk input is an honest miss, never a guess).
    """
    if not isinstance(mac, str):
        return None
    raw = _SEP_RE.sub("", mac).upper()
    if len(raw) != octets * 2 or not _HEX_RE.match(raw):
        return None
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))


def is_locally_administered(mac: Any) -> bool:
    """True when `mac`'s locally-administered bit (bit 1 of the first octet,
    mask 0x02) is set -- i.e. a randomized / not-globally-unique MAC. Accepts a
    full BSSID or a 24-bit OUI (the bit lives in the first octet of both).
    Returns False for anything unparseable."""
    norm = normalize_mac(mac, 6) or normalize_mac(mac, 3)
    if norm is None:
        return False
    try:
        return bool(int(norm[:2], 16) & 0x02)
    except ValueError:
        return False


def validate_match(match_block: Any) -> tuple[bool, str]:
    """Validate a registry entry's `match` block for the CRUD write path (the
    server raises 422 with `reason` on failure). Pure -- no side effects.

    Rules: at least one match key must be set; a `bssid` must be a 48-bit MAC;
    an `oui` must be a 24-bit prefix; a `vendor_regex` must compile.
    """
    if not isinstance(match_block, dict):
        return False, "match must be an object"

    keys = ("ssid_exact", "ssid_prefix", "bssid", "oui", "vendor_regex")
    present = [k for k in keys if _nonempty(match_block.get(k))]
    if not present:
        return False, ("match must set at least one of "
                       "ssid_exact/ssid_prefix/bssid/oui/vendor_regex")

    bssid = match_block.get("bssid")
    if _nonempty(bssid) and normalize_mac(bssid, 6) is None:
        return False, "bssid must be a 48-bit MAC (6 hex octets)"

    oui = match_block.get("oui")
    if _nonempty(oui) and normalize_mac(oui, 3) is None:
        return False, "oui must be a 24-bit OUI prefix (3 hex octets)"

    vendor_regex = match_block.get("vendor_regex")
    if _nonempty(vendor_regex):
        vr = str(vendor_regex)
        if len(vr) > _VENDOR_REGEX_MAX_LEN:
            return False, (f"vendor_regex must be at most {_VENDOR_REGEX_MAX_LEN} "
                           f"characters (a vendor match is a short token)")
        if has_catastrophic_backtracking(vr):
            return False, ("vendor_regex has a catastrophic-backtracking shape "
                           "(a nested quantifier like (a+)+ or an adjacent .*.* "
                           "run) that could freeze the event loop; simplify it")
        try:
            re.compile(vr)
        except re.error as exc:
            return False, f"vendor_regex is not a valid regular expression: {exc}"

    return True, ""


def match(identity: Any, entries: Any) -> dict:
    """Return the no-strike verdict for one contact `identity` against the
    registry `entries`.

    On a hit:
        {matched: True, entry_id, category, hard, label, basis, randomized}
    where `basis` is the field that matched
    (bssid|ssid_exact|ssid_prefix|oui|vendor_regex) and `randomized` is True
    only when the match was MAC-based (bssid/oui) AND the contact's MAC is
    locally-administered (see module docstring).

    On no hit, or on any malformed input: {"matched": False}. Never raises.
    """
    try:
        if not isinstance(identity, dict) or not isinstance(entries, (list, tuple)):
            return dict(_NO_MATCH)
        rows = [e for e in entries if isinstance(e, dict)]
        if not rows:
            return dict(_NO_MATCH)

        ssid = identity.get("ssid")
        ssid_l = str(ssid).lower() if _nonempty(ssid) else None
        bssid_norm = normalize_mac(identity.get("bssid"), 6)
        oui_field = normalize_mac(identity.get("oui"), 3)
        oui_from_bssid = bssid_norm[:8] if bssid_norm else None  # "AA:BB:CC"
        cand_ouis = {o for o in (oui_field, oui_from_bssid) if o}
        manuf = identity.get("manuf")
        manuf_s = (str(manuf)[:_MAX_MANUF_LEN] if _nonempty(manuf) else None)

        # A MAC-based match on a locally-administered (randomized) input MAC is
        # flagged so a caller never silently trusts it as a stable infra AP.
        randomized = is_locally_administered(
            identity.get("bssid") if bssid_norm else identity.get("oui"))

        # 1. bssid (48-bit exact) -- most specific.
        if bssid_norm is not None:
            for e in rows:
                blk = _blk(e)
                if normalize_mac(blk.get("bssid"), 6) == bssid_norm:
                    return _hit(e, "bssid", randomized)

        # 2. ssid_exact.
        if ssid_l is not None:
            for e in rows:
                target = _blk(e).get("ssid_exact")
                if _nonempty(target) and str(target) == str(ssid):
                    return _hit(e, "ssid_exact", False)

        # 3. ssid_prefix (case-insensitive).
        if ssid_l is not None:
            for e in rows:
                prefix = _blk(e).get("ssid_prefix")
                if _nonempty(prefix) and ssid_l.startswith(str(prefix).lower()):
                    return _hit(e, "ssid_prefix", False)

        # 4. oui (24-bit) -- from an explicit oui field or derived from bssid.
        if cand_ouis:
            for e in rows:
                eo = normalize_mac(_blk(e).get("oui"), 3)
                if eo is not None and eo in cand_ouis:
                    return _hit(e, "oui", randomized)

        # 5. vendor_regex vs manuf -- least specific. Prefer the pattern the
        # caller precompiled ONCE at cache-load (`_vendor_regex_compiled`): the
        # hot classification path then never re-compiles per tick, and a
        # broken/dangerous pattern (precompiled to None at load) is skipped and
        # can never be run. A caller that did not precompile (e.g. a direct unit
        # test) falls back to a defensive per-call compile, still non-fatal.
        if manuf_s is not None:
            for e in rows:
                blk = _blk(e)
                if "_vendor_regex_compiled" in blk:
                    compiled = blk["_vendor_regex_compiled"]
                    if compiled is None:  # known-broken/dangerous at load -> skip
                        continue
                    try:
                        if compiled.search(manuf_s):
                            return _hit(e, "vendor_regex", False)
                    except re.error:
                        continue
                    continue
                pattern = blk.get("vendor_regex")
                if not _nonempty(pattern):
                    continue
                compiled = compile_vendor_regex(str(pattern))
                if compiled is None:  # over-cap / dangerous / uncompilable -> skip
                    continue
                try:
                    if compiled.search(manuf_s):
                        return _hit(e, "vendor_regex", False)
                except re.error:
                    continue

        return dict(_NO_MATCH)
    except Exception:
        # Governing honesty rule: a malformed input can never crash the caller
        # -- an unparseable identity is an honest non-match, not an exception.
        return dict(_NO_MATCH)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _nonempty(value: Any) -> bool:
    """True for a value that is present and, for strings, not blank."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _blk(entry: dict) -> dict:
    blk = entry.get("match")
    return blk if isinstance(blk, dict) else {}


def _hit(entry: dict, basis: str, randomized: bool) -> dict:
    return {
        "matched": True,
        "entry_id": entry.get("id"),
        "category": entry.get("category"),
        "hard": bool(entry.get("hard")),
        "label": entry.get("label"),
        "basis": basis,
        "randomized": bool(randomized),
    }
