# SPDX-License-Identifier: AGPL-3.0-or-later
"""The simulator against what PROTOCOL.md records from the real controller."""
import asyncio

import pytest

from padsquire import gcm, hid
from padsquire.client import NotApplied, NovaClient
from padsquire.gcm import DeadZone, Key, Vibration
from padsquire.simulator import SimulatedNova, SimulatedSticks, SimulatedUsbNova, simulated


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("asked,stored", [(1, 0), (24, 0), (26, 25), (99, 75), (150, 150), (200, 200), (255, 250)])
def test_grip_strength_is_rounded_down_to_25(asked, stored):
    sim = simulated("cable")
    reply = sim.handle(gcm.vibration_set(Vibration(asked, 0, False, False, 0, 0, False, False)))
    assert gcm.vibration_parse(reply, reply).left_grip == stored


def test_no_trigger_motors():
    sim = simulated("bluetooth")
    sim.handle(gcm.vibration_set(Vibration(50, 75, False, False, 50, 75, False, False)))
    assert (sim.vibration.left_trigger, sim.vibration.right_trigger) == (0, 0)


def test_links():
    assert type(simulated("bluetooth")) is SimulatedNova
    assert simulated("receiver").link == "receiver" and isinstance(simulated("cable"), SimulatedUsbNova)
    with pytest.raises(ValueError):
        simulated("wifi")


def test_bluetooth_has_no_reportless_send():
    # BleTransport has no send(), so the vibration test isn't offered over Bluetooth
    with pytest.raises(gcm.ProtocolError):
        run(NovaClient(simulated("bluetooth")).test_vibration(50, 0, seconds=0))


@pytest.mark.parametrize("link", ["cable", "receiver"])
def test_usb_gets_no_reply_to_sleep_or_profiles(link):
    nova = NovaClient(simulated(link))
    with pytest.raises(hid.HidError):
        run(nova.auto_sleep())
    with pytest.raises(hid.HidError):
        run(nova.slots())


@pytest.mark.parametrize("feature", sorted(gcm.USB_READONLY_FEATURES))
def test_usb_refuses_six_toggles(feature):
    with pytest.raises(NotApplied):
        run(NovaClient(simulated("cable")).set_feature(feature, True))
    run(NovaClient(simulated("bluetooth")).set_feature(feature, True))     # untested on hardware: allowed


def test_cable_basic_info_is_the_19_byte_form():
    sim = simulated("cable")
    reply = sim.handle(gcm.basic_info_query())
    assert len(reply) == 19 and reply[18] == 0xFF                           # 255 = charging, on the cable
    assert run(NovaClient(sim).info()).firmware_version == "8201"


def test_receiver_reports_its_own_firmware_and_the_controllers_in_a_state_report():
    nova = NovaClient(simulated("receiver"))
    assert run(nova.info()).firmware_version == "2800"
    assert run(nova.controller_firmware()) == "8201"
    with pytest.raises(gcm.ProtocolError):
        run(NovaClient(simulated("bluetooth")).controller_firmware())


def test_usb_back_button_command_reads_unassigned_and_changes_nothing():
    nova = NovaClient(simulated("cable"))
    assert run(nova.mapping(Key.L4)).targets == ()                          # scan 0, flag 0
    with pytest.raises(NotApplied):
        run(nova.set_mapping(Key.L4, Key.A))


def test_usb_remaps_refuse_stick_directions():
    nova = NovaClient(simulated("receiver"))
    assert run(nova.set_remap(Key.A, [Key.B, Key.X])) == (Key.B, Key.X)
    with pytest.raises(NotApplied):
        run(nova.set_remap(Key.A, [Key.LS_UP]))


def test_restore_keeps_the_link():
    sim = simulated("cable")
    sim.dead_zones[DeadZone.LEFT_STICK_INNER] = 20
    run(NovaClient(sim).restore_defaults())
    assert sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 5 and sim.link == "cable"
    assert sim.mappings[Key.L4] == (0, 0)


def test_simulated_sticks_come_out_after_the_dead_zones():
    sim = simulated("cable")
    sticks = SimulatedSticks(sim, seed=1)
    st = sticks.state()
    assert (st["lx"], st["ly"]) == (0, 0)                                  # the 5% inner dead zone hides the drift
    sim.dead_zones[DeadZone.LEFT_STICK_INNER] = 0
    st = sticks.state()
    assert 0.02 < st["lx"] < 0.04 and st["ly"] < 0                          # about (0.03, -0.012)
    assert set(st) == {"lx", "ly", "rx", "ry", "lt", "rt", "pressed"} and sticks.fd is None


def test_simulated_cyclone2_answers_reads_and_streams_state_after_a_heartbeat():
    from padsquire import c2
    from padsquire.simulator import SimulatedC2
    sim = SimulatedC2(battery=55)
    assert sim.read(0) is None                       # silent until the heartbeat
    sim.write(c2.pad(c2.heartbeat()))
    assert c2.parse_state(sim.read(0)).battery == 55
    sim.write(c2.pad(c2.read_profile(c2.LIGHT_PROFILE)[0]))
    reply = sim.read(0)
    assert c2.reply_type(reply) == c2.Cmd.READ_PROFILE_ACK and reply[2:6] == bytes([32, 0, 0, 58])
