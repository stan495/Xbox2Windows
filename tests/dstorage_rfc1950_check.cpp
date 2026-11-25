#include "dstorage_rfc1950.h"
#include <wrl/client.h>
#include <zlib.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string_view>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {
void check(HRESULT hr, const char* stage)
{
    if (FAILED(hr)) {
        std::fprintf(stderr, "%s failed: 0x%08lx\n", stage, static_cast<unsigned long>(hr));
        throw std::runtime_error(stage);
    }
}

struct TempFile {
    wchar_t name[MAX_PATH]{};
    TempFile()
    {
        wchar_t directory[MAX_PATH]{};
        DWORD length = GetTempPathW(MAX_PATH, directory);
        if (!length || length >= MAX_PATH || !GetTempFileNameW(directory, L"rfc", 0, name))
            throw std::runtime_error("Create temp file");
    }
    ~TempFile() { DeleteFileW(name); }
};

int run(std::string_view scenario)
{
    constexpr unsigned char sentinel = 0xca;
    std::array<BYTE, 768> plain{};
    for (size_t i = 0; i < plain.size(); ++i) plain[i] = BYTE((i % 32) * 13 + (i / 256));
    uLongf compressed_size = compressBound(static_cast<uLong>(plain.size()));
    std::vector<BYTE> compressed(compressed_size);
    if (compress2(compressed.data(), &compressed_size, plain.data(), static_cast<uLong>(plain.size()), 6) != Z_OK)
        throw std::runtime_error("Compress fixture");
    compressed.resize(compressed_size);
    UINT32 expected_size = static_cast<UINT32>(plain.size());
    if (scenario == "truncated") compressed.pop_back();
    else if (scenario == "checksum") compressed.back() ^= 1;
    else if (scenario == "trailing") compressed.push_back(0xcd);
    else if (scenario == "oversize") ++expected_size;
    else if (scenario == "undersize") --expected_size;
    else if (scenario != "success") return 2;

    TempFile tmp;
    constexpr char prefix[] = "source offset";
    {
        std::ofstream stream(std::filesystem::path(tmp.name), std::ios::binary | std::ios::trunc);
        stream.exceptions(std::ios::failbit | std::ios::badbit);
        stream.write(prefix, sizeof(prefix));
        stream.write(reinterpret_cast<const char*>(compressed.data()), compressed.size());
        stream.close();
    }
    DSTORAGE_CONFIGURATION1 config{};
    config.ForceFileBuffering = TRUE;
    config.DisableBypassIO = TRUE;
    check(DStorageSetConfiguration1(&config), "Set configuration");
    ComPtr<IDStorageFactory> factory;
    check(DStorageGetFactory(IID_PPV_ARGS(&factory)), "Factory");
    ComPtr<IDStorageCustomDecompressionQueue> codec;
    check(factory.As(&codec), "Custom decompression queue");
    ComPtr<IDStorageFile> file;
    check(factory->OpenFile(tmp.name, IID_PPV_ARGS(&file)), "Open file");
    DSTORAGE_QUEUE_DESC desc{};
    desc.SourceType = DSTORAGE_REQUEST_SOURCE_FILE;
    desc.Capacity = DSTORAGE_MIN_QUEUE_CAPACITY;
    ComPtr<IDStorageQueue1> queue;
    check(factory->CreateQueue(&desc, IID_PPV_ARGS(&queue)), "Create file queue");
    ComPtr<IDStorageStatusArray> status;
    check(factory->CreateStatusArray(1, "RFC1950", IID_PPV_ARGS(&status)), "Create status");
    std::vector<BYTE> output(expected_size + 2, sentinel);
    DSTORAGE_REQUEST request{};
    request.Options.SourceType = DSTORAGE_REQUEST_SOURCE_FILE;
    request.Options.DestinationType = DSTORAGE_REQUEST_DESTINATION_MEMORY;
    request.Options.CompressionFormat = DSTORAGE_CUSTOM_COMPRESSION_0;
    request.Source.File = {file.Get(), sizeof(prefix), static_cast<UINT32>(compressed.size())};
    request.Destination.Memory = {output.data() + 1, expected_size};
    request.UncompressedSize = expected_size;
    auto event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!event) throw std::runtime_error("Create completion event");
    struct CloseEvent { HANDLE h; ~CloseEvent() { CloseHandle(h); } } completion{event};
    queue->EnqueueRequest(&request);
    queue->EnqueueStatus(status.Get(), 0);
    queue->EnqueueSetEvent(event);
    if (WaitForSingleObject(codec->GetEvent(), 0) != WAIT_TIMEOUT ||
        WaitForSingleObject(event, 0) != WAIT_TIMEOUT ||
        !std::all_of(output.begin(), output.end(), [sentinel](BYTE b) { return b == sentinel; }))
        throw std::runtime_error("Queue changed before manual Submit");
    queue->Submit();
    if (WaitForSingleObject(codec->GetEvent(), 10000) != WAIT_OBJECT_0) throw std::runtime_error("Dequeue timeout");
    if (WaitForSingleObject(event, 0) != WAIT_TIMEOUT || status->IsComplete(0))
        throw std::runtime_error("Premature queue completion");
    dstorage_rfc1950::DecodeResult decoded{};
    const HRESULT completed = dstorage_rfc1950::complete_one(codec.Get(), decoded);
    if (completed != S_OK) throw std::runtime_error("Missing job or failed completion");
    if (WaitForSingleObject(event, 10000) != WAIT_OBJECT_0 || !status->IsComplete(0))
        throw std::runtime_error("Completion event or status missing");
    const HRESULT completion_hr = status->GetHResult(0);
    std::printf("consumed=%llu/%zu produced=%llu/%u codec=0x%08lx completion=0x%08lx\n",
        decoded.consumed, compressed.size(), decoded.produced, expected_size,
        static_cast<unsigned long>(decoded.status), static_cast<unsigned long>(completion_hr));
    const bool success = scenario == "success";
    const HRESULT expected = success ? S_OK : HRESULT_FROM_WIN32(ERROR_INVALID_DATA);
    if (decoded.status != expected || completion_hr != expected || output.front() != sentinel || output.back() != sentinel)
        throw std::runtime_error("Wrong status or corrupt guard byte");
    if (success && (decoded.consumed != compressed.size() || decoded.produced != plain.size() ||
                    !std::equal(plain.begin(), plain.end(), output.begin()+1)))
        throw std::runtime_error("Decoded size or bytes differ");
    if (!success && !std::all_of(output.begin(), output.end(), [sentinel](BYTE b) { return b == sentinel; }))
        throw std::runtime_error("Failed request published bytes");
    if (dstorage_rfc1950::complete_one(codec.Get(), decoded) != S_FALSE)
        throw std::runtime_error("Duplicate completion");
    if (decoded.status != E_PENDING || decoded.consumed || decoded.produced)
        throw std::runtime_error("Empty queue left the previous result in decoded");
    return 0;
}

