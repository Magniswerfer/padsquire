# SPDX-License-Identifier: AGPL-3.0-or-later
"""PadSquire's own updates: versions, how it was installed, and pip run like install.sh does."""
import pytest

from padsquire import appupdate


def test_versions_compare_as_numbers():
    k = appupdate.version_key
    assert k("0.1.10") > k("0.1.9") > k("0.1.3")
    assert k("0.2.0") > k("0.2.0rc1") > k("0.1.9")
    assert k("1.0") == k("1.0.0")


def test_how_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(appupdate, "INSTALLER_VENV", tmp_path / "venv")
    (tmp_path / "venv").mkdir()
    assert appupdate.how_installed(tmp_path / "venv").kind == "installer"
    pipx = tmp_path / ".local" / "pipx" / "venvs" / "padsquire"
    pipx.mkdir(parents=True)
    assert appupdate.how_installed(pipx) == appupdate.Install("pipx", "pipx upgrade padsquire")


def test_install_runs_pip_for_exactly_that_version(monkeypatch, tmp_path):
    monkeypatch.setattr(appupdate, "how_installed", lambda: appupdate.Install("installer", None))
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    assert appupdate.install("0.2.0", python="/venv/bin/python", run=run) == "ok"
    assert ran == [["/venv/bin/python", "-m", "pip", "install", "--upgrade", "--no-cache-dir", "padsquire[gui]==0.2.0"]]
    with pytest.raises(appupdate.UpdateError, match="not a version"):
        appupdate.install("0.2.0; rm -rf /", run=run)


def test_only_the_installer_s_environment_updates_itself(monkeypatch):
    monkeypatch.setattr(appupdate, "how_installed", lambda: appupdate.Install("pipx", "pipx upgrade padsquire"))
    with pytest.raises(appupdate.UpdateError, match="installer"):
        appupdate.install("0.2.0", run=lambda *a, **k: None)
