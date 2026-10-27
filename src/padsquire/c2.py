# SPDX-License-Identifier: AGPL-3.0-or-later
"""The GameSir Cyclone 2's configuration protocol (not GCM). Pure functions: frames and parsers.

Decoded from GameSir Connect 1.16.7 (`main.js`: the Cyclone 2's handler `proxyC2`, its command
builder and its profile layout); none of it has been run yet. PROTOCOL.md, "Cyclone 2", has the
details and how sure each part is. Two other projects have used it on hardware, and agree with
what is here: Deadband (github.com/broroeror/gamesir-linux-tools, which configures the Cyclone 2
in full) and cyclone2-linux (github.com/vdemonchy/cyclone2-linux: battery and lighting).

Everything goes through one vendor HID interface (usage page 0xFFF0) as 64-byte reports with
report IDs: requests are output report 0x0F `[0F, command, args...]`, replies come back with the
command type in byte 1, and the controller streams its state as input report 0x12.

The settings live in profiles the controller keeps as byte images: four profiles of 680 bytes
(1-4), the Shift layer (5, same layout) and the lighting profile (32, 635 bytes). Connect reads
a profile in 58-byte pieces and writes back only the bytes that changed.

Replies use input report 0x10: seen on hardware by Deadband and cyclone2-linux, not yet by us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

VENDOR_ID = 0x3537
# Connect's device table (C2): two cable and two 2.4 GHz identities. 0x1053 is a hardware
# edition with its own firmware ("C2 ADC", 3.5.6 against 1.2.3).
PIDS = {0x101D: "cable", 0x1053: "cable", 0x102A: "2.4 GHz receiver", 0x100B: "2.4 GHz receiver"}
USAGE_PAGE = 0xFFF0
DESCRIPTOR_PREFIX = bytes.fromhex("06f0ff")      # Usage Page (0xFFF0)

REPORT_SIZE = 64
OUT_REPORT = 0x0F
STATE_REPORT = 0x12

CHUNK = 58                        # profile bytes per request (EveryOutPacketLength)
PROFILE_SIZE = 680                # EveryPacketLength
LIGHT_PROFILE_SIZE = 635          # LightProfilePacketLength
SHIFT_PROFILE = 5
LIGHT_PROFILE = 32
CURRENT_PROFILE_MARK = 48         # "the current profile changed", in a ProfileChanged report
PROFILES = (1, 2, 3, 4, SHIFT_PROFILE, LIGHT_PROFILE)


class C2Error(ValueError):
    pass


class Cmd(IntEnum):
    """Byte 1 of a request or reply (Connect's CommandType)."""
    ENTER_PROFILE_CONFIG = 0x01
    EXIT_PROFILE_CONFIG = 0x02
    WRITE_PROFILE = 0x03
    READ_PROFILE = 0x04
    READ_PROFILE_ACK = 0x05
    ACK = 0x06                    # byte 2 = 1: busy
    SWITCH_PROFILE = 0x07
    WRITE_PROFILE_TO_EEPROM = 0x08
    READ_FIRMWARE_VERSION = 0x09
    READ_FIRMWARE_VERSION_ACK = 0x0A
    READ_CURRENT_PROFILE = 0x0B
    READ_CURRENT_PROFILE_ACK = 0x0C
    READ_RGB = 0x0D               # with arguments it sets the light's play state instead
    READ_RGB_ACK = 0x0E
    PROFILE_CHANGED = 0x0F        # sent by the controller when a profile changed on it
    REFRESH_PROFILE = 0x10
    REFRESH_PROFILE_ACK = 0x11
    WRITE_EEPROM = 0x12
    WRITE_EEPROM_ACK = 0x13
    READ_EEPROM = 0x14
    READ_EEPROM_ACK = 0x15
    MACRO_RECORD = 0x16
    QUICK_UPDATE = 0x17
    VIBRATE = 0x20
    HEARTBEAT = 0xF2
    READ_KEY_STATUS = 0xF3
    READ_KEY_STATUS_ACK = 0xF4
    ENTER_UPGRADE = 0xFC
    CALIBRATION = 0xFD


# The only requests PadSquire sends to a Cyclone 2 until its settings are confirmed on hardware.
# Each is a read in Connect; the transport refuses everything else.
READ_ONLY = frozenset({Cmd.HEARTBEAT, Cmd.READ_FIRMWARE_VERSION, Cmd.READ_CURRENT_PROFILE,
                       Cmd.READ_PROFILE})


# -- requests -------------------------------------------------------------------------

def heartbeat() -> bytes:
    """Connect sends this when it opens the device; afterwards the 0x12 state reports stream."""
    return bytes([OUT_REPORT, Cmd.HEARTBEAT, 0])


def read_firmware_version() -> bytes:
    return bytes([OUT_REPORT, Cmd.READ_FIRMWARE_VERSION])


def read_current_profile() -> bytes:
    return bytes([OUT_REPORT, Cmd.READ_CURRENT_PROFILE])


def profile_size(number: int) -> int:
    if number == LIGHT_PROFILE:
        return LIGHT_PROFILE_SIZE
    if number in (1, 2, 3, 4, SHIFT_PROFILE):
        return PROFILE_SIZE
    raise C2Error(f"no profile {number}")


def read_profile(number: int) -> list[bytes]:
    """`0F 04 <profile> <offset hi> <offset lo> <length>`, one per 58-byte piece."""
    size = profile_size(number)
    return [bytes([OUT_REPORT, Cmd.READ_PROFILE, number, off >> 8, off & 0xFF, min(CHUNK, size - off)])
            for off in range(0, size, CHUNK)]


def pad(frame: bytes) -> bytes:
    if len(frame) > REPORT_SIZE:
        raise C2Error(f"request longer than one report ({len(frame)} bytes)")
    return frame + bytes(REPORT_SIZE - len(frame))


# -- replies --------------------------------------------------------------------------

def reply_type(report: bytes) -> Cmd | None:
    """The command type a reply carries in byte 1, or None for a state report or noise."""
    if len(report) < 2 or report[0] == STATE_REPORT:
        return None
    try:
        return Cmd(report[1])
    except ValueError:
        return None


def is_busy(report: bytes) -> bool:
    return reply_type(report) == Cmd.ACK and len(report) > 2 and report[2] == 1


def _version(raw: bytes) -> str:
    """Connect shows the ASCII digits dot-separated: b"123" -> "1.2.3"."""
    digits = raw.decode("ascii", "replace").replace("\0", "").replace(" ", "")
    return ".".join(digits)


@dataclass
class FirmwareVersion:
    controller: str
    receiver: str           # empty by cable


def parse_firmware_version(report: bytes) -> FirmwareVersion:
    """ReadFirmwareVersionAck: the controller's version in bytes 4-8, the receiver's in 12-16."""
    if reply_type(report) != Cmd.READ_FIRMWARE_VERSION_ACK or len(report) < 17:
        raise C2Error(f"not a firmware version reply: {report[:8].hex(' ')}")
    return FirmwareVersion(_version(report[4:9]), _version(report[12:17]))


def parse_current_profile(report: bytes) -> int:
    if reply_type(report) != Cmd.READ_CURRENT_PROFILE_ACK or len(report) < 3:
        raise C2Error(f"not a current profile reply: {report[:8].hex(' ')}")
    return report[2]


class ProfileReader:
    """Puts a profile together from ReadProfileAck pieces: `[.., 05, profile, off hi, off lo,
    length, data...]`. Complete when a piece ends at the profile's size, as in Connect."""

    def __init__(self, number: int):
        self.number = number
        self.size = profile_size(number)
        self.data = bytearray(self.size)
        self.seen: set[int] = set()

    def add(self, report: bytes) -> bool:
        if reply_type(report) != Cmd.READ_PROFILE_ACK or len(report) < 6 or report[2] != self.number:
            return False
        off, n = report[3] << 8 | report[4], report[5]
        if off + n > self.size or len(report) < 6 + n:
            raise C2Error(f"profile piece out of range: offset {off}, length {n}")
        self.data[off:off + n] = report[6:6 + n]
        self.seen.add(off)
        return True

    @property
    def complete(self) -> bool:
        return self.seen >= set(range(0, self.size, CHUNK))


# -- state report (input report 0x12) -------------------------------------------------
# Connect (proxyC2.parseGamepadState and the gamepad view): sticks 1-4 (128 = centre), the
# D-pad in the low nibble of 5 (0-7 from up, clockwise; 8+ = released), buttons in 5-7,
# triggers 8 and 9, then 35 charge state, 36 battery level, 37 the active lighting effect,
# 38-52 the five light colours, 53 macro recording.
# cyclone2-linux, on hardware: 35 is 1 on the cable and 0 on battery, 36 is the percentage.

_BUTTONS = {"X": (5, 4), "A": (5, 5), "B": (5, 6), "Y": (5, 7),
            "LB": (6, 0), "RB": (6, 1), "LT": (6, 2), "RT": (6, 3), "View": (6, 4), "Menu": (6, 5),
            "LS": (6, 6), "RS": (6, 7), "Home": (7, 0), "Capture": (7, 1), "L4": (7, 3), "R4": (7, 4),
            "M": (7, 5)}
_DPAD = ("up", "up-right", "right", "down-right", "down", "down-left", "left", "up-left")


@dataclass
class State:
    battery: int                       # percent
    charging: bool                     # really "on the cable": set even when full
    lighting_effect: int
    colours: list[tuple[int, int, int]]
    sticks: tuple[int, int, int, int]  # LX, LY, RX, RY, 0-255, 128 = centre
    triggers: tuple[int, int]
    dpad: str | None
    buttons: set[str] = field(default_factory=set)


def parse_state(report: bytes) -> State:
    if len(report) < 54 or report[0] != STATE_REPORT:
        raise C2Error("not a 0x12 state report")
    hat = report[5] & 0x0F
    return State(
        battery=report[36], charging=report[35] != 0, lighting_effect=report[37],
        colours=[tuple(report[38 + 3 * i:41 + 3 * i]) for i in range(5)],
        sticks=tuple(report[1:5]), triggers=(report[8], report[9]),
        dpad=_DPAD[hat] if hat < 8 else None,
        buttons={name for name, (byte, bit) in _BUTTONS.items() if report[byte] >> bit & 1},
    )


# -- profile layout (680 bytes) -------------------------------------------------------
# Connect's Uo.fromBytes: name 32, function data 32, 16 buttons x 7, L4 and R4 x 159 (mapping
# plus a 30-step macro), LT and RT x 28, left and right stick x 32, aim and tilt motion x 33.

REPORT_RATES = {0: 250, 1: 500, 2: 1000}    # Deadband, on hardware
BUTTON_ORDER = ("up", "down", "left", "right", "LB", "RB", "LS", "RS", "A", "B", "X", "Y",
                "Home", "View", "Menu", "Capture")


def _curve(b: bytes) -> dict:
    return {"enabled": b[0], "status": b[1], "data": b[2],
            "points": [(b[3 + 2 * i], b[4 + 2 * i]) for i in range(5)]}


def _dead(b: bytes) -> dict:
    """Percent of travel, as Connect shows them: the output starts at `start` and is full at `end`
    (sticks 10 / 100 and triggers 5 / 95 after a reset in Connect)."""
    return {"enabled": b[0], "start": b[1], "end": b[2], "anti_start": b[3], "anti_end": b[4]}


def _mapping(b: bytes) -> dict:
    return {"enabled": b[0], "targets": list(b[1:4])}


def _stick_map(b: bytes) -> dict:
    return {"enabled": b[0], "invert_x": b[1], "invert_y": b[2], "axis_ratio": b[3], "mouse_dpi": b[4],
            "index": b[5], "cross": b[6], "up": b[7], "down": b[8], "left": b[9], "right": b[10],
            "dead": b[11]}


def decode_profile(data: bytes) -> dict:
    if len(data) != PROFILE_SIZE:
        raise C2Error(f"a profile is {PROFILE_SIZE} bytes, not {len(data)}")
    p = 0

    def take(n: int) -> bytes:
        nonlocal p
        p += n
        return bytes(data[p - n:p])

    name = take(32).split(b"\0")[0].decode("utf-8", "replace").strip()
    f = take(32)
    out = {
        "name": name,
        # percent; Connect offers 0/25/50/75/100, Deadband reads 75 by default
        "vibration": dict(zip(("left", "right", "LT", "RT"), f[0:4], strict=True)),
        "audio": {"enabled": f[4] & 1, "volume": f[5], "mixer": f[6], "mic_mute": f[7],
                  "mic_sensitivity": f[8]},
        "shift": {"enabled": f[9] & 1, "button": f[10]},
        "dpad_diagonal_lock": f[11] & 1, "swap_abxy_xinput": f[12] & 1, "swap_abxy_switch": f[13] & 1,
        "report_rate": REPORT_RATES.get(f[14], f[14]),     # Hz
        "buttons": {}, "macros": {}, "triggers": {}, "sticks": {}, "motion": {},
    }
    for key in BUTTON_ORDER:
        b = take(7)
        out["buttons"][key] = {"turbo": b[0], "turbo_speed": b[1], "mapping": _mapping(b[2:6]), "toggle": b[6]}
    for key in ("L4", "R4"):
        b = take(159)
        out["buttons"][key] = {"turbo": b[0], "turbo_speed": b[1], "mapping": _mapping(b[2:6]), "toggle": b[6]}
        steps = []
        for i in range(min(b[10], 30)):
            s = b[11 + 5 * i:16 + 5 * i]
            steps.append({"keys": s[0], "hold_ms": s[1] << 8 | s[2],
                          "delay_ms": (s[3] << 8 | s[4]) if i < 29 else 0})
        out["macros"][key] = {"enabled": b[7] & 1, "on_key": b[7] >> 1 & 1, "repeat": b[7] >> 2 & 1,
                              "repeat_ms": b[8] << 8 | b[9], "steps": steps}
    for key in ("LT", "RT"):
        b = take(28)
        out["triggers"][key] = {"turbo": b[0], "turbo_speed": b[1], "dead_zone": _dead(b[2:7]),
                                "mapping": _mapping(b[7:11]), "toggle": b[11],
                                # status: bit 7 on, bit 0 mode 1, bit 1 mode 2
                                "quick_trigger": {"enabled": b[12] >> 7 & 1, "mode": 1 if b[12] & 1 else
                                                  2 if b[12] & 2 else 0, "start": b[13], "end": b[14]},
                                "curve": _curve(b[15:28])}
    for key in ("left", "right"):
        b = take(32)
        out["sticks"][key] = {"enabled": b[0], "square": b[1], "dead_zone": _dead(b[2:7]),
                              "curve": _curve(b[7:20]), "map": _stick_map(b[20:32])}
    for key in ("aim", "tilt"):
        b = take(33)
        out["motion"][key] = {"enabled": b[0], "key": b[1], "axis": b[2], "dead_zone": _dead(b[3:8]),
                              "curve": _curve(b[8:21]), "map": _stick_map(b[21:33])}
    assert p == PROFILE_SIZE
    return out


# -- lighting profile (635 bytes) -----------------------------------------------------
# Connect's Ks.fromBytes: the active effect (0-3), five effects of 124 bytes (key frames, effect
# count, speed stored as 20 - speed, brightness 0-100, eight frames of five RGB colours), then
# 14 bytes of settings. cyclone2-linux's lighting writes are writes into effect 0 of this
# profile: its "brightness register 4" is effect 0's brightness, and its four zones at 5, 8, 14
# and 17 are frame 0's colours 0, 1, 3 and 4 (colour 2, at 11, went unnoticed).

EFFECT_SIZE = 124


def decode_light(data: bytes) -> dict:
    if len(data) != LIGHT_PROFILE_SIZE:
        raise C2Error(f"the lighting profile is {LIGHT_PROFILE_SIZE} bytes, not {len(data)}")
    effects = []
    for i in range(5):
        e = data[1 + EFFECT_SIZE * i:1 + EFFECT_SIZE * (i + 1)]
        frames = [[tuple(e[4 + 15 * f + 3 * c:7 + 15 * f + 3 * c]) for c in range(5)] for f in range(8)]
        effects.append({"key_frames": e[0], "effect_count": e[1], "speed": 20 - min(e[2], 20),
                        "brightness": min(e[3], 100), "frames": frames})
    t = data[1 + 5 * EFFECT_SIZE:]
    return {"active_effect": data[0] if data[0] <= 3 else 0, "effects": effects,
            "audio_reactive": t[0] & 1, "user_effect": t[1], "profile_led": tuple(t[2:5]),
            "raise_to_wake": t[5] & 1, "standby_time": t[6]}
