// Range provenance (#870): which producer last wrote the range a draw binds, and whether that write was
// visible once its submission fence had completed.
//
// One committed buffer holds four ranges of 64 RGBA32F elements. Range A [0, 64) is red, written by a copy.
// Range B [64, 128) is green, written by a draw whose pixel shader stores it through a structured UAV. Range C
// [128, 192) is never written and keeps the zeros D3D12 gives a committed default-heap buffer; a fourth range
// [192, 256) lies outside every window and is never sampled. The bound range is a StructuredBuffer SRV whose
// FirstElement and NumElements select one of the three ranges: a buffer SRV is the desktop view that carries an
// explicit offset and scope, where a 2D SRV has no texel offset at all.
//
// Each producer is submitted and waited on its own fence before the consumer list is recorded; the completed
// value is checked at that point and printed with every case. The consumer draws 64x1 pixels, each sampling one
// element of its bound window, and copies the target to a readback on the same list. The store itself is copied
// to a second readback after the last submission, so the run reports what the producers wrote independently of
// what the draw sampled. A CPU model of the store, built from the same writes and the same window, predicts
// every sampled pixel.
//
// Cases: exact (A, red, copy), neighbouring (B, green, draw), stale (A, red, with the blue rewrite submitted
// after the consumer's fence), empty (C, zeros, never written) and cleared (C, zeros, written by a whole-store
// clear). The last two sample the same colour, which is the ambiguity the issue raises: a readback alone cannot
// separate a zero fill from a range no producer ever wrote, only the writer class and the submission order can.
// Two mutants, run against the unchanged model, must each mismatch: the exact case bound to the neighbouring
// range, and the stale case with its rewrite submitted before the consumer.
#include "desktop_gpu.h"
#include "startup_range_provenance_read.h"
#include "startup_range_provenance_vs.h"
#include "startup_range_provenance_write.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {
constexpr UINT range_elements = 64;                    // elements in one range, and pixels in the consumer draw
constexpr UINT store_elements = 4 * range_elements;    // A [0, 64), B [64, 128), C [128, 192), unused tail
constexpr UINT region_a = 0;
constexpr UINT region_b = range_elements;
constexpr UINT region_c = 2 * range_elements;
constexpr DXGI_FORMAT format = DXGI_FORMAT_R32G32B32A32_FLOAT;
using Color = std::array<float, 4>;
constexpr Color red{1.0f, 0.0f, 0.0f, 1.0f};    // range A, written by the copy
constexpr Color green{0.0f, 1.0f, 0.0f, 1.0f};  // range B, written by the draw
constexpr Color blue{0.0f, 0.0f, 1.0f, 1.0f};   // the rewrite of range A
constexpr Color zero{0.0f, 0.0f, 0.0f, 0.0f};   // range C: committed zeros, or a clear of zeros
constexpr UINT element_bytes = static_cast<UINT>(sizeof(Color));
constexpr UINT range_bytes = range_elements * element_bytes;
constexpr UINT store_bytes = store_elements * element_bytes;
constexpr D3D12_VIEWPORT viewport{0, 0, static_cast<float>(range_elements), 1, 0, 1};
constexpr D3D12_RECT scissor{0, 0, static_cast<LONG>(range_elements), 1};

enum class Rewrite { none, before_consumer, after_consumer };

// One row of the report: which producer last wrote the bound range, how that range relates to the write, the
// window that is bound, and the two knobs the mutants turn.
struct Case {
    const char* name;
    const char* writer;    // class of the producer that last wrote the bound range: copy, draw, clear or none
    const char* relation;  // exact, neighbouring, stale, empty or cleared
    UINT first;            // the SRV's FirstElement, in elements
    bool zero_clear;       // a whole-store clear of zeros is submitted before the producers
    Rewrite rewrite;       // the blue copy into range A, before or after the consumer
};

struct Capture {
    std::vector<Color> sampled;  // the 64x1 target after the consumer draw
    std::vector<Color> store;    // the whole buffer after the last submission
    std::string order;           // submission order with the fence value of each submission
    UINT64 producer_fence = 0;
    UINT64 producer_completed = 0;
};

D3D12_CPU_DESCRIPTOR_HANDLE cpu_slot(ID3D12DescriptorHeap* heap, UINT index, UINT stride)
{
    auto handle = heap->GetCPUDescriptorHandleForHeapStart();
    handle.ptr += SIZE_T{index} * stride;
    return handle;
}

D3D12_GPU_DESCRIPTOR_HANDLE gpu_slot(ID3D12DescriptorHeap* heap, UINT index, UINT stride)
{
    auto handle = heap->GetGPUDescriptorHandleForHeapStart();
    handle.ptr += UINT64{index} * stride;
    return handle;
}

