"""Preset cyber-physical payload catalogue for the cUAS operator console."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List
from mavlink_codec import (
    payload_force_land,
    payload_rth,
    payload_disarm,
    payload_flight_termination,
    payload_propeller_stop,
    payload_memory_erase,
    payload_reboot,
    payload_rth_spoof_home,
    payload_gnss_denial,
    payload_maneuver_takeover,
    broadcast_takedown,
)

# Hard cap on the sustained RC_CHANNELS_OVERRIDE takeover (PL-011). The
# controlled-landing stream re-emits the override frame at the RC update rate
# for an operator-set duration, but NEVER longer than this — enforced both here
# (documented) and in the sustained driver (field-bridge/mavlink_takeover.py).
# This is a bounded engagement window, NOT transmit-forever.
MANEUVER_TAKEOVER_MAX_DURATION_S = 30.0
MANEUVER_TAKEOVER_DEFAULT_DURATION_S = 8.0
MANEUVER_TAKEOVER_RC_RATE_HZ = 20.0  # typical RC override refresh cadence


@dataclass
class PayloadSpec:
    id: str
    name: str
    category: str  # kinetic | logical | protocol | denial
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    description: str
    effect: str
    mav_cmd: str
    reversible: bool
    duration_ms: int  # simulated engagement duration
    requires_takeover: bool  # whether pre-broadcast auth spoof is needed
    # Sustained-injection payloads (e.g. PL-011 RC override controlled-landing)
    # re-emit their frame at rc_rate_hz for a bounded, hard-capped duration
    # instead of firing one shot. Defaults keep every existing one-shot entry
    # unchanged (sustained=False).
    sustained: bool = False
    max_duration_s: float = 0.0
    rc_rate_hz: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


PAYLOAD_BUILDERS: Dict[str, Callable] = {
    "PL-001": payload_force_land,
    "PL-002": payload_rth,
    "PL-003": payload_disarm,
    "PL-004": payload_flight_termination,
    "PL-005": payload_propeller_stop,
    "PL-006": payload_memory_erase,
    "PL-007": payload_reboot,
    "PL-008": payload_rth_spoof_home,
    "PL-009": payload_gnss_denial,
    "PL-010": lambda ts=0, tc=0, seq=0: broadcast_takedown(seq=seq),
    "PL-011": payload_maneuver_takeover,
}


PAYLOAD_CATALOG: List[PayloadSpec] = [
    PayloadSpec(
        id="PL-001",
        name="FORCE LAND",
        category="kinetic",
        severity="HIGH",
        description="Injects MAV_CMD_NAV_LAND. Commands the target to immediately descend to ground at current position.",
        effect="Target descends and lands within 8-15 seconds.",
        mav_cmd="NAV_LAND (21)",
        reversible=False,
        duration_ms=1200,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-002",
        name="RETURN TO LAUNCH",
        category="kinetic",
        severity="MEDIUM",
        description="Sends MAV_CMD_NAV_RETURN_TO_LAUNCH. Forces the target to fly back to its recorded home position.",
        effect="Target aborts mission and returns to its home coordinates.",
        mav_cmd="NAV_RETURN_TO_LAUNCH (20)",
        reversible=False,
        duration_ms=900,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-003",
        name="FORCE DISARM",
        category="kinetic",
        severity="CRITICAL",
        description="MAV_CMD_COMPONENT_ARM_DISARM with force flag (21196). Cuts motors instantly regardless of altitude.",
        effect="Instant motor cutoff. Target free-falls.",
        mav_cmd="COMPONENT_ARM_DISARM (400)",
        reversible=False,
        duration_ms=400,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-004",
        name="FLIGHT TERMINATION",
        category="kinetic",
        severity="CRITICAL",
        description="Activates MAV_CMD_DO_FLIGHTTERMINATION. Triggers parachute / kill-motor on supported airframes.",
        effect="Target enters emergency termination sequence.",
        mav_cmd="DO_FLIGHTTERMINATION (185)",
        reversible=False,
        duration_ms=500,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-005",
        name="PROPELLER STOP",
        category="kinetic",
        severity="CRITICAL",
        description="MAV_CMD_DO_MOTOR_TEST with throttle=0, timeout=1s per motor. Iterated for all motors.",
        effect="All rotors driven to 0% RPM.",
        mav_cmd="DO_MOTOR_TEST (209)",
        reversible=False,
        duration_ms=800,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-006",
        name="MEMORY ERASE",
        category="logical",
        severity="CRITICAL",
        description="MAV_CMD_PREFLIGHT_STORAGE p1=2 (write), p2=1 (reset params), p3=1 (reset mission), p4=1 (reset log). Wipes onboard parameter store, mission cache, and log flash.",
        effect="Autopilot loses all mission and calibration data. Requires ground re-init.",
        mav_cmd="PREFLIGHT_STORAGE (245)",
        reversible=False,
        duration_ms=1500,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-007",
        name="AUTOPILOT REBOOT",
        category="logical",
        severity="HIGH",
        description="MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN p1=1. Forces the flight controller to reboot mid-flight.",
        effect="Loss of stabilisation for 3-7s → crash or forced landing.",
        mav_cmd="PREFLIGHT_REBOOT_SHUTDOWN (246)",
        reversible=False,
        duration_ms=600,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-008",
        name="RTH HOME SPOOF",
        category="protocol",
        severity="HIGH",
        description="MAV_CMD_DO_SET_HOME with hostile coordinates followed by RTH trigger. Diverts drone to attacker-selected location.",
        effect="Target flies to injected coordinates believing it is home.",
        mav_cmd="DO_SET_HOME (179) + NAV_RETURN_TO_LAUNCH (20)",
        reversible=True,
        duration_ms=700,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-009",
        name="GNSS TELEMETRY DENIAL",
        category="denial",
        severity="MEDIUM",
        description="MAV_CMD_SET_MESSAGE_INTERVAL with interval=-1 for GPS_RAW_INT (msg 24). Starves navigation stack of GNSS updates.",
        effect="Target loses GPS lock reporting. Fallback to inertial → drift.",
        mav_cmd="SET_MESSAGE_INTERVAL (511)",
        reversible=True,
        duration_ms=500,
        requires_takeover=True,
    ),
    PayloadSpec(
        id="PL-010",
        name="BROADCAST TAKEDOWN (SWARM)",
        category="protocol",
        severity="CRITICAL",
        description="COMMAND_LONG with target_system=0 (broadcast) carrying NAV_LAND. All MAVLink-speaking drones in RF earshot execute landing.",
        effect="Simultaneous forced-land across entire swarm.",
        mav_cmd="COMMAND_LONG broadcast",
        reversible=False,
        duration_ms=1000,
        requires_takeover=False,
    ),
    PayloadSpec(
        id="PL-011",
        name="MANEUVER TAKEOVER (CONTROLLED LANDING)",
        category="kinetic",
        severity="CRITICAL",
        description=(
            "Sustained MAV_CMD-free RC_CHANNELS_OVERRIDE (msg 70) injection. Holds "
            "roll/pitch/yaw neutral and drives throttle below mid to walk the target "
            "down to a controlled landing — a comparatively humane neutralization vs. "
            "flight-termination/force-disarm (which make it fall). Frames are re-emitted "
            f"at ~{MANEUVER_TAKEOVER_RC_RATE_HZ:.0f}Hz for an operator-set duration, "
            f"HARD-CAPPED at {MANEUVER_TAKEOVER_MAX_DURATION_S:.0f}s (bounded, not "
            "transmit-forever) and terminated immediately on EMERGENCY ABORT. "
            "HONEST SCOPE: effective ONLY against unencrypted / legacy MAVLink-over-RF "
            "craft (pre-paired or unencrypted SiK-radio ArduPilot/PX4). It does NOT work "
            "against an FHSS / encrypted control link (ELRS/CRSF, DJI OcuSync, DSMX, "
            "hop-paired RC) — against such a target the takeover is NOT APPLICABLE and "
            "must be reported as such, not transmitted uselessly."
        ),
        effect=(
            "Legacy/unencrypted-MAVLink target descends to a controlled landing under "
            "injected RC for the bounded window. No effect on encrypted/FHSS links."
        ),
        mav_cmd="RC_CHANNELS_OVERRIDE (70)",
        reversible=True,  # override releases the moment the bounded stream stops
        duration_ms=int(MANEUVER_TAKEOVER_DEFAULT_DURATION_S * 1000),
        requires_takeover=True,
        sustained=True,
        max_duration_s=MANEUVER_TAKEOVER_MAX_DURATION_S,
        rc_rate_hz=MANEUVER_TAKEOVER_RC_RATE_HZ,
    ),
]


def get_payload_by_id(pid: str) -> PayloadSpec | None:
    return next((p for p in PAYLOAD_CATALOG if p.id == pid), None)
