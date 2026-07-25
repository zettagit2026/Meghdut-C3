"""Illuminator-agnostic profile description.

Per PASSIVE_RADAR_ARCHITECTURE.md §3: the reference repo hardcodes DVB-T
because that's what its authors had in Sendai. This project's actual
illuminator has NOT been confirmed (the DVB-T2/Doordarshan feasibility
check for the real deployment site is explicitly outstanding). Nothing in
alignment.py, dsi_suppression.py, or caf.py needs to know *what* the
illuminator is -- they operate purely on "two complex baseband streams
tuned to wherever the illuminator's energy is." Illuminator identity only
matters here and in geometry.py (transmitter position).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

# (lat, lon, alt_m)
LatLonAlt = Tuple[float, float, float]


@dataclass
class IlluminatorProfile:
    name: str                     # e.g. "DVB-T2", "FM_BROADCAST", "GSM_BTS", "DAB"
    center_freq_hz: float
    channel_bandwidth_hz: float
    min_sample_rate_hz: float      # Nyquist-driven floor for this illuminator's bandwidth
    ambiguity_notes: str
    known_transmitter_locations: List[LatLonAlt] = field(default_factory=list)


# Placeholder profiles proving the abstraction is not secretly DVB-T2-only.
# known_transmitter_locations intentionally empty -- populated once the
# site survey / feasibility check (currently outstanding) confirms a
# candidate illuminator and transmitter site.

DVB_T2_PLACEHOLDER = IlluminatorProfile(
    name="DVB-T2",
    center_freq_hz=578.0e6,  # UHF band, typical DVB-T2 mux (placeholder pending site survey)
    channel_bandwidth_hz=8.0e6,
    min_sample_rate_hz=8.0e6,  # Nyquist floor for 8 MHz DVB-T2 channel
    ambiguity_notes=(
        "DVB-T/T2 is COFDM: its autocorrelation is dominated by the guard-"
        "interval periodicity, giving a comparatively clean, narrow "
        "ambiguity function -- but a single RTL-SDR's ~2.4-2.8 MS/s ceiling "
        "cannot natively sample the full 8 MHz channel bandwidth without "
        "decimation/care (see PASSIVE_RADAR_ARCHITECTURE.md table, "
        "'Two-channel acquisition topology' row)."
    ),
    known_transmitter_locations=[],
)

FM_BROADCAST_PLACEHOLDER = IlluminatorProfile(
    name="FM_BROADCAST",
    center_freq_hz=100.0e6,  # representative FM band center; real freq TBD by site survey
    channel_bandwidth_hz=200.0e3,
    min_sample_rate_hz=200.0e3,
    ambiguity_notes=(
        "FM broadcast (mono/stereo composite, ~200 kHz) has a near-"
        "periodic, low-information-content structure that can create "
        "range ambiguities at multiples of the underlying periodicity; "
        "its narrow bandwidth is comfortably within a single RTL-SDR's "
        "native sample rate range with no decimation needed, at the cost "
        "of coarser range resolution than a wider-band illuminator."
    ),
    known_transmitter_locations=[],
)
