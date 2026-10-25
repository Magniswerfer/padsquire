# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import make_ufw

from padsquire import ufw


def test_roundtrip():
    f = ufw.parse(make_ufw())
    assert f.ok, f.problems()
    assert f.chip == "AC695X"
    assert (f.jl_pid, f.jl_vid) == ("AC695X_TRANS", "N2LT")
    assert f.tool_version == "AC695X-v0.01-cfg_tool-v0.08"
    assert [e.name for e in f.entries][:3] == ["info.log", "isd_config.ini", "ota.bin"]
    assert f.entry("isd_config.ini").scrambled
    assert not f.entry("ota.bin").scrambled


def test_payload_offsets_are_aligned():
    f = ufw.parse(make_ufw())
    assert all(e.offset % 0x20 == 0 for e in f.entries)
    assert f.entries[-1].end <= f.file_size


def _flip(data: bytes, pos: int) -> bytes:
    b = bytearray(data)
    b[pos] ^= 0xFF
    return bytes(b)


def test_detects_damaged_payload():
    data = make_ufw()
    ota = ufw.parse(data).entry("ota.bin")
    problems = ufw.parse(_flip(data, ota.offset + 100)).problems()
    assert problems == ["ota.bin: CRC mismatch"]


def test_detects_damaged_header():
    assert "header CRC mismatch" in ufw.parse(_flip(make_ufw(), 5)).problems()


def test_detects_damaged_table():
    assert "entry table CRC mismatch" in ufw.parse(_flip(make_ufw(), 0x40 + 70)).problems()


def test_detects_truncation():
    problems = ufw.parse(make_ufw()[:-10]).problems()
    assert any(p.startswith("size mismatch") for p in problems)


def test_rejects_non_ufw():
    with pytest.raises(ufw.UfwError):
        ufw.parse(b"\x7fELF" + bytes(200))
    with pytest.raises(ufw.UfwError):
        ufw.parse(b"short")


def test_identity_unreadable_when_config_damaged():
    data = make_ufw()
    isd = ufw.parse(data).entry("isd_config.ini")
    f = ufw.parse(_flip(data, isd.offset + 3))
    assert f.jl_vid is None
    assert not f.ok
