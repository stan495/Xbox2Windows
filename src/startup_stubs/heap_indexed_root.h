#pragma once

// Title root signatures that index descriptor heaps directly: D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED
// (0x400) and D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED (0x800). The desktop debug layer rejects setting
// such a root signature on a list that has no heap of that type bound (message ID 1419 for the sampler heap). The
// stand-in only tracks the heaps a title sets (command-list slot 32) and never forwards them, so slot 34 does not set a
// flagged root signature on the desktop list. Translated draws keep using their own root signature. A real draw under
// a flagged root is decided here: it may bind the deferred root signature only when the list holds every heap the root
// indexes, at the indices the draw reads, and every root parameter value can be set again.

#include <d3d12.h>

#include <climits>

namespace x2w_startup {

// The flag values, spelled out so older headers build.
constexpr UINT root_flag_view_heap_indexed = 0x400;
constexpr UINT root_flag_sampler_heap_indexed = 0x800;
constexpr UINT root_flags_heap_indexed = root_flag_view_heap_indexed | root_flag_sampler_heap_indexed;

inline bool root_heap_indexed(UINT root_flags) { return (root_flags & root_flags_heap_indexed) != 0; }

// The highest heap index a draw reads is not known (a shader may compute it); only a heap holding the whole title heap covers it.
constexpr UINT unknown_heap_index = UINT_MAX;

// A shader-visible desktop heap of one type that is set on the list and holds the title heap's descriptors at the title's indices.
struct MirroredHeap {
    bool bound = false;  // a heap of this type is set on the desktop list
    UINT entries = 0;    // title entries it holds, from index 0
    bool whole = false;  // it holds every entry of the title heap
};

struct RealRootDraw {
    UINT root_flags = 0;
    MirroredHeap views, samplers;
    UINT highest_view = unknown_heap_index;     // highest view-heap index the draw reads
    UINT highest_sampler = unknown_heap_index;  // highest sampler-heap index the draw reads
    bool state_restorable = false;              // every root constant, descriptor table and root descriptor can be set again
};

enum class RealRootDecision { bind, view_heap_missing, sampler_heap_missing, both_heaps_missing, state_not_restorable };
constexpr int real_root_decision_count = 5;

inline const char* real_root_reason(RealRootDecision decision)
{
    switch (decision) {
    case RealRootDecision::bind: return "bind";
    case RealRootDecision::view_heap_missing: return "view heap missing";
    case RealRootDecision::sampler_heap_missing: return "sampler heap missing";
    case RealRootDecision::both_heaps_missing: return "view and sampler heaps missing";
    case RealRootDecision::state_not_restorable: return "root state not restorable";
    }
    return "unknown";
}

inline bool heap_covers(const MirroredHeap& heap, UINT highest)
{
    return heap.bound && (heap.whole || (highest != unknown_heap_index && highest < heap.entries));
}

// An unflagged root signature is set by slot 34 as before, so nothing is deferred and nothing needs restoring.
inline RealRootDecision decide_real_root(const RealRootDraw& draw)
{
    if (!root_heap_indexed(draw.root_flags)) return RealRootDecision::bind;
    const bool views = !(draw.root_flags & root_flag_view_heap_indexed) || heap_covers(draw.views, draw.highest_view);
    const bool samplers = !(draw.root_flags & root_flag_sampler_heap_indexed) || heap_covers(draw.samplers, draw.highest_sampler);
    if (!views && !samplers) return RealRootDecision::both_heaps_missing;
    if (!views) return RealRootDecision::view_heap_missing;
    if (!samplers) return RealRootDecision::sampler_heap_missing;
    return draw.state_restorable ? RealRootDecision::bind : RealRootDecision::state_not_restorable;
}

}  // namespace x2w_startup
