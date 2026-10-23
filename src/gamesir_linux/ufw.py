"""Parser for JieLi .ufw firmware containers, as shipped by GameSir.

Layout (all little-endian, reverse-engineered and checked against every .ufw
bundled with GameSir Connect 1.14.5):

    0x00  header, 64 bytes, ENC-scrambled with key 0xFFFF
          u16 header_crc   CRC-16 of header bytes 2..63 (descrambled)
          u16 table_crc    CRC-16 of the raw (still scrambled) entry table
          u32 file_size
          u16 entry_count
          u16 unknown (4)
          u32 unknown (0x200)
          char chip[16]    e.g. "AC695X"
    0x40  entry table, entry_count x 80 bytes, each ENC-scrambled with key 0xFFFF
          u8  type
          u8  flags
          u16 index
          u16 crc          CRC-16 of the payload (raw, or descrambled for configs)
          u16 reserved
          u32 offset
          u32 length
          u32 padded_length
          ...              unknown
          char name[16]    at +64
    ...   payloads

Firmware payloads (ota.bin, flash.bin, ...) are encrypted for the chip and are
never decrypted here; the tool only verifies and delivers them. Small config
payloads (isd_config.ini, script.ver, ...) use position-keyed block scrambling
and are descrambled so the device identity can be read.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .jlcrypt import crc16, enc, enc_blocks

HEADER_SIZE = 0x40
ENTRY_SIZE = 0x50
_HEADER = struct.Struct("<HHIHHI")
_ENTRY = struct.Struct("<BBHHHIII")


class UfwError(ValueError):
    """The file is not a readable .ufw container."""


@dataclass
class UfwEntry:
    name: str
    type: int
    flags: int
    index: int
    crc: int
    offset: int
    length: int
    padded_length: int
    crc_ok: bool = False
    scrambled: bool = False  # payload needed block descrambling to verify

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass
class UfwFile:
    data: bytes
    chip: str
    file_size: int
    header_crc: int
    table_crc: int
    entries: list[UfwEntry]
    header_crc_ok: bool
    table_crc_ok: bool
    size_ok: bool
    config: dict[str, str] = field(default_factory=dict)
    tool_version: str | None = None

    # -- identity ---------------------------------------------------------

    @property
    def jl_pid(self) -> str | None:
        """JieLi product id string, e.g. "AC695X_TRANS"."""
        return self.config.get("PID")

    @property
    def jl_vid(self) -> str | None:
        """JieLi version id string, e.g. "N2LT" (Nova 2 Lite controller)."""
        return self.config.get("VID")

    # -- integrity --------------------------------------------------------

    def problems(self) -> list[str]:
        """Every integrity problem found; an empty list means the file is intact."""
        out = []
        if not self.header_crc_ok:
            out.append("header CRC mismatch")
        if not self.size_ok:
            out.append(f"size mismatch: header says {self.file_size}, file is {len(self.data)}")
        if not self.table_crc_ok:
            out.append("entry table CRC mismatch")
        for e in self.entries:
            if e.end > len(self.data):
                out.append(f"{e.name}: extends past end of file")
            elif not e.crc_ok:
                out.append(f"{e.name}: CRC mismatch")
        return out

    @property
    def ok(self) -> bool:
        return not self.problems()

    def entry(self, name: str) -> UfwEntry | None:
        return next((e for e in self.entries if e.name == name), None)

    def payload(self, name: str) -> bytes:
        """Payload of an entry, descrambled if it is a config partition."""
        e = self.entry(name)
        if e is None:
            raise KeyError(name)
        raw = self.data[e.offset : e.end]
        return enc_blocks(raw, e.offset) if e.scrambled else raw


def _parse_ini(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"^\s*([A-Za-z0-9_]+)\s*=\s*([^;\r\n]*?)\s*;?\s*$", text, re.M):
        out.setdefault(m.group(1).upper(), m.group(2))
    return out


def parse(data: bytes) -> UfwFile:
    if len(data) < HEADER_SIZE:
        raise UfwError("file too short for a .ufw header")
    header = enc(data[:HEADER_SIZE])
    header_crc, table_crc, file_size, count, _, _ = _HEADER.unpack_from(header, 0)
    chip = header[16:32].split(b"\0", 1)[0].decode("latin1")

    header_crc_ok = crc16(header[2:HEADER_SIZE]) == header_crc
    if not header_crc_ok and not chip.isprintable():
        raise UfwError("not a .ufw file (header does not descramble)")

    table_end = HEADER_SIZE + count * ENTRY_SIZE
    if table_end > len(data):
        raise UfwError("entry table extends past end of file")
    table_crc_ok = crc16(data[HEADER_SIZE:table_end]) == table_crc

    entries = []
    for i in range(count):
        raw = enc(data[HEADER_SIZE + i * ENTRY_SIZE : HEADER_SIZE + (i + 1) * ENTRY_SIZE])
        typ, flags, index, crc, _, offset, length, padded = _ENTRY.unpack_from(raw, 0)
        name = raw[64:80].split(b"\0", 1)[0].decode("latin1")
        e = UfwEntry(name, typ, flags, index, crc, offset, length, padded)
        payload = data[offset : offset + length]
        if len(payload) == length:
            if crc16(payload) == crc:
                e.crc_ok = True
            elif crc16(enc_blocks(payload, offset)) == crc:
                e.crc_ok = e.scrambled = True
        entries.append(e)

    ufw = UfwFile(
        data=data,
        chip=chip,
        file_size=file_size,
        header_crc=header_crc,
        table_crc=table_crc,
        entries=entries,
        header_crc_ok=header_crc_ok,
        table_crc_ok=table_crc_ok,
        size_ok=file_size == len(data),
    )

    isd = ufw.entry("isd_config.ini")
    if isd is not None and isd.crc_ok:
        ufw.config = _parse_ini(ufw.payload("isd_config.ini").decode("latin1"))
    ver = ufw.entry("script.ver")
    if ver is not None and ver.crc_ok:
        ufw.tool_version = ufw.payload("script.ver").decode("latin1").strip("\0 \r\n")
    return ufw


def load(path: str | Path) -> UfwFile:
    return parse(Path(path).read_bytes())


# -- builder, used to make synthetic fixtures for tests ---------------------

def build(chip: str, files: list[tuple[str, int, bytes]], *, scramble: set[str] = frozenset()) -> bytes:
    """Build a .ufw container.

    `files` is a list of (name, type, payload). Payloads named in `scramble` are
    stored with config-partition block scrambling, as GameSir's tooling does.
    """
    count = len(files)
    pos = HEADER_SIZE + count * ENTRY_SIZE
    pos = (pos + 0x3FF) & ~0x3FF  # payloads start on a 1 KiB boundary, as observed
    layout = []
    for name, typ, payload in files:
        layout.append((name, typ, payload, pos))
        padded = (len(payload) + 0x1F) & ~0x1F
        pos += padded
    file_size = pos

    out = bytearray(file_size)
    table = bytearray()
    for idx, (name, typ, payload, offset) in enumerate(layout):
        stored = enc_blocks(payload, offset) if name in scramble else payload
        out[offset : offset + len(stored)] = stored
        entry = bytearray(ENTRY_SIZE)
        padded = (len(payload) + 0x1F) & ~0x1F
        _ENTRY.pack_into(entry, 0, typ, 0, idx, crc16(payload), 0, offset, len(payload), padded)
        entry[64:80] = name.encode("latin1")[:16].ljust(16, b"\0")
        table += enc(bytes(entry))
    out[HEADER_SIZE : HEADER_SIZE + len(table)] = table

    header = bytearray(HEADER_SIZE)
    _HEADER.pack_into(header, 0, 0, crc16(bytes(table)), file_size, count, 4, 0x200)
    header[16:32] = chip.encode("latin1")[:16].ljust(16, b"\0")
    struct.pack_into("<H", header, 0, crc16(bytes(header[2:])))
    out[:HEADER_SIZE] = enc(bytes(header))
    return bytes(out)
