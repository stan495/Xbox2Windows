"""Source-built little-endian (ADAT) and big-endian (TADA) AWC sound-effect and music metadata controls."""
from dataclasses import asdict
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


MAGIC = {"<": b"ADAT", ">": b"TADA"}


def fixture(order="<") -> bytes:
    # Three sounds with interleaved tag order and discontiguous format headers.
    awc = bytearray(512)
    struct.pack_into(order + "4sIII3I", awc, 0, MAGIC[order], 0xff020001, 3, 76,
                     2 << 29 | 11, 2 << 29 | 12, 2 << 29 | 13)
    tags = ((0x55, 208, 16), (0x76, 96, 32), (0x76, 240, 32),
            (0x55, 304, 16), (0x55, 336, 16), (0x76, 400, 32))
    for index, (tag, offset, length) in enumerate(tags):
        struct.pack_into(order + "Q", awc, 28 + index * 8, tag << 56 | length << 28 | offset)
    for offset, samples, rate, codec in ((96, 1001, 48000, 11), (240, 225, 22050, 8), (400, 4097, 48000, 11)):
        struct.pack_into(order + "I", awc, offset, samples)
        struct.pack_into(order + "H", awc, offset + 8, rate)
        awc[offset + 28] = codec
    return bytes(awc)


EXPECTED = [
    {"sample_rate_hz": 22050, "codec_id": 8, "streams": 1, "samples": 225},
    {"sample_rate_hz": 48000, "codec_id": 11, "streams": 2, "samples": 5098},
]


def sfx_bank(streams, length=512, order="<") -> bytes:
    # Sound-effect bank: one tag list (type, length, offset) per stream, ids 11, 12, ...
    count = len(streams)
    tags = [tag for stream in streams for tag in stream]
    awc = bytearray(length)
    struct.pack_into(order + "4sIII", awc, 0, MAGIC[order], 0xff020001, count, 16 + 4 * count + 8 * len(tags))
    for index, stream in enumerate(streams):
        struct.pack_into(order + "I", awc, 16 + 4 * index, len(stream) << 29 | 11 + index)
    for index, (kind, size, offset) in enumerate(tags):
        struct.pack_into(order + "Q", awc, 16 + 4 * count + 8 * index, kind << 56 | size << 28 | offset)
        if kind == 0x55:
            awc[offset:offset + size] = b"\xa5" * size
    return awc


def fa_bank(fa=(0xFA, 20, 96), extra=(), order="<") -> bytes:
    # Two sounds: one 0xFA and one 0x76 format tag.
    awc = sfx_bank([[(0x55, 16, 128), fa, *extra], [(0x76, 32, 160), (0x55, 16, 200)]], order=order)
    struct.pack_into(order + "I", awc, 96, 300)
    struct.pack_into(order + "H", awc, 104, 22050)
    awc[96 + 0x13] = 4
    awc[96 + 28] = 9  # would be the codec if 0xFA were read like 0x76
    struct.pack_into(order + "I", awc, 160, 700)
    struct.pack_into(order + "H", awc, 168, 48000)
    awc[160 + 28] = 11
    return bytes(awc)


DEFAULT_WORDS = (2 << 29 | 0, 0 << 29 | 0x101, 1 << 29 | 0x102)
DEFAULT_TAGS = ((0x55, 64, 160), (0x48, 76, 64), (0xBD, 16, 144))
DEFAULT_RECORDS = ((0x101, 1000, 48000, 11), (0x102, 990, 48000, 11),
                   (0x103, 500, 32000, 11), (0x104, 10, 48000, 11))


