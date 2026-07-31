# FPGA Acceleration Scope — OB-06 (Army requirement, CRITICAL)

**Status:** Scoping/design only. No HDL, no drivers, no procurement commitment.
This document evaluates whether/where FPGA-based acceleration applies to the
current MEGHDUT C3 signal-processing stack, and lays out options and honest
effort estimates for the Army's OB-06 requirement ("FPGA-based processing
acceleration backbone").

Current stack, for reference: pure Python (NumPy/SciPy) running on a
general-purpose CPU, fed by one (soon two) HackRF One SDRs via `hackrf_sweep`
(spectrum sweeps) and `hackrf_transfer` (raw IQ capture). No FPGA hardware or
code exists anywhere in this repo today.

---

## 1. Where is the CPU actually the bottleneck?

The honest answer, based on reading `hackrf_rx.py`, `passive_radar_bridge.py`,
`ml_classify_bridge.py`, and `passive_radar/caf.py`: **today, the CPU is not
the bottleneck in most of this pipeline — the bottleneck is USB/device
throughput and a single-SDR sweep cadence.** But there are two real spots
where an FPGA-class accelerator would help, and several where it plainly would
not.

### Real candidates for FPGA offload

- **Bistatic radar CAF (Cross-Ambiguity Function) computation**
  (`passive_radar/caf.py`, `caf_bruteforce`/`caf_fft_batched`). This is the one
  place in the codebase doing genuinely heavy, latency-sensitive DSP: for each
  trial Doppler bin, demodulate the surveillance channel and cross-correlate
  against the reference channel over a lag window (`DEFAULT_MAX_LAG=512`),
  across ~100 Doppler bins (`DEFAULT_DOPPLER_HZ = arange(-200,201,4)`), per
  ~0.5s block (`DEFAULT_BLOCK_SAMPLES ≈ 1.05M samples`). This is a classic
  passive-radar workload (per-Doppler-bin FFT correlation) that scales to
  real-time constraints as block size, lag window, or Doppler resolution grow
  — exactly the workload FPGAs (and GPUs) are built for: massively parallel,
  fixed-structure, streaming FFT/correlation math with deterministic timing.
  This is currently CPU-only (`scipy.signal.fftconvolve`); it is real, is
  already the architecturally-flagged "throughput-oriented" path
  (`PASSIVE_RADAR_ARCHITECTURE.md §2.3/§5`), and is the strongest candidate in
  this repo for FPGA acceleration.

- **Wideband channelization / continuous wideband FFT**, if/when the "true
  continuous 400MHz–6GHz sweep" gap noted in `hackrf_rx.py`'s docstring
  (task #55, currently explicitly NOT implemented — only prioritized
  sub-band cycling exists) is ever built with real wideband hardware. A
  polyphase filterbank / wideband channelizer processing multi-hundred-MHz
  instantaneous bandwidth in real time is squarely FPGA/RFSoC territory —
  this is not something the current HackRF-based sub-band cycling approach
  needs, but it is exactly the kind of workload an FPGA backbone is *for*, and
  is likely the underlying reason OB-06 asks for FPGA acceleration at all
  (full-spectrum, real-time coverage per the Meghdut C3 directive's Priority
  1 "full-spectrum scan").

- **Real-time wideband demodulation feeding the ML classifier**, if capture
  windows/sample rates in `ml_classify_bridge.py` (currently 10 MSPS,
  0.3s bursts, gated and infrequent) are scaled up to continuous/always-on
  wideband IQ streaming for classification, rather than today's short,
  energy-gated bursts. At today's gated/bursty scale this is not a CPU
  bottleneck; at "always-on wideband ML front end" scale it would be.

### Not realistic FPGA candidates

- **Protocol/byte-level parsing and heuristics** — the Wi-Fi/Bluetooth/LoRa/
  ELRS-hop persistence heuristics in `hackrf_rx.py` (channel-center
  comparisons, rolling-window duty-cycle checks, dict/deque bookkeeping) and
  the merge/OOD/calibration logic in `ml_classify_bridge.py` are simple
  scalar/control-flow code operating on a handful of numbers per cycle
  (several times per second at most). These already run in microseconds on a
  general-purpose CPU. FPGA offload would add integration complexity for zero
  measurable benefit here.

