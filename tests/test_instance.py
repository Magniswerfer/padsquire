# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starting the settings app while one is already running: the same version is reused; another
version (left running across an update) is replaced, unless it is busy with firmware."""
from pathlib import Path

from padsquire import __version__
from padsquire.web import server


def running(monkeypatch, version, busy):
    stopped, asked = [], []
    monkeypatch.setattr(server, "_running_instance",
                        lambda: {"pid": 4242, "port": 1, "url": "http://127.0.0.1:1/?token=x", "version": version})
    monkeypatch.setattr(server, "_busy", lambda info: asked.append(info) or busy)
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: stopped.append((pid, sig)))
    return stopped, asked


def test_the_same_version_is_reused(monkeypatch, capsys):
    stopped, asked = running(monkeypatch, __version__, busy=False)
    server.serve(Path("."), open_browser=False)
    assert "already running" in capsys.readouterr().out
    assert not stopped and not asked


def test_an_older_version_busy_with_firmware_keeps_running(monkeypatch, capsys):
    stopped, asked = running(monkeypatch, "0.1.0", busy=True)
    server.serve(Path("."), open_browser=False)
    out = capsys.readouterr().out
    assert "stays open" in out and "already running" in out
    assert asked and not stopped          # never stopped in the middle of a firmware step
