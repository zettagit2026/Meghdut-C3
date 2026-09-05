"""pytest environment setup for the field-bridge suite.

TX PINNING FAIL-CLOSED (safety-critical, see hackrf_jam.py `_tx_pinning_error`):
hackrf_jam.py now REFUSES to transmit when HACKRF_TX_SERIAL is unset, unless the
explicit single-HackRF dev opt-out HACKRF_ALLOW_UNPINNED_TX=1 is set. The unit
tests here never own a real radio (subprocess/hackrf_transfer is always mocked
or absent) and deliberately exercise the historical unpinned code paths without
pinning a serial — exactly the "single-HackRF development" case the opt-out
exists for. Declaring the opt-out here (once, for the whole test session) models
a single-HackRF dev box so those pre-existing transmit tests keep exercising the
unpinned path under the new fail-closed default.

This affects ONLY environments that actually run this pytest suite. It cannot
weaken production: the governed bridges pin HACKRF_TX_SERIAL via their systemd
EnvironmentFile (so the guard returns before ever consulting the opt-out), and
production hosts do not run pytest / load this conftest. Tests that must assert
the fail-closed REFUSE path clear this flag per-test with monkeypatch.delenv,
which works because the guard reads the flag live from the environment.

setdefault (not a hard set) so an explicit HACKRF_ALLOW_UNPINNED_TX already in
the environment is preserved.
"""
import os

os.environ.setdefault("HACKRF_ALLOW_UNPINNED_TX", "1")
