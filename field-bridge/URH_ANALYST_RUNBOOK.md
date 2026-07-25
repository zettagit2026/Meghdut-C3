# Analyst Runbook: Exporting IQ for Manual RE Analysis in URH

Scope (task #117, scoped by Software Architect task #108): this covers
getting a real captured IQ waveform for an `unclassified_signal` contact out
of MEGHDUT C³ and into **URH (Universal Radio Hacker)** for manual
inspection. URH is an external, GPL-3.0-licensed tool — it is **not**
vendored or embedded in this repo, and nothing in this codebase performs
demodulation, bit-slicing, or protocol inference on your behalf. That work
happens by you, in URH, using your own judgement.

## 1. Background: how the capture got there

`field-bridge/iq_capture.py` is a standalone, manually-invoked tool that
records real IQ off a HackRF One (receive-only) and writes a SigMF pair:

```
<basename>.sigmf-data   # raw interleaved signed 8-bit I/Q samples (SigMF datatype ci8)
<basename>.sigmf-meta   # JSON sidecar: center frequency, sample rate, datetime, sha512
```

It is **not** wired into the live detection pipeline automatically — an
operator runs it deliberately (typically when a Dashboard contact shows the
`UNCLASSIFIED` badge, i.e. `confidence_type == "unclassified_signal"`:
real energy-gated RF was seen, but the 3-class ML model's confidence in any
of its known classes was too low to trust). After capturing, the operator
attaches that capture to the detection record:

```
POST /api/detections/{detection_id}/iq-capture
{ "basename": "<basename>" }   # the file pair must already exist in IQ_CAPTURE_DIR
```

Once attached, the file pair is downloadable from the Dashboard.

## 2. Getting the export

1. Open the Dashboard. Find the contact with the orange `UNCLASSIFIED`
   badge (see `ConfidenceTypeBadge.jsx`).
2. Click **Export IQ** in that row (only shown for `unclassified_signal`
   contacts with a capture already attached).
3. Your browser downloads `<basename>_iq_export.zip`, containing the
   `.sigmf-data` + `.sigmf-meta` pair.
4. If no capture has been attached yet, the export fails with an honest
   "No IQ capture is associated with this detection" error — this is not a
   bug, it means nobody has run `iq_capture.py` against this contact yet.
   Ask whoever owns the HackRF hardware for this sensor to do so, then
   attach it via the endpoint above.
5. Unzip the archive locally: `unzip <basename>_iq_export.zip`.

## 3. Opening it in URH

URH is not installed by this project — get it from
https://github.com/jopohl/urh (GPL-3.0) or `pip install urh` and run
`urh` if you don't already have it.

1. **File → Open** (or drag-and-drop) the `.sigmf-data` file. URH has a
   native SigMF importer and will auto-read the paired `.sigmf-meta` file
   sitting next to it (same basename, same directory — do not rename one
   without the other, and do not unzip them into separate folders).
2. Confirm URH picked up the metadata correctly: check the displayed
   center frequency and sample rate against what's in the `.sigmf-meta`
   JSON (`captures[0]["core:frequency"]`, `global["core:sample_rate"]`).
   If URH shows 0 Hz / a wrong rate, the importer didn't find the sidecar —
   re-check the two files are alongside each other with matching basenames.
3. **Spectrogram / waterfall view**: look at the signal's time-frequency
   structure first. Look for: hopping patterns, bursts vs. continuous
   carrier, bandwidth, repetition interval.
4. **Automatic modulation detection**: URH's Analysis view can attempt
   auto-detection of modulation type (ASK/FSK/PSK/etc) and estimated
   symbol rate. Treat this as a starting hypothesis, not ground truth —
   verify against the eye diagram / constellation view.
5. **Bit-slicing / samples-per-symbol**: once you have a modulation
   hypothesis, use URH's demodulation view to set samples-per-symbol and
   extract a bitstream. Iterate — a real unknown signal often needs several
   passes before symbol boundaries look clean.

## 4. If you find a stable, repeatable pattern

Do **not** hardcode a guess back into this codebase's detection pipeline.
If URH analysis produces a bitstream you believe is a real, decodable
protocol frame (consistent framing/preamble/CRC across multiple captures),
the honest next step is the same one this project already uses for every
other real parser:

- Write up what you found: the modulation, symbol rate, bit pattern,
  and — critically — **evidence** (which capture files, what made you
  confident this isn't noise/an artifact).
- Follow the same evidence-based parser-building convention as
  `field-bridge/crsf_parser.py` — that parser was built from real captured
  and reverse-engineered CRSF frames, not assumed from a spec sheet, and it
  documents its own provenance inline. Any new protocol parser derived from
  URH findings should do the same: cite the actual capture(s) it was built
  from, document known limitations, and avoid claiming decode-confidence
  the evidence doesn't support.
- Hand the write-up + evidence to whoever is doing the next iteration of
  parser/classifier work on this project (currently tracked as backlog
  items, not this task) — this task (#117) stops at "get the analyst a
  clean IQ file," not "build the parser."

## What this feature deliberately does NOT do

- No demodulation, bit-slicing, or protocol inference happens in the
  backend or frontend — the export endpoint only bundles the two files
  that already existed on disk.
- No new dependency on URH or the `sigmf`/`urh` Python packages was added
  to this repo (server-side hand-rolled the SigMF metadata already; see
  `iq_capture.py`'s own docstring for why).
- No automatic capture-to-detection linkage exists — a human has to run
  `iq_capture.py` and attach the result. This is intentional: this project
  does not fabricate a "signal was captured" claim it can't back up.