def music_fixture(*, count=3, stream_words=DEFAULT_WORDS, tags=DEFAULT_TAGS, channels_field=None,
                  records=DEFAULT_RECORDS, block_count=2, block_chunk=0x800, flags=0xff020001,
                  length=256, order="<") -> bytes:
    # Streamed bank: tags are (type, length, offset) in table order; every body is clipped to the bank.
    awc = bytearray(length)

    def put(offset: int, data: bytes) -> None:
        data = data[:max(length - offset, 0)]
        awc[offset:offset + len(data)] = data

    struct.pack_into(order + "4sIII", awc, 0, MAGIC[order], flags, count, 16 + 4 * count + 8 * len(tags))
    for index, word in enumerate(stream_words):
        struct.pack_into(order + "I", awc, 16 + 4 * index, word)
    for index, (kind, size, offset) in enumerate(tags):
        struct.pack_into(order + "Q", awc, 16 + 4 * count + 8 * index, kind << 56 | size << 28 | offset)
    first = next(index for index, tag in enumerate(tags) if tag[0] == 0x48)
    for index, (kind, size, offset) in enumerate(tags):
        if index != first:
            put(offset, b"\xa5" * size)
    body = struct.pack(order + "III", block_count, block_chunk, len(records) if channels_field is None else channels_field)
    for hash_, samples, rate, codec in records:
        body += struct.pack(order + "IIHHB3x", hash_, samples, 0, rate, codec)
    put(tags[first][2], body)
    return bytes(awc)


MUSIC_FORMATS = [
    {"sample_rate_hz": 32000, "codec_id": 11, "streams": 1, "samples": 500},
    {"sample_rate_hz": 48000, "codec_id": 11, "streams": 3, "samples": 1000},
]
MUSIC_HEADER = {"channels": 4, "block_count": 2, "block_chunk": 2048}


