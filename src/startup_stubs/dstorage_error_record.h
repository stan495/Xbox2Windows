#pragma once

#include <windows.h>
#include <cstddef>
#include <cstdint>

namespace x2w_startup {

// x64 layout from Microsoft's MIT-licensed Xbox-GDK-Samples dstorage_win32.h,
// revision 1f44bdabed6e340170e2c7d1007500e4dff897bb, SimpleDirectStorageCombo.
// The request's options and its address unions are represented by their raw storage.
struct StorageRequest {
    std::uint64_t options;
    void* destination;
    std::uint64_t destination_page_offset;
    std::uint32_t destination_size;
    void* source;
    std::uint64_t file_offset;
    std::uint32_t source_size;
    std::uint32_t intermediate_size;
    std::uint64_t cancellation_tag;
    const char* name;
};

// DSTORAGE_COMMAND_TYPE, from the Xbox GDK reference (dstorage_xs.h): the kind of the first failed
// entry. NONE means no failure is recorded and the rest of the record is undefined.
enum StorageCommandType : std::int32_t {
    command_type_none = -1,
    command_type_request = 0,
    command_type_status = 1,
    command_type_signal = 2,
};

// DSTORAGE_ERROR_RECORD as the Xbox GDK reference publishes it: HResult, then CommandType, then a
// union of the request, status and signal arms. Only the request arm is filled here, since only read
// requests are recorded as failures. The Win32 sample header leaves CommandType out and has padding
// at its offset; every other offset is the same.
struct StorageErrorRecord {
    std::uint32_t failure_count;
    struct Failure {
        HRESULT result;
        std::int32_t command_type;
        std::uint64_t options;
        void* destination;
        std::uint32_t destination_size;
        wchar_t filename[MAX_PATH];
        std::uint64_t file_offset;
        void* source;
        std::uint32_t source_size;
        std::uint64_t cancellation_tag;
        char name[64];
    } first_failure;
};

static_assert(sizeof(StorageRequest) == 0x48);
static_assert(offsetof(StorageRequest, options) == 0x00);
static_assert(offsetof(StorageRequest, destination) == 0x08);
static_assert(offsetof(StorageRequest, destination_page_offset) == 0x10);
static_assert(offsetof(StorageRequest, destination_size) == 0x18);
static_assert(offsetof(StorageRequest, source) == 0x20);
static_assert(offsetof(StorageRequest, file_offset) == 0x28);
static_assert(offsetof(StorageRequest, source_size) == 0x30);
static_assert(offsetof(StorageRequest, intermediate_size) == 0x34);
static_assert(offsetof(StorageRequest, cancellation_tag) == 0x38);
static_assert(offsetof(StorageRequest, name) == 0x40);

static_assert(sizeof(StorageErrorRecord) == 0x290);
static_assert(sizeof(StorageErrorRecord::Failure) == 0x288);
static_assert(offsetof(StorageErrorRecord, failure_count) == 0x000);
static_assert(offsetof(StorageErrorRecord, first_failure) == 0x008);
static_assert(offsetof(StorageErrorRecord, first_failure.result) == 0x008);
static_assert(offsetof(StorageErrorRecord, first_failure.command_type) == 0x00c);
static_assert(offsetof(StorageErrorRecord, first_failure.options) == 0x010);
static_assert(offsetof(StorageErrorRecord, first_failure.destination) == 0x018);
static_assert(offsetof(StorageErrorRecord, first_failure.destination_size) == 0x020);
static_assert(offsetof(StorageErrorRecord, first_failure.filename) == 0x024);
static_assert(offsetof(StorageErrorRecord, first_failure.file_offset) == 0x230);
static_assert(offsetof(StorageErrorRecord, first_failure.source) == 0x238);
static_assert(offsetof(StorageErrorRecord, first_failure.source_size) == 0x240);
static_assert(offsetof(StorageErrorRecord, first_failure.cancellation_tag) == 0x248);
static_assert(offsetof(StorageErrorRecord, first_failure.name) == 0x250);

// The record of a queue with no failure since the last retrieve: a zero count and CommandType NONE.
// All-zero bytes would read as CommandType REQUEST.
inline StorageErrorRecord empty_error_record() noexcept
{
    StorageErrorRecord record{};
    record.first_failure.command_type = command_type_none;
    return record;
}

}  // namespace x2w_startup
