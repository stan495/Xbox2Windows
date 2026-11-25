// Checks src/startup_stubs/heap_indexed_root.h without a device: whether a real draw under a title root signature
// with D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED (0x400) and/or SAMPLER_HEAP_DIRECTLY_INDEXED (0x800)
// may bind it, or which refusal applies. Every combination of the flags, the two heaps (absent, one entry, a few
// entries, the whole title heap), the highest index the draw reads, and whether the root state can be restored is
// compared with expectations written out below.
//
//   startup_heap_indexed_root_check [--mutate sampler-alone|bind-unrestorable]
//
// The mutations change the expectations to those of a wrong decision (a one-entry sampler heap that satisfies any
// root, or a bind without the root state); each must make the check fail.
#include "startup_stubs/heap_indexed_root.h"

#include <cstdio>
#include <cstring>
#include <initializer_list>

namespace {
using namespace x2w_startup;
using D = RealRootDecision;
int failures, cases;
bool sampler_alone, bind_unrestorable;

const char* name_of(D d) { return real_root_reason(d); }

void expect(const char* label, const RealRootDraw& draw, D expected)
{
    ++cases;
    const D got = decide_real_root(draw);
    if (got == expected) return;
    ++failures;
    if (failures <= 20)
        std::printf("FAIL %s: flags 0x%x views{%d,%u,%d} highest %u samplers{%d,%u,%d} highest %u restorable %d -> %s, expected %s\n",
                    label, draw.root_flags, draw.views.bound, draw.views.entries, draw.views.whole, draw.highest_view,
                    draw.samplers.bound, draw.samplers.entries, draw.samplers.whole, draw.highest_sampler,
                    draw.state_restorable, name_of(got), name_of(expected));
}

RealRootDraw make(UINT flags, MirroredHeap views, UINT highest_view, MirroredHeap samplers, UINT highest_sampler, bool restorable)
{
    RealRootDraw draw;
    draw.root_flags = flags;
    draw.views = views;
    draw.samplers = samplers;
    draw.highest_view = highest_view;
    draw.highest_sampler = highest_sampler;
    draw.state_restorable = restorable;
    return draw;
}

const MirroredHeap absent{};
const MirroredHeap one{true, 1, false};
const MirroredHeap four{true, 4, false};
const MirroredHeap whole{true, 4, true};
constexpr UINT unknown = unknown_heap_index;

// The named cases from the review of #870.
void named()
{
    // Unflagged: bound at slot 34 as before, whatever the heaps or the state.
    expect("unflagged, nothing", make(0, absent, unknown, absent, unknown, false), D::bind);
    expect("unflagged, heaps", make(0, whole, 0, whole, 0, true), D::bind);
    // Each flag alone.
    expect("views flag, no heap", make(0x400, absent, 0, whole, 0, true), D::view_heap_missing);
    expect("views flag, heap", make(0x400, whole, unknown, absent, unknown, true), D::bind);
    expect("sampler flag, no heap", make(0x800, whole, 0, absent, 0, true), D::sampler_heap_missing);
    expect("sampler flag, one-entry heap, index 0", make(0x800, absent, unknown, one, 0, true), D::bind);
    expect("sampler flag, one-entry heap, index 1", make(0x800, absent, unknown, one, 1, true), D::sampler_heap_missing);
    expect("sampler flag, one-entry heap, index unknown", make(0x800, absent, unknown, one, unknown, true), D::sampler_heap_missing);
    // Both flags: a sampler heap alone never satisfies a root that also indexes the view heap.
    expect("both flags, one-entry sampler heap alone", make(0xc00, absent, 0, one, 0, true), D::view_heap_missing);
    expect("both flags, whole sampler heap alone", make(0xc00, absent, 0, whole, unknown, true), D::view_heap_missing);
    expect("both flags, view heap alone", make(0xc00, whole, unknown, absent, 0, true), D::sampler_heap_missing);
    expect("both flags, neither heap", make(0xc00, absent, 0, absent, 0, true), D::both_heaps_missing);
    expect("both flags, both heaps", make(0xc00, whole, unknown, whole, unknown, true), D::bind);
    // The root state: constants and descriptor tables that cannot be set again keep the draw refused.
    expect("both flags, both heaps, state lost", make(0xc00, whole, unknown, whole, unknown, false), D::state_not_restorable);
    expect("views flag, heap, state lost", make(0x400, whole, 0, absent, 0, false), D::state_not_restorable);
    expect("sampler flag, heap, state lost", make(0x800, absent, 0, whole, 0, false), D::state_not_restorable);
    // A missing heap is named before the state.
    expect("sampler flag, no heap, state lost", make(0x800, absent, 0, absent, 0, false), D::sampler_heap_missing);
    // Other root signature flags do not make a root heap-indexed.
    expect("input layout flag only", make(0x1, absent, 0, absent, 0, false), D::bind);
    expect("input layout flag with sampler flag", make(0x801, absent, 0, absent, 0, true), D::sampler_heap_missing);
    // The stand-in's request: no mirrored heap, indices unknown, state not restorable.
    expect("stand-in, views flag", make(0x400, absent, unknown, absent, unknown, false), D::view_heap_missing);
    expect("stand-in, sampler flag", make(0x800, absent, unknown, absent, unknown, false), D::sampler_heap_missing);
    expect("stand-in, both flags", make(0xc00, absent, unknown, absent, unknown, false), D::both_heaps_missing);
}

// Whether a heap holds the indices a draw reads, from its fields; one-entry heaps are where --mutate sampler-alone lies.
bool covers(const MirroredHeap& heap, UINT highest, bool sampler)
{
    if (sampler && sampler_alone && heap.bound) return true;
    if (!heap.bound) return false;
    if (heap.whole) return true;
    if (highest == unknown) return false;
    return highest < heap.entries;
}

D expected_for(UINT flags, const MirroredHeap& views, UINT highest_view, const MirroredHeap& samplers, UINT highest_sampler,
               bool restorable)
{
    const bool indexes_views = (flags & 0x400) != 0, indexes_samplers = (flags & 0x800) != 0;
    if (!indexes_views && !indexes_samplers) return D::bind;
    const bool view_missing = indexes_views && !covers(views, highest_view, false);
    // --mutate sampler-alone: any bound sampler heap is enough, also for the view heap.
    const bool sampler_missing = indexes_samplers && !covers(samplers, highest_sampler, true);
    const bool views_waived = sampler_alone && samplers.bound;
    if (view_missing && !views_waived && sampler_missing) return D::both_heaps_missing;
    if (view_missing && !views_waived) return D::view_heap_missing;
    if (sampler_missing) return D::sampler_heap_missing;
    if (!restorable && !bind_unrestorable) return D::state_not_restorable;
    return D::bind;
}

void matrix()
{
    const UINT flags[] = {0x0, 0x400, 0x800, 0xc00};
    const MirroredHeap heaps[] = {absent, one, four, whole};
    const UINT highest[] = {0, 1, 3, 4, unknown};
    char label[64];
    for (UINT f : flags)
        for (const MirroredHeap& v : heaps)
            for (UINT hv : highest)
                for (const MirroredHeap& s : heaps)
                    for (UINT hs : highest)
                        for (bool restorable : {false, true}) {
                            std::snprintf(label, sizeof(label), "matrix 0x%x", f);
                            expect(label, make(f, v, hv, s, hs, restorable), expected_for(f, v, hv, s, hs, restorable));
                        }
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc == 3 && std::strcmp(argv[1], "--mutate") == 0) {
        sampler_alone = std::strcmp(argv[2], "sampler-alone") == 0;
        bind_unrestorable = std::strcmp(argv[2], "bind-unrestorable") == 0;
        if (!sampler_alone && !bind_unrestorable) return 2;
    } else if (argc != 1) {
        return 2;
    }
    if (!sampler_alone && !bind_unrestorable) named();
    matrix();
    std::printf("%d of %d heap-indexed root decisions as expected\n", cases - failures, cases);
    return failures ? 1 : 0;
}
