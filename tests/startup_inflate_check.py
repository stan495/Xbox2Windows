"""Check the DirectStorage stand-in's zlib decoder (src/startup_stubs/inflate.h) against CPython's zlib.

The stand-in inflates zlib requests with its own RFC 1950/1951 decoder. The stub consumer only
feeds it stored blocks, a fixed block that repeats one byte at distance 1, and single-byte runs.
This check decodes streams that CPython's zlib encodes from seeded synthetic data with stored,
fixed and dynamic Huffman blocks (including a sparse alphabet whose block header needs all three
code-length repeat codes), back-references across the 32 KiB window and a sync flush between
blocks. Each decoded result must equal the input. The controls are streams the decoder must reject
without writing past the destination: one byte too little capacity and a truncated stream.

The distance bound is checked on bare deflate data, so neither the zlib header nor the Adler-32
check can reject it: after four literals, a match at distance 4 decodes and the same block with
distance 5 is rejected before the match copies anything. A hand-built dynamic block with a single
1-bit distance code, the one incomplete code set zlib accepts, must decode with and without the table.

The decoder looks Huffman codes up to 10 bits long up in a table. A differential case decodes 900
seeded random streams (sources, levels, strategies and flushes mixed; some with codes longer than the
table), each at exact capacity, one byte short, truncated and with flipped bits, and requires status,
size and bytes equal to the same probe built to decode one bit at a time, and to CPython's zlib except
where inflate.h decodes past an incomplete code set that zlib rejects. The control is the probe built with
one wrong table entry, which must differ on some case.

A boundary case runs small streams at every capacity up to one past their size and truncated to every
length: matches at distances 1 to 16 around the decoder's copy widths, stored blocks of 0 and 65535
bytes and stored blocks at every bit position. A window case runs streams of 96 to 160 KiB with matches
up to the 32 KiB window (hand-built ones at distance 32768 and length 258) at exact capacity, one byte
short and truncated. tests/startup_inflate_probe.cpp runs the decoder; no GPU and no game files are
involved.

With --bench and one or more probe builds, it instead prints decoded MiB/s for 16 MiB seeded streams
of four kinds (stored, fixed Huffman copies, dynamic Huffman word text, dynamic Huffman bytes of about
7.3 bits of entropy) next to CPython's zlib: the builds run interleaved, the first round is dropped and
the best of the next five is kept, and each run's output checksum must match the input's.
"""
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import time
import zlib


def corpus(rng, size):
    # Random literals and copies of 3..258 bytes from 1..32000 bytes back, both log-uniform, so an
    # encoder emits most length and distance codes with their extra bits.
    data = bytearray(rng.randbytes(4096))
    while len(data) < size:
        if rng.random() < 0.3:
            data += rng.randbytes(rng.randint(1, 40))
            continue
        length = round(3 * 86 ** rng.random())
        start = len(data) - min(len(data), round(32000 ** rng.random()))
        for i in range(length):
            data.append(data[start + i])
    return bytes(data[:size])


def compress(data, level=9, strategy=zlib.Z_DEFAULT_STRATEGY):
    encoder = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
    return encoder.compress(data) + encoder.flush()


def first_block_type(stream, mode="zlib"):
    return (stream[2 if mode == "zlib" else 0] >> 1) & 3


class Bits:
    """Deflate bit order: fields least significant bit first, Huffman codes most significant bit first."""

    def __init__(self):
        self.value = 0
        self.count = 0

    def put(self, value, count):
        self.value |= value << self.count
        self.count += count

    def code(self, code, count):
        self.put(int(format(code, "0%db" % count)[::-1], 2), count)

    def to_bytes(self):
        return self.value.to_bytes((self.count + 7) // 8, "little")


def words(rng, size):
    # Lowercase words and spaces: most literal codes are unused, so the dynamic block header needs
    # the zero-run codes 17 and 18 as well as code 16.
    out = bytearray()
    while len(out) < size:
        out += bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(1, 9))) + b" "
    return bytes(out[:size])


def fixed_match_block(literals, distance_code, extra_bits, extra_value):
    # Bare deflate data, one final fixed block: the literals, a length-3 match, end of block.
    bits = Bits()
    bits.put(1, 1)
    bits.put(1, 2)
    for literal in literals:
        bits.code(0x30 + literal, 8)
    bits.code(257 - 256, 7)
    bits.code(distance_code, 5)
    bits.put(extra_value, extra_bits)
    bits.code(256 - 256, 7)
    return bits.to_bytes()


