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

    def __init__(self, kind: str):
        self.id = next(self._ids)
        self.kind = kind
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
                "error": self.error, "stay": self.stay}


class UpdateState:
    def __init__(self, backup_dir: Path, loader_cache: Path):
        self.backup_dir, self.loader_cache = backup_dir, loader_cache
        self.job: Job | None = None
        self.installed_version: int | None = None    # read from the controller before entering
        self.identity: dict | None = None
        self.stay = False                            # a write failed; restarting is refused

    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, kind: str, work: Callable[[Job], object]) -> Job:
        if self.busy():
            raise web.HTTPConflict(text='{"ok": false, "error": "another step is still running"}',
                                   content_type="application/json")
        job = self.job = Job(kind)

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

    def firmware_named(name):
        try:
            bundled = installer.load_manifest(fw_dir)
        except (OSError, KeyError, ValueError):
            bundled = []
        match = next((f for f in bundled if f.file == name and f.target == "controller"), None)
        if match is None:
            raise web.HTTPBadRequest(text='{"ok": false, "error": "no such firmware file"}',
                                     content_type="application/json")
        return match

    def with_device(work):
        """Open the download-mode disk for one job and close it afterwards."""
        def run(job: Job):
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

    @routes.get("/api/update")
    async def status(_):
        download = jlboot.find()
        cable = [d.path for d in find_usb() if d.link == "cable"]
        mode = "download" if download else "normal" if cable else "none"
        return ok({
            "mode": mode, "sg_missing": not Path("/sys/class/scsi_generic").exists(),
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
        cables = [x.path for x in find_usb() if x.link == "cable"]
        if len(cables) != 1:
            return fail("connect exactly one Nova 2 Lite by cable, in PC/Xbox mode (Home light green)")
        path = cables[0]
        try:
            async with session.lock:
                # Read the firmware version while the controller still answers, then free the cable.
                if session.connected and session.mode == "usb":
                    info = await session.client.info()
                    await session.disconnect()
                else:
                    await session.disconnect()
                    async with hid.connect(path) as transport:
                        info = await NovaClient(transport).info()
        except (hid.HidError, ProtocolError, asyncio.TimeoutError) as e:
            return fail(f"the controller did not answer: {e}", 422)
        st.installed_version = int(info.firmware_version) if info.firmware_version.isdigit() else None
        st.identity = None

        def work(job: Job):
            job.add(f"Controller on firmware {info.firmware_version}. Sending the update-mode command.")
            job.progress("Waiting for update mode", 0, 1)
            dev = updatemode.enter(path)
            job.add(f"In update mode as {dev.vendor} {dev.product}.")
            job.progress("Identifying the chip", 0, 1)
            return with_device(identify)(job)

        return ok(st.start("enter", work).snapshot())

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

    @routes.post("/api/update/check")
    async def check(request):
        """Dry run, no device I/O: would this firmware go onto the controller in this backup?"""
        d = await body(request)
        fw, b = firmware_named(d.get("file")), backup_named(d.get("backup"))
        errors, warnings = [], []
        if st.installed_version is None:
            errors.append("the firmware version on the controller is unknown; connect it normally first")
        else:
            try:
                dump, key = safety.load_backup(b.path)
                pid, vid = jlflash.identity(dump, jlflash.find(dump, key).base)
            except (OSError, ValueError, KeyError, jlflash.FlashError) as e:
                return ok({"allowed": False, "errors": [f"cannot use the backup: {e}"], "warnings": []})
            plan = safety.plan_update(safety.DeviceState("controller", pid, vid, st.installed_version),
                                      fw, fw_dir / fw.file, bundled=installer.load_manifest(fw_dir))
            errors += plan.errors
            warnings += plan.warnings
        choice = safety.choose_flash_image(fw_dir / fw.file, b.path)
        errors += choice.errors
        return ok({"allowed": not errors, "errors": errors, "warnings": warnings, "version": fw.version,
                   "partition": choice.partition, "start": choice.start, "end": choice.end})

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
        fw, b = firmware_named(d.get("file")), backup_named(d.get("backup"))
        if st.installed_version is None:
            return fail("the firmware version on the controller is unknown; connect it normally first")
        version = st.installed_version

        def work(job: Job, boot):
            new = updatemode.flash(boot, fw_dir / fw.file, b.path, version, fw_dir,
                                   cache_dir=st.loader_cache, log=job.add, progress=job.progress)
            st.installed_version = new
            return {"version": new}
        return ok(st.start("install", with_device(work)).snapshot())

    @routes.post("/api/update/restart")
    async def do_restart(_):
        if st.stay:
            return fail("the last write did not verify; run it again before restarting", 409)

        def work(job: Job):
            dev, boot = updatemode.open_device()
            try:
                job.progress("Restarting", 0, 1)
                updatemode.restart(boot, st.loader_cache)
            finally:
                dev.close()
            job.add("Asked the controller to restart.")
            found = updatemode.wait_for_normal_mode(20)
            job.add(f"Back in normal mode on {found[0].path}.")
            st.identity = None
            return {"path": found[0].path}
        return ok(st.start("restart", work).snapshot())

    return st
