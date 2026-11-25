// Experimental (docs/startup-runtime.md): the caller-buffer layout of an XGameSave blob read.
// One 24-byte record per blob (payload pointer at offset 16), then each name, a NUL and the
// payload. The records point into that same buffer. Synchronous and asynchronous reads share this.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace x2w_save_pack {

struct BlobInfo {
    const char* name;
    std::uint32_t size;
};
struct Blob {
    BlobInfo info;
    unsigned char* data;
};
static_assert(sizeof(Blob) == 24 && offsetof(Blob, data) == 16, "XGameSaveBlob layout");

struct Piece {
    std::string name;
    std::vector<unsigned char> bytes;
};

struct PieceSize {
    std::size_t name_bytes;
    std::size_t data_bytes;
};

namespace detail {
inline bool add_piece_size(std::size_t& bytes, std::size_t name_bytes, std::size_t data_bytes)
{
    if (data_bytes > UINT32_MAX) return false;
    if (name_bytes >= SIZE_MAX - data_bytes) return false;
    const std::size_t extra = name_bytes + 1 + data_bytes;
    if (extra > SIZE_MAX - bytes) return false;
    bytes += extra;
    return true;
}
}  // namespace detail

// False when a payload exceeds uint32_t (BlobInfo::size) or the total byte count does not fit in
// size_t. out is unchanged then.
inline bool bytes_needed(const PieceSize* sizes, std::size_t count, std::size_t& out)
{
    if (count > SIZE_MAX / sizeof(Blob)) return false;
    if (count && !sizes) return false;
    std::size_t bytes = count * sizeof(Blob);
    for (std::size_t i = 0; i < count; ++i)
        if (!detail::add_piece_size(bytes, sizes[i].name_bytes, sizes[i].data_bytes)) return false;
    out = bytes;
    return true;
}

// False when a payload exceeds uint32_t (BlobInfo::size) or the total byte count does not fit in
// size_t. out is unchanged then.
inline bool bytes_needed(const std::vector<Piece>& pieces, std::size_t& out)
{
    if (pieces.size() > SIZE_MAX / sizeof(Blob)) return false;
    std::size_t bytes = pieces.size() * sizeof(Blob);
    for (const Piece& piece : pieces)
        if (!detail::add_piece_size(bytes, piece.name.size(), piece.bytes.size())) return false;
    out = bytes;
    return true;
}

// dest has room for bytes_needed. Pointers in the records address dest itself.
inline void write(const std::vector<Piece>& pieces, void* dest)
{
    if (pieces.empty()) return;
    auto* records = static_cast<Blob*>(dest);
    auto* cursor = reinterpret_cast<unsigned char*>(records + pieces.size());
    for (std::size_t i = 0; i < pieces.size(); ++i) {
        const Piece& piece = pieces[i];
        auto* name = reinterpret_cast<char*>(cursor);
        if (!piece.name.empty()) std::memcpy(cursor, piece.name.data(), piece.name.size());
        cursor[piece.name.size()] = 0;
        cursor += piece.name.size() + 1;
        records[i].info = {name, static_cast<std::uint32_t>(piece.bytes.size())};
        records[i].data = cursor;
        if (!piece.bytes.empty()) std::memcpy(cursor, piece.bytes.data(), piece.bytes.size());
        cursor += piece.bytes.size();
    }
}

}  // namespace x2w_save_pack