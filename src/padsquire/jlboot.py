# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only access to a JieLi chip in its mask-ROM USB download mode ("UBOOT1.00").

In this mode the controller is a USB disk that takes JieLi's vendor SCSI commands (see
PROTOCOL.md, section 3). This module can identify the chip, upload the flash loader into RAM,
read the chip key and the flash, and restart the controller. It cannot erase or write flash or
burn the chip key: every command goes through ALLOWED, and anything else is refused before it
reaches the device. Flash writes come later, in their own module, after a verified dump.

The loader binary is fetched from jl-uboot-tool (MIT, kagaimiq) at a pinned commit and checked
against its SHA-256, so no JieLi binary lives in this repo.

On Linux the commands go through the kernel's SCSI generic driver (SG_IO). macOS has no such
thing, so there they go over raw USB (libusb): the same SCSI commands wrapped in USB mass
storage's Bulk-Only Transport, after taking the disk away from macOS's own driver, which only
root may do.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import plistlib
import struct
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import jlcrypt, paths

# -- what may be sent ---------------------------------------------------------------

WRITE_MEMORY = 0xFB06       # RAM only: loader upload, range-checked below
READ_MEMORY = 0xFD07
JUMP = 0xFB08               # start the loader
READ_FLASH = 0xFD05
READ_KEY = 0xFC09
ONLINE_DEVICE = 0xFC0A
RUN_APP = 0xFC0C
FLASH_CRC16_RAW = 0xFC13    # JieLi's updater verifies writes with this one
BUFFER_SIZE = 0xFC14

ALLOWED = {WRITE_MEMORY, READ_MEMORY, JUMP, READ_FLASH, READ_KEY, ONLINE_DEVICE, RUN_APP,
           FLASH_CRC16_RAW, BUFFER_SIZE}
# Never: FB00/FB01/FB02/FB42 erase, FB04 flash write, FC12 chip key write (eFuses, irreversible).

SPI0_NOR = 0x03             # online_device() type of the controller's flash
SECTOR = 0x1000


@dataclass(frozen=True)
class Loader:
    chip: str
    address: int
    url: str
    sha256: str
    ram_end: int            # the loader must fit below this
    block: int = 512


_JLUT = "https://raw.githubusercontent.com/kagaimiq/jl-uboot-tool/adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a"
LOADERS = {
    # Same load address as JieLi's own updater uses for br23 (jl_firmware_upgrade_x86.dll).
    "br23": Loader("br23", 0x12000, f"{_JLUT}/data/loaderblobs/usb/br23loader.bin",
                   "7384429e33ee1f340fbffe46aa928a0099c163e1374a40f6f9f27468a6f16ddf",
                   ram_end=0x2C000),            # BR23 ram0
    # The 2.4 GHz receiver (AC632N). Same address as JieLi's DLL uses for bd19. jl-uboot-tool has
    # no memory map for it; the bound is conservative (the loader ends at 0x7A00).
    "bd19": Loader("bd19", 0x2000, f"{_JLUT}/data/loaderblobs/usb/bd19loader.bin",
                   "faa1b519a3ff1b082f11ccafac8c73d31150161ac972ef9f1934002bcbb8df4f",
                   ram_end=0x10000),
}
LOADER_ARG = 0x0001         # SPI NOR flash, default clock, half-duplex SPI


class JLBootError(RuntimeError):
    pass


# -- SG_IO ---------------------------------------------------------------------------

class _SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p), ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint), ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


SG_IO = 0x2285
SG_DXFER_NONE, SG_DXFER_TO_DEV, SG_DXFER_FROM_DEV = -1, -2, -3