// Requests the adapter must refuse before zlib runs, built by hand and passed to decode()
// directly. Each refusal reports zero bytes and leaves the destination and its guards alone.
int refusals()
{
    constexpr unsigned char sentinel = 0xca;
    std::array<BYTE, 768> plain{};
    for (size_t i = 0; i < plain.size(); ++i) plain[i] = BYTE((i % 32) * 13 + (i / 256));
    uLongf compressed_size = compressBound(static_cast<uLong>(plain.size()));
    std::vector<BYTE> compressed(compressed_size);
    if (compress2(compressed.data(), &compressed_size, plain.data(), static_cast<uLong>(plain.size()), 6) != Z_OK)
        throw std::runtime_error("Compress fixture");
    compressed.resize(compressed_size);
    std::array<BYTE, 768 + 2> output{};

    using Request = DSTORAGE_CUSTOM_DECOMPRESSION_REQUEST;
    struct Case {
        const char* name;
        void (*edit)(Request&);
        HRESULT expected;
    };
    // Above ULONG_MAX the low 32 bits still hold the true size, so a truncating adapter
    // would run zlib on the whole stream instead of refusing the request.
    static constexpr UINT64 above_ulong = UINT64{1} << 32;
    const Case cases[] = {
        {"baseline", [](Request&) {}, S_OK},
        {"format_custom_1", [](Request& r) {
             r.CompressionFormat = static_cast<DSTORAGE_COMPRESSION_FORMAT>(DSTORAGE_CUSTOM_COMPRESSION_0 + 1);
         }, E_NOTIMPL},
        {"format_gdeflate", [](Request& r) { r.CompressionFormat = DSTORAGE_COMPRESSION_FORMAT_GDEFLATE; },
         E_NOTIMPL},
        {"flag_dest_in_upload_heap",
         [](Request& r) { r.Flags = DSTORAGE_CUSTOM_DECOMPRESSION_FLAG_DEST_IN_UPLOAD_HEAP; }, E_NOTIMPL},
        {"null_src", [](Request& r) { r.SrcBuffer = nullptr; }, E_INVALIDARG},
        {"null_dst", [](Request& r) { r.DstBuffer = nullptr; }, E_INVALIDARG},
        {"zero_src", [](Request& r) { r.SrcSize = 0; }, E_INVALIDARG},
        {"zero_dst", [](Request& r) { r.DstSize = 0; }, E_INVALIDARG},
        {"src_over_ulong_max", [](Request& r) { r.SrcSize += above_ulong; }, E_INVALIDARG},
        {"dst_over_ulong_max", [](Request& r) { r.DstSize += above_ulong; }, E_INVALIDARG},
    };
    int failures = 0;
    for (const auto& test : cases) {
        output.fill(sentinel);
        Request request{};
        request.Id = 7;
        request.CompressionFormat = DSTORAGE_CUSTOM_COMPRESSION_0;
        request.Flags = DSTORAGE_CUSTOM_DECOMPRESSION_FLAG_NONE;
        request.SrcSize = compressed.size();
        request.SrcBuffer = compressed.data();
        request.DstSize = plain.size();
        request.DstBuffer = output.data() + 1;
        test.edit(request);
        const auto decoded = dstorage_rfc1950::decode(request);
        const bool success = test.expected == S_OK;
        const bool counts = success ? decoded.consumed == compressed.size() && decoded.produced == plain.size()
                                    : !decoded.consumed && !decoded.produced;
        const bool guards = output.front() == sentinel && output.back() == sentinel;
        const bool bytes = success ? std::equal(plain.begin(), plain.end(), output.begin() + 1)
                                   : std::all_of(output.begin(), output.end(), [sentinel](BYTE b) { return b == sentinel; });
        if (decoded.status != test.expected || !counts || !guards || !bytes) {
            std::printf("FAIL: refusals %s: status 0x%08lx consumed=%llu produced=%llu, expected 0x%08lx%s\n",
                test.name, static_cast<unsigned long>(decoded.status), decoded.consumed, decoded.produced,
                static_cast<unsigned long>(test.expected), guards && bytes ? "" : ", destination changed");
            ++failures;
            continue;
        }
        std::printf("refusals %s: 0x%08lx\n", test.name, static_cast<unsigned long>(decoded.status));
    }

    dstorage_rfc1950::DecodeResult decoded{S_OK, 1, 2};
    const HRESULT no_queue = dstorage_rfc1950::complete_one(nullptr, decoded);
    if (no_queue != E_POINTER || decoded.status != E_PENDING || decoded.consumed || decoded.produced) {
        std::printf("FAIL: refusals null_queue: returned 0x%08lx, decoded 0x%08lx\n",
            static_cast<unsigned long>(no_queue), static_cast<unsigned long>(decoded.status));
        ++failures;
    } else {
        std::puts("refusals null_queue: E_POINTER, decoded reset to E_PENDING");
    }
    return failures ? 1 : 0;
}
}

int main(int argc, char** argv)
{
    try {
        if (argc == 2 && std::string_view(argv[1]) == "refusals") return refusals();
        return argc == 2 ? run(argv[1]) : 2;
    }
    catch (const std::exception& e) { std::fprintf(stderr, "%s\n", e.what()); return 1; }
}
