import hashlib
import json

import pytest

from test_jlboot import CHIP_KEY, LOADER, LOADER_BLOB, FakeUboot, ScsiFail
from test_jlflash import _ufw, make_dump, make_image
from gamesir_linux import cli, cli_uboot, installer, jlboot, jlcrypt, jlwrite, ufw

JEDEC_1M = 0x1440C8
OLD = make_dump(make_image(key=CHIP_KEY, app_len=0x5E000))        # what the controller has now
NEW = b"\xff" * 0x1000 + make_image(key=CHIP_KEY, app_len=0x5F000)  # an image for [0, 0x64000)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setitem(jlboot.LOADERS, "br23", LOADER)
    d = tmp_path / "cache"
    d.mkdir()
    (d / "br23loader.bin").write_bytes(LOADER_BLOB)
    monkeypatch.setattr(cli_uboot, "LOADER_CACHE", d)
    return d


def started(flash=OLD, **kw):
    dev = FakeUboot(flash, jedec=JEDEC_1M, buffer_size=0x8000, **kw)
    boot = jlboot.JLBoot(dev.execute)
    jlboot.start(boot, cli_uboot.LOADER_CACHE)
    return dev, boot


# -- the writer's guards ----------------------------------------------------------------------

def test_writer_refuses_a_window_over_a_reserved_area(cache):
    _, boot = started()
    with pytest.raises(jlwrite.WriteError, match="overlaps a reserved area"):
        jlwrite.Writer(boot, 0, 0x65000, keep=[(0x64000, 0x100000)])
    with pytest.raises(jlwrite.WriteError, match="not whole sectors"):
        jlwrite.Writer(boot, 0, 0x64010, keep=[])


def test_writer_refuses_addresses_outside_its_window(cache):
    dev, boot = started()
    w = jlwrite.Writer(boot, 0x1000, 0x3000, keep=[(0x64000, 0x100000)])
    for bad in (lambda: w.erase(0x3000), lambda: w.erase(0), lambda: w.write(0x2000, b"x" * 0x1001),
                lambda: w.erase(0x1800)):
        with pytest.raises(jlwrite.WriteError):
            bad()
    assert dev.writes == []


def test_writer_commands_cannot_be_anything_else(cache):
    _, boot = started()
    w = jlwrite.Writer(boot, 0, 0x1000, keep=[])
    with pytest.raises(AssertionError):
        w._send(0xFC12, bytes(6))


# -- burn ---------------------------------------------------------------------------------------

def test_burn_writes_the_image_in_jielis_order(cache):
    dev, boot = started()
    w = jlwrite.Writer(boot, 0, len(NEW), keep=[(len(NEW), 0x100000)])
    jlwrite.burn(w, NEW, 0x1000)
    assert bytes(dev.flash[:len(NEW)]) == NEW and bytes(dev.flash[len(NEW):]) == OLD[len(NEW):]
    ops = dev.writes
    assert ops[:3] == [("erase", 0, 0x1000), ("write", 0, 0x40), ("erase", 0x1000, 0x1000)]
    assert ops[3] == ("erase", 0x63000, 0x1000)                     # then top-down
    assert ops[-2:] == [("write", 0x1000, 0x1000), ("erase", 0, 0x1000)]
    assert max(a + n for _, a, n in ops) <= len(NEW)


def test_burn_fails_loudly_on_a_bad_write_and_succeeds_again(cache):
    dev, boot = started(corrupt_write_at=0x20123)
    w = jlwrite.Writer(boot, 0, len(NEW), keep=[(len(NEW), 0x100000)])
    with pytest.raises(jlwrite.WriteError, match="did not verify, first at 0x20000"):
        jlwrite.burn(w, NEW, 0x1000)
    jlwrite.burn(w, NEW, 0x1000)
    assert bytes(dev.flash[:len(NEW)]) == NEW


@pytest.mark.parametrize("fail_after", [1, 2, 3, 4, 50, 150, 197, 198])
def test_an_interrupted_burn_can_be_run_again(cache, fail_after):
    dev, boot = started(fail_after=fail_after)
    w = jlwrite.Writer(boot, 0, len(NEW), keep=[(len(NEW), 0x100000)])
    with pytest.raises(ScsiFail):
        jlwrite.burn(w, NEW, 0x1000)
    assert bytes(dev.flash[len(NEW):]) == OLD[len(NEW):]          # reserved areas untouched
    dev.fail_after = None
    jlwrite.burn(w, NEW, 0x1000)
    assert bytes(dev.flash[:len(NEW)]) == NEW


def test_connect_record():
    image = make_image()
    out = jlwrite.connect_record(image, bytes.fromhex("855682ede98c"))
    assert out[:-16] == image[:-16]
    assert out[-16:] == bytes.fromhex("646c6c00011500ff855682ede98cffff")


# -- the CLI ------------------------------------------------------------------------------------

def _backup(tmp_path, flash: bytes):
    path = tmp_path / "nova2lite-br23-flash-test.bin"
    path.write_bytes(flash)
    path.with_suffix(".json").write_text(json.dumps({
        "sha256": hashlib.sha256(flash).hexdigest(), "identical": True,
        "chip": {"chip_key": f"{CHIP_KEY:#06x}"}, "flash": {"jedec_id": f"{JEDEC_1M:#08x}"}}))
    return path


