"""Controls for tools/pe_authentihash.py on synthetic PE images built during the test.

The expected digests are not produced by the tool. `reference_digest` hashes explicit
slices of the image at offsets fixed by the builder below. The pinned constants in
EXPECTED and PINNED were first checked against that reference and then replaced
by the values that the Windows catalog-admin hash returned (CryptCATAdminCalcHashFromFileHandle2,
SHA256, Windows 11 build 26300). tests/pe_authentihash_oracle_check.py compares the same
constants with the Windows API.
"""
import hashlib
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests" / "cpu"))
from pe_authentihash import authentihash  # noqa: E402
from synthetic_pe import build_leaf_dll  # noqa: E402

HEADERS = 0x200
PE_AT = 0x40
OPTIONAL = PE_AT + 24
CHECKSUM = OPTIONAL + 64
CERT_CONTENT = bytes((7 * i + 3) % 256 for i in range(48))  # arbitrary bytes, not a signature
EXPECTED = {
    "pe32": "1f895efdcf3f16990dcf4d24a1c52a05fd6c8398b2d6e5a9a848c29910d103a7",
    "pe32plus": "a099207dd3cbe0cbc07a0f2df7eb655c6ab813cc00a4929a8c953011a93637a5",
    "leaf": "07c718e0bb6fce8222a29f79e7812c9ee2075186699a505c66975245657d394c",
}


def build(plus: bool, cert: bytes | None = None, swap_table: bool = False, count: int = 2) -> bytearray:
    """Return a PE32/PE32+ image with `count` file-aligned sections and an optional certificate table."""
    optional_size = 240 if plus else 224
    dirs_at = OPTIONAL + (112 if plus else 96)
    bodies = [bytes((i * 13 + k) % 251 for i in range(0x200 * (k + 1))) for k in range(count)]
    image = bytearray(bytes((i * 5 + 1) % 253 for i in range(HEADERS)))
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, PE_AT)
    image[PE_AT:PE_AT + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, PE_AT + 4, 0x8664, count, 0, 0, 0, optional_size, 0x2022)
    struct.pack_into("<H", image, OPTIONAL, 0x20B if plus else 0x10B)
    struct.pack_into("<II", image, OPTIONAL + 32, 0x1000, 0x200)  # SectionAlignment, FileAlignment
    struct.pack_into("<I", image, OPTIONAL + 56, 0x1000 * (count + 1))  # SizeOfImage
    struct.pack_into("<I", image, CHECKSUM, 0x11223344)
    struct.pack_into("<H", image, OPTIONAL + 68, 3)  # Subsystem: console
    struct.pack_into("<I", image, OPTIONAL + 60, HEADERS)
    struct.pack_into("<I", image, dirs_at - 4, 16)
    struct.pack_into("<II", image, dirs_at + 8 * 4, 0, 0)
    table = [bytearray(40) for _ in range(count)]
    pointer = HEADERS
    for k, body in enumerate(bodies):
        table[k][0:8] = f".s{k}".encode().ljust(8, b"\0")
        struct.pack_into("<IIII", table[k], 8, len(body), 0x1000 * (k + 1), len(body), pointer)
        pointer += len(body)
    if swap_table:
        table.reverse()
    section_table = OPTIONAL + optional_size
    for k, row in enumerate(table):
        image[section_table + 40 * k:section_table + 40 * (k + 1)] = row
    image += b"".join(bodies)
    if cert is not None:
        struct.pack_into("<II", image, dirs_at + 32, len(image), len(cert))
        image += cert
    return image


def build_layout(plus: bool, header_pad: int = 0, gap: int = 0, overlay: bytes = b"",
                 cert: bytes | None = None, trim: int = 0, size_of_headers: int = HEADERS) -> bytearray:
    """Insert nonzero filler after the headers and between the two sections, then add an overlay and a table.

    `trim` removes that many bytes from the end of the second section (SizeOfRawData is not a multiple of 8).
    `size_of_headers` is the SizeOfHeaders field; a value above the first section pointer makes sections start inside it.
    """
    base = build(plus)
    first, second = HEADERS + header_pad, HEADERS + header_pad + 0x200 + gap
    image = bytearray(base[:HEADERS]) + bytes([0xE5]) * header_pad + base[HEADERS:HEADERS + 0x200]
    image += bytes([0xD7]) * gap + base[HEADERS + 0x200:]
    table = OPTIONAL + (240 if plus else 224)
    struct.pack_into("<I", image, OPTIONAL + 60, size_of_headers)
    struct.pack_into("<I", image, table + 20, first)
    struct.pack_into("<I", image, table + 40 + 20, second)
    if trim:
        struct.pack_into("<I", image, table + 40 + 16, 0x400 - trim)
        del image[len(image) - trim:]
    image += overlay
    if cert is not None:
        dirs_at = OPTIONAL + (112 if plus else 96)
        struct.pack_into("<II", image, dirs_at + 32, len(image), len(cert))
        image += cert
    return image


