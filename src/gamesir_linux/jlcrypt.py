"""JieLi primitives: the CRC-16 used in .ufw headers and the "ENC" header scrambling.

Both are reverse-engineered from GameSir Connect's bundled firmware files.

ENC is an XOR keystream from a 16-bit shift register with the CRC-16/CCITT
polynomial (0x1021). The keystream byte is the register's low byte; the
register shifts left once per byte. A ufw header uses the initial key 0xFFFF.
"""

POLY = 0x1021

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ POLY) if _c & 0x8000 else (_c << 1)
        _c &= 0xFFFF
    _CRC_TABLE.append(_c)


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/XMODEM: poly 0x1021, init 0, no reflection, no final XOR."""
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def _step(key: int) -> int:
    key <<= 1
    if key & 0x10000:
        key ^= POLY
    return key & 0xFFFF


def enc(data: bytes, key: int = 0xFFFF) -> bytes:
    """Apply (or remove; it is symmetric) JieLi ENC scrambling."""
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ (key & 0xFF)
        key = _step(key)
    return bytes(out)


# Small config partitions (isd_config.ini, script.ver, ...) in the Nova 2 Lite /
# Super Nova family are scrambled in 32-byte blocks. Each block restarts ENC
# with key = BLOCK_KEY_BASE ^ (absolute_file_offset >> 2).
BLOCK_KEY_BASE = 0x3496
BLOCK_SIZE = 32


def enc_blocks(data: bytes, file_offset: int, base: int = BLOCK_KEY_BASE) -> bytes:
    """Apply (or remove) the position-keyed block scrambling."""
    out = bytearray()
    for i in range(0, len(data), BLOCK_SIZE):
        key = (base ^ ((file_offset + i) >> 2)) & 0xFFFF
        out += enc(data[i : i + BLOCK_SIZE], key)
    return bytes(out)
