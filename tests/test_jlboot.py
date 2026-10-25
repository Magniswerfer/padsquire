# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import json
import random

import pytest

from padsquire import cli, cli_uboot, jlboot, jlcrypt

FLASH_SIZE = 256 * 1024
JEDEC = 0x1240C8            # as online_device() decodes it: capacity 0x12 = 256 KB
CHIP_KEY = 0x1234
LOADER_BLOB = random.Random(1).randbytes(1300)        # 3 blocks, last one short
LOADER = jlboot.Loader("br23", 0x12000, "https://example.invalid/br23loader.bin",
                       hashlib.sha256(LOADER_BLOB).hexdigest(), ram_end=0x2C000)
FORBIDDEN = [0xFB00, 0xFB01, 0xFB02, 0xFB42, 0xFB04, 0xFC12, 0xFC0D, 0xFC00]


class ScsiFail(jlboot.JLBootError):
    pass


class FakeUboot:
    """A BR23 in UBOOT1.00 mode, as SgDevice.execute sees it.

    The ROM knows only memory write, read and jump: it decrypts MengLi uploads in place and
    encrypts what it reads out. Once the uploaded loader is jumped to, the loader commands work
    and memory reads come back plain."""

    def __init__(self, flash: bytes, *, product="UBOOT1.00", vendor="BR23", crc=jlcrypt.crc16,
                 flaky_at: int | None = None, buffer_size=512, flash_type=jlboot.SPI0_NOR,
                 jedec=JEDEC, fail_after: int | None = None, corrupt_write_at: int | None = None):
        self.flash, self.product, self.vendor, self.crc = bytearray(flash), product, vendor, crc
        self.jedec, self.fail_after, self.corrupt_write_at = jedec, fail_after, corrupt_write_at
        self.writes: list[tuple[str, int, int]] = []      # ("erase" | "write", address, length)
        self.flaky_at, self.buffer_size, self.flash_type = flaky_at, buffer_size, flash_type
        self.ram = bytearray(0x2C000)
        self.loader = False
        self.cdbs: list[bytes] = []
        self.reads_at: dict[int, int] = {}
        self.restarted = False

    def _reply(self, cdb: bytes, body: bytes = b"") -> bytes:
        return (cdb[:2] + body).ljust(16, b"\0")

    def execute(self, cdb: bytes, data_out: bytes | None, data_in_len: int) -> bytes:
        self.cdbs.append(cdb)
        if cdb[0] == 0x12:
            return (bytes(8) + self.vendor.ljust(8).encode() + self.product.ljust(16).encode()
                    + b"1.00")[:data_in_len]
        cmd = int.from_bytes(cdb[:2], "big")
        addr, size = int.from_bytes(cdb[2:6], "big"), int.from_bytes(cdb[6:8], "big")
        assert cdb[len(cdb.rstrip(b"\xff")):] == b"\xff" * (16 - len(cdb.rstrip(b"\xff")))
        if cmd == jlboot.WRITE_MEMORY and not self.loader:
            assert len(data_out) == size and int.from_bytes(cdb[9:11], "little") == jlcrypt.crc16(data_out)
            self.ram[addr:addr + size] = jlcrypt.mengli(data_out)
            return b""
        if cmd == jlboot.READ_MEMORY:
            data = bytes(self.ram[addr:addr + size])
            return data if self.loader else jlcrypt.mengli(data)
        if cmd == jlboot.JUMP:
            assert (addr, size) == (LOADER.address, jlboot.LOADER_ARG)
            assert self.ram[addr:addr + len(LOADER_BLOB)] == _mengli_blocks(LOADER_BLOB)
            self.loader = True
            return self._reply(cdb)
        if not self.loader:
            raise ScsiFail(f"ROM: unknown command {cmd:#06x}")
        if cmd == jlboot.BUFFER_SIZE:
            return self._reply(cdb, self.buffer_size.to_bytes(4, "big"))
        if cmd == jlboot.READ_KEY:
            assert addr == 0xAC6900
            return self._reply(cdb, bytes(4) + jlcrypt.mengli(CHIP_KEY.to_bytes(2, "little"))[::-1])
        if cmd == jlboot.ONLINE_DEVICE:
            return self._reply(cdb, bytes([self.flash_type, 0]) + self.jedec.to_bytes(4, "little"))
        if cmd == jlboot.READ_FLASH:
            assert size <= self.buffer_size and data_in_len == size
            data = bytearray(self.flash[addr:addr + size])
            self.reads_at[addr] = self.reads_at.get(addr, 0) + 1
            if self.flaky_at is not None and addr <= self.flaky_at < addr + size and self.reads_at[addr] == 2:
                data[self.flaky_at - addr] ^= 0x01
            return bytes(data)
        if cmd == jlboot.FLASH_CRC16_RAW:
            return self._reply(cdb, self.crc(bytes(self.flash[addr:addr + size])).to_bytes(2, "big"))
        if cmd in (0xFB01, 0xFB04):
            if self.fail_after is not None and len(self.writes) >= self.fail_after:
                raise ScsiFail("USB went away")
            if cmd == 0xFB01:
                assert addr % 0x1000 == 0 and data_out is None
                self.writes.append(("erase", addr, 0x1000))
                self.flash[addr:addr + 0x1000] = b"\xff" * 0x1000
                return self._reply(cdb)
            assert len(data_out) == size and int.from_bytes(cdb[9:11], "little") == jlcrypt.crc16(data_out)
            self.writes.append(("write", addr, size))
            data = bytearray(data_out)
            if self.corrupt_write_at is not None and addr <= self.corrupt_write_at < addr + size:
                data[self.corrupt_write_at - addr] ^= 0x10
                self.corrupt_write_at = None                  # once
            for i, b in enumerate(data):                       # NOR: a write only clears bits
                self.flash[addr + i] &= b
            return b""
        if cmd == jlboot.RUN_APP:
            self.restarted, self.loader = True, False
            return self._reply(cdb)
        raise AssertionError(f"unexpected command {cmd:#06x}")

    def commands(self) -> set[int]:
        return {0x12 if c[0] == 0x12 else int.from_bytes(c[:2], "big") for c in self.cdbs}


