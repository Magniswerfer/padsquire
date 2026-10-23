# SPDX-License-Identifier: AGPL-3.0-or-later
from padsquire import cli, desktop, paths


def test_menu_entry_installs_and_removes(capsys):
    cli.main(["menu"])
    entry = paths.data_home() / "applications" / "padsquire.desktop"   # the test's private XDG folder
    text = entry.read_text()
    assert "Name=PadSquire" in text and "Icon=padsquire" in text and " gui" in text
    assert desktop.icon_file().read_text().startswith("<svg")
    cli.main(["menu", "--remove"])
    assert not entry.exists() and not desktop.icon_file().exists()


def test_udev_rules_ship_with_the_package(capsys):
    assert desktop.rules_path().is_file()
    cli.main(["udev-rules"])
    out = capsys.readouterr().out
    assert 'ATTRS{idVendor}=="3537", TAG+="uaccess"' in out


def test_app_window_prefers_a_chromium_browser(monkeypatch):
    started = []
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert desktop.open_app("http://127.0.0.1:1/?token=x") == "window (chromium)"
    assert started == [["/usr/bin/chromium", "--app=http://127.0.0.1:1/?token=x"]]
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.open_app("u", tab=True) == "browser tab" and opened == ["u"]