def single_distance_block():
    # Bare deflate data, one final dynamic block whose only distance code is distance 1 with a 1-bit
    # code: the one incomplete set zlib accepts. Literals A and B, end of block and length 3 have
    # 2-bit codes 00, 01, 10, 11; eight copies of A plus a length-3 match, then 16 B's.
    bits = Bits()
    bits.put(1, 1)
    bits.put(2, 2)
    bits.put(258 - 257, 5)  # HLIT: symbols 0..257
    bits.put(0, 5)  # HDIST: one distance code
    bits.put(18 - 4, 4)  # HCLEN: code-length codes up to symbol 1 in the transmission order
    order = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1]
    for symbol in order:
        bits.put(2 if symbol in (1, 2, 17, 18) else 0, 3)
    cl = {1: 0, 2: 1, 17: 2, 18: 3}  # code-length codes, all 2 bits
    for symbol, extra, count in [(18, 65 - 11, 7), (2, 0, 0), (2, 0, 0), (18, 138 - 11, 7), (18, 51 - 11, 7),
                                 (2, 0, 0), (2, 0, 0), (1, 0, 0)]:
        bits.code(cl[symbol], 2)
        bits.put(extra, count)
    for _ in range(8):
        bits.code(0, 2)  # A
        bits.code(3, 2)  # length 3
        bits.code(0, 1)  # distance 1
    for _ in range(16):
        bits.code(1, 2)  # B
    bits.code(2, 2)  # end of block
    return bits.to_bytes(), b"AAAA" * 8 + b"B" * 16


def reference(stream, mode):
    return zlib.decompress(stream, 15 if mode == "zlib" else -15)


def reference_rejects(stream, mode="zlib"):
    try:
        reference(stream, mode)
    except zlib.error:
        return True
    return False


def run(probe, work, name, stream, capacity, mode):
    source, output = work / (name + "." + mode), work / (name + ".out")
    source.write_bytes(stream)
    result = subprocess.run([str(probe), mode, str(source), str(capacity), str(output)],
                            capture_output=True, text=True, timeout=60)
    return result, output


def expect_decoded(probe, work, name, stream, data, capacity=None, mode="zlib"):
    assert reference(stream, mode) == data, name + ": the reference decoder disagrees with the input"
    capacity = len(data) if capacity is None else capacity
    result, output = run(probe, work, name, stream, capacity, mode)
    assert result.returncode == 0, "%s: exit %d: %s%s" % (name, result.returncode, result.stdout, result.stderr)
    assert result.stdout.strip() == "inflated %d" % len(data), "%s: %s" % (name, result.stdout.strip())
    decoded = output.read_bytes()
    if decoded != data:
        first = next((i for i, (a, b) in enumerate(zip(decoded, data)) if a != b), min(len(decoded), len(data)))
        raise AssertionError("%s: first mismatch at byte %d of %d" % (name, first, len(data)))
    print("%-22s block %d, %6d -> %6d bytes, decoded" % (name, first_block_type(stream, mode), len(stream), len(data)))


def expect_rejected(probe, work, name, stream, capacity, mode="zlib", decoded_before=None, invalid=True):
    # invalid: the stream itself is damaged, so the reference decoder must reject it too.
    assert not invalid or reference_rejects(stream, mode), name + ": the reference decoder accepted the stream"
    result, _ = run(probe, work, name, stream, capacity, mode)
    want = "rejected" if decoded_before is None else "rejected after %d bytes" % decoded_before
    assert result.returncode == 1 and result.stdout.strip() == want, \
        "%s: exit %d: %s%s" % (name, result.returncode, result.stdout, result.stderr)
    print("%-22s %6d bytes, capacity %6d, %s" % (name, len(stream), capacity, want))


OK, INSUFFICIENT, CORRUPT, BAD_CHECKSUM = range(4)  # x2w_inflate::InflateStatus
STATUS = ["ok", "insufficient_output", "corrupt_stream", "bad_checksum"]