def _mengli_blocks(blob: bytes, block: int = 512) -> bytes:
    """What RAM holds after the ROM has decrypted each uploaded block in place."""
    return b"".join(jlcrypt.mengli(blob[i:i + block]) for i in range(0, len(blob), block))


@pytest.fixture
def flash():
    return random.Random(2).randbytes(FLASH_SIZE)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setitem(jlboot.LOADERS, "br23", LOADER)
    d = tmp_path / "cache"
    d.mkdir()
    (d / "br23loader.bin").write_bytes(LOADER_BLOB)
    return d


# -- the allowlist ----------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", FORBIDDEN)
def test_erase_write_and_key_burn_never_leave_the_host(cmd):
    with pytest.raises(jlboot.JLBootError, match="not allowed"):
        jlboot._cdb(cmd, bytes(6))


def test_allowlist_is_read_only():
    assert jlboot.ALLOWED.isdisjoint(FORBIDDEN)
    assert jlboot.WRITE_MEMORY in jlboot.ALLOWED          # RAM only, for the loader
    assert jlboot._cdb(jlboot.READ_FLASH, bytes.fromhex("000010000200")) == \
        bytes.fromhex("fd05000010000200") + b"\xff" * 8


# -- identify ---------------------------------------------------------------------------------

def test_start_uploads_loader_and_identifies(flash, cache):
    dev = FakeUboot(flash)
    boot = jlboot.JLBoot(dev.execute)
    ident = jlboot.start(boot, cache)
    assert (ident.vendor, ident.product, ident.chip_key) == ("BR23", "UBOOT1.00", CHIP_KEY)
    assert (ident.jedec_id, ident.flash_size, ident.buffer_size) == (JEDEC, FLASH_SIZE, 512)
    uploads = [c for c in dev.cdbs if c[:2] == b"\xfb\x06"]
    assert [int.from_bytes(c[2:6], "big") for c in uploads] == [0x12000, 0x12200, 0x12400]
    assert [int.from_bytes(c[6:8], "big") for c in uploads] == [512, 512, 1300 - 1024]
    assert dev.commands() <= jlboot.ALLOWED | {0x12}


def test_start_does_not_upload_over_a_running_loader(flash, cache):
    dev = FakeUboot(flash)
    jlboot.start(jlboot.JLBoot(dev.execute), cache)
    dev.cdbs.clear()
    jlboot.start(jlboot.JLBoot(dev.execute), cache)       # a second command, same session
    assert not [c for c in dev.cdbs if c[:2] in (b"\xfb\x06", b"\xfb\x08")]


@pytest.mark.parametrize("vendor,product,match", [
    ("BR23", "UDISK", "not in JieLi's UBOOT1.00"),
    ("BR99", "UBOOT1.00", "no loader for chip"),
])
def test_start_refuses_unknown_devices_after_inquiry(flash, cache, vendor, product, match):
    dev = FakeUboot(flash, vendor=vendor, product=product)
    with pytest.raises(jlboot.JLBootError, match=match):
        jlboot.start(jlboot.JLBoot(dev.execute), cache)
    assert dev.commands() == {0x12}


def test_start_stops_on_other_storage(flash, cache):
    with pytest.raises(jlboot.JLBootError, match="not SPI NOR"):
        jlboot.start(jlboot.JLBoot(FakeUboot(flash, flash_type=0x04).execute), cache)


def test_loader_must_match_its_pinned_hash(cache):
    (cache / "br23loader.bin").write_bytes(LOADER_BLOB + b"x")
    with pytest.raises(jlboot.JLBootError, match="SHA-256"):
        jlboot.fetch_loader(LOADER, cache)


def test_buffer_size_must_fit_the_size_field(flash, cache):
    boot = jlboot.JLBoot(FakeUboot(flash, buffer_size=0x10000).execute)
    jlboot.start(boot, cache)
    assert boot.io_size == 512


@pytest.mark.parametrize("jedec,size", [(0x1440EF, 1 << 20), (0xEF4014, 1 << 20), (0x1640C8, 4 << 20)])
def test_flash_size_from_jedec_id(jedec, size):
    assert jlboot.flash_size(jedec) == size


