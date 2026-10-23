import hashlib
import json
import os
import struct
from pathlib import Path

import random

import pytest

from conftest import ISD_TEMPLATE
from gamesir_linux import cli, jlcrypt, jlflash, safety, ufw

KEY = 0x3496
# The raw flash header of a Nova 2 Lite: PID and VID read in plain text, the CRC covers the
# descrambled bytes 2..31.
HEADER = bytes.fromhex("bb1ebf1d4e324c54f031a467ccadff684143363935585f5452414e53ffffffff")
ALL_FF = b"\xff" * jlflash.ENTRY


def _entry(name: str, typ: int, offset: int, length: int, crc: int = 0xFFFF, index: int = 0) -> bytes:
    return struct.pack("<BBHIII", typ, 0, crc, offset, length, index) + name.encode().ljust(16, b"\0")


def make_image(code_start=0x1000, code_end=0x64000, vm=(0x64000, 0x14000), key=KEY,
               length=None, app_len=0x5F000) -> bytes:
    """A firmware image shaped like flash2.bin (code_start 0x1000) or flash.bin (0)."""
    files = [_entry("uboot.boot", 0x03, 0xA0, 0x100), _entry("isd_config.ini", 0x5E, 0x2088, 0x89),
             _entry("app_dir_head", 0x17, 0x3000, 0xFFFFFFFF), _entry("key_mac", 0x70, 0x7F000, 0x1000)]
    areas = [_entry("app.bin", 0x60, 0x120, app_len), _entry("VM", 0xE6, *vm),
             _entry("PRCT", 0x73, code_start, code_end - code_start),
             _entry("BTIF", 0x40, 0x79000, 0x1000), _entry("EXIF", 0xEB, 0x7A000, 0x1000),
             _entry("USERIF", 0xA6, 0x7B000, 0x4000), ALL_FF]
    img = bytearray(b"\xff" * (length or code_end - code_start))
    img[0x3400:len(img) - 0x100] = random.Random(app_len).randbytes(len(img) - 0x3500)   # "code"
    img[0:32] = HEADER
    for i, e in enumerate(files):
        img[32 * (i + 1):32 * (i + 2)] = jlcrypt.enc(e, 0xFFFF)
    for i, e in enumerate(areas):
        img[0x3000 + 32 * i:0x3000 + 32 * (i + 1)] = jlcrypt.enc(e, key ^ ((32 * i) >> 2))
    return bytes(img)


def make_dump(image: bytes, base=0x1000) -> bytes:
    flash = bytearray(b"\xff" * 0x100000)
    flash[base:base + len(image)] = image
    flash[0x79000:0x79010] = b"\x22\x66\x60\x00\x8d\x38\xf1\x25\xdf\xd9" + bytes(6)   # BTIF
    flash[0x7B000:0x7B010] = bytes(range(16))                                         # USERIF
    return bytes(flash)


@pytest.fixture
def device():
    return jlflash.parse(make_dump(make_image(key=KEY, length=0x63000 - 0x100)), 0x1000, KEY)


def test_parse_reads_file_table_and_app_directory(device):
    assert [e.name for e in device.files] == ["uboot.boot", "isd_config.ini", "app_dir_head", "key_mac"]
    assert device.app == 0x4000 and device.code == (0x1000, 0x64000)
    assert device.area("VM") == (0x64000, 0x78000) and device.area("USERIF") == (0x7B000, 0x7F000)


def test_parse_needs_the_chip_key():
    with pytest.raises(jlflash.FlashError, match="chip key 0x1234"):
        jlflash.parse(make_dump(make_image()), 0x1000, 0x1234)


def test_parse_needs_a_valid_header():
    dump = bytearray(make_dump(make_image()))
    dump[0x1005] ^= 1
    with pytest.raises(jlflash.FlashError, match="no valid flash header"):
        jlflash.parse(bytes(dump), 0x1000, KEY)


