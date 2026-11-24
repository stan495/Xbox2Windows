// Draws a translated pixel shader that samples one R32_FLOAT depth texture through a comparison sampler.
// The sampler is a descriptor-heap sampler built from the case file, so each case can carry the filter and
// compare function that tools/pixel_aot.py decoded from the console sampler words.
#include "desktop_gpu.h"
#include "pixel_aot_vs.h"
#include <array>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {
struct CompareCase {
    UINT filter, func, texWidth, texHeight, width, height;
    std::array<UINT, 64> constants;
    std::vector<float> depths;
    std::vector<std::array<float, 4>> expected;
};

CompareCase read_case(const char* path, UINT mipLevels)
{
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open comparison case");
    CompareCase c{};
    std::array<UINT, 6> header{};
    file.read(reinterpret_cast<char*>(header.data()), sizeof(header));
    c.filter = header[0];
    c.func = header[1];
    c.texWidth = header[2];
    c.texHeight = header[3];
    c.width = header[4];
    c.height = header[5];
    if (!file || !c.texWidth || !c.texHeight || c.texWidth > 64 || c.texHeight > 64 ||
        !c.width || !c.height || c.width > 64 || c.height > 64 ||
        c.func < D3D12_COMPARISON_FUNC_NEVER || c.func > D3D12_COMPARISON_FUNC_ALWAYS ||
        (c.filter != D3D12_FILTER_COMPARISON_MIN_MAG_MIP_POINT &&
         c.filter != D3D12_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT))
        throw std::runtime_error("Invalid comparison case header");
    file.read(reinterpret_cast<char*>(c.constants.data()), sizeof(c.constants));
    if (mipLevels == 2 && (c.texWidth < 2 || c.texHeight < 2))
        throw std::runtime_error("Two-mip comparison case needs both texture dimensions at least 2");
    size_t texels = size_t(c.texWidth) * c.texHeight;
    if (mipLevels == 2) texels += size_t(c.texWidth / 2) * (c.texHeight / 2);
    c.depths.resize(texels);
    file.read(reinterpret_cast<char*>(c.depths.data()), std::streamsize(c.depths.size() * sizeof(float)));
    c.expected.resize(size_t(c.width) * c.height);
    file.read(reinterpret_cast<char*>(c.expected.data()), std::streamsize(c.expected.size() * 16));
    if (!file || file.peek() != std::char_traits<char>::eof())
        throw std::runtime_error("Comparison case size does not match its header");
    return c;
}

ComPtr<ID3D12Resource> upload_bytes(ID3D12Device* device, const void* data, UINT64 size)
{
    auto buffer = make_buffer(device, size, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    void* mapped = nullptr;
    D3D12_RANGE none{};
    check(buffer->Map(0, &none, &mapped), "Map comparison upload");
    std::memcpy(mapped, data, size_t(size));
    buffer->Unmap(0, nullptr);
    return buffer;
}
}

