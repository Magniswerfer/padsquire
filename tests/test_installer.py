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


# Two entries in the shape of Connect 1.16.7's device registry (main.js, Kv): one handled by the
# GameSir DLL, one by a JavaScript proxy, with a colour edition repeating a mode.
DEVICES_JS = (
    'var Nd=13623,Cd=4111,Md=4417,xd=4186;var qd={init:function(){const e=go.getDfuPath('
    '"sapjoyuniapis_t4n2_x86_v1");return Jc.defineUtility(e),function(){const e={pcwszName:"x",'
    'vid:[Nd],pid:[Cd,Md,xd]};Jc.createSAPJoy(e)}(),0}},'
    'op=[{Model:"T4N2",ProductName:"Nova 2 Lite",UDiskVersionId:"N2LT",UdiskProductName:"AC695X_TRANS",'
    'PackedFirmware:[{Type:"Internal",Version:"8200",Path:"packages/A.ufw"}],'
    'Mapping:{L1:"LB",Fake:"Macro"},Proxy:(e,t,r,n)=>new sp(e,t,r,n),ConfigMethods:[],'
    'Modes:[{Mode:"Wired",AssetsFolder:"T4N2/White",ConfigModeVid:13623,ConfigModePid:4111,'
    'UsagePage:65535,HandleByDll:!0},{PackedFirmware:ap,Mode:"Wireless",UdiskProductName:"AC632N_TRANS",'
    'ConfigModeVid:13623,ConfigModePid:4248,UsagePage:65535,HandleByDll:!0}]}],'
    'Kv=[{Model:"C2",ProductName:"CYCLONE 2",UdiskProductName:"GS_C2_DEVICE",PackedFirmware:{Version:'
    '"1.2.3",Path:"packages/GS_C2_DEVICE_V123_Key.ufw",Identifier:"C2"},Proxy:(e,t,r)=>new ou(e,t,r),'
    'Modes:[{Mode:"Wired",AssetsFolder:"C2/White",ConfigModeVid:13623,ConfigModePid:4125,UsagePage:65520},'
    '{Mode:"Wired",AssetsFolder:"C2/Black",ConfigModeVid:13623,ConfigModePid:4125,UsagePage:65520},'
    '{UdiskProductName:"GS_C2_ADC_DEVICE",PackedFirmware:{Version:"3.5.6",Path:"packages/B.ufw"},'
    'Mode:"Wired",ConfigModeVid:13623,ConfigModePid:4179,UsagePage:65520}]},{Model:"GS_C2_Dongle",'
    'ProductName:"C2 Dongle",Modes:[]}];')


def test_device_table_reads_the_registry():
    table = {d["model"]: d for d in installer.device_table(DEVICES_JS)}
    assert set(table) == {"T4N2", "C2", "GS_C2_Dongle"}
    nova, cyclone = table["T4N2"], table["C2"]
    assert (nova["handler"], nova["udisk_version"], nova["firmware"]) == ("sp", "N2LT", ["A.ufw"])
    assert [(m["mode"], m["pid"], m["dll"]) for m in nova["modes"]] == [
        ("Wired", 0x100F, "true"), ("Wireless", 0x1098, "true")]
    assert cyclone["handler"] == "ou" and cyclone["firmware"] == ["GS_C2_DEVICE_V123_Key.ufw"]
    assert [(m["pid"], m["usage_page"], m.get("udisk")) for m in cyclone["modes"]] == [
        (0x101D, 0xFFF0, None), (0x1053, 0xFFF0, "GS_C2_ADC_DEVICE")]
    assert table["GS_C2_Dongle"]["handler"] is None and table["GS_C2_Dongle"]["modes"] == []


def test_dll_pids_resolves_the_registered_ids():
    assert installer.dll_pids(DEVICES_JS) == {"sapjoyuniapis_t4n2_x86_v1": [0x100F, 0x1141, 0x105A]}
