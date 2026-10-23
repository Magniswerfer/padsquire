"""GCM: GameSir's controller configuration protocol, as used over Bluetooth LE.

Reverse-engineered from the GameSir Android app 5.8.20 (com.xiaoji.sdk.gcm).
Pure functions only: build request frames and parse replies. No I/O.

Frame: [cmd, subtype, length, ...body, checksum]
  length   = total frame length, checksum included
  checksum = 8-bit sum of every preceding byte
A reply echoes the command byte and carries the same checksum.

Everything is static analysis; ASSUMPTION marks what needs hardware to confirm.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# -- framing -----------------------------------------------------------------


class ProtocolError(ValueError):
    pass


def checksum(frame: bytes) -> int:
    return sum(frame[:-1]) & 0xFF


def seal(frame: bytes | bytearray) -> bytes:
    """Fill in the trailing checksum byte."""
    f = bytearray(frame)
    f[-1] = checksum(f)
    return bytes(f)


def verify(request: bytes, reply: bytes | None, min_len: int = 2) -> bytes:
    """Validate a reply the way the app's BaseCheck does; return it or raise."""
    if not reply:
        raise ProtocolError("empty reply")
    if len(reply) < min_len:
        raise ProtocolError(f"reply too short ({len(reply)} < {min_len} bytes)")
    if reply[0] != request[0]:
        raise ProtocolError(f"reply is for command {reply[0]:#04x}, expected {request[0]:#04x}")
    if reply[-1] != checksum(reply):
        raise ProtocolError("reply checksum mismatch")
    return reply


# -- the common 6-byte query/set shape ----------------------------------------
# Dead zones (0x07), feature toggles (0x23) and general settings (0x26) all use
#   query: [cmd, sub, 6, 0, 0, sum]   set: [cmd, sub, 6, 1, value, sum]
# and the reply carries the value at byte 4.

CMD_DEAD_ZONE = 0x07
CMD_FEATURE = 0x23
CMD_GET_INFO = 0x24
CMD_VIBRATION = 0x25
CMD_GENERAL = 0x26
CMD_RESTORE = 0x15
CMD_BASIC_INFO = 0x01


def _small(cmd: int, sub: int, value: int | None) -> bytes:
    if value is not None and not 0 <= value <= 0xFF:
        raise ValueError(f"value {value} out of range 0-255")
    flag, val = (0, 0) if value is None else (1, value)
    return seal(bytes([cmd, sub, 6, flag, val, 0]))


def _small_value(request: bytes, reply: bytes) -> int:
    return verify(request, reply, min_len=6)[4]


class DeadZone(IntEnum):
    LEFT_STICK_INNER = 1
    RIGHT_STICK_INNER = 2
    LEFT_STICK_OUTER = 3
    RIGHT_STICK_OUTER = 4
    LEFT_TRIGGER_FRONT = 5
    RIGHT_TRIGGER_FRONT = 6
    LEFT_TRIGGER_BACK = 7
    RIGHT_TRIGGER_BACK = 8
    LEFT_STICK_ANTI = 9      # "reverse" dead zone in the app
    RIGHT_STICK_ANTI = 10


def dead_zone_query(zone: DeadZone) -> bytes:
    return _small(CMD_DEAD_ZONE, zone, None)


def dead_zone_set(zone: DeadZone, value: int) -> bytes:
    return _small(CMD_DEAD_ZONE, zone, value)


def dead_zone_parse(request: bytes, reply: bytes) -> int:
    return _small_value(request, reply)


class Feature(IntEnum):
    DPAD_DIAGONAL_LOCK = 0x01
    LOW_BATTERY_WARNING = 0x02
    SWAP_STICKS = 0x03
    SWAP_LEFT_STICK_DPAD = 0x04
    INVERT_STICKS_XY = 0x05
    INVERT_LEFT_X = 0x06
    INVERT_LEFT_Y = 0x07
    INVERT_RIGHT_X = 0x08
    INVERT_RIGHT_Y = 0x09
    LEFT_STICK_RAW = 0x10
    RIGHT_STICK_RAW = 0x11
    PLAY_WHILE_CHARGING = 0x12
    AUTO_POWER_ON_DOCK = 0x13
    SWAP_LT_LB = 0x15
    SWAP_RT_RB = 0x16


def feature_query(feature: Feature) -> bytes:
    return _small(CMD_FEATURE, feature, None)