def filler(size: int) -> bytes:
    return bytes((11 * i + 5) % 256 for i in range(size))


# name -> (build_layout arguments, exotic). The expected digests are in PINNED. "Exotic" layouts were not seen in
# the Windows system and build files compared with the Windows hash; the registered oracle test skips them.
LAYOUTS = {
    "overlay1": (dict(overlay=filler(1)), False),
    "overlay5": (dict(overlay=filler(5)), False),
    "overlay13": (dict(overlay=filler(13)), False),
    "header_filler": (dict(header_pad=0x200), False),
    "headers_field_past_section": (dict(size_of_headers=0x400), False),
    "headers_field_past_section_trim7": (dict(size_of_headers=0x400, trim=7), False),
    "trim1": (dict(trim=1), False),
    "trim7": (dict(trim=7), False),
    "trim7_cert": (dict(trim=7, overlay=filler(7), cert=CERT_CONTENT), False),
    "trim3_overlay5": (dict(trim=3, overlay=filler(5)), False),
    "gap512": (dict(gap=0x200), True),
    "gap1": (dict(gap=1), True),
    "gap1_trim3": (dict(gap=1, trim=3), True),
    "gap1_cert": (dict(gap=1, overlay=filler(7), cert=CERT_CONTENT), True),
    "filler_gap_cert": (dict(header_pad=0x200, gap=0x400, cert=CERT_CONTENT), True),
}
# Digests returned by CryptCATAdminCalcHashFromFileHandle2 (SHA256) on Windows 11 build 26300 for the images
# build_layout makes with the arguments in LAYOUTS. Key: "<pe32|pe32plus>_<layout name>".
PINNED = {
    "pe32_overlay1": "fdd056769720aeb602fd5f6953cba5294b6edbc9ad733e0cb7db052f4daf0c2b",
    "pe32_overlay5": "896627d206387c4591dfaff9e215aa63c9c3879a4d723e8c7b1478e643826b7e",
    "pe32_overlay13": "6c69dcd76e69ec0b3b32caeb53a091a0b3298947cad0f1179b8a85dcb86a97cf",
    "pe32_header_filler": "a71c23ef3319aa75fad9f6e242830ce42955855da9e8bf0a7e7569dc89e95f9a",
    "pe32_trim1": "3b0cd402a8ddce77fe2bbdf60d8121f44d1fc2cfc696986b7ff598bc0829fa33",
    "pe32_trim7": "fb2576732edea761b3eae84bec4fe936eb7500843a8207d4d81278844658a467",
    "pe32_trim7_cert": "2d5e32583c09cff52e1a73d852447430f1067d6e984d7e8720f5860e34ba9732",
    "pe32_trim3_overlay5": "e67b1b21f73fed96ceb9aee1ecd1adc7793f78be1897e3d7486e075da12e6154",
    "pe32_gap512": "159e7ac9090711b59eff80403683044c331b3c0197a5ce7d171aa7928ed34d3a",
    "pe32_gap1": "b2daedbfd036c1f98b8323f8a32b2d4621a852c34b5b91be744afea0bd9f59e7",
    "pe32_gap1_trim3": "01b567aa69d574f8492c832cb079b12e382152378e4cfa43a0ac22cf31ff855e",
    "pe32_gap1_cert": "64bc431d2c0686e541eac4ebbd19c3742a55d28d0f13e98dbb5ffbd85c530f87",
    "pe32_filler_gap_cert": "9f61082d3d4eded55500e84646a80cf3b357296b6021a87b88769ad9f331112c",
    "pe32plus_overlay1": "2ffdbd32c5cbff33fd9b347a586ca3734781fc38fa144b67eb697bc7ba812d82",
    "pe32plus_overlay5": "5e6b881ba19daee8df244a26e57244c6e1b332cfe30920850d7e2838eb73e36b",
    "pe32plus_overlay13": "dd0cd3218a63593ff736b536bb08deb7814a73ac8470f29dd5e227ac79f56f4a",
    "pe32plus_header_filler": "fe265cb1f5a68c73291775a8826daf1d5fded60d27071fcddb1572a596b79970",
    "pe32plus_trim1": "b05d2f8407d4987d4cd5f92a45189565b7985078bc282eae871551831452c53b",
    "pe32plus_trim7": "cd4400d0f6003c8aa844b4358485f9db7bd7175f39b4fcbdec9b2bd864a6b8fc",
    "pe32plus_trim7_cert": "834352e68c2558bb35ddcded8dd6cfff034e61cfbc3a735459bc80fc099c9899",
    "pe32plus_trim3_overlay5": "f31724b6f88142cc2037242deadf3bc0c6b7d0be5acbd35811e2b1daabad49eb",
    "pe32plus_gap512": "77f03b0a4353a1b40a76344e5ee19a97f028dc2b0f66279ee84f24e817472ba7",
    "pe32plus_gap1": "58d262fd16202b9a9d0679bcdd32d50cd1bf061abc9e7cef01634461c8e3b67c",
    "pe32plus_gap1_trim3": "7f5f3bb6ef3c4a74b8477c044ad9b5b828decbab4fd8f4fd2016bd549e6867fe",
    "pe32plus_gap1_cert": "1e81ccca99f425c58d4630eb4e9429ae41ef0fe0e1114764b33d38bc7f01c547",
    "pe32plus_filler_gap_cert": "ef0c42d6d052bf6f9c3715d030511a3b801a16a8ffbacf0d26609883506820a5",
    "pe32_headers_field_past_section": "594e9958e445d45a4910a5fa9b89da7b2e6fbcca54aa8b292c893ba3825c2ed9",
    "pe32_headers_field_past_section_trim7": "a397057a2b476e0136cda991552d6adc9d0b39b1e561d49cf7783407df8a6801",
    "pe32plus_headers_field_past_section": "5ad2f388f4bf89b4c0f07b188496a50d25a87172b34c18ac82a9d9c9f71b11f7",
    "pe32plus_headers_field_past_section_trim7": "5f154174085a8cc2f608fd6a5979f334fe0e807a1e8cf19d113f18cf1ba0dbe6",
}


