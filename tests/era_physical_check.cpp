// Page-number bookkeeping behind XMemAllocatePhysicalPages and XMemFreePhysicalPages.
// The same source is linked twice: era_physical is the xmem.dll policy, and era_physical_lenient is
// that file built with ERA_PHYSICAL_LENIENT_RELEASE, whose release clears any listed page like
// SlimEra's FreeTitlePhysicalPages. Every ownership and page-count control must be refused by the
// policy, leaving the granted page total unchanged, and accepted by the lenient build, which shows
// the control can detect that handling. All page arrays are synthetic.
#include "era_physical.h"

#include <algorithm>
#include <cstdio>
#include <string_view>
#include <vector>

namespace era_physical_lenient {
enum class Status { ok, invalid, exhausted };
Status allocate(std::uint64_t flags, std::uint64_t* count, std::uint64_t* pages) noexcept;
Status release(std::uint64_t count, const std::uint64_t* pages) noexcept;
std::uint64_t granted_pages() noexcept;
}

using namespace std::literals;
using Pages = std::vector<std::uint64_t>;

namespace {
template <typename Status>
struct Module {
    Status (*allocate)(std::uint64_t, std::uint64_t*, std::uint64_t*) noexcept;
    Status (*release)(std::uint64_t, const std::uint64_t*) noexcept;
    std::uint64_t (*granted)() noexcept;