def canonical(lengths):
    table, code = {}, 0
    for n in range(1, 16):
        for symbol, length in enumerate(lengths):
            if length == n:
                table[n, code] = symbol
                code += 1
        code <<= 1
    return table


def longest_code(stream):
    # Longest literal/length or distance code of the first block when it is a dynamic one, else 0.
    value, pos = int.from_bytes(stream[2:], "little"), 0

    def get(n):
        nonlocal pos
        pos += n
        return (value >> (pos - n)) & ((1 << n) - 1)

    get(1)
    if get(2) != 2:
        return 0
    nlen, ndist, ncode = get(5) + 257, get(5) + 1, get(4) + 4
    order = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]
    code_lengths = [0] * 19
    for i in range(ncode):
        code_lengths[order[i]] = get(3)
    table, lengths = canonical(code_lengths), []
    while len(lengths) < nlen + ndist:
        n = code = 0
        while (n, code) not in table:
            code, n = code << 1 | get(1), n + 1
        symbol = table[n, code]
        if symbol < 16:
            lengths.append(symbol)
        elif symbol == 16:
            lengths += [lengths[-1]] * (3 + get(2))
        else:
            lengths += [0] * (3 + get(3) if symbol == 17 else 11 + get(7))
    return max(lengths)


def expected_status(stream, capacity):
    # CPython's zlib run as inflate_zlib_ex classifies: header, deflate data up to capacity + 1 bytes,
    # end of data, Adler-32. None where zlib rejects a code set as "invalid ... set": inflate.h rejects
    # over-subscribed sets too but accepts incomplete ones, so it may decode on from there.
    if len(stream) < 6 or stream[0] & 0x0F != 8 or (stream[0] << 8 | stream[1]) % 31 or stream[1] & 0x20:
        return CORRUPT, b""
    decoder = zlib.decompressobj(-15)
    try:
        out = decoder.decompress(stream[2:], capacity + 1)
    except zlib.error as error:
        return (None if " set" in str(error) else CORRUPT), b""
    if len(out) > capacity:
        return INSUFFICIENT, b""
    if not decoder.eof or len(decoder.unused_data) < 4:
        return CORRUPT, b""
    if int.from_bytes(decoder.unused_data[:4], "big") != zlib.adler32(out):
        return BAD_CHECKSUM, out
    return OK, out


ACCESS_VIOLATION = 0xC0000005  # the probe's input and destination each end at a no-access page


def run_batch(probe, work, name, cases, overrun=False):
    # overrun: an access past an input or destination returns a message instead of failing.
    records, results = work / (name + ".records"), work / (name + ".results")
    records.write_bytes(b"".join(struct.pack("<II", capacity, len(stream)) + stream for stream, capacity in cases))
    result = subprocess.run([str(probe), "batch", str(records), str(results)], capture_output=True, text=True,
                            timeout=60)
    if overrun and result.returncode == ACCESS_VIOLATION:
        return "access violation past an input or destination"
    assert result.returncode == 0, "%s: exit %d: %s%s" % (name, result.returncode, result.stdout, result.stderr)
    data, at, decoded = results.read_bytes(), 0, []
    for _ in cases:
        size = int.from_bytes(data[at + 1:at + 5], "little")
        decoded.append((data[at], data[at + 5:at + 5 + size]))
        at += 5 + size
    assert at == len(data), name + ": trailing result bytes"
    return decoded


def random_cases(rng, streams):
    # Seeded streams of up to 12 KiB from several sources, levels and strategies, some with a flush
    # between blocks; each at exact capacity, one byte short, truncated and with one to three bits flipped.
    strategies = [zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED]
    sources = [
        lambda n: rng.randbytes(n),
        lambda n: corpus(rng, n),
        lambda n: words(rng, n),
        # Geometric byte values: rare symbols get codes longer than the decoder's table.
        lambda n: bytes(min(255, int(rng.expovariate(rng.choice([0.3, 0.6, 1.2])))) for _ in range(n)),
    ]
    cases, long_codes = [], 0
    for _ in range(streams):
        data = rng.choice(sources)(rng.choice([rng.randint(0, 300), rng.randint(300, 12288)]))
        encoder = zlib.compressobj(rng.randint(0, 9), zlib.DEFLATED, 15, 9, rng.choice(strategies))
        if len(data) > 64 and rng.random() < 0.3:
            cut = rng.randrange(len(data))
            stream = encoder.compress(data[:cut]) + encoder.flush(rng.choice([zlib.Z_SYNC_FLUSH, zlib.Z_FULL_FLUSH]))
            stream += encoder.compress(data[cut:]) + encoder.flush()
        else:
            stream = encoder.compress(data) + encoder.flush()
        long_codes += longest_code(stream) > 10
        cases.append((stream, len(data)))
        if data:
            cases.append((stream, len(data) - 1))
        cases.append((stream[:rng.randrange(len(stream))], len(data)))
        damaged = bytearray(stream)
        for _ in range(rng.randint(1, 3)):
            damaged[rng.randrange(2, len(damaged))] ^= 1 << rng.randrange(8)
        cases.append((bytes(damaged), len(data) + rng.choice([0, 0, 64])))
    return cases, long_codes