class SgDevice:
    """One /dev/sgN node. execute(cdb, data_out, data_in_len) -> data_in."""

    def __init__(self, path: str, timeout_ms: int = 5000):
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise JLBootError(f"no permission to open {path}. Install the udev rules (the "
                              "settings app's Setup and help page explains how), then replug the controller") from None
        self.timeout_ms = timeout_ms

    def execute(self, cdb: bytes, data_out: bytes | None = None, data_in_len: int = 0) -> bytes:
        hdr = _SgIoHdr(interface_id=ord("S"), cmd_len=len(cdb), mx_sb_len=32, timeout=self.timeout_ms)
        cdb_buf = ctypes.create_string_buffer(cdb, len(cdb))
        sense = ctypes.create_string_buffer(32)
        hdr.cmdp, hdr.sbp = ctypes.addressof(cdb_buf), ctypes.addressof(sense)
        if data_out:
            buf = ctypes.create_string_buffer(data_out, len(data_out))
            hdr.dxfer_direction, hdr.dxfer_len = SG_DXFER_TO_DEV, len(data_out)
        elif data_in_len:
            buf = ctypes.create_string_buffer(data_in_len)
            hdr.dxfer_direction, hdr.dxfer_len = SG_DXFER_FROM_DEV, data_in_len
        else:
            buf, hdr.dxfer_direction = None, SG_DXFER_NONE
        if buf is not None:
            hdr.dxferp = ctypes.addressof(buf)
        import fcntl  # Linux only, like SG_IO itself
        try:
            fcntl.ioctl(self.fd, SG_IO, hdr)
        except OSError as e:
            raise JLBootError(f"SCSI command {cdb[:2].hex()} failed: {e.strerror}") from e
        if hdr.info & 1:
            raise JLBootError(f"SCSI command {cdb[:2].hex()} failed (host {hdr.host_status:#x}, "
                              f"driver {hdr.driver_status:#x}, sense {sense.raw[:hdr.sb_len_wr].hex()})")
        # resid is not usable: the BR23 ROM sends the data yet reports all of it as residue
        # (seen on hardware, 2026-10-23). A bad read is caught by read_twice and the device CRC.
        return buf.raw[:data_in_len] if data_in_len else b""

    def close(self) -> None:
        os.close(self.fd)


# -- raw USB (macOS) --------------------------------------------------------------------------

MACOS = sys.platform == "darwin"
JIELI_VID = 0x4C4A
# USB product IDs of download mode, confirmed on hardware (PROTOCOL.md), and the chip each one is.
# The receiver's USB name says BR34 but its SCSI INQUIRY says BD19, which is what counts.
UBOOT_PIDS = {0x2342: "BR23", 0x3442: "BD19"}
CBW = struct.Struct("<4sIIBBB")         # signature, tag, data length, flags, LUN, command length
CSW = struct.Struct("<4sIIB")           # signature, tag, residue, status


def _usb():
    try:
        import libusb_package
        import usb.core
        import usb.util
    except ImportError:
        raise JLBootError("update mode on macOS needs pyusb and libusb-package: "
                          "pip install pyusb libusb-package") from None
    return libusb_package, usb


