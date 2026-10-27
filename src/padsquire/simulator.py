# SPDX-License-Identifier: AGPL-3.0-or-later
"""A simulated Nova 2 Lite that speaks GCM, for tests and --simulate.

One class per connection, each answering the way PROTOCOL.md records for it: `SimulatedNova`
over Bluetooth, `SimulatedUsbNova` by cable or through the 2.4 GHz receiver (`simulated(link)`
picks one). By cable and receiver that is confirmed on hardware; over Bluetooth it is what the
GameSir app's code suggests, not yet tried. It proves the tool is consistent with those notes,
not that the notes are complete. Not simulated: USB report framing (tests/test_hid.py replays
real replies for that), timing, and the gamepad output that sometimes stops after a write.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from . import c2, gcm, hid

LINKS = ("cable", "receiver", "bluetooth")
RECEIVER_FIRMWARE = "2800"


def simulated(link: str = "bluetooth", **kw) -> SimulatedNova:
    """A simulated controller on `link`: "cable", "receiver" or "bluetooth"."""
    if link not in LINKS:
        raise ValueError(f"link must be one of {', '.join(LINKS)}")
    return SimulatedNova(**kw) if link == "bluetooth" else SimulatedUsbNova(link, **kw)


class SimulatedNova:
    """Over Bluetooth: every command answers, including auto-sleep and profiles (unconfirmed)."""

    link = "bluetooth"

    def __init__(self, firmware: str = "8201", battery: int = 0x80 | 76):
        self.firmware = firmware
        self.battery = battery
        # A real controller's defaults (read over USB): 5 inner, 0 outer, 5 / 95 trigger, 0 anti.
        self.dead_zones = {z: 95 if "BACK" in z.name else 0 if z.name.endswith(("OUTER", "ANTI")) else 5
                           for z in gcm.DeadZone}
        self.features = {f: False for f in gcm.Feature}
        self.vibration = gcm.Vibration(75, 0, False, False, 75, 0, False, False)   # a real controller's defaults
        self.auto_sleep = 10
        self.mappings: dict[int, tuple[int, int]] = {gcm.Key.L4: (0, 0xFF), gcm.Key.R4: (0, 0xFF)}
        self.remaps: dict[int, bytes] = {}       # key code -> target codes (f1); absent = own function
        self.quick_trigger = (False, False)
        self.slot_count, self.slot = 3, 0
        self.light = (255, 255, 255)
        self.log: list[bytes] = []
        self.corrupt_next = False  # test hook: next reply gets a bad checksum

    def reset(self) -> None:
        SimulatedNova.__init__(self, self.firmware, self.battery)

    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes:
        self.log.append(frame)
        reply = self.handle(frame)
        if self.corrupt_next:
            self.corrupt_next = False
            reply = reply[:-1] + bytes([(reply[-1] + 1) & 0xFF])
        return reply

    # -- device logic ---------------------------------------------------------

    def handle(self, f: bytes) -> bytes:
        if f == gcm.basic_info_query():
            fw = bytes.fromhex(self.firmware)
            return bytes([0xC9, 0xC0, 0xFF, 0xEE, 0x12, 0x34, 0x56, 0x01, 0x01, 0x05, 0x00,
                          fw[0], fw[1], 0xEA, 0x07, 3, 14, 0x01, 0x02, 0x00])
        if len(f) >= 5 and f[-1] == gcm.checksum(f):
            # The two commands without a length byte.
            if f[0] == gcm.CMD_KEY_MAPPING and f[2] in gcm.KEY_BY_MAPPING_ID:
                src = gcm.KEY_BY_MAPPING_ID[f[2]]
                if f[1] == 1:
                    self.mappings[src] = (f[3], f[4])
                elif f[1] == 2:
                    self.mappings[src] = (0, 0xFF)
                scan, flag = self.mappings.get(src, (0, 0xFF))
                return gcm.seal(bytes([f[0], f[1], f[2], scan, flag, 0]))
            if f[0] == gcm.CMD_REMAP and len(f) == 9 and f[2] == 9:
                key, targets = f[3], bytes(f[5:5 + f[4]])
                if f[1] == 1 and all((0x100 | t) in gcm.NOVA_REMAP_TARGETS for t in targets):
                    self.remaps[key] = targets
                elif f[1] == 2:
                    self.remaps.pop(key, None)
                # A set answers with what was stored: nothing, if the target was refused (like 8201).
                now = self.remaps.get(key, b"") if f[1] != 1 or self.remaps.get(key) == targets else b""
                return gcm.seal(bytes([f[0], f[1], 9, key, len(now), *now.ljust(3, b"\0"), 0]))
            if f[0] == gcm.CMD_TRIGGER_QUICK:
                if f[1] == 1:
                    self.quick_trigger = (f[2] == 1, f[3] == 1)
                left, right = self.quick_trigger
                return gcm.seal(bytes([f[0], f[1], 1 if left else 2, 1 if right else 2, 0]))
        if len(f) < 4 or f[-1] != gcm.checksum(f) or f[2] != len(f):
            return self._small(0xEE, 0, 0, 1)  # garbage in, error frame out
        cmd, sub = f[0], f[1]
        is_set = f[3] == 1
        if cmd == gcm.CMD_DEAD_ZONE and sub in self.dead_zones.keys():
            zone = gcm.DeadZone(sub)
            if is_set:
                self.dead_zones[zone] = f[4]
            return self._small(cmd, sub, 1 if is_set else 0, self.dead_zones[zone])
        if cmd == gcm.CMD_FEATURE and sub in {x.value for x in gcm.Feature}:
            feat = gcm.Feature(sub)
            if is_set:
                self.features[feat] = bool(f[4])
            return self._small(cmd, sub, 1 if is_set else 0, int(self.features[feat]))
        if cmd == gcm.CMD_GENERAL and sub == gcm.GENERAL_AUTO_SLEEP:
            if is_set:
                self.auto_sleep = f[4]
            return self._small(cmd, sub, 1 if is_set else 0, self.auto_sleep)
        if cmd == gcm.CMD_VIBRATION:
            if is_set:
                v = gcm.vibration_parse(f, f)
                # Grip strength is stored rounded down to a multiple of 25 (150 and 200 are kept,
                # 255 becomes 250); there are no trigger motors, so their strength stays 0.
                self.vibration = gcm.Vibration(v.left_grip // 25 * 25, 0, v.left_trigger_pressed, v.left_trigger_sync,
                                               v.right_grip // 25 * 25, 0, v.right_trigger_pressed, v.right_trigger_sync)
            return gcm.vibration_set(self.vibration)
        if cmd == gcm.CMD_GET_INFO and sub == gcm.INFO_HOST:
            return gcm.seal(bytes([cmd, sub, 6, self.battery, 0, 0]))
        if cmd == gcm.CMD_SLOT and sub == 1:
            if is_set:
                if f[4] >= self.slot_count:
                    return self._small(cmd, sub, 1, self.slot)
                self.slot = f[4]
                return self._small(cmd, sub, 1, self.slot)
            return gcm.seal(bytes([cmd, sub, 7, 0, self.slot_count, self.slot, 0]))
        if cmd == gcm.CMD_LIGHTING and sub == gcm.LIGHTING_BUTTON and len(f) > 4 and f[4] == gcm.scan_code(gcm.Key.HOME):
            if f[3] == 1:
                self.light = (f[5], f[6], f[7])
            elif f[3] == 2:
                self.light = (255, 255, 255)
            return gcm.seal(bytes([cmd, sub, 10, f[3], f[4], *self.light, gcm.EFFECT_SOLID, 0]))
        if cmd == gcm.CMD_RESTORE:
            self.reset()
            return gcm.seal(bytes([cmd, sub, 5, 0, 0]))
        return self._small(cmd, sub, 0, 0)

    @staticmethod
    def _small(cmd: int, sub: int, flag: int, value: int) -> bytes:
        return gcm.seal(bytes([cmd, sub, 6, flag, value, 0]))


class SimulatedUsbNova(SimulatedNova):
    """By cable or through the 2.4 GHz receiver, as confirmed on hardware (firmware 8201)."""

    def __init__(self, link: str = "cable", firmware: str = "8201", battery: int = 82):
        if link not in ("cable", "receiver"):
            raise ValueError("link must be cable or receiver")
        self.link = link
        super().__init__(firmware, battery)
        self.mappings = {gcm.Key.L4: (0, 0), gcm.Key.R4: (0, 0)}      # 0x1F reads "not assigned"

    def reset(self) -> None:
        super().reset()
        self.mappings = {gcm.Key.L4: (0, 0), gcm.Key.R4: (0, 0)}

    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes:
        # Auto-sleep and profiles get no reply at all; hid.py gives up after its attempts.
        if len(frame) > 1 and (frame[0], frame[1]) in ((gcm.CMD_GENERAL, gcm.GENERAL_AUTO_SLEEP), (gcm.CMD_SLOT, 1)):
            self.log.append(frame)
            raise hid.HidError(f"no valid reply to {frame.hex(' ')} (last report: nothing)")
        return await super().request(frame, accept)

    def handle(self, f: bytes) -> bytes:
        if f == gcm.basic_info_query():
            # The 19-byte form; through the receiver its firmware is the receiver's own. Byte 18
            # is 255 on the cable (charging), the battery level through the receiver.
            fw = bytes.fromhex(self.firmware if self.link == "cable" else RECEIVER_FIRMWARE)
            return bytes([0x01, 0xDF, 0x25, 0xF1, 0x38, 0x8D, 0x01, 0x00, 0x00, 0x00, fw[0], fw[1],
                          0, 0, 0, 0, 0x04, 0x02, 0xFF if self.link == "cable" else self.battery & 0x7F])
        if f[0] == gcm.CMD_FEATURE and len(f) == 6 and f[2] == 6 and f[3] == 1 and f[1] in gcm.USB_READONLY_FEATURES:
            # Taken without complaint, and read back unchanged.
            return self._small(f[0], f[1], 1, int(self.features[gcm.Feature(f[1])]))
        if f[0] == gcm.CMD_KEY_MAPPING and len(f) == 6 and f[2] in gcm.KEY_BY_MAPPING_ID:
            src = gcm.KEY_BY_MAPPING_ID[f[2]]                 # the back buttons' own command: no effect
            scan, flag = self.mappings[src]
            return gcm.seal(bytes([f[0], f[1], f[2], scan, flag, 0]))
        return super().handle(f)

    async def send(self, frame: bytes) -> None:
        """Reports without a reply (the vibration test): logged, nothing else."""
        self.log.append(frame)

    async def state_report(self) -> bytes:
        """One 0xF4 state report, carrying the controller's firmware version at bytes 20-21."""
        fw = bytes.fromhex(self.firmware)
        report = bytearray(64)
        report[0:3] = bytes([gcm.STATE_REPORT, 0, 0x17])
        report[19], report[20], report[21], report[22] = 0xFF, fw[0], fw[1], 0x8D
        return bytes(report)