def reference_digest(image: bytes, plus: bool, cert_len: int = 0, count: int = 2) -> str:
    """Hash explicit slices; offsets come from the builder constants, not from parsing."""
    cert_entry = OPTIONAL + (112 if plus else 96) + 32
    end = len(image) - cert_len
    parts = [image[:CHECKSUM], image[CHECKSUM + 4:cert_entry], image[cert_entry + 8:HEADERS],
             image[HEADERS:end]]
    return hashlib.sha256(b"".join(parts)).hexdigest()


class Authentihash(unittest.TestCase):
    def test_reference_and_pinned_values(self) -> None:
        for plus in (False, True):
            for swap in (False, True):
                plain = build(plus, swap_table=swap)
                signed = build(plus, CERT_CONTENT, swap_table=swap)
                expected = reference_digest(plain, plus)
                self.assertEqual(authentihash(bytes(plain)), expected)
                self.assertEqual(authentihash(bytes(signed)), expected)
                if not swap:
                    self.assertEqual(expected, EXPECTED["pe32plus" if plus else "pe32"])

    def test_section_byte_changes_digest(self) -> None:
        for plus in (False, True):
            base = authentihash(bytes(build(plus, CERT_CONTENT)))
            for offset in (HEADERS, HEADERS + 0x200 - 1, HEADERS + 0x200, HEADERS + 0x600 - 1):
                changed = build(plus, CERT_CONTENT)
                changed[offset] ^= 1
                self.assertNotEqual(authentihash(bytes(changed)), base, hex(offset))
            changed = build(plus, CERT_CONTENT)
            changed[OPTIONAL + 4] ^= 1  # a header byte that is hashed
            self.assertNotEqual(authentihash(bytes(changed)), base)

    def test_excluded_bytes_do_not_change_digest(self) -> None:
        for plus in (False, True):
            image = build(plus, CERT_CONTENT)
            base = authentihash(bytes(image))
            cert_entry = OPTIONAL + (112 if plus else 96) + 32
            variants = []
            changed = bytearray(image)
            struct.pack_into("<I", changed, CHECKSUM, 0xDEADBEEF)
            variants.append(changed)
            changed = bytearray(image)
            changed[-1] ^= 0xFF
            changed[-len(CERT_CONTENT)] ^= 0x55
            variants.append(changed)
            bigger = build(plus, CERT_CONTENT + bytes(range(80)))  # other certificate size
            variants.append(bigger)
            variants.append(build(plus))  # no certificate table
            for variant in variants:
                self.assertEqual(authentihash(bytes(variant)), base)
            self.assertEqual(len(image) - len(CERT_CONTENT), struct.unpack_from("<I", image, cert_entry)[0])

    def test_overlay_is_hashed_and_padded_to_eight(self) -> None:
        """Bytes after the last section (no certificate table) are hashed, zero-padded to a multiple of 8."""
        for plus in (False, True):
            cert_entry = OPTIONAL + (112 if plus else 96) + 32
            for size in (1, 5, 8, 13):
                overlay = bytes((11 * i + 5) % 256 for i in range(size))
                image = bytes(build(plus)) + overlay
                expected = hashlib.sha256(
                    image[:CHECKSUM] + image[CHECKSUM + 4:cert_entry] + image[cert_entry + 8:HEADERS + 0x600]
                    + overlay + bytes(-size % 8)).hexdigest()
                self.assertEqual(authentihash(image), expected, (plus, size))
                self.assertNotEqual(expected, authentihash(bytes(build(plus))))

    def test_pinned_windows_digests(self) -> None:
        for plus in (False, True):
            for name, (options, _) in LAYOUTS.items():
                key = f"{'pe32plus' if plus else 'pe32'}_{name}"
                with self.subTest(key=key):
                    self.assertEqual(authentihash(bytes(build_layout(plus, **options))), PINNED[key])

    def test_padding_follows_file_length(self) -> None:
        """Zero padding makes the file length (or the certificate offset) a multiple of 8, whatever the section sizes."""
        for plus in (False, True):
            cert_entry = OPTIONAL + (112 if plus else 96) + 32
            for gap, trim in ((0, 1), (0, 7), (1, 0), (7, 0), (1, 3)):
                image = bytes(build_layout(plus, gap=gap, trim=trim))
                expected = hashlib.sha256(
                    image[:CHECKSUM] + image[CHECKSUM + 4:cert_entry] + image[cert_entry + 8:]
                    + bytes(-len(image) % 8)).hexdigest()
                self.assertEqual(authentihash(image), expected, (plus, gap, trim))

    def test_no_sections_with_data_is_refused(self) -> None:
        table = OPTIONAL + 240
        for count in (0, 2):
            image = build(True, CERT_CONTENT)
            struct.pack_into("<H", image, PE_AT + 6, count)
            if count:
                for index in range(count):
                    struct.pack_into("<I", image, table + 40 * index + 16, 0)
            with self.assertRaises(ValueError):
                authentihash(bytes(image))

    def test_header_filler_and_section_gaps(self) -> None:
        """Filler after the headers and between sections is hashed like the rest of the image body."""
        for plus in (False, True):
            cert_entry = OPTIONAL + (112 if plus else 96) + 32
            for header_pad, gap in ((0, 0x200), (0x200, 0), (0x200, 0x400)):
                for cert in (None, CERT_CONTENT):
                    image = bytes(build_layout(plus, header_pad, gap, cert=cert))
                    first = HEADERS + header_pad
                    second = first + 0x200 + gap
                    body = image[:CHECKSUM] + image[CHECKSUM + 4:cert_entry] + image[cert_entry + 8:first]
                    body += image[first:second + 0x400]
                    self.assertEqual(authentihash(image), hashlib.sha256(body).hexdigest(), (plus, header_pad, gap))

    def test_layouts_the_windows_hash_does_not_reproduce_are_refused(self) -> None:
        for plus in (False, True):
            dirs_at = OPTIONAL + (112 if plus else 96)
            cases = {
                "certificate size not a multiple of 8": build(plus, bytes(45)),
                "certificate offset not a multiple of 8": build_layout(plus, overlay=bytes(3), cert=bytes(48)),
            }
            for count in (0, 1, 3, 4):
                image = build(plus, CERT_CONTENT)
                struct.pack_into("<I", image, dirs_at - 4, count)
                cases[f"{count} data directories"] = image
            for label, image in cases.items():
                with self.subTest(label=label, plus=plus):
                    with self.assertRaises(ValueError):
                        authentihash(bytes(image))

    def test_leaf_dll_with_and_without_table(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "leaf.dll"
            build_leaf_dll(path)
            image = bytearray(path.read_bytes())
            plain = authentihash(bytes(image))
            cert_entry = 0x58 + 112 + 32
            signed = bytearray(image)
            struct.pack_into("<II", signed, cert_entry, len(signed), len(CERT_CONTENT))
            signed += CERT_CONTENT
            self.assertEqual(authentihash(bytes(signed)), plain)
            self.assertEqual(plain, reference_leaf(bytes(image)))
            self.assertEqual(plain, EXPECTED["leaf"])
            changed = bytearray(image)
            changed[0x200] ^= 1
            self.assertNotEqual(authentihash(bytes(changed)), plain)

    def test_malformed_inputs_raise(self) -> None:
        good = build(True, CERT_CONTENT)
        cert_entry = OPTIONAL + 112 + 32
        cases = {"empty": b"", "not a PE": b"hello" * 100, "mz only": b"MZ" + bytes(100)}
        for length in (0x3F, 0x50, CHECKSUM, 0x100, 0x1F0, HEADERS, HEADERS + 0x100, len(good) - len(CERT_CONTENT) - 1):
            cases[f"truncated at {length:#x}"] = bytes(good[:length])

        def patched(fmt: str, offset: int, *values: int, extra: bytes = b"") -> bytes:
            image = bytearray(good)
            struct.pack_into(fmt, image, offset, *values)
            return bytes(image) + extra

        section_table = OPTIONAL + 240
        end = len(good)
        cases.update({
            "bad PE signature": patched("<4s", PE_AT, b"PX\0\0"),
            "e_lfanew past file": patched("<I", 0x3C, 0x7FFFFFF0),
            "unknown optional magic": patched("<H", OPTIONAL, 0x107),
            "optional header past file": patched("<H", PE_AT + 20, 0xFFFF),
            "too many directories": patched("<I", OPTIONAL + 108, 0x1000),
            "section table past file": patched("<H", PE_AT + 6, 0x7000),
            "SizeOfHeaders past file": patched("<I", OPTIONAL + 60, 0x10000000),
            "SizeOfHeaders inside section table": patched("<I", OPTIONAL + 60, section_table + 10),
            "section past end of file": patched("<I", section_table + 16, 0x10000),
            "section inside section table": patched("<I", section_table + 20, 0x100),
            "overlapping sections": patched("<I", section_table + 40 + 20, HEADERS + 0x100),
            "certificate not last": patched("<I", cert_entry + 4, len(CERT_CONTENT) - 8),
            "certificate past file": patched("<I", cert_entry + 4, len(CERT_CONTENT) + 8),
            "certificate offset in sections": patched("<II", cert_entry, HEADERS, end - HEADERS),
            "certificate offset without size": patched("<I", cert_entry + 4, 0),
            "certificate size without offset": patched("<I", cert_entry, 0),
            "bytes after certificate": bytes(good) + b"\0" * 8,
            "unsigned image with trailing data and bogus entry": patched("<II", cert_entry, end + 100, 8),
        })
        for label, data in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    authentihash(data)

    def test_cli(self) -> None:
        tool = str(ROOT / "tools" / "pe_authentihash.py")
        with TemporaryDirectory() as directory:
            good = Path(directory) / "good.bin"
            data = bytes(build(True, CERT_CONTENT))
            good.write_bytes(data)
            result = subprocess.run([sys.executable, "-B", tool, str(good)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), reference_digest(data, True, len(CERT_CONTENT)))
            self.assertEqual(good.read_bytes(), data)  # read-only
            bad = Path(directory) / "bad.bin"
            bad.write_bytes(data[:0x150])
            for path in (bad, Path(directory) / "missing.bin"):
                failed = subprocess.run([sys.executable, "-B", tool, str(path)], capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(failed.stdout, "")
                self.assertTrue(failed.stderr.strip())


def reference_leaf(image: bytes) -> str:
    """Explicit slices for the leaf DLL: 0x200 header bytes, then all remaining bytes."""
    checksum, cert_entry = 0x58 + 64, 0x58 + 112 + 32
    return hashlib.sha256(image[:checksum] + image[checksum + 4:cert_entry] + image[cert_entry + 8:]).hexdigest()


if __name__ == "__main__":
    unittest.main()
