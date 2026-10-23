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
    checks its length (and the MAC), so there is no checksum to verify.
    Over USB HID the 19-byte form arrives padded to a 64-byte report (confirmed)."""
    if len(reply) > 20:
        reply = reply[:19]
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


# -- keys ------------------------------------------------------------------------
# The app's key codes: 0x1xx = gamepad keys, 0x2xx = keyboard keys. On the air a
# key is sent as its low byte. Tables recovered by executing KeyCodeUtil's bytecode.

class Key(IntEnum):
    UP = 432
    DOWN = 433
    LEFT = 434
    RIGHT = 435
    A = 436
    B = 437
    X = 439
    Y = 440
    LB = 442
    RB = 443
    SELECT = 444
    START = 445
    LT = 446
    RT = 447
    L3 = 448
    R3 = 449
    HOME = 450
    L4 = 459
    R4 = 461
    CAPTURE = 468
    LS_LEFT = 495
    LS_UP_LEFT = 496
    LS_UP = 497
    LS_UP_RIGHT = 498
    LS_RIGHT = 499
    LS_DOWN_RIGHT = 500
    LS_DOWN = 501
    LS_DOWN_LEFT = 502
    RS_LEFT = 503
    RS_UP_LEFT = 504
    RS_UP = 505
    RS_UP_RIGHT = 506
    RS_RIGHT = 507
    RS_DOWN_RIGHT = 508
    RS_DOWN = 509
    RS_DOWN_LEFT = 510


def scan_code(key: int) -> int:
    return key & 0xFF


def key_from_scan(scan: int, kind: int = 1) -> int:
    """getHidKeyCodeByGcmScanCode: kind 1 = gamepad, 2 = keyboard."""
    return 0 if scan == 0 else (kind << 8) | scan


# Remappable source keys -> GCM mapping id (getSupportMappingKeyIdByHidKeyCode).
MAPPING_ID = {Key.L4: 1, Key.R4: 2, 462: 3, 463: 4, Key.A: 11, Key.B: 12, Key.X: 13, Key.Y: 14}
KEY_BY_MAPPING_ID = {v: k for k, v in MAPPING_ID.items()}

# What the Nova 2 Lite exposes over Bluetooth (Nova2LiteDevCfg + DevConfig).
NOVA_REMAPPABLE = [Key.L4, Key.R4]
NOVA_TARGETS = [Key.A, Key.B, Key.X, Key.Y, Key.UP, Key.DOWN, Key.LEFT, Key.RIGHT, Key.LB, Key.RB,
                Key.LT, Key.RT, Key.L3, Key.R3, Key.SELECT, Key.START]
NOVA_STICK_TARGETS = list(range(Key.LS_LEFT, Key.RS_DOWN_LEFT + 1))   # firmware >= 6900
STICK_TARGETS_MIN_FIRMWARE = 6900

CMD_KEY_MAPPING = 0x1F


@dataclass
class Mapping:
    source: int                 # Key
    kind: str                   # "key", "disabled", "macro", "keyboard"
    targets: tuple[int, ...] = ()


def mapping_query(source: int) -> bytes:
    """[0x1F, 0, id, 0, 0, sum]. Note: this command has no length byte."""
    return seal(bytes([CMD_KEY_MAPPING, 0, MAPPING_ID[source], 0, 0, 0]))


def mapping_set(source: int, target: int | None) -> bytes:
    """Map `source` to `target` (a Key), or disable it with target=None."""
    if target is None:
        scan, flag = 0, 0xFF
    else:
        scan, flag = scan_code(target), 0
    return seal(bytes([CMD_KEY_MAPPING, 1, MAPPING_ID[source], scan, flag, 0]))


def mapping_reset(source: int) -> bytes:
    return seal(bytes([CMD_KEY_MAPPING, 2, MAPPING_ID[source], 0, 0, 0]))


def mapping_parse(request: bytes, reply: bytes) -> Mapping:
    r = verify(request, reply, min_len=6)
    source = KEY_BY_MAPPING_ID.get(r[2])
    if source is None:
        raise ProtocolError(f"unknown mapping id {r[2]}")
    flag = r[4]
    if flag == 0xFF:
        return Mapping(source, "disabled")
    if flag == 0xFE:
        return Mapping(source, "macro")
    if flag == 0x01:
        return Mapping(source, "keyboard", (key_from_scan(r[3], 2),))
    targets = tuple(k for k in (key_from_scan(r[3]), key_from_scan(flag)) if k)
    return Mapping(source, "key", targets)


# -- trigger quick mode (0x21) ------------------------------------------------------
# "Quick trigger": the trigger reports fully pressed early. No length byte.
CMD_TRIGGER_QUICK = 0x21


def trigger_quick_query() -> bytes:
    return seal(bytes([CMD_TRIGGER_QUICK, 0, 1, 1, 0]))


def trigger_quick_set(left: bool, right: bool) -> bytes:
    return seal(bytes([CMD_TRIGGER_QUICK, 1, 1 if left else 2, 1 if right else 2, 0]))


def trigger_quick_parse(request: bytes, reply: bytes) -> tuple[bool, bool]:
    r = verify(request, reply, min_len=5)
    return r[2] == 1, r[3] == 1


# -- config slots / profiles (0x29) -------------------------------------------------
CMD_SLOT = 0x29


def slot_query() -> bytes:
    return seal(bytes([CMD_SLOT, 1, 5, 0, 0]))


def slot_switch(slot: int) -> bytes:
    if not 0 <= slot <= 0xFF:
        raise ValueError("slot out of range")
    return seal(bytes([CMD_SLOT, 1, 6, 1, slot, 0]))


def slot_query_parse(request: bytes, reply: bytes) -> tuple[int, int]:
    """(total slots, current slot)"""
    r = verify(request, reply, min_len=7)
    return r[4], r[5]


def slot_switch_parse(request: bytes, reply: bytes) -> int:
    return verify(request, reply, min_len=6)[4]


# -- button LED, lighting V3 (0x05 / 0x08) -----------------------------------------
# The Nova 2 Lite has one: the Home button. Colours travel as RGB bytes.
CMD_LIGHTING = 0x05
LIGHTING_BUTTON = 0x08
EFFECT_SOLID = 1


@dataclass
class ButtonLight:
    key: int
    rgb: tuple[int, int, int]
    effect: int
    rate: int | None = None


def light_query(key: int = Key.HOME) -> bytes:
    return seal(bytes([CMD_LIGHTING, LIGHTING_BUTTON, 6, 0, scan_code(key), 0]))


def light_set(rgb: tuple[int, int, int], key: int = Key.HOME, effect: int = EFFECT_SOLID) -> bytes:
    if any(not 0 <= c <= 255 for c in rgb):
        raise ValueError("colour components must be 0-255")
    return seal(bytes([CMD_LIGHTING, LIGHTING_BUTTON, 10, 1, scan_code(key), *rgb, effect, 0]))


def light_reset(key: int = Key.HOME) -> bytes:
    return seal(bytes([CMD_LIGHTING, LIGHTING_BUTTON, 6, 2, scan_code(key), 0]))


def light_parse(request: bytes, reply: bytes) -> ButtonLight:
    r = verify(request, reply, min_len=10)
    return ButtonLight(key_from_scan(r[4]), (r[5], r[6], r[7]), r[8], r[9] if len(r) == 11 else None)
