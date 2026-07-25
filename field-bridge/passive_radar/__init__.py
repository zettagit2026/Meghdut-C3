"""Passive bistatic radar CAF/Doppler processing (task #43, C10).

RECEIVE ONLY. No transmission happens anywhere in this package -- passive
radar exploits an illuminator of opportunity (broadcast TV/FM/cellular)
that is already transmitting; nothing here emits RF.

See field-bridge/PASSIVE_RADAR_ARCHITECTURE.md for the full design spec
this package implements. Summary of module boundaries (mirrors that doc's
§2.1):

  channel_source.py      -- DualChannelSource ABC + Synthetic/RecordedFile
                             implementations (buildable now) + DualRTLSDRSource
                             (HARDWARE-BLOCKED stub, task #57).
  alignment.py            -- inter-channel delay estimation (port of
                             goship.m's USB-bus delay correction).
  dsi_suppression.py      -- least-squares direct-signal-interference removal
                             (port of goship.m's dsi_suppression block).
  caf.py                  -- Cross-Ambiguity Function / range-Doppler map
                             (port of goship.m's Doppler-bin xcorr loop).
  detector.py              -- CFAR/peak-picking over the range-Doppler map.
                             NEW -- not present in the reference repo.
  geometry.py              -- bistatic range/Doppler equations + placeholder
                             bearing model.
  illuminator_profile.py   -- IlluminatorProfile dataclass + placeholder
                             profiles (DVB-T2, FM broadcast). Illuminator
                             identity is NOT hardcoded anywhere else in this
                             package.
  passive_radar_bridge.py  -- CLI bridge, analogous to hackrf_rx.py, wiring
                             the above into /api/detections/ingest.

DualRTLSDRSource remains an explicit NotImplementedError stub. Real dual
RTL-SDR acquisition (clock sync / GPSDO distribution) is task #57's scope,
not this package's.
"""
