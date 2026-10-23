import pytest

from gamesir_linux import gcm
from gamesir_linux.gcm import DeadZone, Feature, ProtocolError, Vibration


# Frames that appear verbatim in the Android app (BluetoothLeService static
# initialiser): our builders must produce exactly these bytes.
def test_matches_app_constants():
    assert gcm.basic_info_query() == bytes([1, 1])                      # BluetoothLeService.X
    assert gcm.host_info_query() == bytes([0x24, 0x01, 0x04, 0x29])     # BluetoothLeService.Y


def test_dead_zone_frames():
    assert gcm.dead_zone_query(DeadZone.LEFT_STICK_INNER).hex(" ") == "07 01 06 00 00 0e"
    assert gcm.dead_zone_set(DeadZone.LEFT_STICK_INNER, 5).hex(" ") == "07 01 06 01 05 14"
    assert gcm.dead_zone_set(DeadZone.RIGHT_STICK_ANTI, 0).hex(" ") == "07 0a 06 01 00 18"


def test_feature_frames():
    assert gcm.feature_query(Feature.DPAD_DIAGONAL_LOCK).hex(" ") == "23 01 06 00 00 2a"
    assert gcm.feature_set(Feature.SWAP_RT_RB, True).hex(" ") == "23 16 06 01 01 41"


def test_other_frames():
    q = gcm.vibration_query()
    assert len(q) == 13 and q[:4] == bytes([0x25, 1, 13, 0]) and q[-1] == 0x33
    assert gcm.restore_all().hex(" ") == "15 01 04 1a"
    assert gcm.auto_sleep_set(10).hex(" ") == "26 01 06 01 0a 38"


def test_every_frame_is_self_consistent():
    frames = [gcm.dead_zone_set(z, 7) for z in DeadZone] + [gcm.feature_query(f) for f in Feature]
    frames += [gcm.vibration_set(Vibration(1, 2, True, False, 3, 4, False, True)),
               gcm.host_info_query(), gcm.restore_all(), gcm.auto_sleep_query()]
    for f in frames:
        assert f[2] == len(f), f.hex(" ")
        assert f[-1] == sum(f[:-1]) & 0xFF


def test_values_are_range_checked():
    with pytest.raises(ValueError):
        gcm.dead_zone_set(DeadZone.LEFT_STICK_INNER, 256)
    with pytest.raises(ValueError):
        gcm.vibration_set(Vibration(-1, 0, False, False, 0, 0, False, False))


def test_verify_rejects_bad_replies():
    q = gcm.dead_zone_query(DeadZone.LEFT_STICK_INNER)
    good = gcm.seal(bytes([7, 1, 6, 0, 9, 0]))
    assert gcm.dead_zone_parse(q, good) == 9
    with pytest.raises(ProtocolError, match="checksum"):
        gcm.dead_zone_parse(q, good[:-1] + b"\x00")
    with pytest.raises(ProtocolError, match="command"):
        gcm.dead_zone_parse(q, gcm.seal(bytes([0x23, 1, 6, 0, 9, 0])))
    with pytest.raises(ProtocolError, match="short"):
        gcm.dead_zone_parse(q, gcm.seal(bytes([7, 1, 0])))
    with pytest.raises(ProtocolError, match="empty"):
        gcm.verify(q, b"")


def test_vibration_roundtrip():
    v = Vibration(10, 20, True, False, 30, 40, False, True)
    frame = gcm.vibration_set(v)
    assert gcm.vibration_parse(frame, frame) == v


def test_basic_info_both_reply_forms():
    body = bytes([0xC0, 0xFF, 0xEE, 0x12, 0x34, 0x56, 0x01, 0x01, 0x05, 0x00,
                  0x82, 0x01, 0xEA, 0x07, 3, 14, 0x01, 0x02, 0x00])
    for reply in (body, b"\xc9" + body):
        info = gcm.basic_info_parse(reply)
        assert info.firmware_version == "8201"
        assert info.mac == "C0:FF:EE:12:34:56"
        assert info.production_date == "2026/03/14"
        assert info.gcm_version == "2.1"
    with pytest.raises(ProtocolError):
        gcm.basic_info_parse(b"\x01\x01\x00")