class SimulatedSticks:
    """Live input for a simulated controller, like liveinput.LiveInput but without a device node:
    the sticks rest a little off centre (the left one more), with some noise, and come out after
    the simulated controller's dead zones. Nothing is pressed."""

    fd = None
    REST = {"left": (0.03, -0.012), "right": (0.004, 0.002)}

    def __init__(self, nova: SimulatedNova, seed: int | None = None):
        self.nova = nova
        self.rng = random.Random(seed)

    def pump(self) -> bool:
        return True

    def _stick(self, side: str) -> tuple[float, float]:
        x, y = self.REST[side]
        x += self.rng.uniform(-0.006, 0.006)
        y += self.rng.uniform(-0.006, 0.006)
        prefix = side.upper()
        inner = self.nova.dead_zones[gcm.DeadZone[f"{prefix}_STICK_INNER"]] / 100
        outer = self.nova.dead_zones[gcm.DeadZone[f"{prefix}_STICK_OUTER"]] / 100
        r = math.hypot(x, y)
        if r <= inner:
            return 0.0, 0.0
        t = min(1.0, (r - inner) / max(0.01, 1 - outer - inner))
        return round(x / r * t, 3) + 0.0, round(y / r * t, 3) + 0.0

    def state(self) -> dict:
        lx, ly = self._stick("left")
        rx, ry = self._stick("right")
        return {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": 0.0, "rt": 0.0, "pressed": []}

    def close(self) -> None:
        pass


class SimulatedC2:
    """A Cyclone 2 on its vendor HID interface, answering the read requests the way PROTOCOL.md
    ("Cyclone 2") reads them from Connect. None of it is confirmed on hardware yet. Plugs into
    `c2usb.C2Transport(sim.read, sim.write)`; `busy_once` makes the next request answer busy."""

    def __init__(self, firmware: str = "123", receiver: str = "", battery: int = 80, cable: bool = True):
        self.firmware, self.receiver = firmware, receiver
        self.battery, self.cable = battery, cable
        self.current = 1
        self.profiles = {n: self._default_profile(n) for n in (1, 2, 3, 4, c2.SHIFT_PROFILE)}
        self.profiles[c2.LIGHT_PROFILE] = self._default_light()
        self.queue: list[bytes] = []
        self.writes: list[bytes] = []
        self.awake = False
        self.busy_once = False

    @staticmethod
    def _default_profile(n: int) -> bytes:
        p = bytearray(c2.PROFILE_SIZE)
        name = b"Shift" if n == c2.SHIFT_PROFILE else f"Profile {n}".encode()
        p[0:len(name)] = name
        p[32:36] = bytes([75, 75, 75, 75])                   # vibration 75 %
        p[46] = 2                                            # 1000 Hz
        for stick in (550, 582):                             # left, right: dead zone on, 10 / 100
            p[stick + 2:stick + 5] = bytes([1, 10, 100])
        for trigger in (494, 522):                           # LT, RT: 5 / 95
            p[trigger + 2:trigger + 5] = bytes([1, 5, 95])
        return bytes(p)

    @staticmethod
    def _default_light() -> bytes:
        light = bytearray(c2.LIGHT_PROFILE_SIZE)
        light[1:5] = bytes([1, 5, 10, 50])                   # effect 0: static, speed 10, 50 %
        light[5:20] = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0, 255, 255, 255])
        return bytes(light)

    def _reply(self, *data: int | bytes) -> None:
        out = bytearray([0x10])
        for d in data:
            out += d if isinstance(d, bytes) else bytes([d])
        self.queue.append(bytes(out + bytes(c2.REPORT_SIZE - len(out))))

    def write(self, report: bytes) -> int:
        assert len(report) == c2.REPORT_SIZE and report[0] == c2.OUT_REPORT
        self.writes.append(report)
        if self.busy_once and report[1] != c2.Cmd.HEARTBEAT:
            self.busy_once = False
            self._reply(c2.Cmd.ACK, 1)
            return len(report)
        cmd = report[1]
        if cmd == c2.Cmd.HEARTBEAT:
            self.awake = True
        elif cmd == c2.Cmd.READ_FIRMWARE_VERSION:
            self._reply(c2.Cmd.READ_FIRMWARE_VERSION_ACK, 0, 0, self.firmware.encode().ljust(5, b"\0"),
                        bytes(3), self.receiver.encode().ljust(5, b"\0"))
        elif cmd == c2.Cmd.READ_CURRENT_PROFILE:
            self._reply(c2.Cmd.READ_CURRENT_PROFILE_ACK, self.current)
        elif cmd == c2.Cmd.READ_PROFILE:
            number, off, n = report[2], report[3] << 8 | report[4], report[5]
            self._reply(c2.Cmd.READ_PROFILE_ACK, number, off >> 8, off & 0xFF, n,
                        self.profiles[number][off:off + n])
        return len(report)

    def state_report(self) -> bytes:
        r = bytearray(c2.REPORT_SIZE)
        r[0] = c2.STATE_REPORT
        r[1:5] = bytes([128, 128, 128, 128])
        r[5] = 0x08                                          # D-pad released
        r[35], r[36], r[37] = int(self.cable), self.battery, 0
        r[38:53] = self.profiles[c2.LIGHT_PROFILE][5:20]
        return bytes(r)

    def read(self, timeout: float) -> bytes | None:
        if self.queue:
            return self.queue.pop(0)
        return self.state_report() if self.awake else None