def test_update_with_matching_image_is_allowed(device):
    plan = jlflash.check_update(device, make_image(), KEY)
    assert plan.ok, plan.problems
    assert (plan.start, plan.end) == (0x1000, 0x64000)


@pytest.mark.parametrize("image,match", [
    (dict(code_start=0, code_end=0x63000, vm=(0x63000, 0x16000)), "built for the other layout"),
    (dict(vm=(0x65000, 0x13000)), "VM moves"),
    (dict(length=0x64000), "past the end of the code area"),
    (dict(key=0x1234), "doesn't fit this controller"),
])
def test_update_is_refused(device, image, match):
    plan = jlflash.check_update(device, make_image(**image), KEY)
    assert not plan.ok and any(match in p for p in plan.problems), plan.problems


def test_an_image_overlapping_a_reserved_area_is_refused():
    # A controller whose VM starts inside where the image would be written.
    dev = jlflash.parse(make_dump(make_image(vm=(0x60000, 0x18000))), 0x1000, KEY)
    plan = jlflash.check_update(dev, make_image(vm=(0x60000, 0x18000)), KEY)
    assert any("would overwrite VM" in p for p in plan.problems)


# -- safety: choosing the image from a .ufw, against a backup ---------------------------------

def _ufw(*images: tuple[str, int, bytes]) -> bytes:
    isd = ISD_TEMPLATE.format(chip="AC695X", pid="AC695X_TRANS", vid="N2LT").encode()
    return ufw.build("AC695X", [("isd_config.ini", 0x34, isd), *images, ("tail.bin", 0xFF, bytes(64))],
                     scramble={"isd_config.ini"})


def _backup(tmp_path: Path, dump: bytes, key=KEY, identical=True) -> Path:
    path = tmp_path / "nova2lite-br23-flash-test.bin"
    path.write_bytes(dump)
    path.with_suffix(".json").write_text(json.dumps({
        "sha256": hashlib.sha256(dump).hexdigest(), "identical": identical,
        "chip": {"chip_key": f"{key:#06x}"}}))
    return path


@pytest.fixture
def both_layouts(tmp_path):
    fw = tmp_path / "fw.ufw"
    fw.write_bytes(_ufw(("flash.bin", 0x00, make_image(code_start=0, code_end=0x63000, vm=(0x63000, 0x16000))),
                        ("flash2.bin", 0x20, make_image())))
    return fw


def test_choose_flash_image_picks_the_one_for_this_layout(tmp_path, both_layouts):
    choice = safety.choose_flash_image(both_layouts, _backup(tmp_path, make_dump(make_image())))
    assert (choice.partition, choice.start, choice.end, choice.errors) == ("flash2.bin", 0x1000, 0x64000, [])


def test_choose_flash_image_refuses_a_tampered_backup(tmp_path, both_layouts):
    backup = _backup(tmp_path, make_dump(make_image()))
    backup.write_bytes(backup.read_bytes()[:-1] + b"\x00")
    assert "does not match its manifest" in safety.choose_flash_image(both_layouts, backup).errors[0]


def test_choose_flash_image_refuses_when_nothing_fits(tmp_path, both_layouts):
    dump = make_dump(make_image(vm=(0x65000, 0x13000)))
    choice = safety.choose_flash_image(both_layouts, _backup(tmp_path, dump))
    assert choice.partition is None and "no image" in choice.errors[0]


def test_choose_flash_image_refuses_to_guess(tmp_path):
    fw = tmp_path / "fw.ufw"
    fw.write_bytes(_ufw(("flash.bin", 0x00, make_image()), ("flash2.bin", 0x20, make_image())))
    choice = safety.choose_flash_image(fw, _backup(tmp_path, make_dump(make_image())))
    assert choice.partition is None and "refusing to guess" in choice.errors[0]


