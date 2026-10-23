# SPDX-License-Identifier: AGPL-3.0-or-later
"""JieLi BR23 flash images: read the layout, and check that an update keeps the reserved areas.

Decoded from the first flash dump of a Nova 2 Lite (PROTOCOL.md, section 3, "Flash layout"):

  base (0x1000 on the Nova 2 Lite)
    header, 32 bytes, ENC key 0xFFFF; u16 CRC-16 of the descrambled bytes 2..31 first
    file table, 32-byte entries, each ENC key 0xFFFF; offsets relative to base:
      uboot.boot, isd_config.ini, app_dir_head, key_mac
  app area (base + app_dir_head offset)
    app directory, 32-byte entries, each ENC key chip_key ^ ((addr - app) >> 2);
    absolute flash addresses: app.bin, VM, PRCT (the code), BTIF, EXIF, USERIF, ...

A firmware image (flash.bin / flash2.bin in a .ufw) has the same structure, starting at its
header. It is built for one layout (where its code starts) and one chip key; check_update()
refuses it unless both match the controller's own flash, and unless writing it leaves every
reserved area (settings, Bluetooth address, calibration) untouched.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import jlcrypt

ENTRY = 32
HEADER_KEY = 0xFFFF
# Areas an update must never write: settings, Bluetooth address and pairing, the factory
# calibration (USERIF), and whatever EXIF holds. isd_config.ini marks them _OPT=1 ("keep").
RESERVED = ("VM", "BTIF", "EXIF", "USERIF")
CODE = "PRCT"
# Where an image goes, by the CRC field of its app_dir_head entry, as JieLi's updater files them
# (jl_firmware_upgrade_x86.dll, FUN_1000ded0): laid out from 0, from 0x1000 or from 0x10000.
PLACEMENT = {0xFFFF: 0, 0x00FF: 0x1000, 0x0FFF: 0x10000, 0xFF00: 0x10000}


class FlashError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    type: int
    crc: int
    offset: int
    length: int
    index: int

    @property
    def end(self) -> int:
        return self.offset + self.length


def _named(e: Entry) -> bool:
    """A real entry: an ASCII name of letters, digits, '_' and '.' (decrypted junk is not)."""
    return bool(e.name) and e.name.isascii() and e.name.replace("_", "").replace(".", "").isalnum()


def _entry(block: bytes) -> Entry:
    typ, _flags, crc, offset, length, index = struct.unpack("<BBHIII", block[:16])
    name = block[16:].split(b"\0")[0].split(b"\xff")[0].decode("ascii", "replace")
    return Entry(name, typ, crc, offset, length, index)


@dataclass
class Layout:
    base: int                   # where the header sits (flash address, or 0 within an image)
    files: list[Entry]          # file table; offsets relative to base
    app: int                    # where the app directory sits, same coordinates as base
    areas: dict[str, Entry]     # app directory by name; absolute flash addresses

    def area(self, name: str) -> tuple[int, int]:
        e = self.areas[name]
        return e.offset, e.end

    @property
    def code(self) -> tuple[int, int]:
        """The firmware area: PRCT where the image declares it (the controller), otherwise from the
        header up to the settings area (the 2.4 GHz receiver's images have no PRCT entry)."""
        if CODE in self.areas:
            return self.area(CODE)
        head = next(e for e in self.files if e.name == "app_dir_head")
        return PLACEMENT.get(head.crc, self.base), self.areas["VM"].offset


def parse(data: bytes, base: int, chip_key: int) -> Layout:
    """Layout of a whole flash dump (base = where the header is) or of an image (base = 0)."""
    header = jlcrypt.enc(data[base:base + ENTRY], HEADER_KEY)
    if len(header) < ENTRY or int.from_bytes(header[:2], "little") != jlcrypt.crc16(header[2:]):
        raise FlashError(f"no valid flash header at {base:#x}")
    files = []
    for pos in range(base + ENTRY, base + 9 * ENTRY, ENTRY):
        e = _entry(jlcrypt.enc(data[pos:pos + ENTRY], HEADER_KEY))
        if not _named(e):
            break
        files.append(e)
    head = next((e for e in files if e.name == "app_dir_head"), None)
    if head is None:
        raise FlashError("the file table has no app_dir_head")
    app = base + head.offset
    areas: dict[str, Entry] = {}
    for pos in range(app, min(app + 64 * ENTRY, len(data)), ENTRY):
        block = jlcrypt.enc(data[pos:pos + ENTRY], (chip_key ^ ((pos - app) >> 2)) & 0xFFFF)
        e = _entry(block)
        if not _named(e):
            break
        areas[e.name] = e
    missing = [n for n in RESERVED if n not in areas]
    if missing:
        raise FlashError(f"app directory unreadable with chip key {chip_key:#06x} "
                         f"(missing {', '.join(missing)})")
    return Layout(base, files, app, areas)


@dataclass
class UpdatePlan:
    start: int                  # flash address the image goes to
    end: int                    # first address after it
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def check_update(device: Layout, image: bytes, chip_key: int) -> UpdatePlan:
    """Would writing `image` at the device's header address keep everything it must keep?"""
    start = device.base
    end = start + len(image)
    problems: list[str] = []
    try:
        new = parse(image, 0, chip_key)
    except FlashError as e:
        return UpdatePlan(start, end, [f"the image doesn't fit this controller: {e}"])

    for what, at in (("this device's firmware area ends", device.code[1]), ("the image ends", end)):
        if at % 0x1000:
            problems.append(f"{what} at {at:#x}, not on a 4 KB boundary: that layout needs 256-byte "
                            "erases, which this tool does not do")
    if new.code[0] != device.code[0]:
        problems.append(f"the image's code starts at {new.code[0]:#x}, this controller's at "
                        f"{device.code[0]:#x}: it is built for the other layout")
    if start != device.code[0]:
        problems.append(f"the flash header ({start:#x}) is not where the code starts "
                        f"({device.code[0]:#x})")
    if end > device.code[1]:
        problems.append(f"the image ends at {end:#x}, past the end of the code area "
                        f"({device.code[1]:#x})")
    for name in RESERVED:
        lo, hi = device.area(name)
        if name not in new.areas or new.area(name) != (lo, hi):
            got = f"{new.area(name)[0]:#x}-{new.area(name)[1]:#x}" if name in new.areas else "nothing"
            problems.append(f"{name} moves: this controller has it at {lo:#x}-{hi:#x}, "
                            f"the image expects {got}")
        if start < hi and lo < end:
            problems.append(f"writing {start:#x}-{end:#x} would overwrite {name} ({lo:#x}-{hi:#x})")
    old_key_mac = next((e for e in device.files if e.name == "key_mac"), None)
    new_key_mac = next((e for e in new.files if e.name == "key_mac"), None)
    if old_key_mac and (new_key_mac is None or new_key_mac.offset != old_key_mac.offset):
        problems.append("key_mac moves in the image's file table")
    return UpdatePlan(start, end, problems)


def identity(data: bytes, base: int) -> tuple[str, str]:
    """(PID, VID) from a flash header, e.g. ("AC695X_TRANS", "N2LT"); stored in plain text."""
    vid = data[base + 4:base + 8].split(b"\xff")[0].split(b"\0")[0]
    pid = data[base + 16:base + 32].split(b"\xff")[0].split(b"\0")[0]
    return pid.decode("ascii", "replace"), vid.decode("ascii", "replace")


AUTH_MAGIC = b"JLDGAUTH"


def ufw_auth(blimit: bytes) -> bytes:
    """The 6 bytes JieLi's updater takes from a .ufw's JLDGAUTH block (blimit.bin, already
    block-descrambled) for the record at the end of the image; zeros without a valid block.
    The block is ENC-scrambled with the key stored at offset 0x0E (which stays as is)."""
    if len(blimit) != 0x90:
        return bytes(6)
    key = int.from_bytes(blimit[0x0E:0x10], "little")
    block = bytearray(jlcrypt.enc(blimit, key))
    block[0x0E:0x10] = blimit[0x0E:0x10]
    if (block[:8] != AUTH_MAGIC
            or int.from_bytes(block[0x0A:0x0C], "little") != jlcrypt.crc16(bytes(block[0x0C:0x90]))
            or not block[0x1E] & 4):
        return bytes(6)
    return bytes(block[0x18:0x1E])


def find(data: bytes, chip_key: int) -> Layout:
    """The layout of a whole flash dump, wherever its header is (0x1000, 0 or 0x10000)."""
    for base in (0x1000, 0, 0x10000):
        try:
            return parse(data, base, chip_key)
        except FlashError:
            continue
    raise FlashError("no readable flash layout")