def test_flash_size_unknown():
    with pytest.raises(jlboot.JLBootError):
        jlboot.flash_size(0x0000FF)


# -- dump -------------------------------------------------------------------------------------

def test_read_twice_returns_identical_reads(flash, cache):
    dev = FakeUboot(flash, buffer_size=1024)
    boot = jlboot.JLBoot(dev.execute)
    jlboot.start(boot, cache)
    seen = []
    assert jlboot.read_twice(boot, FLASH_SIZE, lambda d, t: seen.append((d, t))) == flash
    assert set(dev.reads_at.values()) == {2} and len(dev.reads_at) == FLASH_SIZE // 1024
    assert seen[-1] == (2 * FLASH_SIZE, 2 * FLASH_SIZE)
    assert jlboot.device_crc_mismatches(boot, flash) == []


def test_read_twice_names_differing_sectors(flash, cache):
    dev = FakeUboot(flash, flaky_at=0x5123)
    boot = jlboot.JLBoot(dev.execute)
    jlboot.start(boot, cache)
    with pytest.raises(jlboot.Mismatch) as e:
        jlboot.read_twice(boot, FLASH_SIZE)
    assert e.value.sectors == [0x5000]


def test_device_crc_catches_what_two_reads_cannot(flash, cache):
    bad = bytearray(flash)
    bad[0x3000] ^= 0xFF                      # the same wrong byte, read twice
    boot = jlboot.JLBoot(FakeUboot(flash).execute)
    jlboot.start(boot, cache)
    assert jlboot.device_crc_mismatches(boot, bytes(bad)) == [0x3000]


# -- the CLI ----------------------------------------------------------------------------------

@pytest.fixture
def cli_env(flash, cache, tmp_path, monkeypatch):
    dev = FakeUboot(flash)
    monkeypatch.setattr(cli_uboot, "LOADER_CACHE", cache)
    monkeypatch.setattr(cli_uboot, "_open", lambda args: (type("D", (), {"close": lambda s: None})(),
                                                         jlboot.JLBoot(dev.execute)))
    return dev


def test_cli_backup_saves_a_verified_dump(cli_env, flash, tmp_path, capsys):
    out = tmp_path / "backups"
    assert cli.main(["uboot", "backup", "-o", str(out)]) == 0
    (image,) = out.glob("*.bin")
    (manifest,) = out.glob("*.json")
    assert image.read_bytes() == flash
    assert image.stat().st_mode & 0o777 == 0o600
    m = json.loads(manifest.read_text())
    assert m["sha256"] == hashlib.sha256(flash).hexdigest() and m["identical"] is True
    assert m["chip"]["chip_key"] == "0x1234" and m["device_crc"]["mismatched"] == 0
    assert "both reads are identical" in capsys.readouterr().out
    assert cli_env.commands() <= jlboot.ALLOWED | {0x12} and not cli_env.restarted


def test_cli_backup_saves_nothing_when_reads_differ(cli_env, tmp_path, capsys):
    cli_env.flaky_at = 0x100
    out = tmp_path / "backups"
    assert cli.main(["uboot", "backup", "-o", str(out)]) == 1
    assert "differ in 1 sector" in capsys.readouterr().err
    assert not out.exists() or not any(out.iterdir())


def test_cli_backup_tolerates_another_crc_variant(cli_env, tmp_path, capsys):
    cli_env.crc = lambda data: jlcrypt.crc16(data, 0xFFFF)
    assert cli.main(["uboot", "backup", "-o", str(tmp_path)]) == 0
    assert "differs in every sector" in capsys.readouterr().out


def test_cli_backup_refuses_a_path_inside_the_repo(cli_env, capsys):
    assert cli.main(["uboot", "backup", "-o", str(cli_uboot.REPO_ROOT / "backups")]) == 1
    assert "inside the PadSquire repo" in capsys.readouterr().err
    assert cli_env.cdbs == []


def test_cli_backup_partial_size(cli_env, flash, tmp_path):
    assert cli.main(["uboot", "backup", "-o", str(tmp_path), "--size", "0x2000"]) == 0
    assert next(tmp_path.glob("*.bin")).read_bytes() == flash[:0x2000]
    with pytest.raises(SystemExit):
        cli.main(["uboot", "backup", "--size", "1000"])


def test_cli_enter_needs_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(cli_uboot.hid, "enter_update_mode", lambda p: pytest.fail("sent without --confirm"))
    assert cli.main(["uboot", "enter"]) == 2
    assert "--confirm" in capsys.readouterr().err


def test_cli_restart(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(cli_uboot.jlboot, "find", lambda: [] if cli_env.restarted else ["sg2"])
    monkeypatch.setattr(cli_uboot.hid, "find", lambda: [cli_uboot.hid.HidDevice("/dev/hidraw11", 0x100F, "")]
                        if cli_env.restarted else [])
    assert cli.main(["uboot", "restart", "--wait", "1"]) == 0
    assert cli_env.restarted and "back in normal mode: /dev/hidraw11 (cable)" in capsys.readouterr().out