def feature_set(feature: Feature, enabled: bool) -> bytes:
    return _small(CMD_FEATURE, feature, int(bool(enabled)))


def feature_parse(request: bytes, reply: bytes) -> bool:
    return _small_value(request, reply) == 1


GENERAL_AUTO_SLEEP = 0x01


def auto_sleep_query() -> bytes:
    return _small(CMD_GENERAL, GENERAL_AUTO_SLEEP, None)


def auto_sleep_set(value: int) -> bytes:
    """ASSUMPTION: the value is minutes; the app stores it as `autoSleepTime`."""
    return _small(CMD_GENERAL, GENERAL_AUTO_SLEEP, value)


def auto_sleep_parse(request: bytes, reply: bytes) -> int:
    return _small_value(request, reply)


# -- vibration (0x25) ----------------------------------------------------------

@dataclass
class Vibration:
    left_grip: int
    left_trigger: int
    left_trigger_pressed: bool
    left_trigger_sync: bool
    right_grip: int
    right_trigger: int
    right_trigger_pressed: bool
    right_trigger_sync: bool


def vibration_query() -> bytes:
    return seal(bytes([CMD_VIBRATION, 1, 13, 0] + [0] * 8 + [0]))


def vibration_set(v: Vibration) -> bytes:
    for name in ("left_grip", "left_trigger", "right_grip", "right_trigger"):
        if not 0 <= getattr(v, name) <= 0xFF:
            raise ValueError(f"{name} out of range 0-255")
    body = [v.left_grip, v.left_trigger, int(v.left_trigger_pressed), int(v.left_trigger_sync),
            v.right_grip, v.right_trigger, int(v.right_trigger_pressed), int(v.right_trigger_sync)]
    return seal(bytes([CMD_VIBRATION, 1, 13, 1] + body + [0]))


def vibration_parse(request: bytes, reply: bytes) -> Vibration:
    r = verify(request, reply, min_len=13)
    return Vibration(r[4], r[5], r[6] == 1, r[7] == 1, r[8], r[9], r[10] == 1, r[11] == 1)


# -- info --------------------------------------------------------------------

def basic_info_query() -> bytes:
    """The app's first command after connecting. Legacy 2-byte frame, no checksum."""
    return bytes([CMD_BASIC_INFO, 0x01])


@dataclass
class BasicInfo:
    mac: str
    hardware_version: str
    radio_version: str
    firmware_version: str      # "8201": same form as GameSir's firmware table
    production_date: str       # YYYY/MM/DD
    gcm_version: str


def basic_info_parse(reply: bytes) -> BasicInfo:
    """Reply is 20 bytes starting 0xC9, or 19 bytes without it. The app only
    checks its length (and the MAC), so there is no checksum to verify."""
    if len(reply) == 19:
        reply = b"\xc9" + reply
    if len(reply) < 20 or reply[0] != 0xC9:
        raise ProtocolError(f"not a basic-info reply ({len(reply)} bytes)")
    r = reply
    return BasicInfo(
        mac=":".join(f"{b:02X}" for b in r[1:7]),
        hardware_version=f"{r[8]:02x}.{r[7]:02x}",
        radio_version=f"{r[9]:02x}",
        # Nova 2 Lite branch in BluetoothLeService: hex(b11) + hex(b12)
        firmware_version=f"{r[11]:02x}{r[12]:02x}",
        production_date=f"{r[13] | r[14] << 8}/{r[15]:02d}/{r[16]:02d}",
        gcm_version=f"{r[18]}.{r[17]}",
    )


INFO_HOST = 0x01


def host_info_query() -> bytes:
    return seal(bytes([CMD_GET_INFO, INFO_HOST, 4, 0]))


@dataclass
class Battery:
    raw: int

    @property
    def level(self) -> int:
        """ASSUMPTION: like the PC app's "rich charging info", bit 7 is the
        charging flag and the low 7 bits are the percentage."""
        return min(self.raw & 0x7F, 100)

    @property
    def charging(self) -> bool:
        return bool(self.raw & 0x80)


def host_info_parse(request: bytes, reply: bytes) -> Battery:
    return Battery(verify(request, reply, min_len=5)[3])


# -- restore -------------------------------------------------------------------

def restore_all() -> bytes:
    """Restore every setting to factory defaults (getCommonCMD(0x15, 1, null))."""
    return seal(bytes([CMD_RESTORE, 0x01, 4, 0]))