def test_plan_requires_a_backup(fw_dir, capsys):
    argv = ["plan", "--firmware-dir", str(fw_dir), "--target", "controller", "--pid", "AC695X_TRANS",
            "--vid", "N2LT", "--version", "8101", "--dongle-version", "2800"]
    assert cli.main(argv) == 2
    assert "no flash backup of this controller" in capsys.readouterr().out


# -- the real thing, when available --------------------------------------------------------------

@pytest.mark.skipif(not (os.environ.get("GAMESIR_BACKUP") and os.environ.get("GAMESIR_UFW")),
                    reason="set GAMESIR_BACKUP (a uboot backup .bin) and GAMESIR_UFW (a Nova 2 Lite .ufw)")
def test_real_backup_and_firmware():
    choice = safety.choose_flash_image(os.environ["GAMESIR_UFW"], os.environ["GAMESIR_BACKUP"])
    assert choice.errors == [] and choice.partition == "flash2.bin"
    assert (choice.start, choice.end) == (0x1000, 0x64000)


# -- the 2.4 GHz receiver: header at 0, no PRCT entry, 256 KB ---------------------------------------

def make_receiver_image(vm=(0x2A000, 0x11000), key=KEY, placement=0xFFFF, seed=1) -> bytes:
    """Shaped like the receiver's flash.bin: laid out from 0, the firmware area ends at VM."""
    files = [_entry("uboot.boot", 0x19, 0xA0, 0x12DF), _entry("isd_config.ini", 0xE0, 0x137F, 0x90),
             _entry("app_dir_head", 0x01, 0x2000, 0xFFFFFFFF, crc=placement),
             _entry("key_mac", 0xB6, 0x3F000, 0x1000)]
    areas = [_entry("app.bin", 0x52, 0x120, 0x25D60), _entry("VM", 0x6E, *vm),
             _entry("BTIF", 0x04, 0x3B000, 0x1000), _entry("EXIF", 0x8A, 0x3C000, 0x1000),
             _entry("USERIF", 0x6E, 0x3D000, 0x2000), ALL_FF]
    img = bytearray(b"\xff" * vm[0])
    img[0x100:0x1400] = random.Random(seed + 100).randbytes(0x1300)     # uboot.boot, isd_config.ini
    img[0x2400:len(img) - 0x100] = random.Random(seed).randbytes(len(img) - 0x2500)
    img[0:32] = HEADER
    for i, e in enumerate(files):
        img[32 * (i + 1):32 * (i + 2)] = jlcrypt.enc(e, 0xFFFF)
    for i, e in enumerate(areas):
        img[0x2000 + 32 * i:0x2000 + 32 * (i + 1)] = jlcrypt.enc(e, key ^ ((32 * i) >> 2))
    return bytes(img)


def make_receiver_dump(image: bytes) -> bytes:
    flash = bytearray(b"\xff" * 0x40000)
    flash[:len(image)] = image
    flash[0x2A000:0x2A010] = b"\x55\xaa\xaa\x55" + bytes(12)          # VM
    flash[0x3B000:0x3B010] = bytes(range(16))                         # BTIF
    return bytes(flash)


def test_receiver_layout_and_update_check():
    device = jlflash.find(make_receiver_dump(make_receiver_image()), KEY)
    assert device.base == 0 and device.code == (0, 0x2A000)
    assert device.area("BTIF") == (0x3B000, 0x3C000)
    plan = jlflash.check_update(device, make_receiver_image(seed=2), KEY)
    assert plan.ok and (plan.start, plan.end) == (0, 0x2A000), plan.problems
    # An image meant for 0x1000 is refused, as is the 256-byte layout.
    assert not jlflash.check_update(device, make_receiver_image(placement=0x00FF), KEY).ok
    odd = jlflash.find(make_receiver_dump(make_receiver_image(vm=(0x28700, 0x13800))), KEY)
    problems = jlflash.check_update(odd, make_receiver_image(vm=(0x28700, 0x13800), seed=2), KEY).problems
    assert any("256-byte erases" in p for p in problems)
