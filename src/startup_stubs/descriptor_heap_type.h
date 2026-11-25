// Experimental (docs/startup-runtime.md): which descriptor heap type values desktop D3D12 answers.
//
// Device slot 15 is desktop GetDescriptorHandleIncrementSize(type). Desktop knows four types (CBV/SRV/UAV,
// sampler, RTV, DSV); for any other value it returns 0 and the debug layer reports an error (#870). The
// stand-in answers 0 for those values itself, so the value is logged and the layer stays quiet; it does
// not make up an increment for a type desktop does not have.
#pragma once

#include <cstdint>

namespace x2w_startup {

// D3D12_DESCRIPTOR_HEAP_TYPE_NUM_TYPES; d3d12_xs_device.cpp asserts it against the SDK value.
inline constexpr std::uint32_t desktop_descriptor_heap_type_count = 4;

// The title passes the type in a 32-bit register: compare it unsigned, as desktop does.
inline constexpr bool is_valid_desktop_descriptor_heap_type(std::uint32_t type) noexcept
{
    return type < desktop_descriptor_heap_type_count;
}

}  // namespace x2w_startup