def compare(name, cases, fast, slow, in_stored=frozenset()):
    # Equal to the bit-at-a-time build in every case, and to CPython's zlib where it classifies the code sets.
    # in_stored: indices of cases truncated inside a stored block's length fields or bytes. inflate.h checks
    # the block's length against the input before the output, so such a block that would not fit either is
    # corrupt_stream where zlib, copying the bytes it has, reports insufficient output first.
    compared = lenient = 0
    for index, ((stream, capacity), got, reference) in enumerate(zip(cases, fast, slow)):
        assert got == reference, "%s case %d: %s %d bytes, bit-at-a-time %s %d bytes" % (
            name, index, STATUS[got[0]], len(got[1]), STATUS[reference[0]], len(reference[1]))
        status, out = expected_status(stream, capacity)
        if index in in_stored and status == INSUFFICIENT and got[0] == CORRUPT:
            lenient += 1
            continue
        if status is None:
            if got[0] != CORRUPT:
                lenient += 1
                continue
            status = CORRUPT
        assert (got[0], got[1] if got[0] in (OK, BAD_CHECKSUM) else b"") == (status, out), \
            "%s case %d: %s, zlib %s" % (name, index, STATUS[got[0]], STATUS[status])
        compared += 1
    counts = [sum(1 for status, _ in fast if status == s) for s in range(4)]
    return compared, lenient, ", ".join("%s %d" % (STATUS[s], counts[s]) for s in range(4))


def expect_differential(probe, bitwise, work, rng):
    streams = 900
    cases, long_codes = random_cases(rng, streams)
    assert long_codes >= 20, "only %d streams have codes longer than 10 bits" % long_codes
    fast, slow = run_batch(probe, work, "random", cases), run_batch(bitwise, work, "random_bitwise", cases)
    compared, lenient, counts = compare("random", cases, fast, slow)
    print("%d random streams (%d with codes over 10 bits) as %d cases: equal to bit-at-a-time decoding in all, "
          "to zlib in %d (%d decoded past an incomplete code set zlib rejects); %s" % (
              streams, long_codes, len(cases), compared, lenient, counts))
    return cases, slow


def zlib_wrap(raw, data):
    return b"\x78\x01" + raw + zlib.adler32(data).to_bytes(4, "big")


def stored_after_fixed(literals, stored):
    # A fixed block of literals, so the stored block that follows starts at any bit position, then a
    # final fixed block whose length-3 match at distance 1 reaches back into the stored bytes.
    bits = Bits()
    bits.put(0, 1)
    bits.put(1, 2)
    for literal in literals:
        bits.code(0x30 + literal, 8) if literal < 144 else bits.code(0x190 + literal - 144, 9)
    bits.code(0, 7)
    bits.put(0, 1)
    bits.put(0, 2)
    bits.put(0, -bits.count % 8)
    at = 2 + bits.count // 8  # the stored block's length fields in the zlib stream
    bits.put(len(stored) | (len(stored) ^ 0xFFFF) << 16, 32)
    for byte in stored:
        bits.put(byte, 8)
    bits.put(1, 1)
    bits.put(1, 2)
    bits.code(1, 7)
    bits.code(0, 5)
    bits.code(0, 7)
    data = literals + stored + (literals + stored)[-1:] * 3
    return zlib_wrap(bits.to_bytes(), data), data, range(at, at + 4 + len(stored))


