# SPDX-License-Identifier: AGPL-3.0-or-later
"""Firmware installation in the settings app: the download-mode steps as background jobs.

Each step (updatemode) runs in a worker thread as a job; the page polls GET /api/update for its
progress and log. One job at a time. Backups and firmware files are picked by name from their
own folders, never by a path from the request. Writes need {"confirm": true}.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path
from typing import Callable

from aiohttp import web

from .. import hid, installer, jlboot, jlflash, safety, updatemode
from ..client import NovaClient
from ..gcm import ProtocolError


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, one_go: bool = False):
        self.id = next(self._ids)
        self.kind = kind
        self.one_go = one_go            # macOS: switch, step and restart in one job (/api/update/run)
        self.state = "running"          # running | done | failed
        self.label, self.done, self.total = "", 0, 0
        self.log: list[str] = []
        self.result = None
        self.error: str | None = None
        self.stay = False               # failed mid-write: do not restart
        self.started = time.time()
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            self.log.append(text)

    def progress(self, label: str, done: int, total: int) -> None:
        self.label, self.done, self.total = label, done, total

    def snapshot(self) -> dict:
        with self._lock:
            log = list(self.log)
        return {"id": self.id, "kind": self.kind, "state": self.state, "label": self.label,
                "done": self.done, "total": self.total, "log": log, "result": self.result,
                "error": self.error, "stay": self.stay, "one_go": self.one_go}


class UpdateState:
    def __init__(self, backup_dir: Path, loader_cache: Path):
        self.backup_dir, self.loader_cache = backup_dir, loader_cache
        self.job: Job | None = None
        self.installed_version: int | None = None    # read from the controller before entering
        self.identity: dict | None = None
        self.stay = False                            # a write failed; restarting is refused

    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, kind: str, work: Callable[[Job], object], one_go: bool = False) -> Job:
        if self.busy():
            raise web.HTTPConflict(text='{"ok": false, "error": "another step is still running"}',
                                   content_type="application/json")
        job = self.job = Job(kind, one_go)

        def run():
            try:
                job.result = work(job)
                job.state = "done"
            except updatemode.Failed as e:
                job.error, job.stay, job.state = str(e), e.stay, "failed"
            except (jlboot.JLBootError, hid.HidError, OSError, ValueError) as e:
                job.error, job.state = str(e) or type(e).__name__, "failed"
            if job.stay:
                self.stay = True
            elif job.state == "done" and kind in ("restore", "install"):
                self.stay = False

        asyncio.get_running_loop().run_in_executor(None, run)
        return job


KEY = web.AppKey("update", UpdateState)


# Which device a download-mode disk is, by its SCSI vendor (cached by the kernel, no I/O).
DEVICE_BY_CHIP = {"br23": "controller", "bd19": "receiver"}
TARGET = {"controller": "controller", "receiver": "dongle"}     # safety / manifest names
RECEIVER_PID = 0x1098


def _identity(ident: jlboot.Identity) -> dict:
    return {"chip": ident.vendor, "chip_key": f"{ident.chip_key:#06x}", "jedec_id": f"{ident.jedec_id:#08x}",
            "flash_size": ident.flash_size}


def add_routes(routes: web.RouteTableDef, session, fw_dir: Path, find_usb, ok, fail, body,
               backup_dir: Path = updatemode.BACKUP_DIR,
               loader_cache: Path = updatemode.LOADER_CACHE) -> UpdateState:
    st = UpdateState(backup_dir, loader_cache)

    def backups() -> list[updatemode.Backup]:
        return updatemode.list_backups(st.backup_dir)

    def backup_named(name) -> updatemode.Backup:
        match = next((b for b in backups() if b.name == name), None)
        if match is None:
            raise web.HTTPBadRequest(text='{"ok": false, "error": "no such backup"}', content_type="application/json")
        return match

    def in_update_mode() -> str | None:
        """'controller' or 'receiver', whichever is in download mode now."""
        found = jlboot.find()
        return DEVICE_BY_CHIP.get(found[0].vendor.lower()) if found else None

    def firmware_named(name, device: str | None):
        try:
            bundled = installer.load_manifest(fw_dir)
        except (OSError, KeyError, ValueError):
            bundled = []
        match = next((f for f in bundled if f.file == name and f.target == TARGET.get(device)), None)
        if match is None:
            raise web.HTTPBadRequest(text='{"ok": false, "error": "no such firmware file"}',
                                     content_type="application/json")
        return match

    def with_device(work):
        """Open the download-mode disk for one job and close it afterwards (on macOS through the
        administrator helper, started first if it isn't running: the page may have been opened
        with the controller already in update mode)."""
        def run(job: Job):
            admin_helper(job)
            dev, boot = updatemode.open_device()
            try:
                return work(job, boot)
            finally:
                dev.close()
        return run

    def identify(job: Job, boot) -> dict:
        ident = jlboot.start(boot, st.loader_cache)
        st.identity = _identity(ident)
        job.add(f"Chip {ident.vendor}, chip key {ident.chip_key:#06x}, flash {ident.jedec_id:#08x}"
                + (f", {ident.flash_size // 1024} KB." if ident.flash_size else "."))
        return st.identity

    def admin_helper(job: Job) -> None:
        """macOS: the administrator helper, started through macOS's password dialog before the
        controller switches, so it doesn't sit in update mode while the password is typed."""
        if not hid.MACOS:
            return
        from .. import macos_helper
        if macos_helper.running():
            return
        job.progress("Waiting for your password in macOS's dialog", 0, 1)
        job.add("macOS asks for your password, so the helper may take the update-mode disk from macOS.")
        try:
            macos_helper.start()
        except macos_helper.HelperError as e:
            raise updatemode.Failed(str(e)) from None
        job.add("The helper runs as administrator for this update session.")

    def admin_helper_done() -> None:
        if hid.MACOS:
            from .. import macos_helper
            macos_helper.stop()

    @routes.get("/api/update")
    async def status(_):
        download = jlboot.find()
        found = find_usb()
        cable = any(d.link == "cable" for d in found)
        receiver = any(d.pid == RECEIVER_PID for d in found)
        mode = "download" if download else "normal" if cable or receiver else "none"
        return ok({
            "mode": mode, "sg_missing": not hid.MACOS and not Path("/sys/class/scsi_generic").exists(),
            "device": in_update_mode(), "cable": cable, "receiver": receiver,
            "job": st.job.snapshot() if st.job else None,
            "installed_version": st.installed_version, "identity": st.identity if download else None,
            "stay": st.stay,
            "backups": [{"name": b.name, "created": b.created, "sha256": b.sha256, "size": b.size,
                         "chip_key": f"{b.chip_key:#06x}", "jedec_id": b.jedec_id} for b in backups()],
        })

    @routes.post("/api/update/enter")
    async def enter(request):
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        device = d.get("target", "controller")
        if device not in TARGET:
            return fail("target must be controller or receiver")
        paths = [x.path for x in find_usb() if (x.pid == RECEIVER_PID) == (device == "receiver")]
        if len(paths) != 1:
            return fail("connect exactly one Nova 2 Lite by cable, in PC/Xbox mode (Home light green)"
                        if device == "controller" else
                        "plug the 2.4 GHz receiver in, with the controller on and in PC/Xbox mode (light green)")
        path = paths[0]
        try:
            async with session.lock:
                # Read the version while it still answers (through the receiver: the receiver's own),
                # then free the device node.
                if session.connected and session.address == path:
                    info = await session.client.info()
                    await session.disconnect()
                else:
                    await session.disconnect()
                    async with hid.connect(path) as transport:
                        info = await NovaClient(transport).info()
        except (hid.HidError, ProtocolError, asyncio.TimeoutError) as e:
            return fail(f"the {device} did not answer: {e}", 422)
        st.installed_version = int(info.firmware_version) if info.firmware_version.isdigit() else None
        st.identity = None
        name = "Controller" if device == "controller" else "Receiver"

        def work(job: Job):
            admin_helper(job)
            job.add(f"{name} on firmware {info.firmware_version}. Sending the update-mode command.")
            job.progress("Waiting for update mode", 0, 1)
            dev = updatemode.enter(path)
            job.add(f"In update mode as {dev.vendor} {dev.product}.")
            job.progress("Identifying the chip", 0, 1)
            return with_device(identify)(job)

        return ok(st.start("enter", work).snapshot())

    @routes.post("/api/update/reboot")
    async def reboot(request):
        """Restart the connected controller (by cable) or receiver, through update mode and straight
        out again. Nothing is erased or written; it takes a few seconds."""
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        if not hid.MACOS and not Path("/sys/class/scsi_generic").exists():
            return fail("restarting needs the sg kernel module; see Setup and help", 409)
        path = session.address if session.mode == "usb" else None
        if path is None:
            return fail("connect the controller by cable or through the receiver first", 409)
        device = "receiver" if session.via == "dongle" else "controller"
        async with session.lock:
            await session.disconnect()

        def work(job: Job):
            admin_helper(job)
            job.progress("Switching to update mode", 0, 3)
            updatemode.enter(path)
            job.progress("Restarting", 1, 3)
            dev, boot = updatemode.open_device()
            try:
                updatemode.restart(boot, st.loader_cache)
            finally:
                dev.close()
                admin_helper_done()
            job.progress("Waiting for it to come back", 2, 3)
            updatemode.wait_for_normal_mode(25, receiver=device == "receiver")
            job.add(f"The {device} restarted.")
            return {"device": device}
        return ok(st.start("reboot", work).snapshot())

    @routes.post("/api/update/identify")
    async def do_identify(_):
        return ok(st.start("identify", with_device(identify)).snapshot())

    @routes.post("/api/update/backup")
    async def do_backup(_):
        def work(job: Job, boot):
            ident = jlboot.start(boot, st.loader_cache)
            st.identity = _identity(ident)
            image = updatemode.backup(boot, ident, st.backup_dir, None, job.add, job.progress)
            return {"backup": image.name}
        return ok(st.start("backup", with_device(work)).snapshot())

    async def read_installed_version() -> None:
        """The firmware version of the controller connected by cable, before switching (macOS's
        one-go actions check and install without a separate switch step)."""
        if st.installed_version is not None or not (session.connected and session.mode == "usb"):
            return
        try:
            async with session.lock:
                info = await session.client.info()
        except (hid.HidError, ProtocolError, asyncio.TimeoutError):
            return
        if session.via != "dongle" and info.firmware_version.isdigit():
            st.installed_version = int(info.firmware_version)

    @routes.post("/api/update/check")
    async def check(request):
        """Dry run, no device I/O: would this firmware go onto the device in this backup?"""
        d = await body(request)
        device = in_update_mode()
        if device is None and hid.MACOS:          # macOS checks before switching (see /run)
            device = "controller"
            await read_installed_version()
        fw, b = firmware_named(d.get("file"), device), backup_named(d.get("backup"))
        errors, warnings = [], []
        if st.installed_version is None:
            errors.append("the firmware version on it is unknown; connect it normally first")
        else:
            try:
                dump, key = safety.load_backup(b.path)
                pid, vid = jlflash.identity(dump, jlflash.find(dump, key).base)
            except (OSError, ValueError, KeyError, jlflash.FlashError) as e:
                return ok({"allowed": False, "errors": [f"cannot use the backup: {e}"], "warnings": []})
            state = safety.DeviceState(TARGET[device], pid, vid, st.installed_version)
            plan = safety.plan_update(state, fw, fw_dir / fw.file, bundled=installer.load_manifest(fw_dir))
            # The same version again is refused unless asked for: say whether that's the only reason.
            again = safety.plan_update(state, fw, fw_dir / fw.file, bundled=installer.load_manifest(fw_dir),
                                       reinstall=True)
            errors += plan.errors
            warnings += plan.warnings
        choice = safety.choose_flash_image(fw_dir / fw.file, b.path)
        errors += choice.errors
        reinstall = bool(errors) and st.installed_version is not None and not again.errors and not choice.errors
        return ok({"allowed": not errors, "errors": errors, "warnings": warnings, "version": fw.version,
                   "reinstall": reinstall,
                   "partition": choice.partition, "start": choice.start, "end": choice.end})

    @routes.post("/api/update/run")
    async def run_all(request):
        """macOS: one action in one go. On a Mac the controller leaves update mode by itself soon
        after nothing is sent to it, so separate clicks race against it: password (the helper),
        switch, identify, the step, restart. A write that fails stops before the restart."""
        d = await body(request)
        action = d.get("action")
        if action not in ("backup", "install", "restore"):
            return fail("action must be backup, install or restore")
        if action != "backup" and d.get("confirm") is not True:
            return fail("confirmation required")
        paths = [x.path for x in find_usb() if x.link == "cable"]
        if len(paths) != 1:
            return fail("connect exactly one Nova 2 Lite by cable, in PC/Xbox mode (Home light green)")
        path = paths[0]
        fw = firmware_named(d.get("file"), "controller") if action == "install" else None
        b = backup_named(d.get("backup")) if action != "backup" else None
        try:
            async with session.lock:
                if session.connected and session.address == path:
                    info = await session.client.info()
                    await session.disconnect()
                else:
                    await session.disconnect()
                    async with hid.connect(path) as transport:
                        info = await NovaClient(transport).info()
        except (hid.HidError, ProtocolError, asyncio.TimeoutError) as e:
            return fail(f"the controller did not answer: {e}", 422)
        st.installed_version = int(info.firmware_version) if info.firmware_version.isdigit() else None
        if action == "install" and st.installed_version is None:
            return fail("the controller didn't say which firmware it has; can't check the update")
        version = st.installed_version
        reinstall = d.get("reinstall") is True

        def work(job: Job):
            admin_helper(job)
            job.add(f"Controller on firmware {info.firmware_version}. Sending the update-mode command.")
            job.progress("Waiting for update mode", 0, 1)
            updatemode.enter(path)
            dev, boot = updatemode.open_device()
            restarted = False
            try:
                identify(job, boot)
                result: dict = {}
                if action == "backup":
                    ident = jlboot.start(boot, st.loader_cache)
                    image = updatemode.backup(boot, ident, st.backup_dir, None, job.add, job.progress)
                    result["backup"] = image.name
                elif action == "restore":
                    updatemode.restore(boot, b.path, st.loader_cache, job.add, job.progress)
                    result["backup"] = b.name
                else:
                    result["version"] = updatemode.flash(boot, fw_dir / fw.file, b.path, version, fw_dir,
                                                         reinstall=reinstall, cache_dir=st.loader_cache, log=job.add,
                                                         progress=job.progress)
                    st.installed_version = result["version"]
                job.progress("Restarting", 0, 1)
                updatemode.restart(boot, st.loader_cache)
                restarted = True
            finally:
                dev.close()
                if restarted:
                    admin_helper_done()
                # else: a write failed; the helper keeps the controller so it can be run again
            found = updatemode.wait_for_normal_mode(25)
            job.add("Back in normal mode" + (f" on {found[0].path}." if found else "."))
            st.identity = None
            return {**result, "restarted": True}
        return ok(st.start(action, work, one_go=True).snapshot())

    @routes.post("/api/update/restore")
    async def do_restore(request):
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        b = backup_named(d.get("backup"))

        def work(job: Job, boot):
            updatemode.restore(boot, b.path, st.loader_cache, job.add, job.progress)
            return {"backup": b.name}
        return ok(st.start("restore", with_device(work)).snapshot())

    @routes.post("/api/update/install")
    async def do_install(request):
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        fw, b = firmware_named(d.get("file"), in_update_mode()), backup_named(d.get("backup"))
        if st.installed_version is None:
            return fail("the firmware version on it is unknown; connect it normally first")
        version = st.installed_version
        reinstall = d.get("reinstall") is True        # the same version again, asked for on the page

        def work(job: Job, boot):
            new = updatemode.flash(boot, fw_dir / fw.file, b.path, version, fw_dir, reinstall=reinstall,
                                   cache_dir=st.loader_cache, log=job.add, progress=job.progress)
            st.installed_version = new
            return {"version": new}
        return ok(st.start("install", with_device(work)).snapshot())

    @routes.post("/api/update/restart")
    async def do_restart(_):
        if st.stay:
            return fail("the last write did not verify; run it again before restarting", 409)

        device = in_update_mode()

        def work(job: Job):
            admin_helper(job)
            dev, boot = updatemode.open_device()
            try:
                job.progress("Restarting", 0, 1)
                updatemode.restart(boot, st.loader_cache)
            finally:
                dev.close()
                admin_helper_done()
            job.add(f"Asked the {device or 'device'} to restart.")
            found = updatemode.wait_for_normal_mode(20, receiver=device == "receiver")
            job.add("Back in normal mode" + (f" on {found[0].path}." if found else "."))
            st.identity = None
            return {"path": found[0].path if found else None, "device": device}
        return ok(st.start("restart", work).snapshot())

    return st
