# SPDX-License-Identifier: AGPL-3.0-or-later
"""Save the controller's settings to a file and load them back.

The file is JSON (format "padsquire-settings", version 1), names everything by its gcm name
(DeadZone, Feature, Key), and holds only what was read from the controller. Loading checks the
whole file first, then writes only what differs, each setting written and read back by
NovaClient; a setting this connection can't change is skipped and said so.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, fields

from . import gcm
from .gcm import DeadZone, Feature, Key, Vibration

FORMAT = "padsquire-settings"
VERSION = 1
VIBRATION_FIELDS = [f.name for f in fields(Vibration)]


# Labels as the settings app words them.
ZONE_LABELS = {
    DeadZone.LEFT_STICK_INNER: "Left stick inner dead zone", DeadZone.RIGHT_STICK_INNER: "Right stick inner dead zone",
    DeadZone.LEFT_STICK_OUTER: "Left stick outer dead zone", DeadZone.RIGHT_STICK_OUTER: "Right stick outer dead zone",
    DeadZone.LEFT_STICK_ANTI: "Left stick anti-dead zone", DeadZone.RIGHT_STICK_ANTI: "Right stick anti-dead zone",
    DeadZone.LEFT_TRIGGER_FRONT: "Left trigger start dead zone", DeadZone.RIGHT_TRIGGER_FRONT: "Right trigger start dead zone",
    DeadZone.LEFT_TRIGGER_BACK: "Left trigger full press point", DeadZone.RIGHT_TRIGGER_BACK: "Right trigger full press point",
}
FEATURE_LABELS = {
    Feature.DPAD_DIAGONAL_LOCK: "Lock D-pad diagonals", Feature.LOW_BATTERY_WARNING: "Low battery warning",
    Feature.SWAP_STICKS: "Swap left and right stick", Feature.SWAP_LEFT_STICK_DPAD: "Swap left stick and D-pad",
    Feature.INVERT_STICKS_XY: "Swap stick axes", Feature.INVERT_LEFT_X: "Invert left stick horizontal",
    Feature.INVERT_LEFT_Y: "Invert left stick vertical", Feature.INVERT_RIGHT_X: "Invert right stick horizontal",
    Feature.INVERT_RIGHT_Y: "Invert right stick vertical", Feature.LEFT_STICK_RAW: "Left stick raw output",
    Feature.RIGHT_STICK_RAW: "Right stick raw output", Feature.PLAY_WHILE_CHARGING: "Play while charging",
    Feature.AUTO_POWER_ON_DOCK: "Turn on when lifted from the dock", Feature.SWAP_LT_LB: "Swap LB and LT",
    Feature.SWAP_RT_RB: "Swap RB and RT",
}


class InvalidFile(ValueError):
    """The file isn't a settings file this version can load; nothing was written."""


def _key(name) -> Key:
    try:
        return Key[name]
    except (KeyError, TypeError):
        raise InvalidFile(f"unknown button {name!r}") from None


def _name(code: int) -> str | None:
    try:
        return Key(code).name
    except ValueError:
        return None


