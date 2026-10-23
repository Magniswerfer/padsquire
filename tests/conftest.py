import hashlib
import json
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from gamesir_linux import installer, ufw


def write_asar(path: Path, files: dict[str, bytes]) -> None:
    """Write a minimal Electron asar archive."""
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


def fake_installer(path: Path, version: str, fw_version: int = 8201) -> Path:
    """An archive laid out like the GameSir Connect NSIS installer (7-Zip opens both)."""
    intl, cn = f"T4Nlite2_V{fw_version}_intl.ufw", f"T4Nlite2_V{fw_version - 1}_cn.ufw"
    js = ('dd={Type:"Internal",Version:"2800",Path:"packages/DOG.ufw",Identifier:"T4N2Dongle"},'
          f'pd=[{{Model:"T4N2",PackedFirmware:[{{Type:"Internal",Version:"{fw_version - 1}",'
          f'Path:"packages/{cn}",MinimumVersion:"5600",Identifier:"T4N2Internal"}},'
          f'{{Type:"International",Version:"{fw_version}",Path:"packages/{intl}",'
          'MinimumVersion:"5601",Identifier:"T4N2International"}]}];')
    with tempfile.TemporaryDirectory() as tmp:
        res = Path(tmp) / "resources"
        pkgs = res / "dfu" / "packages"
        pkgs.mkdir(parents=True)
        write_asar(res / "app.asar", {"package.json": json.dumps({"version": version}).encode(),
                                      "dist/electron/main.js": js.encode()})
        (pkgs / "DOG.ufw").write_bytes(make_ufw(chip="AC632N", pid="AC632N_TRANS", vid="N2DG"))
        (pkgs / intl).write_bytes(make_ufw())
        (pkgs / cn).write_bytes(make_ufw(firmware=b"\x5a" * 3000))
        subprocess.run([installer.find_7zip(), "a", "-t7z", str(path), "resources"],
                       cwd=tmp, check=True, capture_output=True)
    return path

ISD_TEMPLATE = """[EXTRA_CFG_PARAM]
CHIP_NAME = {chip};
PID = {pid};
VID = {vid};
DOWNLOAD_MODEL = USB;
"""


def make_ufw(chip="AC695X", pid="AC695X_TRANS", vid="N2LT", firmware=b"\xa5" * 3000) -> bytes:
    """A synthetic .ufw with the same structure as GameSir's."""
    isd = ISD_TEMPLATE.format(chip=chip, pid=pid, vid=vid).encode()
    return ufw.build(chip, [
        ("info.log", 0x02, b""),
        ("isd_config.ini", 0x34, isd),
        ("ota.bin", 0x64, firmware),
        ("script.ver", 0xFB, f"{chip}-v0.01-cfg_tool-v0.08".encode()),
        ("flash.bin", 0x00, firmware[::-1]),
        ("tail.bin", 0xFF, b"\x00" * 64),
    ], scramble={"isd_config.ini", "script.ver"})


@pytest.fixture
def fw_dir(tmp_path):
    """A firmware directory shaped like `gamesir-linux extract` output."""
    files = {
        "ctrl_intl.ufw": dict(identifier="T4N2International", target="controller", region="international",
                              version=8201, minimum_version=5601, data=make_ufw()),
        "ctrl_cn.ufw": dict(identifier="T4N2Internal", target="controller", region="china",
                            version=8200, minimum_version=5600, data=make_ufw(firmware=b"\x5a" * 3000)),
        "dongle.ufw": dict(identifier="T4N2Dongle", target="dongle", region=None, version=2800,
                           minimum_version=None,
                           data=make_ufw(chip="AC632N", pid="AC632N_TRANS", vid="N2DG")),
    }
    manifest = {"source": "test", "gamesir_connect_version": "0", "model": "T4N2", "firmware": []}
    for name, f in files.items():
        data = f.pop("data")
        (tmp_path / name).write_bytes(data)
        parsed = ufw.parse(data)
        manifest["firmware"].append(dict(
            file=name, sha256=hashlib.sha256(data).hexdigest(), chip=parsed.chip,
            jl_pid=parsed.jl_pid, jl_vid=parsed.jl_vid, **f,
        ))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path