def boundary_cases(rng):
    # Streams whose ends meet every output capacity and input length: matches at each distance from 1 to 16
    # with lengths around 8 and 16 bytes and up to 258, some after a sync flush; stored blocks of 0 and 65535
    # bytes and stored blocks at every bit position. Each runs at every capacity up to one past its size
    # and truncated to every length, at full capacity and at a random one.
    streams = []
    for distance in range(1, 17):
        for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FIXED):
            data = bytearray(rng.randbytes(40))
            for length in list(range(3, 20)) + [24, 31, 32, 33, 258]:
                pattern = rng.randbytes(distance)
                data += (pattern * (length // distance + 2))[:distance + length] + rng.randbytes(1)
            encoder = zlib.compressobj(9, zlib.DEFLATED, 15, 9, strategy)
            if distance % 4 == 0:
                cut = rng.randrange(len(data))
                stream = encoder.compress(data[:cut]) + encoder.flush(zlib.Z_SYNC_FLUSH)
                stream += encoder.compress(data[cut:]) + encoder.flush()
            else:
                stream = encoder.compress(data) + encoder.flush()
            streams.append((stream, bytes(data), range(0)))
    for count in range(8):
        # count 9-bit literal codes put the stored block at bit position count + 2 modulo 8.
        for stored in (b"", rng.randbytes(1), rng.randbytes(300)):
            literals = bytes(rng.randrange(144, 256) for _ in range(count))
            literals += bytes(rng.randrange(144) for _ in range(rng.randint(1, 300)))
            streams.append(stored_after_fixed(literals, stored))
    cases, in_stored = [], set()
    for stream, data, stored_bytes in streams:
        assert reference(stream, "zlib") == data
        cases += [(stream, capacity) for capacity in range(len(data) + 2)]
        for cut in range(len(stream)):
            if cut in stored_bytes:
                in_stored |= {len(cases), len(cases) + 1}
            cases += [(stream[:cut], len(data)), (stream[:cut], rng.randrange(len(data) + 1))]
    big = rng.randbytes(65535)
    stream = zlib_wrap(b"\x01\xff\xff\x00\x00" + big, big)
    assert reference(stream, "zlib") == big
    cases += [(stream, capacity) for capacity in (0, 1, 65534, 65535, 65536)]
    cases += [(stream[:cut], 65535) for cut in range(len(stream) - 12, len(stream))]
    return cases, len(streams) + 1, in_stored


def expect_boundaries(probe, bitwise, work, rng):
    cases, streams, in_stored = boundary_cases(rng)
    fast, slow = run_batch(probe, work, "boundary", cases), run_batch(bitwise, work, "boundary_bitwise", cases)
    compared, lenient, counts = compare("boundary", cases, fast, slow, in_stored)
    print("%d boundary streams as %d cases: equal to bit-at-a-time decoding in all, to zlib in %d (%d truncated "
          "in a stored block that does not fit either); %s" % (streams, len(cases), compared, lenient, counts))
    return cases, slow


LENGTH_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163,
               195, 227, 258]
LENGTH_EXTRA = [0] * 8 + [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4 + [5] * 4 + [0]
DISTANCE_BASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049,
                 3073, 4097, 6145, 8193, 12289, 16385, 24577]


def fixed_tokens(tokens):
    # One final fixed block from literals (ints) and (length, distance) matches, wrapped as zlib with its data.
    bits, data = Bits(), bytearray()
    bits.put(1, 1)
    bits.put(1, 2)

    def symbol(value):
        if value < 144:
            bits.code(0x30 + value, 8)
        elif value < 256:
            bits.code(0x190 + value - 144, 9)
        elif value < 280:
            bits.code(value - 256, 7)
        else:
            bits.code(0xC0 + value - 280, 8)

    for token in tokens:
        if isinstance(token, int):
            symbol(token)
            data.append(token)
            continue
        length, distance = token
        code = max(i for i, base in enumerate(LENGTH_BASE) if base <= length) if length < 258 else 28
        symbol(257 + code)
        bits.put(length - LENGTH_BASE[code], LENGTH_EXTRA[code])
        code = max(i for i, base in enumerate(DISTANCE_BASE) if base <= distance)
        bits.code(code, 5)
        bits.put(distance - DISTANCE_BASE[code], max(0, code // 2 - 1))
        for _ in range(length):
            data.append(data[-distance])
    symbol(256)
    return zlib_wrap(bits.to_bytes(), bytes(data)), bytes(data)


def window_cases(rng):
    # Streams of 96 to 160 KiB whose matches reach far back. zlib's encoder never reaches distance 32768
    # (it keeps 262 bytes of lookahead out of its window), so three are written by hand as fixed blocks with matches at
    # the maximum distance 32768 and lengths up to 258, the last one ending at the end of the output; two
    # more are zlib's dynamic blocks over data that repeats runs from 24 to 32.5 KiB back. Each runs at
    # exact capacity, one byte short, at the end of an inner 32768-byte-distance match, and truncated.
    streams = []
    for _ in range(3):
        tokens, size, ends = [rng.randrange(256) for _ in range(33000)], 33000, []
        target = rng.randint(96, 160) * 1024
        while size < target:
            if rng.random() < 0.3:
                run = [rng.randrange(256) for _ in range(rng.randint(1, 20))]
                tokens += run
                size += len(run)
                continue
            distance = rng.choice([32768, 32768, 32767, rng.randint(24577, 32768), rng.randint(1, 32768)])
            length = rng.choice([258, 258, 257, rng.randint(3, 258)])
            tokens.append((length, distance))
            size += length
            if distance == 32768:
                ends.append(size)
        tokens.append((258, 32768))
        stream, data = fixed_tokens(tokens)
        streams.append((stream, data, rng.sample(ends, 4)))
    for _ in range(2):
        data, target = bytearray(rng.randbytes(33000)), rng.randint(96, 160) * 1024
        while len(data) < target:
            start = len(data) - rng.randint(24 * 1024, 32506)
            data += data[start:start + rng.randint(3, 258)] + rng.randbytes(rng.randint(0, 3))
        streams.append((compress(bytes(data)), bytes(data), []))
    cases = []
    for stream, data, ends in streams:
        assert reference(stream, "zlib") == data
        cases += [(stream, len(data)), (stream, len(data) - 1)] + [(stream, end) for end in ends]
        cases += [(stream[:cut], len(data)) for cut in sorted(rng.sample(range(len(stream)), 6))]
    return cases, len(streams)


def expect_window(probe, bitwise, work, rng):
    cases, streams = window_cases(rng)
    fast, slow = run_batch(probe, work, "window", cases), run_batch(bitwise, work, "window_bitwise", cases)
    compared, lenient, counts = compare("window", cases, fast, slow)
    assert not lenient, "window: %d cases decoded where zlib rejects a code set" % lenient
    print("%d window streams as %d cases: equal to bit-at-a-time decoding and to zlib in all; %s" % (
        streams, len(cases), counts))


def expect_faulty_caught(faulty, work, cases, reference):
    got = run_batch(faulty, work, "random_faulty", cases)
    wrong = sum(1 for a, b in zip(got, reference) if a != b)
    assert wrong, "the faulty-table control decoded every random case like the reference"
    print("faulty-table control: %d of %d random cases differ from bit-at-a-time decoding, caught" % (wrong, len(cases)))


def expect_overrun_caught(name, faulty, work, label, cases):
    # A fast loop entered one output byte short of the longest match writes past a destination when a
    # 258-byte match starts 257 bytes before the capacity; one entered with 7 input bytes left loads a
    # byte past the input.
    got = run_batch(faulty, work, name, cases, overrun=True)
    assert isinstance(got, str), "the %s control stayed within every input and destination" % name
    print("%s control: %s in the %s case, caught" % (name, got, label))


def expect_distance_caught(faulty, work):
    # A fast loop without its distance check copies the match that starts before the first output byte.
    result, _ = run(faulty, work, "faulty_distance", fixed_match_block(b"ABCD", 4, 1, 0) + bytes(32), 4096, "raw")
    got = result.stdout.strip()
    assert got != "rejected after 4 bytes", "the faulty-distance control rejected the match before copying"
    print("faulty-distance control: %s instead of rejected after 4 bytes, caught" % (got or "exit 0x%X" % result.returncode))


def main(probe, bitwise, faulty, faulty_margin, faulty_input_margin, faulty_distance):
    probe, bitwise, faulty = Path(probe).resolve(), Path(bitwise).resolve(), Path(faulty).resolve()
    faulty_margin, faulty_input_margin = Path(faulty_margin).resolve(), Path(faulty_input_margin).resolve()
    faulty_distance = Path(faulty_distance).resolve()
    rng = random.Random(1149)
    text = corpus(rng, 96 * 1024)
    noise = rng.randbytes(70000)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)

        stored = zlib.compress(noise, 0)
        assert first_block_type(stored) == 0
        expect_decoded(probe, work, "stored", stored, noise)

        fixed = compress(text, strategy=zlib.Z_FIXED)
        assert first_block_type(fixed) == 1
        expect_decoded(probe, work, "fixed", fixed, text)

        dynamic = compress(text)
        assert first_block_type(dynamic) == 2
        expect_decoded(probe, work, "dynamic", dynamic, text)
        expect_decoded(probe, work, "dynamic_spare_capacity", dynamic, text, len(text) + 4096)
        sparse = words(rng, 32 * 1024)
        sparse_stream = compress(sparse)
        assert first_block_type(sparse_stream) == 2
        expect_decoded(probe, work, "dynamic_sparse", sparse_stream, sparse)

        # A sync flush ends the first part with an empty stored block; the second part copies from the
        # first across it, so the decoder must keep its window and bit position over the boundary.
        head, tail = noise[:20000], noise[5000:9000] + noise[100:3000] + text[:8192]
        encoder = zlib.compressobj(6)
        flushed = encoder.compress(head) + encoder.flush(zlib.Z_SYNC_FLUSH)
        assert flushed.endswith(b"\x00\x00\xff\xff")
        flushed += encoder.compress(tail) + encoder.flush()
        expect_decoded(probe, work, "sync_flush", flushed, head + tail)

        expect_decoded(probe, work, "empty", zlib.compress(b""), b"")

        # Controls: streams that do not fit or cannot be decoded are rejected.
        expect_rejected(probe, work, "dynamic_short_capacity", dynamic, len(text) - 1, invalid=False)
        expect_rejected(probe, work, "dynamic_truncated", dynamic[:len(dynamic) // 2], len(text))

        # The distance bound, on bare deflate data with room to spare: after "ABCD", distance 4 (code 3)
        # starts the match at the first output byte; distance 5 (code 4, extra bit 0) starts it one byte
        # before the output and must be rejected with only the four literals decoded.
        expect_decoded(probe, work, "raw_distance_4", fixed_match_block(b"ABCD", 3, 0, 0), b"ABCDABC", 64, "raw")
        expect_rejected(probe, work, "raw_distance_5", fixed_match_block(b"ABCD", 4, 1, 0), 64, "raw", 4)
        # The same with input and capacity to spare, so the decoder's fast loop meets the match.
        expect_rejected(probe, work, "raw_distance_5_fast", fixed_match_block(b"ABCD", 4, 1, 0) + bytes(32), 4096,
                        "raw", 4)

        # A distance code set with a single 1-bit code is incomplete; zlib accepts it, so the table and
        # the bit-at-a-time loop must both decode it.
        single, single_data = single_distance_block()
        expect_decoded(probe, work, "raw_single_distance", single, single_data, 64, "raw")
        expect_decoded(bitwise, work, "raw_single_distance_bw", single, single_data, 64, "raw")
        # With input and capacity to spare the fast loop decodes it too; the unused distance code stays a miss.
        expect_decoded(probe, work, "raw_single_distance_fast", single + bytes(32), single_data, 4096, "raw")

        cases, reference = expect_differential(probe, bitwise, work, rng)
        expect_faulty_caught(faulty, work, cases, reference)
        expect_overrun_caught("faulty-input-margin", faulty_input_margin, work, "random", cases)
        # Its own seed, so the random case keeps its streams.
        cases, _ = expect_boundaries(probe, bitwise, work, random.Random(1149 + 1))
        expect_overrun_caught("faulty-margin", faulty_margin, work, "boundary", cases)
        expect_distance_caught(faulty_distance, work)
        # Its own seed as well: long streams with matches up to the 32 KiB window.
        expect_window(probe, bitwise, work, random.Random(1149 + 2))
    print("inflate.h matches zlib %s in 11 cases and rejects 4 controls" % zlib.ZLIB_RUNTIME_VERSION)


BENCH_SIZE, BENCH_REPEATS, BENCH_ROUNDS = 16 << 20, 3, 5


def bench_words(rng, size):
    # Words from a 2000-word vocabulary: matches and a sparse literal alphabet.
    vocabulary = [bytes(rng.choices(b"abcdefghijklmnopqrstuvwxyz", k=rng.randint(2, 10))) for _ in range(2000)]
    out = bytearray()
    while len(out) < size:
        out += rng.choice(vocabulary) + b" "
    return bytes(out[:size])


def bench_skewed(rng, size):
    # Near-incompressible but not random, about 7.3 bits per byte: dynamic blocks of mostly literals.
    table = rng.choices(range(256), [1.0 / (1 + i) ** 0.6 for i in range(256)], k=1 << 16)
    out = bytearray()
    while len(out) < size:
        out += bytes(table[b] for b in rng.randbytes(min(1 << 16, size - len(out))))
    return bytes(out)


def bench(probes):
    probes = [str(Path(probe).resolve()) for probe in probes]
    rng = random.Random(20261124)
    kinds = [("stored", 0, lambda: compress(rng.randbytes(BENCH_SIZE), 0)),
             ("fixed", 1, lambda: compress(corpus(rng, BENCH_SIZE), 6, zlib.Z_FIXED)),
             ("dynamic_text", 2, lambda: compress(bench_words(rng, BENCH_SIZE), 6)),
             ("dynamic_skewed", 2, lambda: compress(bench_skewed(rng, BENCH_SIZE), 6))]
    print("decoded MiB/s, %d MiB streams, best of %d rounds after one dropped" % (BENCH_SIZE >> 20, BENCH_ROUNDS))
    print("%-15s %s %10s" % ("", " ".join("%10s" % ("build %d" % i) for i in range(len(probes))), "zlib"))
    with tempfile.TemporaryDirectory() as temporary:
        records = Path(temporary) / "bench.records"
        for name, block_type, make in kinds:
            stream = make()
            data = zlib.decompress(stream)
            assert len(data) == BENCH_SIZE and first_block_type(stream) == block_type, name
            records.write_bytes((struct.pack("<II", BENCH_SIZE, len(stream)) + stream) * BENCH_REPEATS)
            checksum = "%08x" % (zlib.adler32(data) * BENCH_REPEATS & 0xFFFFFFFF)
            best = [float("inf")] * (len(probes) + 1)
            for turn in range(BENCH_ROUNDS + 1):
                for index, probe in enumerate(probes):
                    result = subprocess.run([probe, "batch", str(records), os.devnull], capture_output=True,
                                            text=True, check=True)
                    # "batch N records, B bytes out, T us in the decoder, output checksum C"
                    words_out = result.stdout.split()
                    assert words_out[-1] == checksum, "%s: %s" % (probe, result.stdout)
                    if turn:
                        best[index] = min(best[index], int(words_out[6]) / 1e6)
                start = time.perf_counter()
                for _ in range(BENCH_REPEATS):
                    zlib.decompress(stream, 15, BENCH_SIZE)
                if turn:
                    best[-1] = min(best[-1], time.perf_counter() - start)
            mib = BENCH_SIZE * BENCH_REPEATS / (1 << 20)
            print("%-15s %s" % (name, " ".join("%10.1f" % (mib / seconds) for seconds in best)), flush=True)
    for index, probe in enumerate(probes):
        print("build %d: %s" % (index, probe))
    print("zlib: CPython %s, zlib %s" % (sys.version.split()[0], zlib.ZLIB_RUNTIME_VERSION))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--bench":
        bench(sys.argv[2:])
        sys.exit()
    if len(sys.argv) != 7:
        sys.exit("usage: startup_inflate_check.py <startup_inflate_probe.exe> <startup_inflate_probe_bitwise.exe> "
                 "<startup_inflate_probe_faulty.exe> <startup_inflate_probe_faulty_margin.exe> "
                 "<startup_inflate_probe_faulty_input_margin.exe> <startup_inflate_probe_faulty_distance.exe>\n"
                 "       startup_inflate_check.py --bench <startup_inflate_probe.exe>...")
    main(*sys.argv[1:])
