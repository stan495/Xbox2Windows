// Source-built control for the API trace's decision about what a provider call wrote.
//
// The trace compares only the outputs a contract declares, over each one's own width, and reports what
// it observed. The four cases here are the ones a reviewer asked for: argument index 0 (which must not
// double as "nothing changed"), two outputs changed at once (both indices reported), an exact and a
// short caller buffer (the width must come from the declaration and from the caller's capacity), and
// the same-value case (a provider may write the value that was already there, which is "same", not
// "not written"). The later cases cover a change of the second output alone, a partial change, and
// every declared contract's argument indices and widths.
//   --mutate slot-only   contracts looked up by slot number alone; must fail (negative control)
#include "startup_stubs/api_trace.h"

#include <cstdio>
#include <cstring>

namespace trace = x2w_api_trace;

namespace {
int failures;
using Bytes = unsigned char[trace::max_snapshot];

void check(const char* label, bool condition)
{
    std::printf("%s: %s\n", label, condition ? "ok" : "MISMATCH");
    if (!condition) ++failures;
}

bool slot_only;

// The contract lookup. The mutated one matches the slot number alone, so the first provider in table
// order that declares that slot answers for every provider.
const trace::DeclaredOut* lookup(unsigned long provider, int slot)
{
    if (!slot_only) return trace::declared(provider, slot);
    static constexpr unsigned long owners[] = {0x01acd177, 0xcef4fac0, 0x37e56907, 0x0dd112ac};
    for (const unsigned long owner : owners)
        if (const trace::DeclaredOut* entry = trace::declared(owner, slot)) return entry;
    return nullptr;
}

// One declared output at argument index 0 with a 16-byte width (the connectivity hint's shape).
void index_zero()
{
    const trace::DeclaredOut contract{0x37e56907, 15, 0, 16, trace::no_argument, trace::no_argument, 0};
    trace::Report report{};
    bool compared[2] = {true, false};
    std::size_t bytes[2] = {16, 0};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    after[0][0] = 1;  // only the output changed
    report = trace::decide(&contract, compared, before, after, bytes);
    char text[48] = {};
    trace::format(report, &contract, text, sizeof(text));
    check("index 0 change is reported", report.outcome == trace::Outcome::Changed && std::strcmp(text, "changed:0") == 0);
}

// Two declared outputs whose bytes both change: both argument indices must be listed.
void two_outputs()
{
    const trace::DeclaredOut contract{0x01acd177, 20, 3, 1, trace::no_argument, 4, 4};
    bool compared[2] = {true, true};
    std::size_t bytes[2] = {1, 4};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    after[0][0] = 1;
    after[1][0] = 2;
    const trace::Report report = trace::decide(&contract, compared, before, after, bytes);
    char text[48] = {};
    trace::format(report, &contract, text, sizeof(text));
    check("both changed outputs are listed", report.changed_count == 2 && std::strcmp(text, "changed:3,4") == 0);
}

// A one-byte output must be compared over one byte: a difference in the byte after it is not its change.
void exact_and_short_buffer()
{
    const trace::DeclaredOut flag{0x01acd177, 13, 1, 1, trace::no_argument, trace::no_argument, 0};
    bool compared[2] = {true, false};
    std::size_t bytes[2] = {1, 0};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    after[0][1] = 0x7f;  // outside the declared width
    trace::Report report = trace::decide(&flag, compared, before, after, bytes);
    check("a byte past the width is not this output's change", report.outcome == trace::Outcome::Same);

    // A caller buffer shorter than the declared width is compared over the caller's capacity only.
    const trace::DeclaredOut string{0x01acd177, 15, 3, 0, 2, 4, 8};
    std::size_t short_bytes[2] = {4, 8};
    unsigned char short_before[2][trace::max_snapshot] = {};
    unsigned char short_after[2][trace::max_snapshot] = {};
    short_after[0][4] = 0x11;  // past the four bytes the caller offered
    report = trace::decide(&string, compared, short_before, short_after, short_bytes);
    check("a short caller buffer is compared over its capacity", report.outcome == trace::Outcome::Same);
}

// Only the second declared output changes: its own argument index is listed, not the first one's. When
// the first output could not be compared, the change is still listed and the line says it is partial.
void second_output_only()
{
    const trace::DeclaredOut contract{0x01acd177, 20, 3, 1, trace::no_argument, 4, 4};
    bool compared[2] = {true, true};
    std::size_t bytes[2] = {1, 4};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    after[1][3] = 0x80;  // the deny reason's last byte
    trace::Report report = trace::decide(&contract, compared, before, after, bytes);
    char text[48] = {};
    trace::format(report, &contract, text, sizeof(text));
    check("a change of the second output alone lists its index", report.outcome == trace::Outcome::Changed &&
                                                                 report.changed_count == 1 && report.changed[0] == 1 &&
                                                                 std::strcmp(text, "changed:4") == 0);

    bool second_only[2] = {false, true};
    std::size_t second_bytes[2] = {0, 4};
    report = trace::decide(&contract, second_only, before, after, second_bytes);
    trace::format(report, &contract, text, sizeof(text));
    check("a change beside an output that was not compared is partial", report.outcome == trace::Outcome::Changed &&
                                                                         report.partial &&
                                                                         std::strcmp(text, "changed:4,partial") == 0);
}

// A provider that writes the value already there is "same", and a declared output that could not be
// compared is "partial", not "same".
void same_value_and_partial()
{
    const trace::DeclaredOut contract{0x01acd177, 20, 3, 1, trace::no_argument, 4, 4};
    bool compared[2] = {true, true};
    std::size_t bytes[2] = {1, 4};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    trace::Report report = trace::decide(&contract, compared, before, after, bytes);
    char text[48] = {};
    trace::format(report, &contract, text, sizeof(text));
    check("identical bytes are reported as same", report.outcome == trace::Outcome::Same && std::strcmp(text, "same") == 0);

    bool partial_compared[2] = {true, false};
    std::size_t partial_bytes[2] = {1, 0};
    report = trace::decide(&contract, partial_compared, before, after, partial_bytes);
    trace::format(report, &contract, text, sizeof(text));
    check("one comparable output is partial", report.outcome == trace::Outcome::Same && std::strcmp(text, "same,partial") == 0);

    bool none[2] = {false, false};
    std::size_t none_bytes[2] = {0, 0};
    report = trace::decide(&contract, none, before, after, none_bytes);
    trace::format(report, &contract, text, sizeof(text));
    check("no comparable output is unreadable", report.outcome == trace::Outcome::Unreadable &&
                                                      std::strcmp(text, "unreadable") == 0);
}

// A slot with no contract is not read at all and reports "?".
void unknown_contract()
{
    trace::Report report{};
    bool compared[2] = {};
    std::size_t bytes[2] = {};
    unsigned char before[2][trace::max_snapshot] = {};
    unsigned char after[2][trace::max_snapshot] = {};
    report = trace::decide(nullptr, compared, before, after, bytes);
    char text[48] = {};
    trace::format(report, nullptr, text, sizeof(text));
    check("undeclared slot reports unknown", report.outcome == trace::Outcome::Unknown && std::strcmp(text, "?") == 0);
}

// The declared table must answer for the slots the stand-in actually answers, including the two the
// review found missing.
void declared_table()
{
    check("XUserGetId is slot 11", lookup(0x01acd177, 11) != nullptr);
    check("XUserFindUserById is slot 12", lookup(0x01acd177, 12) != nullptr);
    check("secondary gamertag interface is declared", lookup(0xcef4fac0, 3) != nullptr);
    check("connectivity hint is declared", lookup(0x37e56907, 15) != nullptr);
    check("undeclared slot has no contract", lookup(0x01acd177, 33) == nullptr);
}

// Each declaration names the argument the stand-in writes and the width it writes there
// (user_call, the GetGamertag answer and area_call in runtime_proxy.cpp). An index one off reads a
// handle, a size or an enum value as if it were an address; a width too wide compares bytes the call
// never owned, and one too narrow misses part of the write.
void declared_contracts()
{
    constexpr unsigned char none = trace::no_argument;
    const trace::DeclaredOut expected[] = {
        {0x01acd177, 3, 1, 8, none, none, 0},   // XUserDuplicateHandle(handle, &duplicate): u64
        {0x01acd177, 8, 1, 8, none, none, 0},   // XUserAddResult(async, &handle): u64
        {0x01acd177, 9, 1, 8, none, none, 0},   // XUserGetLocalId(handle, &local id): u64
        {0x01acd177, 10, 1, 8, none, none, 0},  // XUserFindUserByLocalId(local id, &handle): u64
        {0x01acd177, 11, 1, 8, none, none, 0},  // XUserGetId(handle, &id): u64
        {0x01acd177, 12, 1, 8, none, none, 0},  // XUserFindUserById(id, &handle): u64
        {0x01acd177, 13, 1, 1, none, none, 0},  // XUserGetIsGuest(handle, &is guest): bool
        {0x01acd177, 14, 1, 4, none, none, 0},  // XUserGetState(handle, &state): u32
        {0x01acd177, 15, 3, 0, 2, 4, 8},        // XUserGetGamertag(handle, component, size, buffer, &used)
        {0x01acd177, 19, 1, 4, none, none, 0},  // XUserGetAgeGroup(handle, &group): u32
        {0x01acd177, 20, 3, 1, none, 4, 4},     // XUserCheckPrivilege(handle, options, privilege, &has, &deny)
        {0x01acd177, 40, 1, 8, none, none, 0},  // XUserFindForDevice(device id, &handle): u64
        {0xcef4fac0, 3, 3, 0, 2, 4, 8},         // GetGamertag(handle, component, size, buffer, &used)
        {0x37e56907, 15, 0, 16, none, none, 0}, // connectivity hint: four u32 words at argument 0
        {0x0dd112ac, 3, 1, 8, none, none, 0},   // XStoreCreateContext(user, &context): u64
    };
    for (const trace::DeclaredOut& want : expected) {
        const trace::DeclaredOut* got = lookup(want.provider, want.slot);
        char label[96];
        std::snprintf(label, sizeof(label), "%08lx slot %d declares args %d/%d widths %d/%d capacity %d", want.provider,
                      want.slot, want.first, want.second, want.first_bytes, want.second_bytes, want.size_argument);
        check(label, got && got->provider == want.provider && got->slot == want.slot && got->first == want.first &&
                         got->first_bytes == want.first_bytes && got->size_argument == want.size_argument &&
                         got->second == want.second && got->second_bytes == want.second_bytes);
    }
    // A slot number declared for one provider is not declared for another.
    check("gamertag interface slot 15 has no contract", lookup(0xcef4fac0, 15) == nullptr);
    check("store slot 20 has no contract", lookup(0x0dd112ac, 20) == nullptr);
    check("connectivity slot 3 has no contract", lookup(0x37e56907, 3) == nullptr);
}
}  // namespace

int main(int argc, char** argv)
{
    slot_only = argc == 3 && !std::strcmp(argv[1], "--mutate") && !std::strcmp(argv[2], "slot-only");
    if (argc != 1 && !slot_only) return 2;
    index_zero();
    two_outputs();
    second_output_only();
    exact_and_short_buffer();
    same_value_and_partial();
    unknown_contract();
    declared_table();
    declared_contracts();
    if (failures) {
        std::printf("FAIL\n");
        return 1;
    }
    std::printf("PASS\n");
    return 0;
}