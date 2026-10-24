# SPDX-License-Identifier: AGPL-3.0-or-later
import json

import pytest

from padsquire import profiles, settingsfile


def doc(light=(255, 255, 255)):
    return settingsfile.document({"light": list(light), "dead_zones": {"LEFT_STICK_INNER": 5}},
                                 app_version="0.0.0", firmware="8201", now=0)


def test_create_list_get(tmp_path):
    store = profiles.Store(tmp_path / "profiles")
    assert store.list() == []                                     # no folder yet is fine
    a = store.create("  Racing   games ", doc((255, 0, 0)))
    b = store.create("Shooters", doc())
    assert a == "racing-games"
    assert [p["name"] for p in store.list()] == ["Racing games", "Shooters"]
    got = store.get(a)
    assert got["name"] == "Racing games" and got["settings"]["light"] == [255, 0, 0]
    assert settingsfile.parse(got)                                # still a settings file
    assert store.get(b)["format"] == "padsquire-settings"


def test_names_are_unique_ignoring_case_and_ids_stay_distinct(tmp_path):
    store = profiles.Store(tmp_path)
    store.create("Default", doc())
    with pytest.raises(profiles.ProfileError, match="already"):
        store.create("default", doc())
    assert store.create("Default!", doc()) == "default-2"        # same slug, its own file


@pytest.mark.parametrize("name", ["", "   ", None, 5, "x" * 61])
def test_bad_names(tmp_path, name):
    with pytest.raises(profiles.ProfileError):
        profiles.Store(tmp_path).create(name, doc())


def test_only_valid_settings_are_stored(tmp_path):
    store = profiles.Store(tmp_path)
    with pytest.raises(settingsfile.InvalidFile):
        store.create("Broken", {"format": "nope"})
    assert list(tmp_path.iterdir()) == []


def test_rename_replace_delete(tmp_path):
    store = profiles.Store(tmp_path)
    a = store.create("One", doc())
    store.create("Two", doc())
    with pytest.raises(profiles.ProfileError, match="already"):
        store.rename(a, "two")
    store.rename(a, "One")                                         # its own name is fine
    store.rename(a, "Uno")
    store.replace(a, doc((0, 0, 255)))
    assert store.get(a)["name"] == "Uno" and store.get(a)["settings"]["light"] == [0, 0, 255]
    store.delete(a)
    assert [p["name"] for p in store.list()] == ["Two"]
    with pytest.raises(profiles.ProfileError):
        store.get(a)


@pytest.mark.parametrize("pid", ["../escape", "a/b", "", None, "UPPER", "x" * 49])
def test_ids_cant_leave_the_folder(tmp_path, pid):
    with pytest.raises(profiles.ProfileError):
        profiles.Store(tmp_path).get(pid)


def test_unreadable_files_are_skipped_not_fatal(tmp_path):
    store = profiles.Store(tmp_path)
    store.create("Good", doc())
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "plain-settings.json").write_text(json.dumps(doc()))       # a settings file without a name
    assert [p["name"] for p in store.list()] == ["Good"]
    assert not list(tmp_path.glob(".tmp-*"))
