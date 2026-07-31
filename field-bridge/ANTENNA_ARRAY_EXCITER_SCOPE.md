# Antenna Array / Multi-Channel Exciter -- Scoping Document (OB-05)

Status: SCOPING ONLY -- no implementation code, no procurement commitment.
Author: Software Architect agent, 2026-07-26.
Scope: Army requirement OB-05, task #120. Distinct from task #20
(amplitude-comparison DF using existing independent HackRFs, already
documented in `DIRECTION_FINDING_NOTES.md`) and from task #57/passive-radar
work (`PASSIVE_RADAR_ARCHITECTURE.md`) -- this document is about the array
and multi-channel exciter hardware architecture itself.

---

## 1. What is OB-05 actually asking for?

"Multi-channel exciter" is transmit-side terminology (an "exciter" is the
signal-generation/upconversion stage that drives a power amplifier chain --
it is not a receiver term). Read literally, OB-05 is a **coherent
multi-element transmit** requirement: N channels, phase- and
amplitude-controlled relative to each other, driving N antenna elements so
their radiated fields combine constructively in a chosen direction
(beamforming) and destructively elsewhere.

Reasoning from the surrounding requirements (per
`project_meghdut_c3_army_directive.md`'s four priorities: full-spectrum
scan, **7km+ range**, automated scan+takedown, RF-passive detection
+takedown):

- A single-antenna, single-channel jammer radiates roughly isotropically
  (or with whatever gain a single fixed directional antenna gives). Closing
  a 7km+ range gap against a fixed total RF output power budget has exactly
  two levers: raise total transmit power, or concentrate the same power
  into a narrower beam pointed at the target. Beamforming with N coherent
  elements gives an array gain of up to ~10*log10(N) dB in the main-lobe
  direction *for the same per-element power* -- e.g. an 8-element coherent
  array can add ~9 dB of directive gain over one element, which is a
  meaningfully large fraction of what's needed to push effective range from
  a few km to 7km+ (range in a power-limited link scales roughly with the
  square root of EIRP for free-space-like geometry, so +9 dB EIRP is
  worth roughly a 2.8x range multiplier, all else equal).
- The same coherent array, used receive-side, directly improves
  angle-of-arrival (AoA) accuracy far beyond the coarse two-antenna
  amplitude-comparison DF already scoped in task #20 -- true phase
  interferometry across N elements gives bearing resolution that improves
  with aperture size (more elements / wider spacing), not just a coarse
  "which side is it on" ratio.
