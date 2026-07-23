#!/usr/bin/env python3
"""Minimal serial-number -> role assignment for multi-HackRF deployments.

WHY THIS EXISTS
----------------
As of 2026-07 this field-bridge has a second physical HackRF unit (not yet
passed through to this VM at the hypervisor level -- that's an operator
task, not a software one). Every device-facing script here
(hackrf_rx.py's sweep loop, ml_classify_bridge.py, droneid_decode_bridge.py)
previously had ZERO concept of "which physical unit" -- they just grabbed
whichever HackRF libusb handed them first. hackrf_device_lock.py and
iq_capture.py's capture_iq() now support addressing a device by serial (see
those modules), but something still has to decide WHICH serial goes with
WHICH role/service. This module is that "something" -- kept deliberately
tiny: two named roles (PRIMARY/SECONDARY) plus a generic role-map escape
hatch, nothing more. It does NOT discover devices, validate that a serial
is actually plugged in, or manage device state -- it is pure config lookup.

CONFIGURATION
-------------
Two ways to assign a serial to a role, in priority order:

1. `HACKRF_SERIAL_<ROLE>` env vars, e.g.:
       HACKRF_SERIAL_PRIMARY=0000000000000000457863c82a2f6063
       HACKRF_SERIAL_SECONDARY=00000000000000004788b4c82a3b1122
   This is the expected common case -- systemd EnvironmentFile= (same
   pattern already used for CEMA_API_URL/CEMA_EMAIL/CEMA_PASSWORD) sets
   these once an operator has confirmed both units' serials via
   `hackrf_info` after hypervisor passthrough.

2. `HACKRF_ROLE_MAP` env var: a JSON object string, e.g.
       HACKRF_ROLE_MAP='{"rx_sweep": "0000...6063", "droneid": "0000...1122"}'
   for ad-hoc/custom role names beyond PRIMARY/SECONDARY, without adding a
   new env var per role. Merged UNDER the HACKRF_SERIAL_<ROLE> vars (those
   take precedence for PRIMARY/SECONDARY specifically) -- see get_role_serial().

Concrete example this unlocks (see project task backlog): assign the unit
on the RX-sweep antenna to "primary" (hackrf_rx.py's continuous sweep) and
the unit on the DroneID-decode antenna to "secondary"
(droneid_decode_bridge.py), so the two can run truly concurrently once both
are physically available, instead of time-slicing one shared device.

NOT IN SCOPE HERE
-----------------
This module does not implement direction-finding, does not verify a given
serial is actually attached, and does not auto-detect available devices
(that would need `hackrf_info -l`/parsing, deliberately left out -- keep
this a pure, dependency-free config lookup). See
DIRECTION_FINDING_NOTES.md for the DF groundwork this unblocks and what is
explicitly still missing.
"""
from __future__ import annotations

import json
import os
from typing import Optional

# The only two named roles given first-class env vars. Anything else goes
# through HACKRF_ROLE_MAP.
_NAMED_ROLE_ENV = {
    "primary": "HACKRF_SERIAL_PRIMARY",
    "secondary": "HACKRF_SERIAL_SECONDARY",
}


def get_role_serial(role: str) -> Optional[str]:
    """Return the configured HackRF serial for `role` (e.g. "primary",
    "secondary", or any custom name present in HACKRF_ROLE_MAP), or None if
    nothing is configured for it -- callers must treat None as "use
    whichever HackRF responds first" (the pre-existing, single-device
    default behavior), not as an error.
    """
    role_key = role.strip().lower()

    env_var = _NAMED_ROLE_ENV.get(role_key)
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val

    raw_map = os.environ.get("HACKRF_ROLE_MAP")
    if raw_map:
        try:
            role_map = json.loads(raw_map)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(role_map, dict):
            val = role_map.get(role) or role_map.get(role_key)
            if val:
                return str(val)

    return None