void check_pixel_compare(ID3D12Device* device, const char* shader, const char* casePath, bool twoMips)
{
    const UINT mipLevels = twoMips ? 2 : 1;
    const auto c = read_case(casePath, mipLevels);
    std::ifstream stream(shader, std::ios::binary);
    std::vector<char> ps{std::istreambuf_iterator<char>(stream), {}};
    if (ps.empty()) throw std::runtime_error("Cannot read compiled pixel shader");

    // t0 and s0 in descriptor tables, b0 as a root CBV, all pixel-visible. The root signature embedded in
    // pixel_aot_vs is not used: its static s0 is an ordinary point sampler.
    D3D12_DESCRIPTOR_RANGE ranges[] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER, 1, 0, 0, 0},
    };
    D3D12_ROOT_PARAMETER params[3]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable = {1, &ranges[0]};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_CBV;
    params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[2].DescriptorTable = {1, &ranges[1]};
    for (auto& param : params) param.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_ROOT_SIGNATURE_DESC layout{3, params};
    ComPtr<ID3DBlob> blob, error;
    check(D3D12SerializeRootSignature(&layout, D3D_ROOT_SIGNATURE_VERSION_1, &blob, &error),
          "Serialize comparison root");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(&root)),
          "Comparison root");

    D3D12_GRAPHICS_PIPELINE_STATE_DESC desc{};
    desc.pRootSignature = root.Get();
    desc.VS = {pixel_aot_vs, sizeof(pixel_aot_vs)};
    desc.PS = {ps.data(), ps.size()};
    desc.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    desc.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    desc.RasterizerState.DepthClipEnable = TRUE;
    auto& blend = desc.BlendState.RenderTarget[0];
    blend.SrcBlend = blend.SrcBlendAlpha = D3D12_BLEND_ONE;
    blend.DestBlend = blend.DestBlendAlpha = D3D12_BLEND_ZERO;
    blend.BlendOp = blend.BlendOpAlpha = D3D12_BLEND_OP_ADD;
    blend.LogicOp = D3D12_LOGIC_OP_NOOP;
    blend.RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    desc.SampleMask = UINT_MAX;
    desc.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    desc.NumRenderTargets = desc.SampleDesc.Count = 1;
    desc.RTVFormats[0] = DXGI_FORMAT_R32G32B32A32_FLOAT;
    ComPtr<ID3D12PipelineState> pso;
    check(device->CreateGraphicsPipelineState(&desc, IID_PPV_ARGS(&pso)), "Comparison pipeline");

    D3D12_RESOURCE_DESC texDesc{};
    texDesc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    texDesc.Width = c.texWidth;
    texDesc.Height = c.texHeight;
    texDesc.DepthOrArraySize = texDesc.SampleDesc.Count = 1;
    texDesc.MipLevels = static_cast<UINT16>(mipLevels);
    texDesc.Format = DXGI_FORMAT_R32_FLOAT;
    auto texture = resource(device, texDesc, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_COPY_DEST);
    std::array<D3D12_PLACED_SUBRESOURCE_FOOTPRINT, 2> texLayouts{};
    UINT64 texBytes = 0;
    device->GetCopyableFootprints(&texDesc, 0, mipLevels, 0, texLayouts.data(), nullptr, nullptr, &texBytes);
    std::vector<char> staged(static_cast<size_t>(texBytes));
    size_t texelOffset = 0;
    for (UINT mip = 0; mip < mipLevels; ++mip) {
        const auto& layout = texLayouts[mip];
        const UINT width = c.texWidth >> mip;
        const UINT height = c.texHeight >> mip;
        for (UINT y = 0; y < height; ++y) {
            std::memcpy(staged.data() + layout.Offset + size_t(y) * layout.Footprint.RowPitch,
                        c.depths.data() + texelOffset + size_t(y) * width, width * sizeof(float));
        }
        texelOffset += size_t(width) * height;
    }
    auto staging = upload_bytes(device, staged.data(), texBytes);
    auto constants = upload_bytes(device, c.constants.data(), sizeof(c.constants));

    D3D12_DESCRIPTOR_HEAP_DESC heapDesc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1,
                                        D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE};
    ComPtr<ID3D12DescriptorHeap> views, samplers, rtvs;
    check(device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(&views)), "Comparison texture heap");
    device->CreateShaderResourceView(texture.Get(), nullptr, views->GetCPUDescriptorHandleForHeapStart());
    heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER;
    check(device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(&samplers)), "Comparison sampler heap");
    D3D12_SAMPLER_DESC sampler{};
    sampler.Filter = D3D12_FILTER(c.filter);
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    sampler.MaxAnisotropy = 1;
    sampler.ComparisonFunc = D3D12_COMPARISON_FUNC(c.func);
    sampler.MaxLOD = twoMips ? 1.0f : D3D12_FLOAT32_MAX;
    device->CreateSampler(&sampler, samplers->GetCPUDescriptorHandleForHeapStart());
    heapDesc = {D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_NONE};
    check(device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(&rtvs)), "Comparison target heap");

    D3D12_RESOURCE_DESC targetDesc = texDesc;
    targetDesc.Width = c.width;
    targetDesc.Height = c.height;
    targetDesc.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    targetDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    const D3D12_CLEAR_VALUE clear{targetDesc.Format, {-1, -1, -1, -1}};
    auto target = resource(device, targetDesc, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_RENDER_TARGET, &clear);
    const auto rtv = rtvs->GetCPUDescriptorHandleForHeapStart();
    device->CreateRenderTargetView(target.Get(), nullptr, rtv);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT resultLayout{};
    UINT64 resultBytes = 0;
    device->GetCopyableFootprints(&targetDesc, 0, 1, 0, &resultLayout, nullptr, nullptr, &resultBytes);
    auto back = make_buffer(device, resultBytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);

    ComputeCommands commands(device, pso.Get(), D3D12_COMMAND_LIST_TYPE_DIRECT);
    auto* list = commands.list.Get();
    D3D12_TEXTURE_COPY_LOCATION src{}, dst{};
    src.pResource = staging.Get();
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.pResource = texture.Get();
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    for (UINT mip = 0; mip < mipLevels; ++mip) {
        src.PlacedFootprint = texLayouts[mip];
        dst.SubresourceIndex = mip;
        list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    }
    transition(list, texture.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
    ID3D12DescriptorHeap* heaps[]{views.Get(), samplers.Get()};
    list->SetDescriptorHeaps(2, heaps);
    list->SetGraphicsRootSignature(root.Get());
    list->SetGraphicsRootDescriptorTable(0, views->GetGPUDescriptorHandleForHeapStart());
    list->SetGraphicsRootConstantBufferView(1, constants->GetGPUVirtualAddress());
    list->SetGraphicsRootDescriptorTable(2, samplers->GetGPUDescriptorHandleForHeapStart());
    const D3D12_VIEWPORT viewport{0, 0, float(c.width), float(c.height), 0, 1};
    const D3D12_RECT scissor{0, 0, LONG(c.width), LONG(c.height)};
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
    list->ClearRenderTargetView(rtv, clear.Color, 0, nullptr);
    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    list->DrawInstanced(3, 1, 0, 0);
    transition(list, target.Get(), D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
    src = {};
    dst = {};
    src.pResource = target.Get();
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    dst.pResource = back.Get();
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint = resultLayout;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    commands.submit_and_wait(device);

    void* mapped = nullptr;
    check(back->Map(0, nullptr, &mapped), "Map comparison result");
    const char* rows = static_cast<const char*>(mapped) + resultLayout.Offset;
    unsigned mismatches = 0;
    for (UINT y = 0; y < c.height; ++y) {
        for (UINT x = 0; x < c.width; ++x) {
            std::array<float, 4> actual;
            std::memcpy(actual.data(), rows + size_t(y) * resultLayout.Footprint.RowPitch + x * 16u, 16);
            const auto& expected = c.expected[size_t(y) * c.width + x];
            if (std::memcmp(actual.data(), expected.data(), 16) != 0) {
                if (!mismatches)
                    std::fprintf(stderr, "pixel_mismatch x=%u y=%u expected=%.9g,%.9g,%.9g,%.9g "
                                 "actual=%.9g,%.9g,%.9g,%.9g\n", x, y, expected[0], expected[1], expected[2],
                                 expected[3], actual[0], actual[1], actual[2], actual[3]);
                ++mismatches;
            }
        }
    }
    D3D12_RANGE none{};
    back->Unmap(0, &none);
    std::printf("filter=0x%x func=%u pixels=%u mismatches=%u\n", c.filter, c.func, c.width * c.height, mismatches);
    if (mismatches) throw PixelMismatch();
}