- **`hackrf_sweep` energy-detection sweeps themselves** — the actual
  bottleneck here (per the FIELD FIX 2026-07-22 note in `hackrf_rx.py`) is
  USB open/close churn and device wedging on a single HackRF, not CPU-bound
  post-processing of the returned power bins (`max()` over ~64-6000 floats).
  An FPGA cannot fix a USB device-busy/hang problem; that is a hardware
  procurement / driver-layer issue (task #55/multi-device work), unrelated to
  compute acceleration.

- **The ResNet18 ML classifier inference** (`gamutrf_infer.py`, invoked from
  `ml_classify_bridge.py`) — small, short-burst, already-infrequent (gated,
  ~12s cycle by default) CNN inference on a single ~0.3s IQ window. This is a
  GPU/NPU workload by convention (see §4), not typically what FPGA
  procurement is aimed at, though FPGA inference accelerators exist (see §2).
  At current call rates this is not a bottleneck on CPU either.

**Bottom line:** the current architecture's real-time compute pressure is
concentrated in the passive-radar CAF path, and prospectively in wideband
channelization if/when true wideband hardware is procured. Everything else in
this repo is control-flow/heuristic code that a CPU already handles fine.

---

## 2. Concrete, real hardware options (not vaporware)

Rough cost tiers as of 2026, actual list/street prices vary and are excluded
from firm commitment here per scope (item 5).

| Option | What it is | Fit | Rough cost tier |
|---|---|---|---|
| **AMD/Xilinx RFSoC ZCU111** | Zynq UltraScale+ RFSoC eval board: 8x 4GSPS ADC, 8x 6.4GSPS DAC, integrated FPGA fabric + ARM Cortex-A53/R5 (PS side) | Best fit for wideband channelization + CAF/correlation offload with direct RF sampling (no separate SDR front end needed) | High — eval-board tier, several thousand USD |
| **AMD/Xilinx RFSoC ZCU216** | Newer/higher-performance RFSoC (Gen 3) eval board, more ADC/DAC channels, higher sample rates | Same fit as ZCU111, more headroom for full-spectrum wideband work described in the Meghdut C3 directive's Priority 1 | High — several thousand USD, above ZCU111 |
| **Ettus/NI USRP X310** | SDR with user-programmable Kintex-7 FPGA, 10GigE/PCIe host interface, swappable daughterboards for RF front end | Mature SDR ecosystem (UHD/GNU Radio), FPGA accessible for custom offload (e.g. CAF correlation cores), widely used in real passive-radar research (this is close to the "goship.m" reference lineage this codebase already ports) | Mid-high — several thousand USD per unit + daughterboard cost |
| **Ettus/NI USRP N321/N320** | Networked SDR, Zynq-7000 FPGA, dual RF chains, 200MHz+ instantaneous bandwidth | Lower-cost alternative to X310 with still-usable FPGA fabric; good bistatic-radar dual-channel fit (matches this codebase's `DualChannelSource` model directly) | Mid — roughly half of X310 tier |
| **HackRF-adjacent FPGA dev boards** (e.g. small Artix-7/Cyclone dev boards paired with the existing HackRF as a pure RF front end, FPGA doing post-ADC correlation only) | DIY/low-cost path: keep the HackRF for RF, add a small FPGA board purely for CAF correlation offload via a defined IQ interface | Lowest cost, but requires building the ADC-to-FPGA interface and IQ streaming path from scratch — significant integration risk for a "sandboxed" DIY combination that does not exist as an integrated product | Low hardware cost, high integration-effort cost (see §3) |

**Recommendation direction (not a procurement decision):** if the Army's
OB-06 intent is genuinely a wideband, real-time, deterministic RF backbone
(consistent with the Meghdut C3 directive's Priority 1, full-spectrum scan),
an RFSoC board (ZCU111/ZCU216) or a USRP X310/N321 is the realistic, currently
shipping hardware class to evaluate — not a DIY HackRF+FPGA combination.

---

## 3. Toolchain/integration reality check — this is not a small addition

Be direct about scope:

- **HDL development is a different discipline than the Python/DSP work in
  this repo.** Implementing a CAF/correlation core, a wideband channelizer,
  or a DMA/streaming interface in Verilog/VHDL (or HLS as a partial shortcut)
  requires FPGA/RTL design skills — pipelining, fixed-point quantization,
  clock-domain crossing, timing closure — that nothing in the current
  Python/NumPy/SciPy codebase or team workflow (as evidenced by this repo)
  currently demonstrates. This is a hiring or contracting gap, not a
  "learn as you go" side task.
- **Vivado/Vitis toolchain** (for AMD/Xilinx parts) or equivalent (Quartus for
  Intel/Altera) is required for synthesis, place-and-route, and
  timing closure. These are heavyweight, license-gated (some editions free,
  larger devices need paid tiers), Linux/Windows desktop tools with multi-hour
  build cycles — materially different from the current git-based Python
  development loop.
- **Host interfacing**: getting data between the FPGA fabric and the existing
  Python backend means building (or integrating an existing) PCIe DMA driver
  (RFSoC/X310-class boards) or a 10GigE streaming interface (X310/N320-class),
  then writing a Python-side consumer that replaces or augments
  `passive_radar/channel_source.py`'s `DualChannelSource` abstraction. UHD
  (Ettus) already provides this layer for USRP hardware; RFSoC boards
  typically need more custom platform/PetaLinux work.
- **Validation burden**: the CAF FFT-based path in this repo
  (`caf_fft_batched`) is explicitly validated against a `caf_bruteforce`
  reference for correctness before being trusted. An FPGA CAF core would need
  the same bit-accuracy/tolerance validation discipline against that same
  reference, plus fixed-point quantization-error analysis that a floating-point
  Python reference doesn't have to deal with.
- **Realistic timeline honesty**: for a team without existing FPGA/RTL
  capacity, a first working FPGA-accelerated CAF core integrated end-to-end
  with this backend (not just an isolated benchmark) is a multi-month effort
  even using an eval-board reference design as a starting point, not a
  sprint-sized task. This should be scoped and staffed as its own workstream,
  not appended to the current field-bridge Python sprint cadence.

---

## 4. Recommended phased approach

**Phase 1 (near-term, weeks, if the Army wants visible progress now):**
GPU-based acceleration (CUDA, or OpenCL/ROCm for non-NVIDIA) of the CAF
computation. `scipy.signal.fftconvolve` is already a per-Doppler-bin
batched-FFT structure (`caf_fft_batched`) that maps cleanly onto a GPU batched
FFT (e.g. `cupy`/PyTorch FFT), typically with far less engineering effort
than an FPGA core because it stays in the existing Python toolchain (no HDL,
no Vivado, no new host driver layer — just a GPU-backed array library). This
is a legitimate, fast answer to "the CPU is the bottleneck" for the CAF path
specifically, and would also plausibly help ResNet18 ML inference throughput
if that workload is ever scaled up.

**Phase 2 (mid-term):** stand up an FPGA/RFSoC evaluation track in parallel
(procure one ZCU111/ZCU216 or USRP X310/N321, begin CAF-core HDL/HLS
development against the existing `caf_bruteforce` reference as the
correctness baseline) without blocking on Phase 1's delivery.

**Phase 3 (long-term):** integrate the validated FPGA CAF/channelization core
as the production acceleration backbone once HDL development, toolchain
build-out, and host-interface work (§3) are complete and validated.

**Be explicit with the Army stakeholder about one thing:** GPU acceleration
answers "the CPU is too slow" but does **not** literally satisfy an
FPGA-specific requirement if OB-06's "FPGA" wording exists for reasons beyond
raw throughput — e.g. **deterministic/bounded latency** (FPGAs give
cycle-accurate timing guarantees a GPU's driver/scheduler stack does not),
**power/SWaP-C constraints** for a field-deployed CEMA unit (FPGAs typically
draw far less power than a discrete GPU for equivalent fixed-function DSP
throughput), or a **doctrinal/certification reason** (some defense programs
mandate FPGA specifically for supply-chain, radiation-tolerance, or
airborne-certification reasons that a COTS GPU cannot meet). This
distinction should be confirmed with the Army stakeholder before treating
Phase 1 (GPU) as an acceptable interim substitute rather than just a
"progress to show" milestone — if OB-06 is FPGA-specific for one of those
reasons, Phase 1 buys time but does not reduce Phase 2/3 scope at all.

---

## 5. Explicit non-scope

- No HDL/RTL/HLS code, no drivers, no build scripts are included in or implied
  as delivered by this document.
- No hardware has been ordered or committed to; all board/vendor names above
  are cited as real, currently-available products for comparison purposes
  only, not a purchase recommendation.
- No timeline or budget commitment is made; effort language above ("weeks,"
  "multi-month") is a rough planning signal, not a quoted estimate.
- This document does not modify `hackrf_rx.py`, `passive_radar_bridge.py`,
  `ml_classify_bridge.py`, or any file under `passive_radar/`.
