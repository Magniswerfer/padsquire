# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adding a controller: identification by usage page only, and the tests that unlock settings."""
import asyncio

import pytest

from padsquire import addcontroller, c2, c2usb
from padsquire.client import NovaClient
from padsquire.simulator import SimulatedC2, simulated


def run(coro):
    return asyncio.run(coro)


def test_identify_only_asks_interfaces_of_a_family_it_speaks():
    asked = []

    async def gcm(path):
        asked.append(("gcm", path))
        raise TimeoutError("no reply")

    async def cyclone(path):
        asked.append(("c2", path))
        return {"family": "c2", "firmware": "1.2.3"}
    dev = {"usb_id": "3537:7777", "interfaces": [{"path": "/dev/hidraw1", "usage_page": 0x01},
                                                {"path": "/dev/hidraw2", "usage_page": 0xFF7A},
                                                {"path": "/dev/hidraw3", "usage_page": 0xFFF0}]}
    found = run(addcontroller.identify(dev, {"gcm": gcm, "c2": cyclone}))
    assert asked == [("gcm", "/dev/hidraw2"), ("c2", "/dev/hidraw3")]
    assert (found["family"], found["path"]) == ("c2", "/dev/hidraw3")
    with pytest.raises(addcontroller.AddError, match="no settings channel"):
        run(addcontroller.identify({"usb_id": "3537:7777", "interfaces": [{"path": "/dev/hidraw1", "usage_page": 1}]}))


@pytest.mark.parametrize("group", sorted(addcontroller.GROUPS["gcm"]))
def test_every_gcm_test_passes_and_puts_the_value_back(group):
    pad = simulated("cable")
    nova = NovaClient(pad)
    before = (dict(pad.dead_zones), pad.vibration, pad.quick_trigger, pad.light, dict(pad.features))
    run(addcontroller.test_gcm(nova, group))
    assert (pad.dead_zones, pad.vibration, pad.quick_trigger, pad.light, pad.features) == before


@pytest.mark.parametrize("group", sorted(addcontroller.GROUPS["c2"]))
def test_every_cyclone_test_passes_and_puts_the_value_back(group):
    sim = SimulatedC2()
    before = sim.profiles[1]
    dev = c2usb.C2Client(c2usb.C2Transport(sim.read, sim.write, writes=True))
    run(addcontroller.test_c2(dev, group))
    assert sim.profiles[1] == before


def test_a_controller_that_does_not_keep_the_change_fails_its_test():
    sim = SimulatedC2()
    sim.write = (lambda real: lambda report: 64 if report[1] == c2.Cmd.WRITE_PROFILE else real(report))(sim.write)
    dev = c2usb.C2Client(c2usb.C2Transport(sim.read, sim.write, writes=True))
    with pytest.raises((addcontroller.AddError, c2usb.C2UsbError)):
        run(addcontroller.test_c2(dev, "report_rate"))


def test_keep_test_and_remove(tmp_path):
    f = tmp_path / "added.json"
    entry = addcontroller.save({"usb_id": "3537:7777", "family": "gcm", "firmware": "1234"}, "My pad", path=f)
    assert not addcontroller.unlocked(entry, "vibration") and addcontroller.unlocked(None, "vibration")
    entry = addcontroller.record_test(0x7777, "vibration", "works", path=f)
    assert addcontroller.unlocked(entry, "vibration")
    addcontroller.save({"usb_id": "3537:7777", "family": "gcm"}, "Renamed", path=f)    # once per ID
    addcontroller.remove(0x7777, path=f)
    with pytest.raises(addcontroller.AddError):
        addcontroller.record_test(0x7777, "vibration", "works", path=f)
