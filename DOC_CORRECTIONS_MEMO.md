# Memo: Requirements-Doc Corrections Needed (for doc owner, not engineering)

Raised from the 2026-07-25 requirements gap audit. These are not code issues — they
need whoever maintains the official PMO Suraj requirements paper trail to reconcile
the documents themselves. Listed here so the correction doesn't get lost as a stray
comment in a task description.

## 1. Stale "GAP" markings in the Requirements Gap Analysis document

The gap-analysis doc marks these as unaddressed, but both are implemented in the
current codebase as of 2026-07-25:

- **OB-01** (bulk/broadcast takedown) — implemented: `mavlink_codec.py::broadcast_takedown()`, payload PL-010.
- **OB-03** (physical-parameter exploitation) — implemented: PROPELLER STOP, MEMORY ERASE, AUTOPILOT REBOOT, RTH HOME SPOOF payloads in `backend/payload_library.py`.

Recommend updating the gap-analysis doc to reflect these as closed, so future
reviewers don't re-flag already-solved items or double-count remaining work.

## 2. Numeric inconsistencies across documents

| Parameter | Conflicting values found | Docs involved |
|---|---|---|
| Operational range | 3km / 5km / 7km+ / 20km — four unreconciled figures; only ~1-2km actually demonstrated | OPERATIONAL REQUIREMENTS.md, demo plan, 2026-07-25 Army directive, original RFI response, DEMO_PLAYBOOK.md |
| Jamming power | 100W/7×30W spec vs. 100mW-10W actual hardware | Requirements doc vs. hardware backlog |
| Instantaneous bandwidth | 62MHz/channel spec vs. ~20MHz actual (HackRF-limited) | Requirements doc vs. actual RF front-end |
| Concurrent drone/swarm handling | 40-50 (Army ask) vs. ≥16/scalable-to-32 (SOL-04 spec) | Meeting notes vs. SOL-04 |
| Frequency coverage | 400MHz-6GHz claimed vs. only 915MHz/433MHz actually demonstrated | Capability statement vs. RFI-Response (which admits the narrower demonstrated range) |
| Delivery timeline | "~11 months" (early compliance doc) vs. "[TO BE COMPLETED]" (later, more rigorous capability statement) | CEMA_Compliance_v1.4.docx vs. later capability statement — a regression from committed to unknown |
| Indigenous content | "all software / partially hardware indigenous" (definitive, early doc) vs. "[TO BE COMPLETED]" (later doc) | Same pattern as above |

Recommend the doc owner pick one authoritative figure per parameter (or explicitly
version/date-stamp which figure supersedes which) so engineering isn't scoping
against four different range targets at once.

## 3. File-integrity issues found in the PMO Suraj folder itself

- `tiers and tasks previos.rtf` — appears misplaced; contains unrelated
  SDI-Agency-BEL project content, not CEMA/MEGHDUT material.
- `MCTE_BoM_Commercial_wSynergy.xlsx` — despite its filename/extension, its bytes
  are byte-identical (same MD5) to `Collaboration_Slide.pptx`. No real commercial
  Bill of Materials currently exists under that filename — worth regenerating or
  renaming so nobody mistakes the pptx for a real BoM.

## 4. Real logic gap worth flagging to the doc owner directly

`CEMA_Compliance_v1.4.docx` gives an unconditional "Yes" to jamming/injection
capability against adversary drones. In reality, the SiK-based injection path only
works against a **pre-paired reference craft** (FHSS pairing requirement) — it
cannot inject against an arbitrary, never-before-paired adversary drone without
first defeating that drone's own hop-sequence pairing, which is a separate,
much harder capability (see the ELRS/FrSky/FlySky/Spektrum hop-sequence findings
from tasks #42/#101). The compliance doc's unconditional "Yes" should be caveated
to reflect this real operational constraint.
