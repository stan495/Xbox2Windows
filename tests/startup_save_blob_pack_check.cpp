// Checks src/startup_stubs/save_blob_pack.h without a runtime or disk store:
//   - packed record layout, NUL-terminated names, binary payloads and guard bytes
//   - bytes_needed arithmetic boundaries, uint32_t payload cap and overflow refusals
//   --mutate legacy-size   the earlier check (no uint32_t cap, wrapping extra); must fail
#include "startup_stubs/save_blob_pack.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

namespace {
int failures;
bool mutate_legacy_size;

void fail(const char* name, const char* why)
{
    std::printf("FAIL %s: %s\n", name, why);
    ++failures;
}

bool compute_bytes_needed(const x2w_save_pack::PieceSize* sizes, std::size_t count, std::size_t& out)
{
    if (!mutate_legacy_size) return x2w_save_pack::bytes_needed(sizes, count, out);
    // The earlier check: no UINT32_MAX cap and `extra < name_bytes` misses data_bytes == SIZE_MAX.
    if (count > SIZE_MAX / sizeof(x2w_save_pack::Blob)) return false;
    if (count && !sizes) return false;
    std::size_t bytes = count * sizeof(x2w_save_pack::Blob);
    for (std::size_t i = 0; i < count; ++i) {
        if (sizes[i].name_bytes == SIZE_MAX) return false;
        const std::size_t extra = sizes[i].name_bytes + 1 + sizes[i].data_bytes;
        if (extra < sizes[i].name_bytes || extra > SIZE_MAX - bytes) return false;
        bytes += extra;
    }
    out = bytes;
    return true;
}

void check_size_ok(const char* name, const x2w_save_pack::PieceSize* sizes, std::size_t count, std::size_t expected)
{
    std::size_t out = 0xDEADBEEF;
    if (!compute_bytes_needed(sizes, count, out)) {
        fail(name, "expected acceptance, got refusal");
        return;
    }
    if (out != expected) {
        std::printf("FAIL %s: got %zu, expected %zu\n", name, out, expected);
        ++failures;
    }
}

void check_size_refused(const char* name, const x2w_save_pack::PieceSize* sizes, std::size_t count)
{
    constexpr std::size_t sentinel = 0xDEADBEEF;
    std::size_t out = sentinel;
    if (compute_bytes_needed(sizes, count, out)) {
        std::printf("FAIL %s: expected refusal, got %zu\n", name, out);
        ++failures;
        return;
    }
    if (out != sentinel) fail(name, "refusal modified out");
}

void check_pack(const char* name, const std::vector<x2w_save_pack::Piece>& pieces)
{
    std::size_t expected = pieces.size() * sizeof(x2w_save_pack::Blob);
    for (const auto& piece : pieces) expected += piece.name.size() + 1 + piece.bytes.size();

    std::size_t needed = 0xDEADBEEF;
    if (!x2w_save_pack::bytes_needed(pieces, needed)) {
        fail(name, "bytes_needed refused valid pieces");
        return;
    }
    if (needed != expected) {
        std::printf("FAIL %s: bytes_needed got %zu, expected %zu\n", name, needed, expected);
        ++failures;
        return;
    }

    if (pieces.empty()) {
        x2w_save_pack::write(pieces, nullptr);
        return;
    }

    constexpr std::size_t guard = 32;  // multiple of alignof(Blob)
    constexpr unsigned char guard_byte = 0xA5;
    std::vector<unsigned char> raw(guard + needed + guard, guard_byte);
    unsigned char* dest = raw.data() + guard;
    x2w_save_pack::write(pieces, dest);

    for (std::size_t i = 0; i < guard; ++i) {
        if (raw[i] != guard_byte) {
            fail(name, "wrote before destination buffer");
            return;
        }
        if (raw[guard + needed + i] != guard_byte) {
            fail(name, "wrote past bytes_needed");
            return;
        }
    }

    const auto* records = reinterpret_cast<const x2w_save_pack::Blob*>(dest);
    const unsigned char* cursor = dest + pieces.size() * sizeof(x2w_save_pack::Blob);
    for (std::size_t i = 0; i < pieces.size(); ++i) {
        const auto& piece = pieces[i];
        const auto* got_name = reinterpret_cast<const unsigned char*>(records[i].info.name);
        if (got_name != cursor) {
            fail(name, "name pointer offset mismatch");
            return;
        }
        if (piece.name.size() && std::memcmp(got_name, piece.name.data(), piece.name.size()) != 0) {
            fail(name, "name bytes mismatch");
            return;
        }
        if (got_name[piece.name.size()] != 0) {
            fail(name, "name not NUL-terminated");
            return;
        }
        cursor += piece.name.size() + 1;
        if (records[i].info.size != piece.bytes.size()) {
            fail(name, "payload size mismatch");
            return;
        }
        if (records[i].data != cursor) {
            fail(name, "payload pointer offset mismatch");
            return;
        }
        if (!piece.bytes.empty() && std::memcmp(records[i].data, piece.bytes.data(), piece.bytes.size()) != 0) {
            fail(name, "payload bytes mismatch");
            return;
        }
        cursor += piece.bytes.size();
    }
    if (static_cast<std::size_t>(cursor - dest) != needed) fail(name, "packed cursor did not match bytes_needed");
}
}  // namespace

