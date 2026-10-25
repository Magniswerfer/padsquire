# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import functools
import hashlib
import http.server
import shutil
import threading

import pytest
from conftest import fake_installer

from padsquire import updater
from padsquire.updater import Release, UpdateError

# A real electron-builder latest.yml, as the format is documented.
FEED = """version: 1.15.0
files:
  - url: GameSir Connect Setup 1.15.0.exe
    sha512: {sha}
    size: {size}
path: GameSir Connect Setup 1.15.0.exe
sha512: {sha}
releaseDate: '2026-11-02T08:15:30.000Z'
"""


def test_parse_feed():
    r = updater.parse_feed(FEED.format(sha="abc==", size=123))
    assert r == Release("1.15.0", "GameSir Connect Setup 1.15.0.exe", "abc==", 123,
                        "2026-11-02T08:15:30.000Z")


def test_parse_feed_without_files_list():
    r = updater.parse_feed("version: '2.0.0'\npath: setup.exe\nsha512: xyz\n")
    assert (r.version, r.file, r.sha512, r.size) == ("2.0.0", "setup.exe", "xyz", None)


def test_parse_feed_rejects_junk():
    with pytest.raises(UpdateError):
        updater.parse_feed("<html>404</html>")
    with pytest.raises(UpdateError):
        updater.parse_feed("version: 1.0.0\n")


def test_version_order():
    k = updater.version_key
    assert k("1.14.10") > k("1.14.5") > k("1.9.9") > k(None)
    assert k("1.15.0-beta.1") >= k("1.15.0")


def test_candidate_urls_match_known_download_location():
    r = Release("1.14.5", "GameSir Connect Setup 1.14.5.exe", "x", None, None)
    urls = updater.candidate_urls(updater.FEED_URL, r)
    # The link on gamesir.com's download page:
    assert urls[0] == "https://pc-connect-update.gamesir-cdn.com/1.14.5/GameSir%20Connect%20Setup%201.14.5.exe"


def test_candidate_urls_quote_feed_paths():
    # Since 1.16.7 the feed's url includes the version folder.
    r = Release("1.16.7", "1.16.7/GameSir Connect Setup 1.16.7.exe", "x", None, None)
    assert updater.candidate_urls(updater.FEED_URL, r) == [
        "https://pc-connect-update.gamesir-cdn.com/1.16.7/GameSir%20Connect%20Setup%201.16.7.exe"]


# -- end to end against a local server --------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "www"
    root.mkdir()
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}/latest.yml"
    httpd.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def publish(root, tmp_path, version, fw_version=8201, subdir=None, tamper=False):
    name = f"GameSir Connect Setup {version}.exe"
    exe = fake_installer(tmp_path / f"build-{version}.exe", version, fw_version)
    data = exe.read_bytes()
    sha = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if tamper:
        data = data[:-1] + bytes([data[-1] ^ 1])
    dest = root / (subdir if subdir is not None else version)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / name).write_bytes(data)
    (root / "latest.yml").write_text(
        FEED.replace("1.15.0", version).format(sha=sha, size=len(data)))


needs_7z = pytest.mark.skipif(not any(shutil.which(n) for n in ("7zz", "7z", "7za")),
                              reason="7-Zip not installed")


@needs_7z
def test_update_end_to_end(server, tmp_path):
    root, feed = server
    fw = tmp_path / "firmware"
    publish(root, tmp_path, "1.15.0", 8301)

    assert updater.update(fw, feed_url=feed, check_only=True).updated is False
    r = updater.update(fw, feed_url=feed)
    assert r.updated and r.current is None and r.latest == "1.15.0"
    assert updater.installed_version(fw) == "1.15.0"
    assert {f["version"] for f in r.manifest["firmware"]} == {2800, 8300, 8301}

    # Nothing new: no download.
    again = updater.update(fw, feed_url=feed)
    assert not again.updated and again.current == "1.15.0"

    # A newer release replaces the old firmware completely.
    publish(root, tmp_path, "1.16.0", 8401)
    r = updater.update(fw, feed_url=feed)
    assert r.updated and r.current == "1.15.0"
    assert sorted(p.name for p in fw.glob("*.ufw")) == ["DOG.ufw", "T4Nlite2_V8400_cn.ufw",
                                                         "T4Nlite2_V8401_intl.ufw"]


@needs_7z
def test_update_finds_installer_in_releases_dir(server, tmp_path):
    root, feed = server
    publish(root, tmp_path, "1.15.0", subdir="releases/1.15.0")
    assert updater.update(tmp_path / "fw", feed_url=feed).updated


@needs_7z
def test_tampered_download_is_refused_and_old_firmware_kept(server, tmp_path):
    root, feed = server
    fw = tmp_path / "firmware"
    fw.mkdir()
    (fw / "manifest.json").write_text('{"gamesir_connect_version": "1.14.5", "firmware": []}')
    publish(root, tmp_path, "1.15.0", tamper=True)
    with pytest.raises(UpdateError, match="SHA-512"):
        updater.update(fw, feed_url=feed)
    assert updater.installed_version(fw) == "1.14.5"
    assert not list(tmp_path.glob("gamesir-update-*"))  # temp files cleaned up


def test_missing_installer_reports_where_it_looked(server, tmp_path):
    root, feed = server
    (root / "latest.yml").write_text(FEED.format(sha="AAAA", size=1))
    with pytest.raises(UpdateError, match="not found") as e:
        updater.update(tmp_path / "fw", feed_url=feed)
    assert "releases/1.15.0" in str(e.value)


def test_unreachable_feed(tmp_path):
    with pytest.raises(UpdateError, match="update feed"):
        updater.update(tmp_path / "fw", feed_url="http://127.0.0.1:9/latest.yml")
