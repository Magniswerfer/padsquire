# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import shutil

import pytest
from conftest import fake_installer, write_asar

from padsquire import installer, ufw

HAVE_7Z = any(shutil.which(n) for n in ("7zz", "7z", "7za"))


@pytest.mark.skipif(not HAVE_7Z, reason="7-Zip not installed")
def test_extract_from_installer_layout(tmp_path):
    exe = fake_installer(tmp_path / "GameSir Connect Setup 9.9.9.exe", "9.9.9")
    manifest = installer.extract(exe, tmp_path / "fw")
    assert manifest["gamesir_connect_version"] == "9.9.9"
    assert {(f["target"], f["region"]) for f in manifest["firmware"]} == {
        ("dongle", None), ("controller", "china"), ("controller", "international")}
    assert installer.load_manifest(tmp_path / "fw")[0].jl_vid in ("N2DG", "N2LT")

@pytest.mark.skipif(not HAVE_7Z, reason="7-Zip not installed")
def test_extract_from_nested_installer(tmp_path):
    exe = fake_installer(tmp_path / "GameSir Connect Setup 1.16.7.exe", "1.16.7", nested=True)
    manifest = installer.extract(exe, tmp_path / "fw")
    assert manifest["gamesir_connect_version"] == "1.16.7"
    assert len(manifest["firmware"]) == 3


JS = ('var a=1,dd={Type:"Internal",Version:"2800",Path:"packages/DOG.ufw",MD5:"x",Identifier:"T4N2Dongle"},'
      'pd=[{Model:"T4N2",PackedFirmware:[{Type:"Internal",Version:"8200",Path:"packages/A.ufw",'
      'MinimumVersion:"5600",Identifier:"T4N2Internal"},{Type:"International",Version:"8201",'
      'Path:"packages/B.ufw",MinimumVersion:"5601",Identifier:"T4N2International"}]}],'
      'other={Version:"1.0.0",Path:"packages/ZW.ufw"};')


def test_firmware_table_parses_minified_js():
    table = installer.firmware_table(JS)
    assert [fw["Identifier"] for fw in table] == ["T4N2Dongle", "T4N2Internal", "T4N2International"]
    assert table[2]["MinimumVersion"] == "5601"


def test_rescue_images_are_ignored():
    # Generic rescue images have no Identifier and must never be picked up.
    assert all("ZW" not in fw["Path"] for fw in installer.firmware_table(JS))


def test_asar_reader(tmp_path):
    asar = tmp_path / "app.asar"
    write_asar(asar, {"package.json": b'{"version":"9.9"}', "dist/electron/main.js": JS.encode()})
    assert json.loads(installer.asar_file(asar, "package.json"))["version"] == "9.9"
    assert installer.asar_file(asar, "dist/electron/main.js").decode() == JS
    with pytest.raises(installer.ExtractError):
        installer.asar_file(asar, "missing.js")


REAL = os.environ.get("PADSQUIRE_INSTALLER")


@pytest.mark.installer
@pytest.mark.skipif(not REAL, reason="set PADSQUIRE_INSTALLER to a GameSir Connect Setup .exe")
@pytest.mark.skipif(not any(shutil.which(n) for n in ("7zz", "7z", "7za")), reason="7-Zip not installed")
def test_real_installer(tmp_path):
    manifest = installer.extract(REAL, tmp_path)
    fws = manifest["firmware"]
    targets = {(fw["target"], fw["region"]) for fw in fws}
    assert ("controller", "international") in targets
    assert ("controller", "china") in targets
    assert ("dongle", None) in targets
    for fw in fws:
        f = ufw.load(tmp_path / fw["file"])
        assert f.ok, (fw["file"], f.problems())
        assert fw["jl_vid"] == ("N2DG" if fw["target"] == "dongle" else "N2LT")
        if fw["region"]:
            assert fw["version"] % 10 == (1 if fw["region"] == "international" else 0)
