import hashlib
import json

import pytest

from gamesir_linux import ufw

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