@pytest.fixture
def chip(cache, monkeypatch):
    dev = FakeUboot(OLD, jedec=JEDEC_1M, buffer_size=0x8000)
    monkeypatch.setattr(cli_uboot, "_open", lambda args: (type("D", (), {"close": lambda s: None})(),
                                                         jlboot.JLBoot(dev.execute)))
    return dev


def test_writes_need_confirmation(chip, tmp_path, capsys):
    backup = _backup(tmp_path, OLD)
    assert cli.main(["uboot", "test-write", "--backup", str(backup)]) == 2
    assert cli.main(["uboot", "restore", str(backup)]) == 2
    assert chip.cdbs == []


def test_cli_test_write_leaves_the_flash_as_it_was(chip, tmp_path, capsys):
    assert cli.main(["uboot", "test-write", "--backup", str(_backup(tmp_path, OLD)), "--confirm"]) == 0
    assert bytes(chip.flash) == OLD
    assert chip.writes == [("erase", 0xFF000, 0x1000), ("write", 0xFF000, 0x1000), ("erase", 0xFF000, 0x1000)]
    assert "identical to before the test" in capsys.readouterr().out


def test_cli_test_write_refuses_a_used_sector(chip, tmp_path, capsys):
    backup = _backup(tmp_path, OLD)
    assert cli.main(["uboot", "test-write", "--backup", str(backup), "--address", "0x79000", "--confirm"]) == 1
    assert "not a free sector" in capsys.readouterr().err and chip.writes == []


def test_cli_refuses_another_controller(chip, tmp_path, capsys):
    other = bytearray(OLD)
    other[0x79004] ^= 1                                              # another Bluetooth address
    assert cli.main(["uboot", "restore", str(_backup(tmp_path, bytes(other))), "--confirm"]) == 1
    assert "BTIF" in capsys.readouterr().err and chip.writes == []


def test_cli_restore_writes_the_backup_back(chip, tmp_path):
    backup = _backup(tmp_path, OLD)
    chip.flash[0x20000:0x20010] = b"\x00" * 16                      # something to repair
    chip.flash[0x64000:0x64004] = OLD[0x64000:0x64004]
    assert cli.main(["uboot", "restore", str(backup), "--confirm"]) == 0
    assert bytes(chip.flash) == OLD


def test_cli_flash_installs_an_update(chip, tmp_path, capsys):
    fw_dir = tmp_path / "fw"
    fw_dir.mkdir()
    data = _ufw(("flash.bin", 0x00, make_image(code_start=0, code_end=0x63000, vm=(0x63000, 0x16000), key=CHIP_KEY)),
                ("flash2.bin", 0x20, make_image(key=CHIP_KEY, app_len=0x5F000)))
    (fw_dir / "ctrl.ufw").write_bytes(data)
    parsed = ufw.parse(data)
    (fw_dir / "manifest.json").write_text(json.dumps({"source": "test", "gamesir_connect_version": "0",
        "model": "T4N2", "firmware": [dict(file="ctrl.ufw", sha256=hashlib.sha256(data).hexdigest(),
            chip=parsed.chip, jl_pid=parsed.jl_pid, jl_vid=parsed.jl_vid, identifier="T4N2International",
            target="controller", region="international", version=8201, minimum_version=5601)]}))
    backup = _backup(tmp_path, OLD)
    argv = ["uboot", "flash", str(fw_dir / "ctrl.ufw"), "--backup", str(backup), "--firmware-dir", str(fw_dir)]
    assert cli.main(argv + ["--installed-version", "8201", "--confirm"]) == 2     # same version
    assert chip.writes == []
    assert cli.main(argv + ["--installed-version", "7701", "--confirm"]) == 0
    expected = jlwrite.connect_record(make_image(key=CHIP_KEY, app_len=0x5F000), bytes(6))
    assert bytes(chip.flash[:0x1000]) == b"\xff" * 0x1000
    assert bytes(chip.flash[0x1000:0x64000]) == expected
    assert bytes(chip.flash[0x64000:]) == OLD[0x64000:]
    assert "flash2.bin at 0x1000-0x63fff" in capsys.readouterr().out


def test_burn_with_the_header_at_zero_uses_sector_0x1000_as_spare(cache):
    from test_jlflash import make_receiver_dump, make_receiver_image
    old = make_receiver_dump(make_receiver_image(key=CHIP_KEY))
    new = make_receiver_image(key=CHIP_KEY, seed=2)
    dev = FakeUboot(old, jedec=0x1240C8, buffer_size=0x8000)
    boot = jlboot.JLBoot(dev.execute)
    jlboot.start(boot, cli_uboot.LOADER_CACHE)
    w = jlwrite.Writer(boot, 0, len(new), keep=[(len(new), len(old))])
    jlwrite.burn(w, new, 0)
    assert bytes(dev.flash[:len(new)]) == new and bytes(dev.flash[len(new):]) == old[len(new):]
    assert dev.writes[:3] == [("erase", 0x1000, 0x1000), ("write", 0x1000, 0x40), ("erase", 0, 0x1000)]
    assert dev.writes[-4:] == [("erase", 0, 0x1000), ("write", 0, 0x1000),
                               ("erase", 0x1000, 0x1000), ("write", 0x1000, 0x1000)]