- Because "coherent multi-element" is the *same underlying hardware
  requirement* (phase-locked multi-channel RF chains) whether used for TX
  beamforming or RX AoA, and because OB-05 explicitly says "exciter" (a TX
  term), the most defensible reading is: **OB-05 is primarily a TX
  beamforming requirement, with RX-side coherent AoA as a natural,
  low-incremental-cost side benefit of the same hardware once built.**
  This directly explains why the Army would ask for this now, layered on
  top of the already-scoped amplitude-comparison DF (task #20): amplitude
  DF is the cheap/immediate coarse-bearing stopgap; OB-05 is the
  harder, better long-term answer to both the range/power problem *and*
  the bearing-accuracy problem simultaneously, which is unusual and worth
  stating plainly -- it is rare that one hardware investment closes two
  separate stated gaps (range and DF precision) at once, which is likely
  exactly why it appears as its own requirement rather than being folded
  into either the jamming-power or DF line items.

Recommendation to the Army/PMO: confirm this reading explicitly (TX
beamforming as primary driver, RX AoA as secondary benefit) before
committing budget, since it changes calibration priorities in §3 below
(TX phase coherence into free space is a harder calibration problem than
RX-only phase coherence at the ADC).

---

## 2. Real hardware options

Two credible platform families exist for phase-coherent multi-channel
operation. Both are commercial SDR/RF products; nothing below is
invented for this document.

### 2.1 USRP N310 / X410 (Ettus Research / NI), shared LO/clock distribution

- The N310 is a 4x4 MIMO-capable SDR in one chassis, with an internal
  clock-distribution network across its four RX/TX chains -- this is the
  standard commercial answer to "I need N phase-coherent channels" without
  building custom clock distribution.
- Multiple N310/X410 units can be chained via an external reference
  (10 MHz + PPS, or a shared Rb/GPSDO reference) through Ettus's
  `OctoClock`/`OctoClock-G` clock-distribution accessory, to scale beyond
  one chassis's channel count while keeping all chassis phase-locked to
  the same reference.
- X410 (newer generation) adds higher bandwidth-per-channel and
  RFNoC/FPGA-level control over phase, which is relevant if fine-grained
  per-element phase steering needs to happen in real time (beam steering)
  rather than being fixed at build time.
- Trade-off: this is the "buy a proven, integrated, phase-coherent
  platform" option -- higher unit cost, but clock distribution, phase
  calibration infrastructure, and multi-channel software support (UHD)
  are already solved problems on this platform, which matters given this
  team's stated priority to build from proven reference implementations
  rather than reinvent-then-patch (per
  `feedback_use_zettagit_exhaustively_not_reactively.md`).

### 2.2 Multiple HackRF Ones with an external clock reference (CLKIN/CLKOUT)

- **Important correction to this task's stated premise**: the HackRF One
  hardware does have physical CLKIN/CLKOUT SMA connectors (a documented
  hardware feature of the board -- one unit's CLKOUT can feed another
  unit's CLKIN, or all units can be fed from one external 10 MHz
  reference), but a repo-wide grep of `field-bridge/` confirms **no code
  in this codebase configures, uses, or even mentions CLKIN/CLKOUT
  anywhere** -- `hackrf_config.py` (task #19) only assigns serial numbers
  to logical roles (`primary`/`secondary`) for device *selection*, and
  `DIRECTION_FINDING_NOTES.md` explicitly states current DF work assumes
  "two independent, free-running HackRF Ones have no such shared clock."
  This document should not imply that groundwork exists when it does not
  -- task #19/task #20's groundwork is serial-based device addressing
  only, not clock distribution. Wiring CLKIN/CLKOUT and validating actual
  phase coherence across units would be new hardware-integration work,
  not something already done.
- Even with CLKIN/CLKOUT wired, HackRF's own architecture is a
  weaker starting point than USRP for this specific requirement: each
  HackRF has independent local oscillator synthesis (MAX2837 transceiver
  + RFFC5072 mixer) fed from a shared *reference* clock via CLKIN, which
  removes long-term frequency drift between units, but does **not** by
  itself guarantee a fixed, known, stable **phase** relationship at the
  RF output -- each unit's PLL still locks independently to that
  reference, and PLL lock is not phase-deterministic across units or
  across power-cycles without an explicit phase-alignment/calibration
  step (see §3). USRP's shared-LO architecture (distributing the actual
  LO signal, not just a reference clock, to every channel in the chassis)
  is a materially stronger phase-coherence starting point than
  reference-clock-only synchronization.
- Where multi-HackRF-plus-external-clock remains worth considering: low
  budget, small element count (2-4), and if RX-side coarse-to-medium AoA
  improvement is the actual near-term goal rather than full TX
  beamforming -- i.e., as an interim step if the Army confirms task #20's
  amplitude DF is the near-term priority and OB-05's harder beamforming
  goal is a later phase.

**Recommendation**: if OB-05 is confirmed as primarily a TX beamforming
requirement (per §1), USRP N310/X410 + OctoClock is the technically
credible path; multi-HackRF-plus-CLKIN is not a credible substitute for
true beamforming without a nontrivial phase-calibration engineering effort
on top of hardware this codebase has not yet touched.

---

## 3. What phase coherence actually requires (the hard part)

Phase coherence across N channels needs all of the following -- listing
each concretely because "just share a clock" is a common oversimplification:

1. **Shared frequency reference** (10 MHz reference, or a GPSDO/Rubidium
   standard feeding all units) -- necessary but not sufficient. This
   prevents frequency drift between channels but does not fix phase.
2. **Shared LO (local oscillator) distribution**, not just a shared
   reference -- ideally the actual mixing LO signal is generated once and
   split/distributed to every channel (USRP's internal architecture, or an
   external LO-distribution amplifier feeding multiple independent
   transceiver boards), so every channel's upconversion starts from the
   identical LO phase, not merely the identical LO *frequency*. Two PLLs
   locked to the same reference frequency do not, in general, come up at
   the same phase -- PLL lock phase is influenced by loop-filter transients
   and is not deterministic across power-cycles unless explicitly
   controlled for.
3. **A known, fixed cable/path-length difference between each channel's
   exciter output and its antenna element** -- any differential cable
   length directly translates to a phase offset at RF (phase error =
   2*pi*length_difference/wavelength), so cable lengths must be matched or
   measured and compensated in the beamforming weights.
4. **A calibration procedure**, run at least once at build/install time and
   ideally re-checked periodically (temperature drift changes both cable
   electrical length and PLL/mixer phase slightly): inject a known
   reference signal (or use one channel as reference and null the array by
   sweeping a corrective phase on each other channel), measure the actual
   per-channel phase offset with a vector network analyzer, spectrum
   analyzer with phase-capable front end, or a calibrated far-field/
   near-field antenna range, and store the resulting per-channel phase
   correction table -- this is directly analogous in spirit to the
   empirical delta_dB(theta) calibration table already scoped for
   amplitude DF in `DIRECTION_FINDING_NOTES.md`, just for phase instead of
   amplitude, and for TX instead of RX.
5. **Real-time phase control per channel** if the beam needs to steer
   (rather than being fixed at build time) -- this requires either
   per-channel programmable phase shifters in the RF chain, or (on an
   SDR platform like USRP) baseband-level complex-weight application
   before upconversion, which is the more common modern approach and
   avoids needing analog phase-shifter hardware per element.
6. **Temperature and mechanical stability** of the whole assembly in the
   field -- cable flex, connector wear, and thermal expansion all
   introduce phase drift over time and across deployments; a field
   recalibration procedure (not just a one-time factory calibration)
   should be assumed necessary for a fielded system, not an optional
   nice-to-have.

None of items 1-6 exist in this codebase today. This is genuinely new
hardware-integration and RF-metrology work, not a software task.

---

## 4. Rough array geometry recommendation

Using standard array-design practice (not guessed numbers): for a
uniform linear or planar array intended to avoid grating lobes (spurious
secondary main lobes that would radiate/receive energy in unintended
directions), element spacing d should satisfy:

    d <= lambda / (1 + |sin(theta_max)|)

which for a broadside array steered near boresight (theta_max small)
reduces to the standard rule-of-thumb **d ~ lambda/2** cited in the task.
For a design intended to steer well off boresight (wide scan angle), a
tighter spacing closer to ~0.4*lambda is the more conservative standard
choice to keep grating lobes fully suppressed across the full scan range.

Applying this to the bands already in use in this codebase
(`hackrf_rx.py`'s `DEFAULT_BANDS_MHZ`/`EXTRA_BANDS_MHZ`) rather than
guessing a frequency:

| Band | Representative freq | lambda (c/f) | d ~ lambda/2 |
|---|---|---|---|
| SiK-915 (UHF, 902-928 MHz) | 915 MHz | ~32.8 cm | ~16.4 cm |
| LRS-433 (UHF, 420-450 MHz) | 433 MHz | ~69.3 cm | ~34.6 cm |
| DJI-2G4 (2400-2483 MHz) | 2440 MHz | ~12.3 cm | ~6.1 cm |
| DJI-5G8 / FPV-5G8 (5725-5850 MHz) | 5800 MHz | ~5.17 cm | ~2.6 cm |

Observations that follow directly from this table, not from intuition:

- A single physical array **cannot** be simultaneously half-wavelength
  optimal across this whole band spread -- 915 MHz needs ~16 cm spacing,
  5.8 GHz needs ~2.6 cm spacing, over a 6:1 ratio. This is the standard
  wideband-array problem: either (a) build separate arrays per band
  (a UHF array physically distinct from a 2.4/5.8 GHz array, which is
  common practice -- e.g. a UHF Yagi array plus a separate 2.4/5.8 GHz
  patch array on the same mast), or (b) accept that a single array
  optimized for one band (most likely 2.4/5.8 GHz, since that's where the
  jamming-power/range requirement is most acute against DJI OcuSync-class
  threats) will have grating-lobe risk or reduced gain at other bands.
  Recommend (a) -- band-specific sub-arrays -- as the technically sound
  default given this spread, to be confirmed once OB-05's target band(s)
  are explicitly stated by the Army (the requirement as summarized does
  not yet specify which band(s) the beamforming applies to).
- **Element count**: not specified by the Army per the material reviewed
  for this task. Array gain scales as ~10*log10(N) dB, and cost/complexity
  (N phase-coherent RF chains, N calibrated cable runs, N antenna
  elements) scales roughly linearly with N. A modest 4-8 element array is
  a reasonable starting recommendation for a first fielded system --
  enough to meaningfully move the range/power needle (4 elements: ~6 dB
  gain; 8 elements: ~9 dB gain) without the calibration and hardware-chain
  complexity of a much larger array. This should be revisited once (a) the
  Army confirms target band(s) and (b) an actual link-budget calculation
  (current TX power, current effective range, required 7km+ range) is
  available to size the gain actually needed -- this document does not
  have that link-budget data and should not invent a number to fit it.

---

## 5. Explicit non-scope

- **No code** is written or implied as part of this document. No
  `beamform_exciter.py`, no phase-control library, no calibration script
  -- all such implementation is future work, to be scoped separately once
  a hardware platform decision (§2) is made.
- **No procurement commitment.** Nothing here authorizes purchase of
  USRP N310/X410, OctoClock, additional HackRFs, phase-coherent cabling,
  a VNA, or any antenna array hardware. Cost/quantity/vendor decisions are
  explicitly out of scope for this scoping pass.
- **No clock-distribution or phase-calibration hardware work has been
  done** -- §2.2's correction stands: CLKIN/CLKOUT wiring and any actual
  phase-coherence validation across HackRF units does not exist in this
  codebase today, regardless of this task's original framing.
- **No confirmed target frequency band(s) or element count from the
  Army** -- §4's numbers are standard array-design formulas applied to
  bands this codebase already sweeps, not a confirmed Army specification.
  This must be confirmed with PMO Suraj before any design proceeds past
  this scoping stage.
- This document does not decide whether OB-05 is TX beamforming, RX AoA,
  or both -- §1 gives a reasoned recommendation (primarily TX) but
  explicit Army/PMO confirmation is required before committing to one
  interpretation.
