# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flash writes in JieLi's USB download mode: erase and write, nothing else.

Kept apart from jlboot (read-only) on purpose. A Writer may send exactly two more commands,
sector erase (FB01) and flash write (FB04), and only inside one window of sectors given up front,
never touching a reserved range. Chip erase, block erase and the chip-key write (FC12, eFuses,
irreversible) do not exist here.

burn() follows the order of JieLi's own updater (jl_firmware_upgrade_x86.dll, FUN_1000b560;
PROTOCOL.md section 3): a copy of the header into the spare sector first, then erase up to the
image end, write top-down, rewrite the header sector and sector 0 last, and CRC-check every
sector against the loader's own CRC (FC13).
"""

from __future__ import annotations

from collections.abc import Callable

from . import jlboot, jlcrypt

ERASE_SECTOR = 0xFB01
WRITE_FLASH = 0xFB04
SECTOR = jlboot.SECTOR
BLANK_CRC = jlcrypt.crc16(b"\xff" * SECTOR)
# Never: FB00 block erase, FB02 chip erase, FB42, FC12 chip key write.


class WriteError(jlboot.JLBootError):
    pass


def _span(a: int, b: int) -> str:
    return f"{a:#x}-{b - 1:#x}"


class Writer:
    """Erase and write inside [lo, hi), never inside `keep` (list of (start, end))."""

    def __init__(self, boot: jlboot.JLBoot, lo: int, hi: int, keep: list[tuple[int, int]]):
        if lo % SECTOR or hi % SECTOR or not lo < hi:
            raise WriteError(f"write window {lo:#x}-{hi:#x} is not whole sectors")
        for k_lo, k_hi in keep:
            if lo < k_hi and k_lo < hi:
                raise WriteError(f"write window {_span(lo, hi)} overlaps a reserved area {_span(k_lo, k_hi)}")
        self.boot, self.lo, self.hi, self.keep = boot, lo, hi, list(keep)

    def _check(self, addr: int, size: int) -> None:
        if not (self.lo <= addr and addr + size <= self.hi):
            raise WriteError(f"{_span(addr, addr + size)} is outside the write window {_span(self.lo, self.hi)}")
        for k_lo, k_hi in self.keep:
            if addr < k_hi and k_lo < addr + size:
                raise WriteError(f"{_span(addr, addr + size)} touches a reserved area {_span(k_lo, k_hi)}")

    def _send(self, cmd: int, args: bytes, data: bytes | None = None) -> bytes:
        assert cmd in (ERASE_SECTOR, WRITE_FLASH)
        cdb = (cmd.to_bytes(2, "big") + args).ljust(16, b"\xff")
        if data is not None:
            return self.boot.execute(cdb, data, 0)
        resp = self.boot.execute(cdb, None, 16)
        if int.from_bytes(resp[:2], "big") != cmd:
            raise WriteError(f"reply {resp[:2].hex()} does not answer {cmd:#06x}")
        return resp

    def erase(self, addr: int) -> None:
        """Erase one 4 KB sector and check it reads back blank."""
        if addr % SECTOR:
            raise WriteError(f"{addr:#x} is not the start of a sector")
        self._check(addr, SECTOR)
        self._send(ERASE_SECTOR, addr.to_bytes(4, "big"))
        if self.boot.flash_crc16(addr, SECTOR) != BLANK_CRC:
            raise WriteError(f"sector {addr:#x} is not blank after erasing")

    def write(self, addr: int, data: bytes) -> None:
        """Write up to one sector into an erased sector (no erase here)."""
        if addr % SECTOR or not 0 < len(data) <= SECTOR:
            raise WriteError(f"write of {len(data)} bytes at {addr:#x} is not within one sector")
        self._check(addr, len(data))
        self._send(WRITE_FLASH, addr.to_bytes(4, "big") + len(data).to_bytes(2, "big") + b"\x00"
                   + jlcrypt.crc16(data).to_bytes(2, "little"), data)

    def program(self, addr: int, data: bytes) -> None:
        """Erase a sector and write it (skipping the write when the data is blank)."""
        self.erase(addr)
        if data.rstrip(b"\xff"):
            self.write(addr, data)

    def mismatches(self, addr: int, data: bytes) -> list[int]:
        """Sectors whose flash CRC differs from `data` placed at `addr`."""
        return [a for a in range(addr, addr + len(data), SECTOR)
                if self.boot.flash_crc16(a, len(data[a - addr:a - addr + SECTOR]))
                != jlcrypt.crc16(data[a - addr:a - addr + SECTOR])]


def burn(writer: Writer, image: bytes, header: int,
         progress: Callable[[str, int, int], None] | None = None) -> None:
    """Write `image` from flash address 0, the way JieLi's updater does.

    `image` covers [0, len) and its flash header sits at `header`: 0x1000 on the controller,
    where sector 0 is the spare, or 0 on the 2.4 GHz receiver, where 0x1000 is. The window must be
    [0, len(image)). Raises WriteError if any sector fails its CRC check afterwards; the caller
    decides whether to try again."""
    end = len(image)
    if end % SECTOR or header not in (0, SECTOR) or end < 3 * SECTOR:
        raise WriteError("the image must be whole sectors, with its header at 0 or 0x1000")
    spare = SECTOR if header == 0 else 0
    if (writer.lo, writer.hi) != (0, end):
        raise WriteError(f"the write window must be exactly {_span(0, end)}")
    sector = lambda a: image[a:a + SECTOR]
    steps = end // SECTOR + 2
    done = 0

    def tick(what: str) -> None:
        nonlocal done
        done += 1
        if progress:
            progress(what, done, steps)

    # 1. A copy of the header into the spare sector, checked, so a header exists while its own
    #    sector is rewritten. Only then is the header's sector erased.
    head = sector(header)[:0x40]
    writer.erase(spare)
    writer.write(spare, head)
    if writer.boot.flash_crc16(spare, len(head)) != jlcrypt.crc16(head):
        raise WriteError(f"the header copy in sector {spare:#x} did not verify")
    writer.erase(header)
    tick("header")
    # 2. Everything from 0x2000 up, top-down.
    for addr in range(end - SECTOR, 2 * SECTOR - 1, -SECTOR):
        writer.program(addr, sector(addr))
        tick(f"{addr:#x}")
    # 3. The header sector, then the spare sector, last.
    writer.program(header, sector(header))
    writer.program(spare, sector(spare))
    tick("first two sectors")
    # 4. Every sector against the loader's CRC.
    bad = writer.mismatches(0, image)
    if bad:
        raise WriteError(f"{len(bad)} sector(s) did not verify, first at {bad[0]:#x}")
    tick("verified")


def connect_record(image: bytes, auth: bytes) -> bytes:
    """`image` with the 16-byte record JieLi's updater writes over its last 16 bytes when the
    image header's byte 2 is above 0x0F: "dll\\0", 0x1501, the erase-all flag (0), 0xFF, then
    the 6 bytes of the .ufw's JLDGAUTH block (`auth`), then FF FF."""
    if len(auth) != 6:
        raise ValueError("auth must be 6 bytes")
    header = jlcrypt.enc(image[:32], 0xFFFF)
    if header[2] <= 0x0F:
        return image
    record = b"dll\x00" + (0x1501).to_bytes(2, "little") + b"\x00\xff" + auth + b"\xff\xff"
    return image[:-16] + record