std::uint32_t word(float value)
{
    std::uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

ComPtr<ID3D12RootSignature> create_root(ID3D12Device* device)
{
    D3D12_DESCRIPTOR_RANGE ranges[2]{};
    ranges[0] = {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0};
    ranges[1] = {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 0};
    D3D12_ROOT_PARAMETER params[3]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable = {1, &ranges[0]};
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[1].DescriptorTable = {1, &ranges[1]};
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[2].Constants = {0, 0, 8};  // float4 write_colour, int write_base, int3 pad
    params[2].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_ROOT_SIGNATURE_DESC desc{3, params, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> blob;
    check(D3D12SerializeRootSignature(&desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr),
          "Serialize range-provenance root");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(&root)),
          "Create range-provenance root");
    return root;
}

ComPtr<ID3D12PipelineState> create_pso(ID3D12Device* device, ID3D12RootSignature* root, const void* ps,
                                       std::size_t ps_bytes)
{
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd{};
    pd.pRootSignature = root;
    pd.VS = {startup_range_provenance_vs, sizeof(startup_range_provenance_vs)};
    pd.PS = {ps, ps_bytes};
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;  // blending stays off
    pd.SampleMask = UINT_MAX;
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pd.RasterizerState.DepthClipEnable = TRUE;
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pd.NumRenderTargets = 1;
    pd.RTVFormats[0] = format;
    pd.SampleDesc.Count = 1;
    ComPtr<ID3D12PipelineState> pipeline;
    check(device->CreateGraphicsPipelineState(&pd, IID_PPV_ARGS(&pipeline)), "Create range-provenance PSO");
    return pipeline;
}

