# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one-time move from the old name's folders (gamesir-linux) to PadSquire's."""
import os

from padsquire import cli, paths


def make_old(share, cache):
    backups = share / "gamesir-linux" / "backups"
    backups.mkdir(parents=True, mode=0o700)
    (backups / "flash-1.bin").write_bytes(b"\x01" * 16)
    (backups / "flash-1.json").write_text("{}")
    (share / "gamesir-linux" / "firmware").mkdir()
    (share / "gamesir-linux" / "firmware" / "T4N2.ufw").write_bytes(b"fw")
    (cache / "gamesir-linux" / "loaders").mkdir(parents=True)
    (cache / "gamesir-linux" / "loaders" / "br23.bin").write_bytes(b"ld")
    return backups


def test_old_folders_are_moved_once(tmp_path):
    share, cache = tmp_path / "share", tmp_path / "cache"
    make_old(share, cache)
    said = paths.migrate(share, cache)
    assert len(said) == 2 and all("Moved" in line for line in said)
    assert (share / "padsquire" / "backups" / "flash-1.bin").read_bytes() == b"\x01" * 16
    assert (share / "padsquire" / "firmware" / "T4N2.ufw").exists()
    assert (cache / "padsquire" / "loaders" / "br23.bin").exists()
    assert not (share / "gamesir-linux").exists() and not (cache / "gamesir-linux").exists()
    assert (share / "padsquire" / "backups").stat().st_mode & 0o777 == 0o700   # still private
    assert paths.migrate(share, cache) == []                                  # said once


def test_merge_into_an_existing_folder_never_overwrites(tmp_path):
    share, cache = tmp_path / "share", tmp_path / "cache"
    make_old(share, cache)
    new = share / "padsquire" / "backups"
    new.mkdir(parents=True)
    (new / "flash-1.bin").write_bytes(b"newer")              # same name, different content
    (new / "flash-2.bin").write_bytes(b"other")
    said = paths.migrate(share, cache)
    assert (new / "flash-1.bin").read_bytes() == b"newer"    # kept
    assert (new / "flash-1.json").exists() and (new / "flash-2.bin").exists()
    assert (share / "padsquire" / "firmware" / "T4N2.ufw").exists()
    old = share / "gamesir-linux" / "backups" / "flash-1.bin"
    assert old.read_bytes() == b"\x01" * 16                  # the clash stays where it was
    assert any("Left in" in line and "backups/flash-1.bin" in line for line in said)


def test_nothing_to_do_without_old_folders(tmp_path):
    assert paths.migrate(tmp_path / "share", tmp_path / "cache") == []


def test_cli_moves_and_says_so(capsys):
    share, cache = paths.data_home(), paths.cache_home()     # the test's private XDG folders
    make_old(share, cache)
    try:
        cli.main(["--version"])
    except SystemExit:
        pass
    assert "PadSquire: Moved" in capsys.readouterr().err
    assert (share / "padsquire" / "backups" / "flash-1.json").exists()
    assert os.environ["XDG_DATA_HOME"] in str(share)


def test_files_made_under_sudo_go_back_to_the_user(tmp_path, monkeypatch):
    chowned = []
    monkeypatch.setattr(paths.os, "geteuid", lambda: 0)
    monkeypatch.setattr(paths.os, "chown", lambda p, uid, gid: chowned.append((p, uid, gid)))
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SUDO_GID", "20")
    (tmp_path / "a").mkdir()
    paths.make_dirs(tmp_path / "a" / "b" / "c")
    paths.give_back(tmp_path / "a" / "b" / "c")
    assert chowned == [(tmp_path / "a" / "b", 501, 20), (tmp_path / "a" / "b" / "c", 501, 20),
                       (tmp_path / "a" / "b" / "c", 501, 20)]
    monkeypatch.delenv("SUDO_UID")
    paths.give_back(tmp_path / "a")                   # root without sudo: left alone
    assert len(chowned) == 3
