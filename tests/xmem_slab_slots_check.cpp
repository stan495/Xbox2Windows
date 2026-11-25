// Slab slot bookkeeping of the physical pages (#1199), against a fake slab factory. Standard C++17 only.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "xmem_slab_slots.h"

namespace {
using Slots = x2w_xmem::SlabSlots<int>;
constexpr std::uint64_t slot_bytes = 65536;
constexpr std::uint64_t per_slab = 4096;

#define CHECK(condition)                                                                  \
    do {                                                                                  \
        if (!(condition)) {                                                               \
            std::fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__, #condition); \
            std::exit(1);                                                                 \
        }                                                                                 \
    } while (0)

struct Factory {
    int made = 0, discarded = 0;
    int fail_at = -1;  // the n-th creation (0-based) fails
    int attempts = 0;
    bool operator()(int& handle)
    {
        if (attempts++ == fail_at) return false;
        handle = 100 + made++;
        return true;
    }
};

Slots::Slot fresh(Slots& slots, Factory& factory)
{
    Slots::Slot slot;
    CHECK(slots.take_fresh(factory, [&](int) { ++factory.discarded; }, slot));
    return slot;
}

// What xmem_mapped_pages.cpp does: a released slot first, otherwise a fresh one.
Slots::Slot acquire(Slots& slots, Factory& factory)
{
    Slots::Slot slot;
    if (slots.lowest_free(slot)) {
        slots.take_lowest_free();
        return slot;
    }
    return fresh(slots, factory);
}

void counters(const Slots& slots, std::uint64_t open, std::uint64_t used, std::uint64_t free_slots)
{
    CHECK(slots.slab_count() == open);
    CHECK(slots.slots_in_use() == used);
    CHECK(slots.slots_free() == free_slots);
}

void fresh_slots_and_slab_boundary()
{
    Slots slots(slot_bytes);
    Factory factory;
    CHECK(slots.slab_bytes() == per_slab * slot_bytes);
    counters(slots, 0, 0, 0);
    for (std::uint64_t i = 0; i < per_slab; ++i) {
        const auto slot = fresh(slots, factory);
        CHECK(slot.slab == 100 && slot.slab_index == 0 && slot.offset == i * slot_bytes);
    }
    CHECK(factory.made == 1);
    counters(slots, 1, per_slab, 0);
    const auto next = fresh(slots, factory);  // the 4097th slot opens a second slab
    CHECK(next.slab == 101 && next.slab_index == 1 && next.offset == 0);
    CHECK(factory.made == 2);
    counters(slots, 2, per_slab + 1, per_slab - 1);
}

void reuse_is_lowest_first_and_adjacent()
{
    Slots slots(slot_bytes);
    Factory factory;
    for (int i = 0; i < 200; ++i) fresh(slots, factory);
    for (const std::uint64_t index : {100u, 7u, 5u, 6u, 3u}) CHECK(slots.release(0, index * slot_bytes));
    counters(slots, 1, 195, per_slab - 195);
    const std::uint64_t expected[] = {3, 5, 6, 7, 100};
    for (const std::uint64_t index : expected) CHECK(acquire(slots, factory).offset == index * slot_bytes);
    counters(slots, 1, 200, per_slab - 200);
    CHECK(acquire(slots, factory).offset == 200 * slot_bytes);  // nothing free: the next fresh slot
    CHECK(factory.made == 1);
}

void reuse_prefers_the_older_slab()
{
    Slots slots(slot_bytes);
    Factory factory;
    std::vector<Slots::Slot> all;
    for (std::uint64_t i = 0; i < per_slab + 10; ++i) all.push_back(fresh(slots, factory));
    CHECK(slots.release(1, 4 * slot_bytes));
    CHECK(slots.release(0, 9 * slot_bytes));
    const auto first = acquire(slots, factory);
    CHECK(first.slab_index == 0 && first.offset == 9 * slot_bytes);
    const auto second = acquire(slots, factory);
    CHECK(second.slab_index == 1 && second.offset == 4 * slot_bytes);
}

void freeing_everything_keeps_the_slabs()
{
    Slots slots(slot_bytes);
    Factory factory;
    std::vector<Slots::Slot> all;
    for (std::uint64_t i = 0; i < per_slab + 5; ++i) all.push_back(fresh(slots, factory));
    for (const auto& slot : all) CHECK(slots.release(slot.slab_index, slot.offset));
    counters(slots, 2, 0, 2 * per_slab);
    CHECK(factory.made == 2 && slots.slabs().size() == 2);
    // The slots are handed out again without a new slab.
    for (std::uint64_t i = 0; i < 2 * per_slab; ++i) acquire(slots, factory);
    CHECK(factory.made == 2);
    counters(slots, 2, 2 * per_slab, 0);
    acquire(slots, factory);
    CHECK(factory.made == 3);
}

void a_failing_factory_changes_nothing()
{
    Slots slots(slot_bytes);
    Factory factory;
    factory.fail_at = 0;
    Slots::Slot slot;
    CHECK(!slots.take_fresh(factory, [&](int) { ++factory.discarded; }, slot));
    counters(slots, 0, 0, 0);
    CHECK(slots.slabs().empty());
    CHECK(fresh(slots, factory).offset == 0);  // the next attempt succeeds from a clean state
    counters(slots, 1, 1, per_slab - 1);

    // A failure on the second slab leaves the full first slab and its counters as they were.
    Slots full(slot_bytes);
    Factory second_fails;
    second_fails.fail_at = 1;
    for (std::uint64_t i = 0; i < per_slab; ++i) fresh(full, second_fails);
    CHECK(!full.take_fresh(second_fails, [&](int) { ++second_fails.discarded; }, slot));
    counters(full, 1, per_slab, 0);
    CHECK(full.slabs().size() == 1 && second_fails.made == 1);
    CHECK(fresh(full, second_fails).slab_index == 1);
    counters(full, 2, per_slab + 1, per_slab - 1);
}

void unwinding_a_fresh_slot_gives_it_back()
{
    Slots slots(slot_bytes);
    Factory factory;
    const auto zero = fresh(slots, factory);
    slots.unwind_fresh(zero);
    counters(slots, 1, 0, per_slab);
    CHECK(!slots.release(0, 0));  // unwound slot is not handed out
    CHECK(fresh(slots, factory).offset == 0 && factory.made == 1);
    const auto lost = fresh(slots, factory);
    counters(slots, 1, 2, per_slab - 2);
    slots.unwind_fresh(lost);
    counters(slots, 1, 1, per_slab - 1);
    CHECK(fresh(slots, factory).offset == lost.offset);
    const auto first = Slots::Slot{100, 0, 0};
    slots.unwind_fresh(first);  // not the last slot handed out: ignored
    counters(slots, 1, 2, per_slab - 2);
}

void invalid_releases_are_refused()
{
    Slots slots(slot_bytes);
    Factory factory;
    const auto slot = fresh(slots, factory);
    CHECK(!slots.release(5, 0));                            // no such slab
    CHECK(!slots.release(0, 1));                            // not a slot boundary
    CHECK(!slots.release(0, per_slab * slot_bytes));        // past the slab
    CHECK(!slots.release(0, slot_bytes));                   // never handed out
    CHECK(slots.release(0, slot.offset));
    CHECK(!slots.release(0, slot.offset));                  // already free
    counters(slots, 1, 0, per_slab);
}
}  // namespace

int main()
{
    fresh_slots_and_slab_boundary();
    reuse_is_lowest_first_and_adjacent();
    reuse_prefers_the_older_slab();
    freeing_everything_keeps_the_slabs();
    a_failing_factory_changes_nothing();
    unwinding_a_fresh_slot_gives_it_back();
    invalid_releases_are_refused();
    std::puts("xmem slab slots: all checks passed");
    return 0;
}