    bool grant(std::uint64_t count, Pages& pages) const
    {
        pages.assign(count, 0);
        auto inout = count;
        return allocate(era_physical::mem_64k_pages, &inout, pages.data()) == Status::ok && inout == count;
    }
    bool free(const Pages& pages) const
    {
        return release(pages.size(), pages.data()) == Status::ok;
    }
};

struct Control {
    const char* name;
    // Builds the release array from three live grants of 4, 3 and 1 pages. When release_a_first
    // is set, a has already been released once.
    Pages (*build)(const Pages& a, const Pages& b, const Pages& c);
    bool release_a_first;
};

const Control controls[] = {
    {"short_count", [](const Pages& a, const Pages&, const Pages&) { return Pages(a.begin(), a.end() - 1); }, false},
    {"long_count", [](const Pages& a, const Pages& b, const Pages&) { Pages p(a); p.push_back(b[0]); return p; }, false},
    {"single_page_of_grant", [](const Pages&, const Pages& b, const Pages&) { return Pages{b[1]}; }, false},
    {"double_free", [](const Pages& a, const Pages&, const Pages&) { return a; }, true},
    {"never_issued", [](const Pages&, const Pages&, const Pages& c) { return Pages{c[0] + 1000}; }, false},
    {"forged_mix", [](const Pages& a, const Pages& b, const Pages&) { return Pages{a[0], a[1], b[0], b[1]}; }, false},
    {"duplicate_entry", [](const Pages& a, const Pages&, const Pages&) { return Pages{a[0], a[0], a[2], a[3]}; }, false},
};

// Runs one control from a clean registry. Returns 1 when the module refused the array and the
// granted total did not move, 0 when it accepted, and -1 when the setup or cleanup failed.
template <typename Status>
int run(const Module<Status>& m, const Control& control)
{
    if (m.granted() != 0) return -1;
    Pages a, b, c;
    if (!m.grant(4, a) || !m.grant(3, b) || !m.grant(1, c)) return -1;
    if (control.release_a_first && !m.free(a)) return -1;
    const auto before = m.granted();
    const auto pages = control.build(a, b, c);
    const bool refused = m.release(pages.size(), pages.data()) != Status::ok;
    const bool unchanged = m.granted() == before;
    // Release whatever is still granted. After an accepted (lenient) release, list every page.
    if (refused) {
        if (!control.release_a_first && !m.free(a)) return -1;
        if (!m.free(b) || !m.free(c)) return -1;
    } else {
        Pages rest;
        for (const auto* grant : {&a, &b, &c}) rest.insert(rest.end(), grant->begin(), grant->end());
        m.release(rest.size(), rest.data());  // lenient: clears whatever is left
    }
    if (m.granted() != 0) return -1;
    return refused && unchanged ? 1 : 0;
}

bool controls_check()
{
    const Module<era_physical::Status> strict{era_physical::allocate, era_physical::release,
                                              era_physical::granted_pages};
    const Module<era_physical_lenient::Status> lenient{era_physical_lenient::allocate,
                                                       era_physical_lenient::release,
                                                       era_physical_lenient::granted_pages};
    bool ok = true;
    for (const auto& control : controls) {
        const int policy = run(strict, control), control_build = run(lenient, control);
        std::printf("%-22s policy=%s lenient=%s\n", control.name,
                    policy == 1 ? "refused" : policy == 0 ? "ACCEPTED" : "SETUP-FAILED",
                    control_build == 0 ? "accepted" : control_build == 1 ? "REFUSED" : "SETUP-FAILED");
        ok = ok && policy == 1 && control_build == 0;
    }
    return ok;
}

// Exact bookkeeping for valid grants and releases, and allocation failure with cleanup.
bool lifetime_check()
{
    using era_physical::Status;
    const Module<Status> m{era_physical::allocate, era_physical::release, era_physical::granted_pages};
    Pages a, b;
    if (m.granted() != 0 || !m.grant(5, a) || m.granted() != 5 || !m.grant(2, b) || m.granted() != 7) return false;
    Pages sorted(a);
    sorted.insert(sorted.end(), b.begin(), b.end());
    std::sort(sorted.begin(), sorted.end());
    if (sorted.front() == 0 || sorted.back() > era_physical::capacity_pages ||
        std::adjacent_find(sorted.begin(), sorted.end()) != sorted.end()) return false;
    // Two whole grants in one call, in any order.
    Pages both(b.rbegin(), b.rend());
    both.insert(both.end(), a.begin(), a.end());
    if (!m.free(both) || m.granted() != 0) return false;

    // Exhaustion: all or nothing, the count is reset to 0 and the array is untouched.
    Pages most;
    if (!m.grant(era_physical::capacity_pages - 1, most)) return false;
    Pages probe(2, 0xa5a5a5a5a5a5a5a5);
    std::uint64_t count = 2;
    if (m.allocate(era_physical::mem_64k_pages, &count, probe.data()) != Status::exhausted || count != 0 ||
        probe[0] != 0xa5a5a5a5a5a5a5a5 || probe[1] != 0xa5a5a5a5a5a5a5a5 ||
        m.granted() != era_physical::capacity_pages - 1) return false;
    if (!m.free(most) || m.granted() != 0) return false;

    // Invalid allocate/release arguments leave granted_pages() unchanged.
    std::uint64_t sentinel_page = 0xa5a5a5a5a5a5a5a5;
    std::uint64_t zero_count = 0;
    std::uint64_t too_many = era_physical::capacity_pages + 1;
    std::uint64_t one_count = 1;
    if (m.allocate(era_physical::mem_64k_pages, nullptr, &sentinel_page) != Status::invalid ||
        m.allocate(era_physical::mem_64k_pages, &zero_count, &sentinel_page) != Status::invalid || zero_count != 0 ||
        m.allocate(era_physical::mem_64k_pages, &too_many, &sentinel_page) != Status::invalid || too_many != 0 ||
        m.allocate(era_physical::mem_64k_pages, &one_count, nullptr) != Status::invalid || one_count != 0) {
        return false;
    }
    one_count = 1;
    if (m.allocate(0, &one_count, &sentinel_page) != Status::invalid || one_count != 0) return false;
    one_count = 1;
    if (m.allocate(era_physical::mem_64k_pages | (1ull << 32), &one_count, &sentinel_page) != Status::invalid ||
        one_count != 0 || sentinel_page != 0xa5a5a5a5a5a5a5a5) {
        return false;
    }
    const std::uint64_t zero_page = 0;
    const std::uint64_t out_of_range_page = era_physical::capacity_pages + 1;
    if (m.release(0, &sentinel_page) != Status::invalid || m.release(1, nullptr) != Status::invalid ||
        m.release(era_physical::capacity_pages + 1, &sentinel_page) != Status::invalid ||
        m.release(1, &zero_page) != Status::invalid || m.release(1, &out_of_range_page) != Status::invalid ||
        m.granted() != 0) {
        return false;
    }
    std::printf("lifetime: grants=2 pages=7 exhausted_at=%llu\n",
                static_cast<unsigned long long>(era_physical::capacity_pages - 1));
    return true;
}

// Ownership as XMemMapPhysicalPages sees it (owns), from an empty registry to a full one and back.
bool owns_check()
{
    using era_physical::Status;
    using era_physical::capacity_pages;
    using era_physical::owns;
    const Module<Status> m{era_physical::allocate, era_physical::release, era_physical::granted_pages};
    bool ok = true;
    const auto expect = [&ok](bool condition, const char* what) {
        if (!condition) std::printf("FAIL: owns: %s\n", what);
        ok = ok && condition;
    };

    // Before the first grant the registry does not exist: nothing is owned and nothing releases.
    const std::uint64_t first_page = 1, last_page = capacity_pages;
    expect(!owns(0) && !owns(first_page) && !owns(last_page) && m.granted() == 0, "a page is owned before any grant");
    expect(m.release(1, &first_page) == Status::invalid && m.granted() == 0, "a release before any grant was accepted");

    // Two live grants: each of their pages is owned, other numbers are not.
    Pages a, b;
    if (!m.grant(4, a) || !m.grant(3, b)) {
        expect(false, "setup: grants of 4 and 3 pages");
        return false;
    }
    Pages live(a);
    live.insert(live.end(), b.begin(), b.end());
    std::sort(live.begin(), live.end());
    expect(std::all_of(live.begin(), live.end(), owns), "a granted page is not owned");
    std::uint64_t free_page = 1;
    while (std::binary_search(live.begin(), live.end(), free_page)) ++free_page;
    expect(!owns(free_page) && !owns(live.back() + 1), "a page that was never issued is owned");
    expect(!owns(0) && !owns(capacity_pages + 1) && !owns(~std::uint64_t{0}) &&
           !owns(std::uint64_t{1} << 32 | live.front()), "a number outside 1..capacity_pages is owned");

    // A refused release changes no ownership; a whole release ends it for that grant only.
    expect(m.release(2, b.data()) == Status::invalid && std::all_of(b.begin(), b.end(), owns),
           "a refused release changed ownership");
    expect(m.free(a) && std::none_of(a.begin(), a.end(), owns) && std::all_of(b.begin(), b.end(), owns),
           "a release did not end ownership of exactly its grant");

    // Granting every remaining page issues each number 1..capacity_pages exactly once, the last one
    // included, and every one of them is owned.
    Pages rest;
    if (!m.grant(capacity_pages - b.size(), rest)) {
        expect(false, "setup: grant of every remaining page");
        return false;
    }
    Pages all(rest);
    all.insert(all.end(), b.begin(), b.end());
    std::sort(all.begin(), all.end());
    bool every_number = all.size() == capacity_pages;
    for (std::uint64_t i = 0; every_number && i < all.size(); ++i) every_number = all[i] == i + 1;
    expect(every_number, "a full registry does not hold each page number once");
    expect(owns(first_page) && owns(last_page), "last page: the first or last page number is not owned when granted");
    expect(std::all_of(all.begin(), all.end(), owns), "a page of a full registry is not owned");
    std::uint64_t one = 1, extra = 0;
    expect(m.allocate(era_physical::mem_64k_pages, &one, &extra) == Status::exhausted && one == 0,
           "a full registry granted another page");

    // After every release nothing is owned.
    expect(m.free(rest) && m.free(b) && m.granted() == 0, "cleanup: release every grant");
    expect(!owns(first_page) && !owns(last_page) && std::none_of(all.begin(), all.end(), owns),
           "a released page is still owned");
    if (ok) std::printf("owns: 2 grants, %llu pages owned at full capacity\n",
                        static_cast<unsigned long long>(all.size()));
    return ok;
}
}

int main(int argc, char** argv)
{
    if (argc != 2) return 2;
    const std::string_view scenario = argv[1];
    bool passed;
    if (scenario == "controls"sv) {
        passed = controls_check();
    } else if (scenario == "lifetime"sv) {
        passed = lifetime_check();
    } else if (scenario == "owns"sv) {
        passed = owns_check();
    } else {
        return 2;
    }
    std::printf("era_physical %s: %s\n", argv[1], passed ? "PASS" : "FAIL");
    return passed ? 0 : 1;
}