class UsbBotDevice:
    """A download-mode chip over raw USB: execute(cdb, data_out, data_in_len) -> data_in, like
    SgDevice. `device` is a pyusb device; the mass storage interface is taken from macOS's disk
    driver for as long as this is open, and handed back on close()."""

    def __init__(self, device, timeout_ms: int = 5000):
        _, usb = _usb()
        self.usb, self.dev, self.timeout_ms, self.tag = usb, device, timeout_ms, 0
        intf = next((i for i in device.get_active_configuration()
                     if i.bInterfaceClass == 0x08 and i.bInterfaceProtocol == 0x50), None)   # mass storage, BOT
        if intf is None:
            raise JLBootError("the download-mode device has no USB disk interface")
        self.intf = intf.bInterfaceNumber
        bulk = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        self.ep_in = next(e for e in bulk if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.ep_out = next(e for e in bulk if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.detached = False
        try:
            if device.is_kernel_driver_active(self.intf):
                device.detach_kernel_driver(self.intf)
                self.detached = True
            usb.util.claim_interface(device, self.intf)
        except usb.core.USBError as e:
            if self.detached:                  # don't leave it half taken: give it back to macOS
                try:
                    device.attach_kernel_driver(self.intf)
                except usb.core.USBError:
                    pass
            usb.util.dispose_resources(device)
            if e.errno == 13 or "Access denied" in str(e):
                raise JLBootError("macOS lets only the administrator take the update-mode disk from its "
                                  "disk driver: run this with sudo") from None
            raise JLBootError(f"could not open the download-mode device: {e}") from e

    def execute(self, cdb: bytes, data_out: bytes | None = None, data_in_len: int = 0) -> bytes:
        usb = self.usb
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        length = len(data_out) if data_out else data_in_len
        flags = 0x80 if data_in_len and not data_out else 0x00
        what = f"SCSI command {cdb[:2].hex()}"
        try:
            self.ep_out.write(CBW.pack(b"USBC", self.tag, length, flags, 0, len(cdb)) + cdb.ljust(16, b"\0"),
                              self.timeout_ms)
            data = b""
            try:
                if data_out:
                    self.ep_out.write(data_out, self.timeout_ms)
                elif data_in_len:
                    data = bytes(self.ep_in.read(data_in_len, self.timeout_ms))
            except usb.core.USBError as e:
                if e.errno != 32:              # a stall ends the data phase; the status still follows
                    raise
                self.dev.clear_halt(self.ep_in if flags else self.ep_out)
            status = bytes(self.ep_in.read(CSW.size, self.timeout_ms))
        except usb.core.USBError as e:
            raise JLBootError(f"{what} failed: {e}") from e
        if len(status) != CSW.size:
            raise JLBootError(f"{what} failed: a {len(status)}-byte status")
        signature, tag, _residue, result = CSW.unpack(status)
        if signature != b"USBS" or tag != self.tag:
            raise JLBootError(f"{what} failed: the status doesn't belong to it ({status.hex()})")
        if result != 0:
            raise JLBootError(f"{what} failed (status {result})")
        # The residue is not usable, as over SG_IO: read_twice and the device CRC catch bad reads.
        return data[:data_in_len] if data_in_len else b""

    def close(self) -> None:
        usb = self.usb
        try:
            usb.util.release_interface(self.dev, self.intf)
            if self.detached:
                self.dev.attach_kernel_driver(self.intf)
        except usb.core.USBError:
            pass                               # gone already: it restarted, or was unplugged
        usb.util.dispose_resources(self.dev)


def _registry_devices() -> list[tuple[str, int]]:
    """JieLi USB devices from macOS's device registry: (usb:<bus>:<address>, product ID).

    Not libusb: on macOS every libusb lookup opens each device and keeps it open, and while the
    settings app held the download-mode disk like that, its administrator helper couldn't take
    it ("Other error", 2026-10-25). The registry opens nothing. Bus and address are worked out
    the way libusb does (bus = locationID >> 24), so the paths match _usb_path()."""
    try:
        out = subprocess.run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-d", "1"],
                             capture_output=True, check=False, timeout=10).stdout
        devices = plistlib.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return []
    return [(f"usb:{d.get('locationID', 0) >> 24}:{d.get('USB Address', 0)}", d.get("idProduct", 0))
            for d in devices if d.get("idVendor") == JIELI_VID]


def _usb_devices():
    libusb_package, _ = _usb()
    return list(libusb_package.find(find_all=True, idVendor=JIELI_VID))


def _usb_path(device) -> str:
    return f"usb:{device.bus}:{device.address}"


def open_device(path: str):
    """SgDevice for /dev/sgN; on macOS, UsbBotDevice for a usb:<bus>:<address> path."""
    if path.startswith("usb:"):
        device = next((d for d in _usb_devices() if _usb_path(d) == path), None)
        if device is None:
            raise JLBootError(f"{path} is no longer connected")
        return UsbBotDevice(device)
    return SgDevice(path)


# -- finding the device -------------------------------------------------------------------

@dataclass
class UbootDevice:
    path: str           # /dev/sgN, or usb:<bus>:<address> on macOS
    vendor: str         # chip family, e.g. "BR23"
    product: str        # "UBOOT1.00"

    @property
    def chip(self) -> str:
        return self.vendor.strip().lower()


def disks(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[UbootDevice]:
    """Every SCSI generic node with its INQUIRY strings, as the kernel cached them (no I/O).
    On macOS: every JieLi USB device, from its USB IDs (also no I/O, and no root needed)."""
    if MACOS:
        return [UbootDevice(path, UBOOT_PIDS.get(pid, f"{pid:04x}"), "UBOOT1.00" if pid in UBOOT_PIDS else "unknown")
                for path, pid in _registry_devices()]
    out = []
    for node in sorted((sys_root / "class" / "scsi_generic").glob("sg*")):
        try:
            vendor = (node / "device" / "vendor").read_text().strip()
            model = (node / "device" / "model").read_text().strip()
        except OSError:
            continue
        out.append(UbootDevice(str(dev_root / node.name), vendor, model))
    return out


def find(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[UbootDevice]:
    """Every JieLi chip currently in UBOOT mode, from sysfs (no I/O to the device)."""
    return [d for d in disks(sys_root, dev_root) if d.product.upper().startswith("UBOOT")]


# -- the protocol ---------------------------------------------------------------------------

def _cdb(cmd: int, args: bytes = b"") -> bytes:
    if cmd not in ALLOWED:
        raise JLBootError(f"command {cmd:#06x} is not allowed: this tool never erases or writes flash")
    return (cmd.to_bytes(2, "big") + args).ljust(16, b"\xff")


class JLBoot:
    """The read-only command set, over anything with execute(cdb, data_out, data_in_len)."""

    def __init__(self, execute: Callable[[bytes, bytes | None, int], bytes]):
        self.execute = execute
        self.io_size = 512

    def _ask(self, cmd: int, args: bytes = b"") -> bytes:
        resp = self.execute(_cdb(cmd, args), None, 16)
        if int.from_bytes(resp[:2], "big") != cmd:
            raise JLBootError(f"reply {resp[:2].hex()} does not answer {cmd:#06x}")
        return resp[2:]

    def inquiry(self) -> tuple[str, str, str]:
        data = self.execute(bytes([0x12, 0, 0, 0, 36, 0]), None, 36)
        return tuple(data[a:b].decode("ascii", "replace").strip() for a, b in ((8, 16), (16, 32), (32, 36)))

    def read_memory(self, addr: int, size: int) -> bytes:
        return self.execute(_cdb(READ_MEMORY, addr.to_bytes(4, "big") + size.to_bytes(2, "big")), None, size)

    # ROM stage
    def loader_running(self, loader: Loader, blob: bytes) -> bool:
        """Is our loader already running? It answers memory reads in plain, where the ROM
        MengLi-encrypts them, so the first bytes of the loader read back decrypted only from it.
        Checked before an upload, so the loader never gets written over itself."""
        head = blob[:64]
        try:
            return self.read_memory(loader.address, len(head)) == jlcrypt.mengli(head)
        except JLBootError:
            return False

    def upload_loader(self, loader: Loader, blob: bytes) -> None:
        if loader.address + len(blob) > loader.ram_end:
            raise JLBootError("loader does not fit in RAM")
        for off in range(0, len(blob), loader.block):
            block = blob[off:off + loader.block]
            addr = loader.address + off
            # The BR23 ROM decrypts MengLi data in place; the loader file is stored encrypted.
            self.execute(_cdb(WRITE_MEMORY, addr.to_bytes(4, "big") + len(block).to_bytes(2, "big")
                              + b"\x00" + jlcrypt.crc16(block).to_bytes(2, "little")), block, 0)
        self._ask(JUMP, loader.address.to_bytes(4, "big") + LOADER_ARG.to_bytes(2, "big"))

    # loader stage
    def buffer_size(self) -> int:
        size = int.from_bytes(self._ask(BUFFER_SIZE)[:4], "big")
        if 256 <= size <= 0x8000:          # the read command's size field is 16 bits
            self.io_size = size
        return size

    def chip_key(self) -> int:
        resp = self._ask(READ_KEY, (0xAC6900).to_bytes(4, "big"))   # also unlocks flash reads
        return int.from_bytes(jlcrypt.mengli(resp[4:6][::-1]), "little")

    def online_device(self) -> tuple[int, int]:
        """(device type, JEDEC ID). Type 3 is SPI NOR flash on SPI0."""
        resp = self._ask(ONLINE_DEVICE)
        return resp[0], int.from_bytes(resp[2:6], "little")

    def flash_read(self, addr: int, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            n = min(self.io_size, size - len(out))
            pos = addr + len(out)
            chunk = self.execute(_cdb(READ_FLASH, pos.to_bytes(4, "big") + n.to_bytes(2, "big")), None, n)
            if not chunk:
                raise JLBootError(f"no data read at {pos:#x}")     # never loop forever on a bad device
            out += chunk
        return bytes(out)

    def flash_crc16(self, addr: int, size: int) -> int:
        """CRC16 the loader computes over raw flash (FC13)."""
        return int.from_bytes(self._ask(FLASH_CRC16_RAW, addr.to_bytes(4, "big") + size.to_bytes(2, "big"))[:2], "big")

    def run_app(self) -> None:
        """Leave download mode and boot the firmware in flash."""
        self.execute(_cdb(RUN_APP, (1).to_bytes(4, "big")), None, 16)


def flash_size(jedec_id: int) -> int:
    """Capacity from a SPI NOR JEDEC ID: its capacity byte is log2(size in bytes)."""
    for cap in (jedec_id >> 16 & 0xFF, jedec_id & 0xFF):
        if 0x12 <= cap <= 0x18:          # 256 KB .. 16 MB
            return 1 << cap
    raise JLBootError(f"can't tell the flash size from JEDEC ID {jedec_id:#08x}")


def fetch_loader(loader: Loader, cache_dir: Path) -> bytes:
    path = cache_dir / Path(loader.url).name
    if path.exists():
        blob = path.read_bytes()
    else:
        try:
            with urllib.request.urlopen(loader.url, timeout=30) as r:
                blob = r.read()
        except OSError as e:
            raise JLBootError(f"could not download the {loader.chip} loader from {loader.url}: {e}") from e
    if hashlib.sha256(blob).hexdigest() != loader.sha256:
        raise JLBootError(f"the {loader.chip} loader does not match its pinned SHA-256; not using it")
    paths.make_dirs(cache_dir)
    path.write_bytes(blob)
    paths.give_back(path)
    return blob


# -- a session: identify, then dump -------------------------------------------------------------

@dataclass
class Identity:
    vendor: str             # INQUIRY, e.g. "BR23"
    product: str            # "UBOOT1.00"
    revision: str
    chip_key: int
    flash_type: int         # SPI0_NOR expected
    jedec_id: int
    flash_size: int | None  # None when the JEDEC ID doesn't say
    buffer_size: int


def ensure_loader(boot: JLBoot, cache_dir: Path) -> tuple[str, str, str]:
    """Get the flash loader running in RAM (unless it already is); returns the INQUIRY strings.

    Refuses anything that is not a known chip in UBOOT1.00 mode, so the loader only ever goes
    to the chip it was built for. The loader is gone after a restart; flash is not touched."""
    vendor, product, revision = boot.inquiry()
    if product != "UBOOT1.00":
        raise JLBootError(f"{vendor} {product} is not in JieLi's UBOOT1.00 download mode; not touching it")
    loader = LOADERS.get(vendor.lower())
    if loader is None:
        raise JLBootError(f"no loader for chip {vendor!r} (known: {', '.join(LOADERS)}); not touching it")
    blob = fetch_loader(loader, cache_dir)
    if not boot.loader_running(loader, blob):
        boot.upload_loader(loader, blob)
    return vendor, product, revision


def start(boot: JLBoot, cache_dir: Path) -> Identity:
    """Identify the chip and its flash, with the loader running. Read-only."""
    vendor, product, revision = ensure_loader(boot, cache_dir)
    buffer = boot.buffer_size()
    key = boot.chip_key()
    kind, jedec = boot.online_device()
    if kind != SPI0_NOR:
        raise JLBootError(f"the loader reports storage type {kind:#04x}, not SPI NOR flash; stopping")
    try:
        size = flash_size(jedec)
    except JLBootError:
        size = None
    return Identity(vendor, product, revision, key, kind, jedec, size, buffer)


class Mismatch(JLBootError):
    def __init__(self, sectors: list[int]):
        self.sectors = sectors
        shown = ", ".join(f"{a:#x}" for a in sectors[:8]) + (" ..." if len(sectors) > 8 else "")
        super().__init__(f"the two reads differ in {len(sectors)} sector(s) at {shown}")


def read_twice(boot: JLBoot, size: int, progress: Callable[[int, int], None] | None = None) -> bytes:
    """Read the whole flash twice; return it only if both reads are identical."""
    reads = []
    for i in range(2):
        data = bytearray()
        for addr in range(0, size, SECTOR):
            data += boot.flash_read(addr, min(SECTOR, size - addr))
            if progress:
                progress(i * size + len(data), 2 * size)
        reads.append(bytes(data))
    first, second = reads
    if first != second:
        raise Mismatch([a for a in range(0, size, SECTOR) if first[a:a + SECTOR] != second[a:a + SECTOR]])
    return first


def device_crc_mismatches(boot: JLBoot, data: bytes) -> list[int]:
    """Sectors where the loader's own CRC16 of the flash disagrees with CRC-16/XMODEM of the dump.
    Every sector disagreeing most likely means a different CRC variant, not a bad dump."""
    return [a for a in range(0, len(data), SECTOR)
            if boot.flash_crc16(a, len(data[a:a + SECTOR])) != jlcrypt.crc16(data[a:a + SECTOR])]
