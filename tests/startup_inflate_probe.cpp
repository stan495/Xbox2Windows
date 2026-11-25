// Input for tests/startup_inflate_check.py: runs the DirectStorage stand-in's zlib decoder
// (src/startup_stubs/inflate.h) on one stream file and writes the decoded bytes.
//
// usage: startup_inflate_probe zlib|raw <stream> <capacity> <output>
// "zlib" runs inflate_zlib on a zlib stream; "raw" runs inflate_raw on bare deflate data, so a
// rejection cannot come from the zlib header or the Adler-32 check. Prints "inflated <size>" and
// writes the output file when the decoder accepts the stream. Otherwise it prints "rejected", and in
// raw mode the number of bytes decoded before the rejection.
//
// usage: startup_inflate_probe batch <records> <results>
// Runs inflate_zlib_ex on every record (u32 capacity, u32 size, size stream bytes, little endian)
// and writes per record the status (u8), the reported size (u32) and that many output bytes. Prints
// the record count, the output bytes of ok records, the time spent in the decoder and the sum of the
// Adler-32 values of the ok outputs (tests/startup_inflate_check.py --bench checks it).
//
// In both modes the stream and the destination each end at a no-access page, so a read past the
// input or a write past the capacity faults (exit code 0xC0000005).
#include "startup_stubs/inflate.h"

#include <windows.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

// size bytes that end at a no-access page.
class Guarded {
public:
    explicit Guarded(std::size_t size)
    {
        const std::size_t page = 4096, span = (size + page - 1) / page * page;
        pages_ = static_cast<unsigned char*>(VirtualAlloc(nullptr, span + page, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
        DWORD old_protect;
        if (!pages_ || !VirtualProtect(pages_ + span, page, PAGE_NOACCESS, &old_protect)) {
            std::fprintf(stderr, "cannot map %zu guarded bytes\n", size);
            std::exit(2);
        }
        data_ = pages_ + span - size;
    }
    ~Guarded() { VirtualFree(pages_, 0, MEM_RELEASE); }
    Guarded(const Guarded&) = delete;
    Guarded& operator=(const Guarded&) = delete;
    unsigned char* data() const { return data_; }

private:
    unsigned char* pages_ = nullptr;
    unsigned char* data_ = nullptr;
};

int batch(const char* records_path, const char* results_path)
{
    std::ifstream file(records_path, std::ios::binary);
    const std::vector<unsigned char> records{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    std::ofstream results(results_path, std::ios::binary);
    if (!file || !results) {
        std::fprintf(stderr, "cannot open %s or %s\n", records_path, results_path);
        return 2;
    }
    auto u32 = [&](std::size_t at) {
        return std::uint32_t{records[at]} | std::uint32_t{records[at + 1]} << 8 | std::uint32_t{records[at + 2]} << 16 |
               std::uint32_t{records[at + 3]} << 24;
    };
    std::size_t count = 0, total = 0;
    std::uint32_t checksum = 0;
    std::chrono::steady_clock::duration spent{};
    for (std::size_t at = 0; at < records.size(); ++count) {
        if (records.size() - at < 8 || records.size() - at - 8 < u32(at + 4)) {
            std::fprintf(stderr, "record %zu is truncated\n", count);
            return 2;
        }
        const std::size_t capacity = u32(at), size = u32(at + 4);
        const Guarded input(size), destination(capacity);
        if (size) std::memcpy(input.data(), records.data() + at + 8, size);
        std::memset(destination.data(), 0xA5, capacity);  // also commits the pages before the clock starts
        std::size_t produced = 0;
        const auto start = std::chrono::steady_clock::now();
        const auto status = x2w_inflate::inflate_zlib_ex(input.data(), size, destination.data(), capacity, produced);
        spent += std::chrono::steady_clock::now() - start;
        at += 8 + size;
        if (status == x2w_inflate::InflateStatus::ok) {
            total += produced;
            checksum += x2w_inflate::adler32(destination.data(), produced);
        }
        const unsigned char head[5] = {static_cast<unsigned char>(status), static_cast<unsigned char>(produced),
                                       static_cast<unsigned char>(produced >> 8), static_cast<unsigned char>(produced >> 16),
                                       static_cast<unsigned char>(produced >> 24)};
        results.write(reinterpret_cast<const char*>(head), sizeof(head));
        results.write(reinterpret_cast<const char*>(destination.data()), static_cast<std::streamsize>(produced));
    }
    if (!results.flush()) {
        std::fprintf(stderr, "cannot write %s\n", results_path);
        return 2;
    }
    std::printf("batch %zu records, %zu bytes out, %lld us in the decoder, output checksum %08x\n", count, total,
                static_cast<long long>(std::chrono::duration_cast<std::chrono::microseconds>(spent).count()),
                static_cast<unsigned>(checksum));
    return 0;
}

int main(int argc, char** argv)
{
    if (argc == 4 && std::strcmp(argv[1], "batch") == 0) return batch(argv[2], argv[3]);
    const bool raw = argc == 5 && std::strcmp(argv[1], "raw") == 0;
    if (argc != 5 || (!raw && std::strcmp(argv[1], "zlib") != 0)) {
        std::fprintf(stderr, "usage: startup_inflate_probe zlib|raw <stream> <capacity> <output>\n"
                             "       startup_inflate_probe batch <records> <results>\n");
        return 2;
    }
    std::ifstream file(argv[2], std::ios::binary);
    if (!file) {
        std::fprintf(stderr, "cannot read %s\n", argv[2]);
        return 2;
    }
    const std::vector<unsigned char> bytes{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    const std::size_t capacity = std::strtoull(argv[3], nullptr, 10);
    const Guarded stream(bytes.size()), destination(capacity);
    if (!bytes.empty()) std::memcpy(stream.data(), bytes.data(), bytes.size());
    std::size_t produced = 0;
    bool ok;
    if (raw) {
        x2w_inflate::Stream s{stream.data(), bytes.size()};
        s.out = destination.data();
        s.out_size = capacity;
        ok = x2w_inflate::inflate_raw(s);
        produced = s.out_pos;
    } else {
        ok = x2w_inflate::inflate_zlib(stream.data(), bytes.size(), destination.data(), capacity, &produced);
    }
    if (!ok) {
        if (raw)
            std::printf("rejected after %zu bytes\n", produced);
        else
            std::printf("rejected\n");
        return 1;
    }
    std::ofstream out(argv[4], std::ios::binary);
    out.write(reinterpret_cast<const char*>(destination.data()), static_cast<std::streamsize>(produced));
    if (!out) {
        std::fprintf(stderr, "cannot write %s\n", argv[4]);
        return 2;
    }
    std::printf("inflated %zu\n", produced);
    return 0;
}
