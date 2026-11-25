#include <windows.h>

#include <cstdint>
#include <cstring>

#include "startup_stream_setup.h"

namespace {
template<class T> StreamWord word_of(T* value) { return reinterpret_cast<std::uintptr_t>(value); }
}

// A descriptor table and a root constant are set under the stream root signature, then
// X2W_TEST_ROOT_RESET names what the title does before the draw:
//   bound        nothing: both bindings stay
//   same_root    SetGraphicsRootSignature with the same signature: both bindings stay
//   switch_root  a different signature and back: both bindings are invalidated
//   out_of_range six out-of-range SetGraphicsRoot32BitConstants calls; both bindings stay
int draw_root_reset(void* guest, void* list)
{
    char mode[16]{};
    GetEnvironmentVariableA("X2W_TEST_ROOT_RESET", mode, sizeof(mode));
    const StreamWord root = word_of(stream_root_signature());
    if (!root) return 40;
    const D3D12_RESOURCE_DESC desc{D3D12_RESOURCE_DIMENSION_TEXTURE1D, 0, 4, 1, 1, 1,
        DXGI_FORMAT_R8G8B8A8_UNORM, {1, 0}, D3D12_TEXTURE_LAYOUT_UNKNOWN, D3D12_RESOURCE_FLAG_NONE};
    static unsigned char texels[16];
    for (unsigned x = 0; x < 4; ++x) {
        texels[4 * x] = static_cast<unsigned char>(10 + 20 * x);
        texels[4 * x + 3] = static_cast<unsigned char>(200 - 30 * x);
    }
    void* texture = nullptr;
    if (FAILED(static_cast<HRESULT>(invoke(guest, 133, word_of(texels), word_of(&desc), D3D12_RESOURCE_STATE_COMMON, 0,
                                           word_of(&__uuidof(ID3D12Resource)), word_of(&texture))))) return 41;
    struct Extended {
        D3D12_DESCRIPTOR_HEAP_DESC desc;
        StreamWord address, bytes;
    } heap_desc{{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE}, 0, 0};
    void* heap = nullptr;
    if (FAILED(static_cast<HRESULT>(invoke(guest, 14, word_of(&heap_desc), word_of(&__uuidof(ID3D12DescriptorHeap)),
                                           word_of(&heap))))) return 42;
    D3D12_SHADER_RESOURCE_VIEW_DESC view{};
    view.Format = desc.Format;
    view.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE1D;
    view.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    view.Texture1D.MipLevels = 1;
    invoke(guest, 18, word_of(texture), word_of(&view), invoke(heap, 9));
    invoke(list, 34, root);  // SetGraphicsRootSignature
    invoke(list, 32, 1, word_of(&heap));
    invoke(list, 36, 0, invoke(heap, 10));
    const std::uint32_t blue = 173;
    invoke(list, 40, 1, 1, word_of(&blue), 0);
    if (!std::strcmp(mode, "same_root")) {
        invoke(list, 34, root);
    } else if (!std::strcmp(mode, "switch_root")) {
        const D3D12_ROOT_SIGNATURE_DESC empty{};
        Microsoft::WRL::ComPtr<ID3DBlob> blob;
        void* alternate = nullptr;
        if (FAILED(D3D12SerializeRootSignature(&empty, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr)) ||
            FAILED(static_cast<HRESULT>(invoke(guest, 16, 0, word_of(blob->GetBufferPointer()), blob->GetBufferSize(),
                                               word_of(&__uuidof(ID3D12RootSignature)), word_of(&alternate)))) ||
            !alternate)
            return 40;
        invoke(list, 34, word_of(alternate));
        invoke(list, 34, root);
    } else if (!std::strcmp(mode, "out_of_range")) {
        static std::uint32_t words[10];
        invoke(list, 40, 16, 1, word_of(words), 0);          // parameter 16
        invoke(list, 40, 0xffffffffu, 1, word_of(words), 0);  // parameter -1
        invoke(list, 40, 0, 1, word_of(words), 8);           // offset 8
        invoke(list, 40, 0, 4, word_of(words), 6);           // truncated to two words
        invoke(list, 40, 0, 9, word_of(words), 0);           // truncated to eight words
        invoke(list, 40, 99, 2, word_of(words), 0);          // parameter 99
    } else if (std::strcmp(mode, "bound")) {
        return 2;
    }
    invoke(list, 12, 3, 1);
    return 0;
}