def test_battery_bits():
    b = gcm.Battery(0x80 | 76)
    assert (b.level, b.charging) == (76, True)
    assert (gcm.Battery(55).level, gcm.Battery(55).charging) == (55, False)


# -- second batch: mapping, quick trigger, profiles, lighting ----------------------
from gamesir_linux.gcm import Key


def test_mapping_frames():
    # m(0, id=1, ...) -> [0x1F, 0, 1, 0, 0, sum]: no length byte in this command.
    assert gcm.mapping_query(Key.L4).hex(" ") == "1f 00 01 00 00 20"
    # p(L4 -> A): scan = 436 & 0xFF = 0xB4, flag 0
    assert gcm.mapping_set(Key.L4, Key.A).hex(" ") == "1f 01 01 b4 00 d5"
    # p(R4 -> 255) disables: scan 0, flag 0xFF
    assert gcm.mapping_set(Key.R4, None).hex(" ") == "1f 01 02 00 ff 21"
    assert gcm.mapping_reset(Key.R4).hex(" ") == "1f 02 02 00 00 23"


def test_mapping_parse_kinds():
    q = gcm.mapping_query(Key.L4)
    seal = gcm.seal
    assert gcm.mapping_parse(q, seal(bytes([0x1F, 0, 1, 0xB4, 0, 0]))) == gcm.Mapping(Key.L4, "key", (Key.A,))
    assert gcm.mapping_parse(q, seal(bytes([0x1F, 0, 1, 0xB4, 0xB5, 0]))).targets == (Key.A, Key.B)
    assert gcm.mapping_parse(q, seal(bytes([0x1F, 0, 1, 0, 0xFF, 0]))).kind == "disabled"
    assert gcm.mapping_parse(q, seal(bytes([0x1F, 0, 1, 0, 0xFE, 0]))).kind == "macro"
    kb = gcm.mapping_parse(q, seal(bytes([0x1F, 0, 1, 0x04, 0x01, 0])))
    assert kb.kind == "keyboard" and kb.targets == (0x204,)
    with pytest.raises(ProtocolError):
        gcm.mapping_parse(q, seal(bytes([0x1F, 0, 99, 0, 0, 0])))


def test_scan_codes_match_recovered_table():
    # Values produced by running KeyCodeUtil.getGcmScanCodeByHidKeyCode's bytecode.
    assert [gcm.scan_code(k) for k in (Key.HOME, Key.A, Key.L4, Key.LS_LEFT)] == [0xC2, 0xB4, 0xCB, 0xEF]


def test_trigger_quick_frames():
    assert gcm.trigger_quick_query().hex(" ") == "21 00 01 01 23"
    assert gcm.trigger_quick_set(True, False).hex(" ") == "21 01 01 02 25"
    q = gcm.trigger_quick_query()
    assert gcm.trigger_quick_parse(q, gcm.seal(bytes([0x21, 0, 1, 2, 0]))) == (True, False)


def test_slot_frames():
    assert gcm.slot_query().hex(" ") == "29 01 05 00 2f"
    assert gcm.slot_switch(2).hex(" ") == "29 01 06 01 02 33"
    q = gcm.slot_query()
    assert gcm.slot_query_parse(q, gcm.seal(bytes([0x29, 1, 7, 0, 3, 1, 0]))) == (3, 1)


def test_light_frames():
    assert gcm.light_query().hex(" ") == "05 08 06 00 c2 d5"
    f = gcm.light_set((255, 0, 128))
    assert f.hex(" ") == "05 08 0a 01 c2 ff 00 80 01 " + f"{sum(f[:-1]) & 0xFF:02x}"
    assert len(f) == f[2] == 10
    light = gcm.light_parse(gcm.light_query(), gcm.seal(f[:3] + b"\x00" + f[4:]))
    assert light.rgb == (255, 0, 128) and light.effect == gcm.EFFECT_SOLID and light.key == Key.HOME
