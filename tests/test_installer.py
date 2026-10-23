import json
import os
import shutil
import struct
from pathlib import Path

import pytest

from gamesir_linux import installer, ufw

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


def _write_asar(path: Path, files: dict[str, bytes]):
    tree, blob, off = {"files": {}}, b"", 0
    for name, data in files.items():
        node = tree
        *dirs, leaf = name.split("/")
        for d in dirs:
            node = node["files"].setdefault(d, {"files": {}})
        node["files"][leaf] = {"size": len(data), "offset": str(off)}
        blob += data
        off += len(data)
    js = json.dumps(tree).encode()
    pad = (4 - len(js) % 4) % 4
    pickle = struct.pack("<II", len(js) + 4 + pad, len(js)) + js + b"\0" * pad
    path.write_bytes(struct.pack("<II", 4, len(pickle)) + pickle + blob)


def test_asar_reader(tmp_path):
    asar = tmp_path / "app.asar"
    _write_asar(asar, {"package.json": b'{"version":"9.9"}', "dist/electron/main.js": JS.encode()})
    assert json.loads(installer.asar_file(asar, "package.json"))["version"] == "9.9"
    assert installer.asar_file(asar, "dist/electron/main.js").decode() == JS
    with pytest.raises(installer.ExtractError):
        installer.asar_file(asar, "missing.js")


REAL = os.environ.get("GAMESIR_INSTALLER")


@pytest.mark.installer
@pytest.mark.skipif(not REAL, reason="set GAMESIR_INSTALLER to a GameSir Connect Setup .exe")
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