def _byte(v, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 255:
        raise InvalidFile(f"{what} must be a whole number 0-255")
    return v


async def read(nova, *, remaps: bool, mappings: bool, skip: set[str] = frozenset()) -> dict:
    """Everything readable as plain values. A read that fails is left out."""
    errors = (gcm.ProtocolError, ValueError, KeyError, TimeoutError, OSError, RuntimeError)

    async def get(fn, *args):
        try:
            return await fn(*args)
        except errors:
            return None

    s: dict = {}
    zones = {z.name: v for z in DeadZone if (v := await get(nova.dead_zone, z)) is not None}
    if zones:
        s["dead_zones"] = zones
    feats = {f.name: v for f in Feature if (v := await get(nova.feature, f)) is not None}
    if feats:
        s["features"] = feats
    if (v := await get(nova.vibration)) is not None:
        s["vibration"] = asdict(v)
    if "auto_sleep" not in skip and (v := await get(nova.auto_sleep)) is not None:
        s["auto_sleep"] = v
    if (v := await get(nova.trigger_quick)) is not None:
        s["trigger_quick"] = list(v)
    if (v := await get(nova.light)) is not None:
        s["light"] = list(v.rgb)
    if remaps:
        got = {}
        for k in gcm.NOVA_REMAP_SOURCES:
            v = await get(nova.remap, k)
            names = None if v is None else [_name(t) for t in v]
            if names is not None and None not in names:      # a code we don't know can't be saved
                got[k.name] = names
        if got:
            s["remaps"] = got
    if mappings:
        got = {}
        for k in gcm.NOVA_REMAPPABLE:
            m = await get(nova.mapping, k)
            if m is None:
                continue
            target = _name(m.targets[0]) if m.kind == "key" and m.targets else None
            if m.kind == "key" and target is None:
                continue
            got[k.name] = {"kind": m.kind, "target": target}
        if got:
            s["mappings"] = got
    return s


def document(settings: dict, *, app_version: str, firmware: str | None, now: float | None = None) -> dict:
    return {
        "format": FORMAT, "version": VERSION,
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "app": f"PadSquire {app_version}",
        "controller": {"model": "Nova 2 Lite", "firmware": firmware},
        "settings": settings,
    }


def parse(doc) -> dict:
    """Check a whole file and return its settings with gcm types. Raises InvalidFile."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise InvalidFile("this isn't a PadSquire settings file")
    if doc.get("version") != VERSION:
        raise InvalidFile(f"settings file version {doc.get('version')!r}; this PadSquire reads version {VERSION}")
    s = doc.get("settings")
    if not isinstance(s, dict):
        raise InvalidFile("the file has no settings")
    out: dict = {}
    for name, v in _dict(s, "dead_zones").items():
        try:
            zone = DeadZone[name]
        except KeyError:
            raise InvalidFile(f"unknown dead zone {name!r}") from None
        _byte(v, f"dead zone {name}")
        if zone in gcm.OUTER_ZONES and v > gcm.OUTER_DEAD_ZONE_MAX:
            raise InvalidFile(f"{name} is {v}%; above {gcm.OUTER_DEAD_ZONE_MAX}% the controller's firmware gets full tilt wrong")
        out.setdefault("dead_zones", {})[zone] = v
    for name, v in _dict(s, "features").items():
        try:
            feature = Feature[name]
        except KeyError:
            raise InvalidFile(f"unknown setting {name!r}") from None
        if not isinstance(v, bool):
            raise InvalidFile(f"{name} must be true or false")
        out.setdefault("features", {})[feature] = v
    if "vibration" in s:
        v = _dict(s, "vibration")
        if set(v) != set(VIBRATION_FIELDS):
            raise InvalidFile("vibration needs " + ", ".join(VIBRATION_FIELDS))
        for k in VIBRATION_FIELDS:
            if k.endswith(("_grip", "_trigger")):
                _byte(v[k], f"vibration {k}")
            elif not isinstance(v[k], bool):
                raise InvalidFile(f"vibration {k} must be true or false")
        out["vibration"] = Vibration(**{k: v[k] for k in VIBRATION_FIELDS})
    if "auto_sleep" in s:
        out["auto_sleep"] = _byte(s["auto_sleep"], "auto sleep")
    if "trigger_quick" in s:
        q = s["trigger_quick"]
        if not (isinstance(q, list) and len(q) == 2 and all(isinstance(x, bool) for x in q)):
            raise InvalidFile("trigger_quick must be [left, right], each true or false")
        out["trigger_quick"] = tuple(q)
    if "light" in s:
        rgb = s["light"]
        if not (isinstance(rgb, list) and len(rgb) == 3):
            raise InvalidFile("light must be [red, green, blue]")
        out["light"] = tuple(_byte(c, "light colour") for c in rgb)
    for name, targets in _dict(s, "remaps").items():
        key = _key(name)
        if key not in gcm.NOVA_REMAP_SOURCES:
            raise InvalidFile(f"{name} can't be remapped")
        if not isinstance(targets, list) or len(targets) > gcm.REMAP_MAX_TARGETS:
            raise InvalidFile(f"{name} needs a list of up to {gcm.REMAP_MAX_TARGETS} buttons")
        keys = [_key(t) for t in targets]
        if len(set(keys)) != len(keys) or key in keys or any(t not in gcm.NOVA_REMAP_TARGETS for t in keys):
            raise InvalidFile(f"{name} has a button it can't act as")
        out.setdefault("remaps", {})[key] = tuple(keys)
    for name, m in _dict(s, "mappings").items():
        key = _key(name)
        if key not in gcm.NOVA_REMAPPABLE or not isinstance(m, dict):
            raise InvalidFile(f"{name} can't be mapped")
        kind = m.get("kind")
        if kind == "key":
            target = _key(m.get("target"))
            if target not in set(gcm.NOVA_TARGETS) | set(gcm.NOVA_STICK_TARGETS):
                raise InvalidFile(f"{name} can't act as {target.name}")
            out.setdefault("mappings", {})[key] = ("key", target)
        elif kind in ("disabled", "macro", "keyboard"):
            out.setdefault("mappings", {})[key] = (kind, None)
        else:
            raise InvalidFile(f"{name}: unknown kind {kind!r}")
    return out


def typed(settings: dict) -> dict:
    """What `read` returned, with gcm types, unchecked: the controller's current settings may hold
    values a file may not (set with GameSir's app, say)."""
    out: dict = {}
    if "dead_zones" in settings:
        out["dead_zones"] = {DeadZone[n]: v for n, v in settings["dead_zones"].items()}
    if "features" in settings:
        out["features"] = {Feature[n]: v for n, v in settings["features"].items()}
    if "vibration" in settings:
        out["vibration"] = Vibration(**settings["vibration"])
    for name in ("auto_sleep", "trigger_quick", "light"):
        if name in settings:
            out[name] = tuple(settings[name]) if isinstance(settings[name], list) else settings[name]
    if "remaps" in settings:
        out["remaps"] = {Key[n]: tuple(Key[t] for t in v) for n, v in settings["remaps"].items()}
    if "mappings" in settings:
        out["mappings"] = {Key[n]: (m["kind"], Key[m["target"]] if m["target"] else None)
                           for n, m in settings["mappings"].items()}
    return out


def _dict(s: dict, name: str) -> dict:
    v = s.get(name, {})
    if not isinstance(v, dict):
        raise InvalidFile(f"{name} must be an object")
    return v


def changes(wanted: dict, current: dict) -> list[dict]:
    """What loading `wanted` (from parse) would change, against `current` (from typed).
    Each change: what (a label), section, name, from, to; plus how to write it."""
    out = []

    def add(section, name, label, old, new, write):
        if old != new:
            out.append({"section": section, "name": name, "label": label, "from": _plain(old),
                        "to": _plain(new), "write": write})

    for zone, v in wanted.get("dead_zones", {}).items():
        add("dead_zones", zone.name, ZONE_LABELS[zone],
            current.get("dead_zones", {}).get(zone), v, ("set_dead_zone", zone, v))
    for feature, v in wanted.get("features", {}).items():
        add("features", feature.name, FEATURE_LABELS[feature],
            current.get("features", {}).get(feature), v, ("set_feature", feature, v))
    if "vibration" in wanted:
        add("vibration", "vibration", "Vibration", current.get("vibration"), wanted["vibration"],
            ("set_vibration", wanted["vibration"]))
    if "auto_sleep" in wanted:
        add("auto_sleep", "auto_sleep", "Sleep after", current.get("auto_sleep"), wanted["auto_sleep"],
            ("set_auto_sleep", wanted["auto_sleep"]))
    if "trigger_quick" in wanted:
        add("trigger_quick", "trigger_quick", "Quick trigger", current.get("trigger_quick"), wanted["trigger_quick"],
            ("set_trigger_quick", *wanted["trigger_quick"]))
    if "light" in wanted:
        add("light", "light", "Home light", current.get("light"), wanted["light"], ("set_light", wanted["light"]))
    for key, targets in wanted.get("remaps", {}).items():
        old = current.get("remaps", {}).get(key)
        add("remaps", key.name, f"What {key.name} does", None if old is None else tuple(sorted(old)),
            tuple(sorted(targets)), ("set_remap", key, list(targets)))
    for key, (kind, target) in wanted.get("mappings", {}).items():
        write = ("set_mapping", key, target if kind == "key" else None) if kind in ("key", "disabled") else None
        add("mappings", key.name, f"What back button {key.name} does", current.get("mappings", {}).get(key), (kind, target), write)
    return out


def _plain(v):
    if isinstance(v, Vibration):
        return asdict(v)
    if isinstance(v, Key):
        return v.name
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    return v


def unwritable(change: dict) -> str | None:
    return None if change["write"] else "a macro or keyboard key can't be set from PadSquire"


async def apply(nova, todo: list[dict], skip: Callable[[dict], str | None] = lambda c: None) -> dict:
    """Write each change (from `changes`), each read back. A failure is reported and the rest
    still written: the settings are independent of each other."""
    done, skipped, failed = [], [], []
    for c in todo:
        public = {k: v for k, v in c.items() if k != "write"}
        why = skip(c) or unwritable(c)
        if why:
            skipped.append({**public, "why": why})
            continue
        method, *args = c["write"]
        try:
            await getattr(nova, method)(*args)
            done.append(public)
        except Exception as e:                       # write-then-read-back failures, timeouts
            failed.append({**public, "error": str(e) or type(e).__name__})
    return {"applied": done, "skipped": skipped, "failed": failed}
