# SPDX-License-Identifier: AGPL-3.0-or-later
from gamesir_linux import installer, safety
from gamesir_linux.safety import DeviceState


def ctrl(version=8101, vid="N2LT", pid="AC695X_TRANS"):
    return DeviceState("controller", pid, vid, version)


def plan_for(fw_dir, device, **kw):
    bundled = installer.load_manifest(fw_dir)
    fw = safety.choose_firmware(device, bundled)
    assert fw is not None
    return safety.plan_update(device, fw, fw_dir / fw.file, bundled=bundled, **kw)


def test_normal_update_allowed(fw_dir):
    p = plan_for(fw_dir, ctrl(), dongle_version=2800)
    assert p.allowed, p.errors
    assert p.firmware.version == 8201 and p.firmware.region == "international"
    assert p.warnings == []


def test_picks_firmware_for_device_region(fw_dir):
    assert plan_for(fw_dir, ctrl(version=8100), dongle_version=2800).firmware.region == "china"


def test_region_mismatch_blocked(fw_dir):
    bundled = installer.load_manifest(fw_dir)
    china = next(fw for fw in bundled if fw.region == "china")
    p = safety.plan_update(ctrl(version=8101), china, fw_dir / china.file, bundled=bundled)
    assert any("region mismatch" in e for e in p.errors)


def test_other_model_gets_nothing(fw_dir):
    # A Super Nova (VID "N2S") must not be offered Nova 2 Lite firmware.
    assert safety.choose_firmware(ctrl(vid="N2S"), installer.load_manifest(fw_dir)) is None


def test_model_mismatch_blocked_even_if_forced(fw_dir):
    bundled = installer.load_manifest(fw_dir)
    fw = next(f for f in bundled if f.region == "international")
    p = safety.plan_update(ctrl(vid="N2S"), fw, fw_dir / fw.file, bundled=bundled)
    assert any("model mismatch" in e for e in p.errors)


def test_dongle_firmware_never_goes_on_controller(fw_dir):
    bundled = installer.load_manifest(fw_dir)
    dongle = next(f for f in bundled if f.target == "dongle")
    p = safety.plan_update(ctrl(), dongle, fw_dir / dongle.file, bundled=bundled)
    assert any("dongle firmware" in e for e in p.errors)
    assert any("model mismatch" in e for e in p.errors)


def test_unknown_identity_blocked(fw_dir):
    p = plan_for(fw_dir, ctrl(), dongle_version=2800)
    unknown = DeviceState("controller", None, None, 8101)
    p = safety.plan_update(unknown, p.firmware, fw_dir / p.firmware.file)
    assert any("identity" in e for e in p.errors)


def test_downgrade_blocked(fw_dir):
    p = plan_for(fw_dir, ctrl(version=9001))
    assert any("downgrade" in e for e in p.errors)


def test_same_version_needs_reinstall(fw_dir):
    assert not plan_for(fw_dir, ctrl(version=8201)).allowed
    assert plan_for(fw_dir, ctrl(version=8201), reinstall=True, dongle_version=2800).allowed


def test_too_old_needs_intermediate_update(fw_dir):
    p = plan_for(fw_dir, ctrl(version=5501))
    assert any("intermediate update" in e for e in p.errors)


def test_tampered_file_blocked(fw_dir):
    path = fw_dir / "ctrl_intl.ufw"
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))
    p = plan_for(fw_dir, ctrl())
    assert any("sha256" in e for e in p.errors)
    assert any("damaged" in e for e in p.errors)


def test_old_dongle_warns(fw_dir):
    p = plan_for(fw_dir, ctrl(), dongle_version=2700)
    assert p.allowed
    assert any("update the dongle" in w for w in p.warnings)


def test_dongle_update(fw_dir):
    p = plan_for(fw_dir, DeviceState("dongle", "AC632N_TRANS", "N2DG", 2700))
    assert p.allowed, p.errors
    assert p.firmware.target == "dongle"
