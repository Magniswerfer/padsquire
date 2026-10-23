# SPDX-License-Identifier: AGPL-3.0-or-later
from gamesir_linux.jlcrypt import crc16, enc, enc_blocks


def test_crc16_is_xmodem():
    assert crc16(b"123456789") == 0x31C3


def test_enc_matches_real_keystream():
    # Bytes 0x20..0x2F of GameSir's T4Nlite2 firmware: zero plaintext, so this
    # is the raw keystream for key 0xFFFF at stream position 32.
    expected = bytes.fromhex("c0a1428408314386 0c183060e1c28408".replace(" ", ""))
    assert enc(bytes(48))[32:48] == expected


def test_enc_is_symmetric():
    data = bytes(range(256)) * 3
    assert enc(enc(data, 0x1234), 0x1234) == data


def test_block_scrambling_depends_on_position():
    data = b"PID = AC695X_TRANS;\nVID = N2LT;\n" * 4
    a, b = enc_blocks(data, 0x400), enc_blocks(data, 0x800)
    assert a != b
    assert enc_blocks(a, 0x400) == data


def test_block_key_matches_real_files():
    # Recovered from real files: isd_config.ini at 0x400 used key 0x3596,
    # at 0x2A400 key 0x9D96. Both must come out of the one formula.
    marker = b"[EXTRA_CFG_PARAM"
    assert enc(marker, 0x3596) == enc_blocks(marker, 0x400)
    assert enc(marker, 0x9D96) == enc_blocks(marker, 0x2A400)
