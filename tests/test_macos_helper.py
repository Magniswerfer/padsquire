# SPDX-License-Identifier: AGPL-3.0-or-later
"""The macOS administrator helper: what it forwards, and a round trip over its socket."""
import os
import shutil
import tempfile
import threading
import time

import pytest

from padsquire import jlboot, jlwrite, macos_helper


def test_only_the_update_commands_get_through():
    assert macos_helper.allowed(bytes([0x12, 0, 0, 0, 36, 0]))                      # INQUIRY
    for cmd in (jlboot.READ_FLASH, jlboot.RUN_APP, jlwrite.ERASE_SECTOR, jlwrite.WRITE_FLASH):
        assert macos_helper.allowed(cmd.to_bytes(2, "big") + bytes(14))
    for cmd in (0xFC12, 0xFB00, 0xFB02, 0xFB42):                                    # chip key, block/chip erase
        assert not macos_helper.allowed(cmd.to_bytes(2, "big") + bytes(14))
    assert not macos_helper.allowed(b"")


class FakeUboot:
    def __init__(self):
        self.sent, self.closed = [], False

    def execute(self, cdb, data_out=None, data_in_len=0):
        self.sent.append((cdb, data_out, data_in_len))
        return bytes(range(data_in_len))

    def close(self):
        self.closed = True


@pytest.fixture
def helper(monkeypatch):
    # a short path: macOS allows 104 bytes for a Unix socket, less than pytest's tmp_path
    folder = macos_helper.Path(tempfile.mkdtemp(prefix="psq", dir="/tmp"))
    os.chmod(folder, 0o700)
    sock = folder / "h.sock"
    fake = FakeUboot()
    monkeypatch.setattr(macos_helper, "_take_device", lambda wait, log: (fake, None))
    owner = (os.getuid(), os.getgid())
    result = {}
    thread = threading.Thread(target=lambda: result.update(rc=macos_helper.serve(sock, owner, log=lambda t: None)),
                              daemon=True)
    thread.start()
    for _ in range(100):
        if macos_helper.running(sock):
            break
        time.sleep(0.02)
    yield sock, fake, thread, result
    macos_helper.stop(sock)
    shutil.rmtree(folder, ignore_errors=True)


def test_commands_go_through_the_socket(helper):
    sock, fake, thread, result = helper
    assert oct(sock.stat().st_mode & 0o777) == "0o600"
    dev = macos_helper.HelperDevice(sock, wait=1)
    assert dev.execute(bytes([0x12, 0, 0, 0, 36, 0]), None, 4) == bytes([0, 1, 2, 3])
    assert dev.execute(jlwrite.WRITE_FLASH.to_bytes(2, "big") + bytes(14), b"\xaa\xbb") == b""
    assert fake.sent[1][1] == b"\xaa\xbb"
    with pytest.raises(jlboot.JLBootError, match="not allowed"):
        dev.execute(b"\xfc\x12" + bytes(14))
    dev.close()
    dev = macos_helper.HelperDevice(sock, wait=1)            # the next step: same helper, same device
    dev.execute(bytes([0x12, 0, 0, 0, 36, 0]), None, 1)
    dev.close()
    macos_helper.stop(sock)
    thread.join(5)
    assert result["rc"] == 0 and fake.closed and not sock.exists()


def test_the_helper_refuses_a_folder_others_can_open(tmp_path):
    folder = tmp_path / "open"
    folder.mkdir()
    os.chmod(folder, 0o755)
    assert macos_helper.serve(folder / "h.sock", (os.getuid(), os.getgid()), log=lambda t: None) == 2


def test_the_password_prompt_says_why():
    assert "administrator" in macos_helper.PROMPT and "update mode" in macos_helper.PROMPT


def test_a_helper_that_goes_away_fails_the_step(monkeypatch):
    folder = macos_helper.Path(tempfile.mkdtemp(prefix="psq", dir="/tmp"))
    os.chmod(folder, 0o700)
    sock = folder / "h.sock"

    def broken(wait, log):
        raise jlboot.JLBootError("could not open the download-mode device: Other error")
    monkeypatch.setattr(macos_helper, "_take_device", broken)
    thread = threading.Thread(target=_serve_quietly, args=(sock,), daemon=True)
    thread.start()
    for _ in range(100):
        if macos_helper.running(sock):
            break
        time.sleep(0.02)
    with pytest.raises(jlboot.JLBootError):            # a firmware job catches these
        macos_helper.HelperDevice(sock, wait=1)
    thread.join(5)
    shutil.rmtree(folder, ignore_errors=True)


def _serve_quietly(sock):
    try:
        macos_helper.serve(sock, (os.getuid(), os.getgid()), log=lambda t: None)
    except jlboot.JLBootError:
        pass
