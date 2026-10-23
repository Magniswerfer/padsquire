"""A simulated Nova 2 Lite that speaks GCM, for tests and --simulate.

It behaves the way static analysis says the real controller does. It proves
the tool is consistent with that analysis, not that the analysis is right.
"""

from __future__ import annotations

from typing import Callable

from . import gcm


class SimulatedNova:
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
        self.quick_trigger = (False, False)
        self.slot_count, self.slot = 3, 0
        self.light = (255, 255, 255)
        self.log: list[bytes] = []
        self.corrupt_next = False  # test hook: next reply gets a bad checksum

    def reset(self) -> None:
        self.__init__(self.firmware, self.battery)

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
                self.vibration = gcm.vibration_parse(f, f)
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