// A 64-element upload copy of one colour, the source of the copy producers.
ComPtr<ID3D12Resource> fill_upload(ID3D12Device* device, const Color& colour)
{
    const auto upload = make_buffer(device, range_bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    void* mapped = nullptr;
    const D3D12_RANGE no_read{0, 0};
    check(upload->Map(0, &no_read, &mapped), "Map range-provenance producer");
    for (UINT i = 0; i < range_elements; ++i)
        std::memcpy(static_cast<std::uint8_t*>(mapped) + i * element_bytes, colour.data(), element_bytes);
    const D3D12_RANGE written{0, range_bytes};
    upload->Unmap(0, &written);
    return upload;
}

// Runs one case: the producers, the consumer draw, the readbacks, one fence per submission.
Capture run(ID3D12Device* device, ID3D12RootSignature* root, const Case& c)
{
    const auto read_pso = create_pso(device, root, startup_range_provenance_read,
                                     sizeof(startup_range_provenance_read));
    const auto write_pso = create_pso(device, root, startup_range_provenance_write,
                                      sizeof(startup_range_provenance_write));
    const auto store = make_buffer(device, store_bytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_COPY_DEST,
                                   D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    const auto upload_red = fill_upload(device, red);
    const auto upload_blue = fill_upload(device, blue);

    D3D12_RESOURCE_DESC target_desc{};
    target_desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    target_desc.Width = range_elements;
    target_desc.Height = 1;
    target_desc.DepthOrArraySize = target_desc.MipLevels = 1;
    target_desc.Format = format;
    target_desc.SampleDesc.Count = 1;
    target_desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    const auto target = resource(device, target_desc, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_RENDER_TARGET);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT64 target_bytes = 0;
    device->GetCopyableFootprints(&target_desc, 0, 1, 0, &footprint, nullptr, nullptr, &target_bytes);
    const auto sample_readback = make_buffer(device, target_bytes, D3D12_HEAP_TYPE_READBACK,
                                             D3D12_RESOURCE_STATE_COPY_DEST);
    const auto store_readback = make_buffer(device, store_bytes, D3D12_HEAP_TYPE_READBACK,
                                            D3D12_RESOURCE_STATE_COPY_DEST);

    const D3D12_DESCRIPTOR_HEAP_DESC rtv_desc{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1,
                                              D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    ComPtr<ID3D12DescriptorHeap> rtvs;
    check(device->CreateDescriptorHeap(&rtv_desc, IID_PPV_ARGS(&rtvs)), "Create range-provenance RTV heap");
    const auto target_rtv = rtvs->GetCPUDescriptorHandleForHeapStart();
    device->CreateRenderTargetView(target.Get(), nullptr, target_rtv);

    // 0: the bound window (SRV), 1: the structured UAV the producer draw stores through, 2: the typed UAV the
    // whole-store clear takes. A clear needs a CPU handle in a heap that is not shader visible, so the two UAVs
    // are created in a second, private heap as well.
    const D3D12_DESCRIPTOR_HEAP_DESC views_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 3,
                                                D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    ComPtr<ID3D12DescriptorHeap> views;
    check(device->CreateDescriptorHeap(&views_desc, IID_PPV_ARGS(&views)), "Create range-provenance view heap");
    const D3D12_DESCRIPTOR_HEAP_DESC clear_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 2,
                                                D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    ComPtr<ID3D12DescriptorHeap> clear_views;
    check(device->CreateDescriptorHeap(&clear_desc, IID_PPV_ARGS(&clear_views)),
          "Create range-provenance clear heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);

    D3D12_SHADER_RESOURCE_VIEW_DESC srv{};
    srv.Format = DXGI_FORMAT_UNKNOWN;
    srv.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
    srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    srv.Buffer.FirstElement = c.first;
    srv.Buffer.NumElements = range_elements;
    srv.Buffer.StructureByteStride = element_bytes;
    srv.Buffer.Flags = D3D12_BUFFER_SRV_FLAG_NONE;
    device->CreateShaderResourceView(store.Get(), &srv, cpu_slot(views.Get(), 0, stride));

    D3D12_UNORDERED_ACCESS_VIEW_DESC structured{};
    structured.Format = DXGI_FORMAT_UNKNOWN;
    structured.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
    structured.Buffer.FirstElement = 0;
    structured.Buffer.NumElements = store_elements;
    structured.Buffer.StructureByteStride = element_bytes;
    device->CreateUnorderedAccessView(store.Get(), nullptr, &structured, cpu_slot(views.Get(), 1, stride));
    device->CreateUnorderedAccessView(store.Get(), nullptr, &structured, cpu_slot(clear_views.Get(), 0, stride));

    D3D12_UNORDERED_ACCESS_VIEW_DESC typed = structured;
    typed.Format = DXGI_FORMAT_R32G32B32A32_UINT;
    typed.Buffer.StructureByteStride = 0;
    device->CreateUnorderedAccessView(store.Get(), nullptr, &typed, cpu_slot(views.Get(), 2, stride));
    device->CreateUnorderedAccessView(store.Get(), nullptr, &typed, cpu_slot(clear_views.Get(), 1, stride));

    ComputeCommands commands(device, read_pso.Get(), D3D12_COMMAND_LIST_TYPE_DIRECT);
    auto* list = commands.list.Get();
    ComPtr<ID3D12Fence> fence;
    check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "Create range-provenance fence");
    Capture capture;
    UINT64 submissions = 0;
    const auto note = [&](const char* label, UINT64 value) {
        if (!capture.order.empty()) capture.order += ' ';
        capture.order += label;
        capture.order += '(' + std::to_string(value) + ')';
    };
    // One submission, waited on a fence of its own: nothing recorded later can pass it.
    const auto submit = [&]() {
        check(list->Close(), "Close range-provenance list");
        ID3D12CommandList* submitted[]{list};
        commands.queue->ExecuteCommandLists(1, submitted);
        const UINT64 value = ++submissions;
        check(commands.queue->Signal(fence.Get(), value), "Signal range-provenance fence");
        check(fence->SetEventOnCompletion(value, nullptr), "Wait for range-provenance fence");
        check(device->GetDeviceRemovedReason(), "Range-provenance submission completion");
        if (fence->GetCompletedValue() != value)
            throw std::runtime_error("Unexpected range-provenance fence value");
        check(commands.allocator->Reset(), "Reset range-provenance allocator");
        check(list->Reset(commands.allocator.Get(), nullptr), "Reset range-provenance list");
        return value;
    };
    ID3D12DescriptorHeap* heaps[]{views.Get()};
    const auto bind_heaps = [&]() { list->SetDescriptorHeaps(1, heaps); };

    if (c.zero_clear) {
        // A whole-store clear of zeros, in its own submission: it is the last writer of range C, and it leaves
        // bytes that are indistinguishable from the committed zeros of an unwritten range.
        transition(list, store.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        bind_heaps();
        const UINT zeros[4] = {0, 0, 0, 0};
        list->ClearUnorderedAccessViewUint(gpu_slot(views.Get(), 2, stride), cpu_slot(clear_views.Get(), 1, stride),
                                           store.Get(), zeros, 0, nullptr);
        transition(list, store.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
        note("clear", submit());
    }

    // The producers: range A by a copy, range B by a draw, and the rewrite of range A when it comes first.
    // ps_write declares only the UAV, so the consumer's SRV table stays unset in this submission.
    list->SetPipelineState(write_pso.Get());
    list->SetGraphicsRootSignature(root);
    bind_heaps();
    list->SetGraphicsRootDescriptorTable(1, gpu_slot(views.Get(), 1, stride));
    list->CopyBufferRegion(store.Get(), region_a * element_bytes, upload_red.Get(), 0, range_bytes);
    transition(list, store.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    const std::uint32_t words[8] = {word(green[0]), word(green[1]), word(green[2]), word(green[3]), region_b,
                                    0, 0, 0};
    list->SetGraphicsRoot32BitConstants(2, 8, words, 0);
    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->OMSetRenderTargets(1, &target_rtv, FALSE, nullptr);
    list->DrawInstanced(3, 1, 0, 0);
    if (c.rewrite == Rewrite::before_consumer) {
        transition(list, store.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyBufferRegion(store.Get(), region_a * element_bytes, upload_blue.Get(), 0, range_bytes);
        transition(list, store.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    }
    transition(list, store.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
    note("producer", submit());

    // The fence that separates the producers from the consumer, checked before the consumer list is recorded.
    capture.producer_fence = submissions;
    capture.producer_completed = fence->GetCompletedValue();
    if (capture.producer_completed < capture.producer_fence)
        throw std::runtime_error("the producer fence was not complete before the consumer was recorded");

    // The consumer: 64 pixels, one element of the bound window each.
    list->SetPipelineState(read_pso.Get());
    list->SetGraphicsRootSignature(root);
    bind_heaps();
    list->SetGraphicsRootDescriptorTable(0, gpu_slot(views.Get(), 0, stride));
    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->OMSetRenderTargets(1, &target_rtv, FALSE, nullptr);
    list->DrawInstanced(3, 1, 0, 0);
    transition(list, target.Get(), D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION to{sample_readback.Get(), D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT};
    to.PlacedFootprint = footprint;
    D3D12_TEXTURE_COPY_LOCATION from{target.Get(), D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX};
    list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
    note("consumer", submit());

    // The rewrite after the consumer, when the case has one, and the store readback that witnesses it.
    if (c.rewrite == Rewrite::after_consumer) {
        transition(list, store.Get(), D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyBufferRegion(store.Get(), region_a * element_bytes, upload_blue.Get(), 0, range_bytes);
        transition(list, store.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE);
    } else {
        transition(list, store.Get(), D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_SOURCE);
    }
    list->CopyBufferRegion(store_readback.Get(), 0, store.Get(), 0, store_bytes);
    note(c.rewrite == Rewrite::after_consumer ? "rewrite+store" : "store", submit());

    capture.store.resize(store_elements);
    {
        void* mapped = nullptr;
        const D3D12_RANGE read{0, static_cast<SIZE_T>(store_bytes)}, none{0, 0};
        check(store_readback->Map(0, &read, &mapped), "Map range-provenance store");
        std::memcpy(capture.store.data(), mapped, store_bytes);
        store_readback->Unmap(0, &none);
    }
    capture.sampled.resize(range_elements);
    {
        void* mapped = nullptr;
        const D3D12_RANGE read{0, static_cast<SIZE_T>(footprint.Offset + range_bytes)}, none{0, 0};
        check(sample_readback->Map(0, &read, &mapped), "Map range-provenance sample");
        std::memcpy(capture.sampled.data(), static_cast<std::uint8_t*>(mapped) + footprint.Offset, range_bytes);
        sample_readback->Unmap(0, &none);
    }
    return capture;
}

// The store after the writes the fixture submits before the consumer. The whole-store clear of zeros writes
// zeros over the zeros a committed buffer already holds, so it changes nothing this model can see: that is the
// ambiguity the fixture exists to expose, and only the printed writer class separates the two zero cases.
std::vector<Color> visible_store(const Case& c)
{
    std::vector<Color> store(store_elements, zero);
    std::fill(store.begin() + region_a, store.begin() + region_a + range_elements, red);
    std::fill(store.begin() + region_b, store.begin() + region_b + range_elements, green);
    if (c.rewrite == Rewrite::before_consumer)
        std::fill(store.begin() + region_a, store.begin() + region_a + range_elements, blue);
    return store;
}

std::vector<Color> final_store(const Case& c)
{
    auto store = visible_store(c);
    if (c.rewrite == Rewrite::after_consumer)
        std::fill(store.begin() + region_a, store.begin() + region_a + range_elements, blue);
    return store;
}

// The sampled pixels, predicted from the store and the window alone.
std::vector<Color> model(const Case& c)
{
    const auto store = visible_store(c);
    return {store.begin() + c.first, store.begin() + c.first + range_elements};
}

UINT mismatches(const std::vector<Color>& got, const std::vector<Color>& want)
{
    if (got.size() != want.size()) throw std::runtime_error("range-provenance readback size");
    UINT count = 0;
    for (std::size_t i = 0; i < got.size(); ++i)
        count += std::memcmp(got[i].data(), want[i].data(), sizeof(Color)) != 0;
    return count;
}

std::string text(const Color& c)
{
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "(%g,%g,%g,%g)", c[0], c[1], c[2], c[3]);
    return buffer;
}
}  // namespace

// Returns the number of failed expectations (0 = pass).
int check_startup_range_provenance(ID3D12Device* device)
{
    const auto root = create_root(device);
    const Case cases[] = {
        {"exact", "copy", "exact", region_a, false, Rewrite::none},
        {"neighbouring", "draw", "neighbouring", region_b, false, Rewrite::none},
        {"stale", "copy", "stale", region_a, false, Rewrite::after_consumer},
        {"empty", "none", "empty", region_c, false, Rewrite::none},
        {"cleared", "clear", "cleared", region_c, true, Rewrite::none},
    };
    int failures = 0;
    std::vector<Color> sampled;
    for (const auto& c : cases) {
        const auto capture = run(device, root.Get(), c);
        const auto want = model(c);
        const auto want_store = final_store(c);
        const UINT bad = mismatches(capture.sampled, want);
        const UINT bad_store = mismatches(capture.store, want_store);
        const Color sampled_zero = capture.sampled[0];  // every pixel of a literal window carries one colour
        failures += (bad != 0) + (bad_store != 0);
        sampled.push_back(sampled_zero);
        std::printf("startup_range_provenance %s: writer=%s relation=%s bound=[%u,%u) order=%s "
                    "producer_fence=%llu complete=%llu sampled=%s%s\n", c.name, c.writer, c.relation, c.first,
                    c.first + range_elements, capture.order.c_str(),
                    static_cast<unsigned long long>(capture.producer_fence),
                    static_cast<unsigned long long>(capture.producer_completed), text(sampled_zero).c_str(),
                    bad || bad_store ? "  FAIL" : "");
        std::printf("  model=%s store_actual=A%s B%s C%s sampled_mismatches=%u/%u store_mismatches=%u/%u\n",
                    text(want[0]).c_str(), text(capture.store[region_a]).c_str(),
                    text(capture.store[region_b]).c_str(), text(capture.store[region_c]).c_str(), bad,
                    range_elements, bad_store, store_elements);
    }
    // The three written ranges must give three different colours, the stale case must keep the old one and the
    // cleared range must be indistinguishable from the unwritten one. The indices are the case order above.
    const bool distinct = sampled.size() == std::size(cases) && sampled[0] != sampled[1] &&
                          sampled[0] != sampled[3] && sampled[1] != sampled[3];
    const bool stale_kept_old = sampled.size() == std::size(cases) && sampled[2] == sampled[0];
    const bool cleared_matches_empty = sampled.size() == std::size(cases) && sampled[4] == sampled[3];
    failures += !distinct + !stale_kept_old + !cleared_matches_empty;
    // Mutants run against the unchanged model of their case; each must mismatch.
    struct Mutant { const char* name; Case run_as; std::size_t model_case; };
    Case neighbour_bound = cases[0];
    neighbour_bound.first = region_b;
    Case rewrite_first = cases[2];
    rewrite_first.rewrite = Rewrite::before_consumer;
    const Mutant mutants[] = {
        {"exact case bound to the neighbouring range", neighbour_bound, 0},
        {"stale case with the rewrite before the consumer", rewrite_first, 2},
    };
    for (const auto& m : mutants) {
        const auto capture = run(device, root.Get(), m.run_as);
        const UINT bad = mismatches(capture.sampled, model(cases[m.model_case]));
        failures += bad == 0;
        std::printf("startup_range_provenance mutant %s: order=%s sampled=%s mismatches=%u/%u%s\n", m.name,
                    capture.order.c_str(), text(capture.sampled[0]).c_str(), bad, range_elements,
                    bad ? "" : "  FAIL (mutant matched)");
    }
    std::printf("startup_range_provenance cases=%zu distinct=%d stale_kept_old=%d cleared_matches_empty=%d "
                "mutants=%zu failures=%d\n", std::size(cases), distinct ? 1 : 0, stale_kept_old ? 1 : 0,
                cleared_matches_empty ? 1 : 0, std::size(mutants), failures);
    return failures;
}
