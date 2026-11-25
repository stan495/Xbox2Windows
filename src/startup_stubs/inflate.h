// Experimental (docs/startup-runtime.md): a small zlib (RFC 1950) / deflate (RFC 1951)
// decoder for the DirectStorage stand-in. Written from the RFCs. Huffman codes up to fast_bits long
// decode with one table lookup; longer codes and the last byte of input take canonical-code decoding
// one bit at a time. While 8 input bytes and 258 output bytes remain, codes() decodes whole symbols
// from a 64-bit bit buffer without per-symbol bounds checks.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

// tests/startup_inflate_check.py builds its reference probe with 0 (bit-at-a-time decoding only) and
// its negative controls with the X2W_INFLATE_FAULTY_* macros. The functions here are inline, so every
// translation unit linked into one binary must see the same values (one-definition rule).
#ifndef X2W_INFLATE_FAST_BITS
#define X2W_INFLATE_FAST_BITS 10
#endif

// The fast loop's 8-byte loads take the first input byte as the lowest.
#if !defined(_MSC_VER) && !(defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__)
#error "inflate.h assumes a little-endian target"
#endif

namespace x2w_inflate {

constexpr int fast_bits = X2W_INFLATE_FAST_BITS;
// Table entries keep the code length in 4 bits.
static_assert(fast_bits >= 0 && fast_bits <= 15, "X2W_INFLATE_FAST_BITS must be 0 to 15");
#ifndef X2W_INFLATE_FAULTY_MARGIN
constexpr std::size_t fast_out_margin = 258;  // the longest match
#else  // negative control for tests/startup_inflate_check.py: one byte short of the longest match
constexpr std::size_t fast_out_margin = 257;
#endif
#ifndef X2W_INFLATE_FAULTY_INPUT_MARGIN
constexpr std::size_t fast_in_margin = 8;  // one 8-byte load
#else  // negative control: the load reads one byte past the input
constexpr std::size_t fast_in_margin = 7;
#endif

struct Stream {
    const unsigned char* in;
    std::size_t in_size;
    std::size_t in_pos = 0;
    unsigned char* out;
    std::size_t out_size;
    std::size_t out_pos = 0;
    std::uint32_t bit_buffer = 0;
    int bit_count = 0;
    bool failed = false;
    bool overflow = false;
};

inline std::uint32_t bits(Stream& s, int need) noexcept
{
    std::uint32_t value = s.bit_buffer;
    while (s.bit_count < need) {
        if (s.in_pos == s.in_size) {
            s.failed = true;
            return 0;
        }
        value |= static_cast<std::uint32_t>(s.in[s.in_pos++]) << s.bit_count;
        s.bit_count += 8;
    }
    s.bit_buffer = value >> need;
    s.bit_count -= need;
    return value & ((std::uint32_t{1} << need) - 1);
}

struct Huffman {
    std::uint16_t count[16];
    std::uint16_t symbol[288];
    // Indexed by the next fast_bits input bits: symbol << 4 | code length, or 0 for the slow path.
    std::uint16_t fast[1 << fast_bits];
};

// Builds canonical decoding tables from code lengths; returns false for an over-subscribed set.
inline bool construct(Huffman& h, const std::uint8_t* lengths, int n) noexcept
{
    for (auto& c : h.count) c = 0;
    for (int i = 0; i < n; ++i) ++h.count[lengths[i]];
    int left = 1;
    for (int len = 1; len <= 15; ++len) {
        left <<= 1;
        left -= h.count[len];
        if (left < 0) return false;
    }
    std::uint16_t offsets[16];
    offsets[1] = 0;
    for (int len = 1; len < 15; ++len) offsets[len + 1] = static_cast<std::uint16_t>(offsets[len] + h.count[len]);
    for (int i = 0; i < n; ++i)
        if (lengths[i]) h.symbol[offsets[lengths[i]]++] = static_cast<std::uint16_t>(i);
    for (auto& f : h.fast) f = 0;
    // Canonical codes in order; input bits arrive first bit lowest, so each code is stored reversed.
    for (int len = 1, code = 0, index = 0; len <= fast_bits; ++len, code <<= 1) {
        for (int k = 0; k < h.count[len]; ++k, ++code, ++index) {
            int reversed = 0;
            for (int b = 0; b < len; ++b) reversed |= ((code >> b) & 1) << (len - 1 - b);
#ifndef X2W_INFLATE_FAULTY_TABLE
            const int entry = h.symbol[index] << 4 | len;
#else  // negative control for tests/startup_inflate_check.py: codes fast_bits long decode as the next symbol
            const int entry = (h.symbol[index] + (len == fast_bits)) << 4 | len;
#endif
            for (int i = reversed; i < (1 << fast_bits); i += 1 << len) h.fast[i] = static_cast<std::uint16_t>(entry);
        }
    }
    return true;
}

inline int decode(Stream& s, const Huffman& h) noexcept
{
    // bit_count stays below 8 between calls, so two more input bytes hold at least 16 bits. The peek
    // does not consume them; bits() takes only the code's length, as the loop below would.
    if (s.in_size - s.in_pos >= 2) {
        const std::uint32_t window = s.bit_buffer | std::uint32_t{s.in[s.in_pos]} << s.bit_count |
                                     std::uint32_t{s.in[s.in_pos + 1]} << (s.bit_count + 8);
        const int entry = h.fast[window & ((1u << fast_bits) - 1)];
        if (entry) {
            bits(s, entry & 15);
            return entry >> 4;
        }
    }
    int code = 0, first = 0, index = 0;
    for (int len = 1; len <= 15; ++len) {
        code |= static_cast<int>(bits(s, 1));
        if (s.failed) return -1;
        const int count = h.count[len];
        if (code - count < first) return h.symbol[index + (code - first)];
        index += count;
        first += count;
        first <<= 1;
        code <<= 1;
    }
    return -1;
}

// Writes length bytes from distance back (distance >= 1, both already checked) and nothing past them.
inline void copy_match(unsigned char* out, std::size_t distance, std::size_t length) noexcept
{
    const unsigned char* from = out - distance;
    if (fast_bits > 0 && distance >= 8 && length >= 8) {
        // No chunk overlaps its source; the last one ends at the match end and rewrites bytes already final.
        for (; length > 8; length -= 8, out += 8, from += 8) std::memcpy(out, from, 8);
        std::memcpy(out + length - 8, from + length - 8, 8);
    } else if (fast_bits > 0 && distance == 1) {
        std::memset(out, from[0], length);
    } else {
        for (std::size_t i = 0; i < length; ++i) out[i] = from[i];
    }
}

inline bool codes(Stream& s, const Huffman& lencode, const Huffman& distcode) noexcept
{
    static const std::uint16_t length_base[29] = {3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
                                                  35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258};
    static const std::uint8_t length_extra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
                                                  3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
    static const std::uint16_t dist_base[30] = {1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513,
                                                769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577};
    static const std::uint8_t dist_extra[30] = {0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
                                                7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13};
    for (;;) {
        // While 8 input bytes and the longest match of output remain, one 8-byte load refills a 64-bit
        // buffer to at least 56 bits, more than a symbol with its extra bits takes (2 * fast_bits + 18),
        // and no symbol can overflow the output. This loop takes only whole valid symbols that both
        // tables decode; anything else (end of block, a longer code, an invalid symbol or distance) is
        // left unconsumed for the careful loop below. On exit the whole bytes pulled in but not consumed
        // go back to in_pos, so bit_count is below 8 again as decode(), stored() and the trailer expect.
        if (fast_bits > 0 && s.in_size - s.in_pos >= fast_in_margin && s.out_size - s.out_pos >= fast_out_margin) {
            constexpr std::uint64_t mask = (std::uint64_t{1} << fast_bits) - 1;
            std::uint64_t buffer = s.bit_buffer;
            int count = s.bit_count;
            std::size_t in_pos = s.in_pos, out_pos = s.out_pos;
            while (s.in_size - in_pos >= fast_in_margin && s.out_size - out_pos >= fast_out_margin) {
                std::uint64_t next;
                std::memcpy(&next, s.in + in_pos, 8);  // little endian: first input bit lowest
                buffer |= next << count;
                in_pos += (63 - count) >> 3;
                count |= 56;
                const int entry = lencode.fast[buffer & mask];
                if (!entry) break;
                int used = entry & 15, symbol = entry >> 4;
                if (symbol < 256) {
                    s.out[out_pos++] = static_cast<unsigned char>(symbol);
                    buffer >>= used;
                    count -= used;
                    continue;
                }
                symbol -= 257;
                if (symbol < 0 || symbol >= 29) break;
                const std::size_t length = length_base[symbol] + ((buffer >> used) & ((1u << length_extra[symbol]) - 1));
                used += length_extra[symbol];
                const int dist_entry = distcode.fast[(buffer >> used) & mask];
                symbol = dist_entry >> 4;
                if (!dist_entry || symbol >= 30) break;
                used += dist_entry & 15;
                const std::size_t distance = dist_base[symbol] + ((buffer >> used) & ((1u << dist_extra[symbol]) - 1));
                used += dist_extra[symbol];
#ifndef X2W_INFLATE_FAULTY_DISTANCE  // negative control: no distance check in this loop
                if (distance > out_pos) break;
#endif
                buffer >>= used;
                count -= used;
                copy_match(s.out + out_pos, distance, length);
                out_pos += length;
            }
            s.in_pos = in_pos - (count >> 3);
            s.bit_count = count & 7;
            s.bit_buffer = static_cast<std::uint32_t>(buffer) & ((1u << s.bit_count) - 1);
            s.out_pos = out_pos;
        }
        int symbol = decode(s, lencode);
        if (symbol < 0) return false;
        if (symbol < 256) {
            if (s.out_pos == s.out_size) {
                s.overflow = true;
                return false;
            }
            s.out[s.out_pos++] = static_cast<unsigned char>(symbol);
        } else if (symbol == 256) {
            return true;
        } else {
            symbol -= 257;
            if (symbol >= 29) return false;
            const std::size_t length = length_base[symbol] + bits(s, length_extra[symbol]);
            symbol = decode(s, distcode);
            if (symbol < 0 || symbol >= 30) return false;
            const std::size_t distance = dist_base[symbol] + bits(s, dist_extra[symbol]);
            if (s.failed || distance > s.out_pos) return false;
            if (length > s.out_size - s.out_pos) {
                s.overflow = true;
                return false;
            }
            copy_match(s.out + s.out_pos, distance, length);
            s.out_pos += length;
        }
    }
}

inline bool stored(Stream& s) noexcept
{
    s.bit_buffer = 0;
    s.bit_count = 0;
    if (s.in_size - s.in_pos < 4) return false;
    const unsigned length = s.in[s.in_pos] | (s.in[s.in_pos + 1] << 8);
    const unsigned check = s.in[s.in_pos + 2] | (s.in[s.in_pos + 3] << 8);
    s.in_pos += 4;
    if ((length ^ check) != 0xFFFF || length > s.in_size - s.in_pos) return false;
    if (length > s.out_size - s.out_pos) {
        s.overflow = true;
        return false;
    }
    if (fast_bits > 0 && length) {  // out may be null when length is 0
        std::memcpy(s.out + s.out_pos, s.in + s.in_pos, length);
        s.out_pos += length;
        s.in_pos += length;
    } else {
        for (unsigned i = 0; i < length; ++i) s.out[s.out_pos++] = s.in[s.in_pos++];
    }
    return true;
}

struct FixedTables {
    Huffman lencode;
    Huffman distcode;
};

inline FixedTables make_fixed_tables() noexcept
{
    FixedTables tables{};
    std::uint8_t lengths[288];
    int i = 0;
    for (; i < 144; ++i) lengths[i] = 8;
    for (; i < 256; ++i) lengths[i] = 9;
    for (; i < 280; ++i) lengths[i] = 7;
    for (; i < 288; ++i) lengths[i] = 8;
    construct(tables.lencode, lengths, 288);
    for (i = 0; i < 30; ++i) lengths[i] = 5;
    construct(tables.distcode, lengths, 30);
    return tables;
}

inline bool fixed_block(Stream& s) noexcept
{
    static const FixedTables tables = make_fixed_tables();
    return codes(s, tables.lencode, tables.distcode);
}

inline bool dynamic_block(Stream& s) noexcept
{
    static const std::uint8_t order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
    const int nlen = static_cast<int>(bits(s, 5)) + 257;
    const int ndist = static_cast<int>(bits(s, 5)) + 1;
    const int ncode = static_cast<int>(bits(s, 4)) + 4;
    if (s.failed || nlen > 286 || ndist > 30) return false;
    std::uint8_t lengths[320] = {};
    for (int i = 0; i < ncode; ++i) lengths[order[i]] = static_cast<std::uint8_t>(bits(s, 3));
    Huffman lencode{}, distcode{};
    if (s.failed || !construct(lencode, lengths, 19)) return false;
    for (auto& l : lengths) l = 0;
    int index = 0;
    while (index < nlen + ndist) {
        int symbol = decode(s, lencode);
        if (symbol < 0) return false;
        if (symbol < 16) {
            lengths[index++] = static_cast<std::uint8_t>(symbol);
            continue;
        }
        std::uint8_t value = 0;
        int repeat;
        if (symbol == 16) {
            if (index == 0) return false;
            value = lengths[index - 1];
            repeat = 3 + static_cast<int>(bits(s, 2));
        } else if (symbol == 17) {
            repeat = 3 + static_cast<int>(bits(s, 3));
        } else {
            repeat = 11 + static_cast<int>(bits(s, 7));
        }
        if (s.failed || index + repeat > nlen + ndist) return false;
        while (repeat--) lengths[index++] = value;
    }
    if (lengths[256] == 0) return false;
    // Incomplete distance or literal sets with a single code are legal; over-subscribed ones are not.
    if (!construct(lencode, lengths, nlen) || !construct(distcode, lengths + nlen, ndist)) return false;
    return codes(s, lencode, distcode);
}

// Raw deflate. Returns true when the final block ended; *produced gets the output size.
inline bool inflate_raw(Stream& s) noexcept
{
    for (;;) {
        const bool last = bits(s, 1) != 0;
        const std::uint32_t type = bits(s, 2);
        if (s.failed) return false;
        const bool ok = type == 0 ? stored(s) : type == 1 ? fixed_block(s) : type == 2 ? dynamic_block(s) : false;
        if (!ok || s.failed) return false;
        if (last) return true;
    }
}

inline std::uint32_t adler32(const unsigned char* data, std::size_t size) noexcept
{
    std::uint32_t a = 1, b = 0;
    while (size) {
        std::size_t chunk = size < 5552 ? size : 5552;
        size -= chunk;
        while (chunk--) {
            a += *data++;
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    return (b << 16) | a;
}

enum class InflateStatus {
    ok,
    insufficient_output,
    corrupt_stream,
    bad_checksum,
};

// zlib stream (2-byte header, deflate data, Adler-32). Distinguishes output capacity overflow
// and Adler-32 mismatch from corrupt headers/deflate data.
inline InflateStatus inflate_zlib_ex(const std::uint8_t* input, std::size_t input_size, std::uint8_t* output,
                                     std::size_t capacity, std::size_t& out_size) noexcept
{
    out_size = 0;
    if (!input || (!output && capacity > 0) || input_size < 6 || (input[0] & 0x0F) != 8 ||
        ((input[0] << 8) | input[1]) % 31 != 0 || (input[1] & 0x20)) {
        return InflateStatus::corrupt_stream;
    }
    Stream s{input + 2, input_size - 2, 0, output, capacity};
    if (!inflate_raw(s)) {
        return s.overflow ? InflateStatus::insufficient_output : InflateStatus::corrupt_stream;
    }
    out_size = s.out_pos;
    if (s.in_size - s.in_pos < 4) return InflateStatus::corrupt_stream;
    const unsigned char* tail = s.in + s.in_pos;
    const std::uint32_t expected = (std::uint32_t{tail[0]} << 24) | (std::uint32_t{tail[1]} << 16) |
                                   (std::uint32_t{tail[2]} << 8) | tail[3];
    return adler32(output, s.out_pos) == expected ? InflateStatus::ok : InflateStatus::bad_checksum;
}

// zlib stream (2-byte header, deflate data, Adler-32). The checksum must match.
inline bool inflate_zlib(const unsigned char* in, std::size_t in_size, unsigned char* out, std::size_t out_size,
                         std::size_t* produced) noexcept
{
    std::size_t out_size_tmp = 0;
    const InflateStatus status = inflate_zlib_ex(in, in_size, out, out_size, out_size_tmp);
    if (produced) *produced = out_size_tmp;
    return status == InflateStatus::ok;
}

}  // namespace x2w_inflate