class MetadataChecks(unittest.TestCase):
    def test_command(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"prefix!" + fixture() + b"neighbor")
            command = [sys.executable, "-B", str(ROOT / "tools/awc_metadata.py"), str(path), "--offset", "7"]
            result = subprocess.run([*command, "--size", "0x200"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"stream_count": 3, "formats": EXPECTED})
            short = subprocess.run([*command, "--size", "430"], capture_output=True, text=True)
            self.assertNotEqual(short.returncode, 0)
            self.assertEqual(short.stdout, "")

    def test_short_reads_are_metadata_only(self) -> None:
        from awc_metadata import read_formats

        class MetadataOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if not any(low <= start <= start + size <= high for low, high in
                           ((7, 83), (103, 135), (247, 279), (407, 439))):
                    raise AssertionError("Read outside an AWC metadata range")
                return super().read(min(size, 3))

        groups = read_formats(MetadataOnly(b"prefix!" + fixture()), 7, 512)
        self.assertEqual([asdict(g) for g in groups], EXPECTED)
        aliased = bytearray(fixture())
        struct.pack_into("<Q", aliased, 36, 0x76 << 56 | 32 << 28 | 208)
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            read_formats(MetadataOnly(b"prefix!" + aliased), 7, 512)

    def test_unflagged_v1(self) -> None:
        from awc_metadata import read_formats

        awc = bytearray(fixture())
        struct.pack_into("<I", awc, 4, 0xff000001)
        self.assertEqual([asdict(g) for g in read_formats(io.BytesIO(awc), 0, 512)], EXPECTED)

    def test_rejects_invalid_metadata(self) -> None:
        from awc_metadata import read_formats

        for label, offset, fmt, value in (
            ("signature", 0, "<4s", b"RIFF"),
            ("unsupported flags", 4, "<I", 0xff250001),
            ("stream budget", 8, "<I", 4097),
            ("truncated tag table", 12, "<I", 75),
            ("streamed bank", 16, "<I", 2 << 29),
            ("wrong tag owner", 20, "<I", 1 << 29 | 12),
            ("format truncated", 36, "<Q", 0x76 << 56 | 28 << 28 | 96),
            ("tag outside supplied span", 28, "<Q", 0x55 << 56 | 16 << 28 | 511),
            ("empty stream table", 8, "<I", 0),
            ("empty data tag", 28, "<Q", 0x55 << 56 | 0 << 28 | 208),
        ):
            with self.subTest(label=label):
                awc = bytearray(fixture())
                struct.pack_into(fmt, awc, offset, value)
                with self.assertRaises(ValueError):
                    read_formats(io.BytesIO(awc), 0, len(awc))

    def test_fa_format_tags(self) -> None:
        from awc_metadata import read_formats

        bank = fa_bank

        class MetadataOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if not any(low <= start <= start + size <= high for low, high in ((0, 56), (96, 116), (160, 192))):
                    raise AssertionError("Read outside an AWC metadata range")
                return super().read(min(size, 3))

        expected = [{"sample_rate_hz": 22050, "codec_id": 4, "streams": 1, "samples": 300},
                    {"sample_rate_hz": 48000, "codec_id": 11, "streams": 1, "samples": 700}]
        self.assertEqual([asdict(g) for g in read_formats(MetadataOnly(bank()), 0, 512)], expected)
        for label, awc, message in (
            ("truncated 0xFA", bank(fa=(0xFA, 19, 96)), "Truncated AWC sound format tag"),
            ("0xFA and 0x76 on one sound", bank(extra=((0x76, 32, 224),)), "one 0x76 or 0xFA format tag"),
            ("two 0xFA on one sound", bank(extra=((0xFA, 20, 224),)), "one 0x76 or 0xFA format tag"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    read_formats(io.BytesIO(awc), 0, len(awc))

    def test_sfx_accepts_limits(self) -> None:
        from awc_metadata import read_bank

        def formatted(streams, length=512) -> bytes:
            # Every 0x76 tag records one sample at 48,000 Hz with codec 11.
            awc = sfx_bank(streams, length)
            for kind, _, offset in (tag for stream in streams for tag in stream):
                if kind == 0x76:
                    struct.pack_into("<IIH", awc, offset, 1, 0, 48000)
                    awc[offset + 28] = 11
            return bytes(awc)

        # 4096 streams of two tags: header 16 + 4 * 4096 + 16 * 4096 = 81936, then 33 bytes per stream.
        many = [[(0x55, 1, 81936 + 33 * i), (0x76, 32, 81937 + 33 * i)] for i in range(4096)]
        high_id = bytearray(formatted([[(0x55, 16, 128), (0x76, 32, 160)]]))
        struct.pack_into("<I", high_id, 16, 2 << 29 | 0x10000000)  # id bit 28 lies inside the 29-bit id field
        one = [{"sample_rate_hz": 48000, "codec_id": 11, "streams": 1, "samples": 1}]
        for label, awc, stream_count, formats in (
            ("4096 streams", formatted(many, 81936 + 33 * 4096), 4096,
             [{"sample_rate_hz": 48000, "codec_id": 11, "streams": 4096, "samples": 4096}]),
            ("stream id 0x10000000", bytes(high_id), 1, one),
            ("data touching the format tag", formatted([[(0x55, 16, 128), (0x76, 32, 144)]]), 1, one),
            ("empty tag inside the format tag", formatted([[(0x55, 16, 128), (0x76, 32, 160), (0xBD, 0, 170)]]),
             1, one),
        ):
            with self.subTest(label=label):
                bank = read_bank(io.BytesIO(awc), 0, len(awc))
                self.assertEqual((bank.layout, bank.stream_count, bank.music), ("sfx", stream_count, None))
                self.assertEqual([asdict(g) for g in bank.formats], formats)

    def test_music_command(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"prefix!" + music_fixture() + b"neighbor")
            command = [sys.executable, "-B", str(ROOT / "tools/awc_metadata.py"), str(path), "--offset", "7"]
            result = subprocess.run([*command, "--size", "0x100"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"stream_count": 3, "layout": "music",
                                                         "music": MUSIC_HEADER, "formats": MUSIC_FORMATS})
            short = subprocess.run([*command, "--size", "223"], capture_output=True, text=True)
            self.assertNotEqual(short.returncode, 0)
            self.assertEqual(short.stdout, "")

    def test_music_reads_are_metadata_only(self) -> None:
        from awc_metadata import read_bank

        class MetadataOnly(io.BytesIO):
            def read(self, size: int = -1, /) -> bytes:
                start = self.tell()
                if not any(low <= start <= start + size <= high for low, high in ((7, 51), (71, 147))):
                    raise AssertionError("Read outside an AWC metadata range")
                return super().read(min(size, 3))

        bank = read_bank(MetadataOnly(b"prefix!" + music_fixture()), 7, 256)
        self.assertEqual(asdict(bank), {"layout": "music", "stream_count": 3,
                                        "formats": MUSIC_FORMATS, "music": MUSIC_HEADER})

    def test_formats_reject_music_bank(self) -> None:
        from awc_metadata import read_formats

        music = music_fixture()
        with self.assertRaisesRegex(ValueError, "Streamed AWC banks are unsupported"):
            read_formats(io.BytesIO(music), 0, len(music))

    def test_bank_reads_sfx(self) -> None:
        from awc_metadata import read_bank

        awc = fixture()
        bank = read_bank(io.BytesIO(awc), 0, len(awc))
        self.assertEqual((bank.layout, bank.stream_count, bank.music), ("sfx", 3, None))
        self.assertEqual([asdict(g) for g in bank.formats], EXPECTED)

    def test_music_ignores_other_streams_tags(self) -> None:
        from awc_metadata import read_bank

        for label, tag in (("cloned data chunk", (0x55, 64, 160)), ("second music header", (0x48, 76, 64)),
                           ("out-of-range tag", (0xBD, 16, 250))):
            with self.subTest(label=label):
                awc = music_fixture(tags=(*DEFAULT_TAGS[:2], tag))
                bank = read_bank(io.BytesIO(awc), 0, len(awc))
                self.assertEqual((bank.layout, bank.stream_count, asdict(bank.music)),
                                 ("music", 3, MUSIC_HEADER))
                self.assertEqual([asdict(g) for g in bank.formats], MUSIC_FORMATS)

    def test_music_accepts_limits(self) -> None:
        from awc_metadata import read_bank

        words = (2 << 29 | 0, *(0x101 + i for i in range(30)), 1 << 29 | 0x120)
        for label, awc, stream_count, channels, formats in (
            ("32 streams", music_fixture(count=32, stream_words=words, length=416,
                                         tags=((0x55, 64, 352), (0x48, 76, 256), (0xBD, 16, 336))),
             32, 4, MUSIC_FORMATS),
            ("32 channels", music_fixture(records=(DEFAULT_RECORDS[0],) * 32, length=672,
                                          tags=((0x55, 64, 592), (0x48, 524, 64), (0xBD, 16, 656))),
             3, 32, [{"sample_rate_hz": 48000, "codec_id": 11, "streams": 32, "samples": 1000}]),
            ("data at header end, touching the music header",
             music_fixture(tags=((0x55, 12, 52), *DEFAULT_TAGS[1:])), 3, 4, MUSIC_FORMATS),
            ("data ending at the bank end", music_fixture(tags=((0x55, 32, 224), *DEFAULT_TAGS[1:])),
             3, 4, MUSIC_FORMATS),
            ("repeated nonzero id", music_fixture(stream_words=(2 << 29 | 0, 0 << 29 | 0x102, 1 << 29 | 0x102)),
             3, 4, MUSIC_FORMATS),
            ("stream id 0x10000000", music_fixture(stream_words=(2 << 29 | 0, 0 << 29 | 0x10000000, 1 << 29 | 0x102)),
             3, 4, MUSIC_FORMATS),
            ("empty tag inside the music header",
             music_fixture(stream_words=(3 << 29 | 0, 0 << 29 | 0x101, 1 << 29 | 0x102),
                           tags=(*DEFAULT_TAGS[:2], (0xBD, 0, 100), DEFAULT_TAGS[2])), 3, 4, MUSIC_FORMATS),
            ("unflagged v1", music_fixture(flags=0xff000001), 3, 4, MUSIC_FORMATS),
            ("codec 0 on every channel", music_fixture(records=tuple(r[:3] + (0,) for r in DEFAULT_RECORDS)),
             3, 4, [{"sample_rate_hz": 32000, "codec_id": 0, "streams": 1, "samples": 500},
                    {"sample_rate_hz": 48000, "codec_id": 0, "streams": 3, "samples": 1000}]),
        ):
            with self.subTest(label=label):
                bank = read_bank(io.BytesIO(awc), 0, len(awc))
                self.assertEqual((bank.layout, bank.stream_count, bank.music.channels), ("music", stream_count, channels))
                self.assertEqual([asdict(g) for g in bank.formats], formats)

    def test_big_endian_banks(self) -> None:
        from awc_metadata import read_bank, read_formats

        def guarded(data: bytes, ranges):
            class MetadataOnly(io.BytesIO):
                def read(self, size: int = -1, /) -> bytes:
                    start = self.tell()
                    if not any(low <= start <= start + size <= high for low, high in ranges):
                        raise AssertionError("Read outside an AWC metadata range")
                    return super().read(min(size, 3))
            return MetadataOnly(data)

        sfx = read_bank(guarded(b"prefix!" + fixture(order=">"), ((7, 83), (103, 135), (247, 279), (407, 439))), 7, 512)
        self.assertEqual((sfx.layout, sfx.stream_count, [asdict(g) for g in sfx.formats]), ("sfx", 3, EXPECTED))
        music = read_bank(guarded(b"prefix!" + music_fixture(order=">"), ((7, 51), (71, 147))), 7, 256)
        self.assertEqual(asdict(music), {"layout": "music", "stream_count": 3,
                                         "formats": MUSIC_FORMATS, "music": MUSIC_HEADER})
        fa = read_formats(guarded(fa_bank(order=">"), ((0, 56), (96, 116), (160, 192))), 0, 512)
        self.assertEqual([asdict(g) for g in fa],
                         [{"sample_rate_hz": 22050, "codec_id": 4, "streams": 1, "samples": 300},
                          {"sample_rate_hz": 48000, "codec_id": 11, "streams": 1, "samples": 700}])
        unflagged = music_fixture(flags=0xff000001, order=">")
        self.assertEqual(asdict(read_bank(io.BytesIO(unflagged), 0, len(unflagged)).music), MUSIC_HEADER)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive"
            path.write_bytes(b"prefix!" + music_fixture(order=">") + b"neighbor")
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/awc_metadata.py"), str(path),
                                     "--offset", "7", "--size", "0x100"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"stream_count": 3, "layout": "music",
                                                         "music": MUSIC_HEADER, "formats": MUSIC_FORMATS})

        def swapped_magic(data: bytes, magic: bytes) -> bytes:
            return magic + data[4:]

        for label, awc, message, reader in (
            ("TADA over little-endian fields", swapped_magic(music_fixture(), b"TADA"), "Expected an ADAT or TADA v1",
             read_bank),
            ("ADAT over big-endian fields", swapped_magic(music_fixture(order=">"), b"ADAT"),
             "Expected an ADAT or TADA v1", read_bank),
            ("unknown magic", swapped_magic(music_fixture(order=">"), b"DATA"), "Expected an ADAT or TADA v1", read_bank),
            ("big-endian flag 0x00010000", music_fixture(flags=0xff010001, order=">"), "Expected an ADAT or TADA v1",
             read_bank),
            ("big-endian flag 0x00080000", music_fixture(flags=0xff080001, order=">"), "Expected an ADAT or TADA v1",
             read_bank),
            ("big-endian streamed bank in read_formats", music_fixture(order=">"), "Streamed AWC banks are unsupported",
             read_formats),
            ("big-endian data past bank", music_fixture(tags=((0x55, 64, 224), *DEFAULT_TAGS[1:]), order=">"),
             "outside supplied bank extent", read_bank),
            ("big-endian overlap", music_fixture(tags=((0x55, 64, 100), *DEFAULT_TAGS[1:]), order=">"),
             "Overlapping AWC tag extents", read_bank),
            ("big-endian truncated 0xFA", fa_bank(fa=(0xFA, 19, 96), order=">"), "Truncated AWC sound format tag",
             read_formats),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    reader(io.BytesIO(awc), 0, len(awc))

    def test_music_rejects_invalid_metadata(self) -> None:
        from awc_metadata import read_bank

        def tags(index: int, tag: tuple) -> tuple:
            return (*DEFAULT_TAGS[:index], tag, *DEFAULT_TAGS[index + 1:])

        def patched(fmt: str, offset: int, value: int) -> bytes:
            awc = bytearray(music_fixture())
            struct.pack_into(fmt, awc, offset, value)
            return bytes(awc)

        many_words = (2 << 29 | 0, *(0x101 + i for i in range(31)), 1 << 29 | 0x120)
        many_records = (DEFAULT_RECORDS[0],) * 33
        records = list(DEFAULT_RECORDS)
        records[2] = (0x103, 500, 32000, 8)
        for label, awc, message in (
            ("33 streams", music_fixture(count=33, stream_words=many_words, length=416,
                                         tags=((0x55, 64, 352), (0x48, 76, 256), (0xBD, 16, 336))),
             "more than 32 streams"),
            ("second id 0", music_fixture(stream_words=(2 << 29 | 0, 0, 1 << 29 | 0x102)),
             "Duplicate id-0 AWC stream"),
            ("data past bank", music_fixture(tags=tags(0, (0x55, 64, 224))), "outside supplied bank extent"),
            ("tag before header end", patched("<Q", 36, 0x48 << 56 | 76 << 28 | 40),
             "outside supplied bank extent"),
            ("empty tag inside header", music_fixture(stream_words=(3 << 29 | 0, 0 << 29 | 0x101, 1 << 29 | 0x102),
                                                      tags=(*DEFAULT_TAGS[:2], (0xBD, 0, 40), DEFAULT_TAGS[2])),
             "outside supplied bank extent"),
            ("tag table past header", patched("<I", 12, 43), "AWC tag table outside declared header"),
            ("no music header", music_fixture(tags=tags(0, (0xBD, 64, 160))), "Expected one 0x48 music header"),
            ("empty data tag", music_fixture(tags=tags(0, (0x55, 0, 160))), "Expected one 0x48 music header"),
            ("two data tags", music_fixture(stream_words=(3 << 29 | 0, 0 << 29 | 0x101, 1 << 29 | 0x102),
                                            tags=(*DEFAULT_TAGS[:2], (0x55, 16, 224), DEFAULT_TAGS[2])),
             "Expected one 0x48 music header"),
            ("two music headers", music_fixture(stream_words=(3 << 29 | 0, 0 << 29 | 0x101, 1 << 29 | 0x102),
                                                tags=(*DEFAULT_TAGS[:2], (0x48, 16, 224), DEFAULT_TAGS[2])),
             "Expected one 0x48 music header"),
            ("overlap", music_fixture(tags=tags(0, (0x55, 64, 100))), "Overlapping AWC tag extents"),
            ("header under 12 bytes", music_fixture(tags=tags(1, (0x48, 11, 64))),
             "AWC music header shorter than 12 bytes"),
            ("zero channels", music_fixture(channels_field=0), "Music channel count outside 1 to 32"),
            ("33 channels", music_fixture(channels_field=33, records=many_records, length=688,
                                          tags=((0x55, 64, 608), (0x48, 540, 64), (0xBD, 16, 672))),
             "Music channel count outside 1 to 32"),
            ("truncated records", music_fixture(tags=tags(1, (0x48, 75, 64))), "Truncated AWC music header"),
            ("zero block count", music_fixture(block_count=0), "block count or chunk size is zero"),
            ("zero chunk size", music_fixture(block_chunk=0), "block count or chunk size is zero"),
            ("mixed codecs", music_fixture(records=records), "Mixed codecs across music channels"),
            ("codec 0 beside codec 11", music_fixture(records=((0x101, 1000, 48000, 0), *DEFAULT_RECORDS[1:])),
             "Mixed codecs across music channels"),
            ("flag 0x00010000", music_fixture(flags=0xff010001), "Expected an ADAT or TADA v1"),
            ("flag 0x00040000", music_fixture(flags=0xff040001), "Expected an ADAT or TADA v1"),
            ("flag 0x00080000", music_fixture(flags=0xff080001), "Expected an ADAT or TADA v1"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    read_bank(io.BytesIO(awc), 0, len(awc))


if __name__ == "__main__":
    unittest.main()
