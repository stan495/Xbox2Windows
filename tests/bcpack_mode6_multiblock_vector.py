"""Source-built two-block BCPack mode-6 fixture.

Contains two distinguishable mode-6 blocks in one stream, checked in strict mode
without preset history. Both byte strings are fixed test data so tests consume
the input without depending on the encoder that produced it.
"""
from pathlib import Path
import struct
import sys

# 132 packed bytes containing two mode-6 blocks.
PACKED = bytes.fromhex("""
101010101010101005050505050506060101010101010110
000000000000042002027546281800000303df5f4b7c0521
030350782337043110101010101010100605060505050505
1010010101010110200801000000000400000008e4db27a4
21090294fe6b020431330404c4fb37250202bd19500f0341
41050934f4cb070502414111
""")

# Two 16-byte BC7 blocks (32 bytes total).
# Block 0: Mode 6 endpoints (197, 71, 41, 25) and (144, 160, 180, 124).
# Block 1: Mode 6 endpoints (40, 120, 220, 200) and (161, 231, 81, 241).
BLOCKS = bytes.fromhex("""
40 31 72 04 a5 68 19 be 02 34 56 78 9a bc de f0
40 0a 94 37 77 a3 c8 78 11 32 54 76 98 ba dc fe
""")

# RGBA8 in 8x4 raster order (128 bytes total).
# Row 0: Block 0 row 0 (x: 0..3), Block 1 row 0 (x: 4..7)
# Row 1: Block 0 row 1 (x: 0..3), Block 1 row 1 (x: 4..7)
# Row 2: Block 0 row 2 (x: 0..3), Block 1 row 2 (x: 4..7)
# Row 3: Block 0 row 3 (x: 0..3), Block 1 row 3 (x: 4..7)
RGBA = bytes.fromhex("""
c24d321f c5472919 b75f4e33 ba59452d 2878dcc8 307fd3cb 3988c8ce 418fc0d0
af6b6141 b4645739 a976734e ac716a47 4895b7d3 509caed5 59a5a4d9 61ac9bdb
a183865c a67c7c54 9b8e9868 9e888f62 68b392de 70ba89e0 79c37fe4 81ca76e6
939aab76 9793a06e c5472919 90a0b47c 88d06de9 90d765eb 99e05aee a1e751f1
""")


def raster_8x4(rgba0: bytes, rgba1: bytes) -> bytes:
    """Interleave two 4x4 row-major RGBA blocks into an 8x4 raster surface."""
    if len(rgba0) != 64 or len(rgba1) != 64:
        raise ValueError("Expected two 64-byte 4x4 RGBA blocks")
    out = bytearray()
    for y in range(4):
        out.extend(rgba0[y * 16 : (y + 1) * 16])
        out.extend(rgba1[y * 16 : (y + 1) * 16])
    return bytes(out)


if __name__ == "__main__":
    tools_dir = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(tools_dir))
    from bcpack import decode_with_info, BcpackError

    actual, provisional = decode_with_info(PACKED, 7, len(BLOCKS), allow_provisional=False)
    if actual != BLOCKS or provisional:
        raise ValueError(f"Expected {BLOCKS.hex()} with no provisional entries, got {actual.hex()}, {provisional}")

    # Verify that truncated streams are rejected before upload.
    for cut in (len(PACKED) - 1, len(PACKED) // 2, 32):
        try:
            decode_with_info(PACKED[:cut], 7, len(BLOCKS), allow_provisional=False)
            raise AssertionError(f"Expected truncated stream at length {cut} to fail strict decoding")
        except BcpackError:
            pass