int main(int argc, char** argv)
{
    mutate_legacy_size = argc == 3 && !std::strcmp(argv[1], "--mutate") && !std::strcmp(argv[2], "legacy-size");
    if (argc != 1 && !mutate_legacy_size) return 2;

    // Layout and write checks.
    check_pack("empty pieces", {});
    check_pack("single binary payload with NULs", {{"slot0", {0x00, 0x01, 0x00, 0xFE, 0xFF}}});
    check_pack("three mixed pieces", {
        {"profile", {'o', 'k'}},
        {"empty_data", {}},
        {"", {0x42}},
    });

    // Size arithmetic acceptance boundaries.
    check_size_ok("zero count null pointer", nullptr, 0, 0);
    const x2w_save_pack::PieceSize max_u32_payload{4, UINT32_MAX};
    check_size_ok("max uint32 payload", &max_u32_payload, 1,
                  sizeof(x2w_save_pack::Blob) + 4 + 1 + std::size_t{UINT32_MAX});
    const x2w_save_pack::PieceSize exact_size_max{SIZE_MAX - sizeof(x2w_save_pack::Blob) - 1, 0};
    check_size_ok("exact SIZE_MAX total", &exact_size_max, 1, SIZE_MAX);

    // Size arithmetic refusals (out must stay untouched).
    check_size_refused("nonzero count null pointer", nullptr, 1);
    check_size_refused("record table overflow", &max_u32_payload, SIZE_MAX / sizeof(x2w_save_pack::Blob) + 1);
    const x2w_save_pack::PieceSize u32_plus_one{4, std::size_t{UINT32_MAX} + 1};
    check_size_refused("payload exceeds uint32_t", &u32_plus_one, 1);
    const x2w_save_pack::PieceSize wrap_extra{8, SIZE_MAX};
    check_size_refused("data_bytes SIZE_MAX wraparound", &wrap_extra, 1);
    const x2w_save_pack::PieceSize name_plus_data_overflow{SIZE_MAX - 10, 10};
    check_size_refused("name + 1 + data overflow", &name_plus_data_overflow, 1);
    const x2w_save_pack::PieceSize total_plus_extra_overflow{SIZE_MAX - sizeof(x2w_save_pack::Blob), 0};
    check_size_refused("total + extra overflow by 1", &total_plus_extra_overflow, 1);
    const x2w_save_pack::PieceSize two_halves[2] = {{SIZE_MAX / 2, 0}, {SIZE_MAX / 2, 0}};
    check_size_refused("two pieces overflowing total", two_halves, 2);

    if (failures) {
        std::printf("startup_save_blob_pack: %d FAILED\n", failures);
        return 1;
    }
    std::printf("startup_save_blob_pack: PASS (3 pack layouts, 3 boundaries, 7 refusals)\n");
    return 0;
}
