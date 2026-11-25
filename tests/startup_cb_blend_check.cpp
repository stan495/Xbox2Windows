// Checks src/startup_stubs/cb_blend.h without a device: every 5-bit factor value in each of the
// four factor slots, the combine function of colour and alpha apart, the refusal reason a caller
// counts, the MIN / MAX rule on each slot, the write mask of a blended target, and control_word
// against raw words.
//
// tests/startup_cb_blend.cpp (offscreen_check --startup-cb-blend) checks the source-colour slot
// against the full factor table and draws on a device; this check covers the other three slots,
// whose decode a device run does not reach for most values.
#include "startup_stubs/cb_blend.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

namespace {
using x2w_cb_blend::control_word;
using x2w_cb_blend::Decoded;

// BlendOp and CombFunc values (Mesa src/amd/registers/gfx10.json).
enum : std::uint32_t {
    ZERO = 0, ONE = 1, SRC_COLOR = 2, ONE_MINUS_SRC_COLOR = 3, SRC_ALPHA = 4, ONE_MINUS_SRC_ALPHA = 5,
    DST_ALPHA = 6, ONE_MINUS_DST_ALPHA = 7, DST_COLOR = 8, ONE_MINUS_DST_COLOR = 9, SRC_ALPHA_SATURATE = 10,
    BOTH_SRC_ALPHA = 11, BOTH_INV_SRC_ALPHA = 12, CONSTANT_COLOR = 13, ONE_MINUS_CONSTANT_COLOR = 14,
    SRC1_COLOR = 15, INV_SRC1_COLOR = 16, SRC1_ALPHA = 17, INV_SRC1_ALPHA = 18, CONSTANT_ALPHA = 19,
    ONE_MINUS_CONSTANT_ALPHA = 20,
};
enum : std::uint32_t { ADD = 0, SUB = 1, MIN = 2, MAX = 3, REV_SUB = 4 };

constexpr D3D12_BLEND none = static_cast<D3D12_BLEND>(0);
// The D3D12 factor of each BlendOp value in SrcBlend / DestBlend.
const D3D12_BLEND colour_slot[21] = {
    D3D12_BLEND_ZERO, D3D12_BLEND_ONE, D3D12_BLEND_SRC_COLOR, D3D12_BLEND_INV_SRC_COLOR, D3D12_BLEND_SRC_ALPHA,
    D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_DEST_ALPHA, D3D12_BLEND_INV_DEST_ALPHA, D3D12_BLEND_DEST_COLOR,
    D3D12_BLEND_INV_DEST_COLOR, D3D12_BLEND_SRC_ALPHA_SAT, none, none, D3D12_BLEND_BLEND_FACTOR,
    D3D12_BLEND_INV_BLEND_FACTOR, D3D12_BLEND_SRC1_COLOR, D3D12_BLEND_INV_SRC1_COLOR, D3D12_BLEND_SRC1_ALPHA,
    D3D12_BLEND_INV_SRC1_ALPHA, D3D12_BLEND_ALPHA_FACTOR, D3D12_BLEND_INV_ALPHA_FACTOR};
// The same in SrcBlendAlpha / DestBlendAlpha: a colour factor reads its alpha component there, and
// D3D12 accepts only the alpha spelling in those fields.
const D3D12_BLEND alpha_slot[21] = {
    D3D12_BLEND_ZERO, D3D12_BLEND_ONE, D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_SRC_ALPHA,
    D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_DEST_ALPHA, D3D12_BLEND_INV_DEST_ALPHA, D3D12_BLEND_DEST_ALPHA,
    D3D12_BLEND_INV_DEST_ALPHA, D3D12_BLEND_SRC_ALPHA_SAT, none, none, D3D12_BLEND_BLEND_FACTOR,
    D3D12_BLEND_INV_BLEND_FACTOR, D3D12_BLEND_SRC1_ALPHA, D3D12_BLEND_INV_SRC1_ALPHA, D3D12_BLEND_SRC1_ALPHA,
    D3D12_BLEND_INV_SRC1_ALPHA, D3D12_BLEND_ALPHA_FACTOR, D3D12_BLEND_INV_ALPHA_FACTOR};

int failures, cases;
// --mutate dest-alpha-colour: expect DestBlendAlpha in its colour spelling, as a decoder that
// skips the alpha form on the destination slot gives (a negative control).
bool colour_dest_alpha;

void fail(const std::string& name, const std::string& what)
{
    std::printf("FAIL %s: %s\n", name.c_str(), what.c_str());
    ++failures;
}

bool refused_factor(std::uint32_t f, bool alpha_factor)
{
    return f == BOTH_SRC_ALPHA || f == BOTH_INV_SRC_ALPHA || f > ONE_MINUS_CONSTANT_ALPHA ||
           (!alpha_factor && (f == CONSTANT_ALPHA || f == ONE_MINUS_CONSTANT_ALPHA));
}

const char* factor_reason(std::uint32_t f)
{
    return f == BOTH_SRC_ALPHA || f == BOTH_INV_SRC_ALPHA ? "BOTH_SRC_ALPHA"
         : f == CONSTANT_ALPHA || f == ONE_MINUS_CONSTANT_ALPHA ? "CONSTANT_ALPHA"
                                                                 : "unknown blend factor";
}

void expect_refused(const std::string& name, const Decoded& d, const char* reason)
{
    ++cases;
    if (d.ok) return fail(name, "accepted");
    if (!d.reason || !std::strstr(d.reason, reason))
        fail(name, std::string("reason \"") + (d.reason ? d.reason : "(none)") + "\", expected one naming " + reason);
}

struct Want {
    D3D12_BLEND s, d;
    D3D12_BLEND_OP op;
    D3D12_BLEND sa, da;
    D3D12_BLEND_OP op_a;
};

void expect_blend(const std::string& name, const Decoded& d, const Want& w, std::uint32_t mask = 0xF)
{
    ++cases;
    if (!d.ok) return fail(name, std::string("refused: ") + (d.reason ? d.reason : "(no reason)"));
    const auto& t = d.desc;
    if (!t.BlendEnable || t.SrcBlend != w.s || t.DestBlend != w.d || t.BlendOp != w.op || t.SrcBlendAlpha != w.sa ||
        t.DestBlendAlpha != w.da || t.BlendOpAlpha != w.op_a || t.RenderTargetWriteMask != mask) {
        char got[160];
        std::snprintf(got, sizeof(got), "got enable %d src %d dst %d op %d, alpha src %d dst %d op %d, mask %x; expected %d %d %d, %d %d %d, %x",
                      t.BlendEnable, t.SrcBlend, t.DestBlend, t.BlendOp, t.SrcBlendAlpha, t.DestBlendAlpha, t.BlendOpAlpha,
                      t.RenderTargetWriteMask, w.s, w.d, w.op, w.sa, w.da, w.op_a, mask);
        fail(name, got);
    }
}

D3D12_BLEND want_dest_alpha(std::uint32_t f) { return colour_dest_alpha ? colour_slot[f] : alpha_slot[f]; }

// Each 5-bit value in each slot, with the other three slots at ONE / ZERO.
void check_slots()
{
    const char* slot_names[4] = {"colour source", "colour destination", "alpha source", "alpha destination"};
    for (unsigned slot = 0; slot < 4; ++slot) {
        for (std::uint32_t f = 0; f < 32; ++f) {
            for (bool alpha_factor : {false, true}) {
                std::uint32_t v[4] = {ONE, ZERO, ONE, ZERO};
                v[slot] = f;
                const auto d = x2w_cb_blend::decode(control_word(v[0], ADD, v[1], v[2], ADD, v[3], true, true), 0xF, alpha_factor);
                const std::string name = std::string(slot_names[slot]) + " factor " + std::to_string(f) +
                                         (alpha_factor ? " (alpha blend factor)" : "");
                if (refused_factor(f, alpha_factor)) {
                    expect_refused(name, d, factor_reason(f));
                    continue;
                }
                Want w{D3D12_BLEND_ONE, D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD, D3D12_BLEND_ONE, D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD};
                if (slot == 0) w.s = colour_slot[f];
                if (slot == 1) w.d = colour_slot[f];
                if (slot == 2) w.sa = alpha_slot[f];
                if (slot == 3) w.da = want_dest_alpha(f);
                expect_blend(name, d, w);
            }
        }
    }
    // Shared alpha: the colour fields fill both channels and the alpha fields (here a refused
    // factor and function in every alpha field) are not read.
    for (std::uint32_t f = 0; f < 32; ++f) {
        for (bool alpha_factor : {false, true}) {
            const std::string tag = std::to_string(f) + (alpha_factor ? " (alpha blend factor)" : "");
            const auto as_source = x2w_cb_blend::decode(control_word(f, ADD, ZERO, 31, 7, 31, false, true), 0xF, alpha_factor);
            const auto as_dest = x2w_cb_blend::decode(control_word(ONE, ADD, f, 31, 7, 31, false, true), 0xF, alpha_factor);
            if (refused_factor(f, alpha_factor)) {
                expect_refused("shared source factor " + tag, as_source, factor_reason(f));
                expect_refused("shared destination factor " + tag, as_dest, factor_reason(f));
                continue;
            }
            expect_blend("shared source factor " + tag, as_source,
                         {colour_slot[f], D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD, alpha_slot[f], D3D12_BLEND_ZERO, D3D12_BLEND_OP_ADD});
            expect_blend("shared destination factor " + tag, as_dest,
                         {D3D12_BLEND_ONE, colour_slot[f], D3D12_BLEND_OP_ADD, D3D12_BLEND_ONE, want_dest_alpha(f), D3D12_BLEND_OP_ADD});
        }
    }
}

// Every pair of colour and alpha CombFunc values, then shared alpha with an unknown alpha field.
void check_combine()
{
    const D3D12_BLEND_OP ops[5] = {D3D12_BLEND_OP_ADD, D3D12_BLEND_OP_SUBTRACT, D3D12_BLEND_OP_MIN, D3D12_BLEND_OP_MAX,
                                   D3D12_BLEND_OP_REV_SUBTRACT};
    for (std::uint32_t cf = 0; cf < 8; ++cf) {
        for (std::uint32_t af = 0; af < 8; ++af) {
            const std::string name = "combine colour " + std::to_string(cf) + " alpha " + std::to_string(af);
            const auto d = x2w_cb_blend::decode(control_word(ONE, cf, ONE, ONE, af, ONE, true, true), 0xF, false);
            if (cf > 4 || af > 4) {
                expect_refused(name, d, "unknown combine function");
                continue;
            }
            expect_blend(name, d, {D3D12_BLEND_ONE, D3D12_BLEND_ONE, ops[cf], D3D12_BLEND_ONE, D3D12_BLEND_ONE, ops[af]});
        }
        const std::string name = "shared combine " + std::to_string(cf);
        const auto d = x2w_cb_blend::decode(control_word(ONE, cf, ONE, ZERO, 7, ZERO, false, true), 0xF, false);
        if (cf > 4)
            expect_refused(name, d, "unknown combine function");
        else
            expect_blend(name, d, {D3D12_BLEND_ONE, D3D12_BLEND_ONE, ops[cf], D3D12_BLEND_ONE, D3D12_BLEND_ONE, ops[cf]});
    }
}

// MIN / MAX keep their meaning only with ONE on both factors of that channel. The other channel
// adds with SRC_ALPHA / INV_SRC_ALPHA, which is not a reason to refuse.
void check_min_max()
{
    for (bool alpha_channel : {false, true}) {
        for (std::uint32_t fcn : {MIN, MAX}) {
            const D3D12_BLEND_OP op = fcn == MIN ? D3D12_BLEND_OP_MIN : D3D12_BLEND_OP_MAX;
            const std::string channel = std::string(alpha_channel ? "alpha " : "colour ") + (fcn == MIN ? "MIN" : "MAX");
            const auto word = [&](std::uint32_t src, std::uint32_t dst) {
                return alpha_channel ? control_word(SRC_ALPHA, ADD, ONE_MINUS_SRC_ALPHA, src, fcn, dst, true, true)
                                     : control_word(src, fcn, dst, SRC_ALPHA, ADD, ONE_MINUS_SRC_ALPHA, true, true);
            };
            const Want w = alpha_channel
                ? Want{D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD, D3D12_BLEND_ONE, D3D12_BLEND_ONE, op}
                : Want{D3D12_BLEND_ONE, D3D12_BLEND_ONE, op, D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD};
            expect_blend(channel + " with ONE / ONE", x2w_cb_blend::decode(word(ONE, ONE), 0xF, false), w);
            for (std::uint32_t other : {ZERO, SRC_ALPHA, INV_SRC1_COLOR}) {
                const std::string f = std::to_string(other);
                expect_refused(channel + " with source factor " + f, x2w_cb_blend::decode(word(other, ONE), 0xF, false), "MIN / MAX");
                expect_refused(channel + " with destination factor " + f, x2w_cb_blend::decode(word(ONE, other), 0xF, false), "MIN / MAX");
            }
        }
    }
}

// The write mask of a blended and of an unblended target, and masks wider than a nibble.
void check_mask()
{
    const Want straight{D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD,
                        D3D12_BLEND_SRC_ALPHA, D3D12_BLEND_INV_SRC_ALPHA, D3D12_BLEND_OP_ADD};
    for (std::uint32_t mask = 0; mask < 16; ++mask) {
        expect_blend("blended mask " + std::to_string(mask), x2w_cb_blend::decode(0xE5040504, mask, false), straight, mask);
        ++cases;
        const auto off = x2w_cb_blend::decode(0xA5040504, mask, false);  // ENABLE clear
        if (!off.ok || off.desc.BlendEnable || off.desc.RenderTargetWriteMask != mask) fail("unblended mask " + std::to_string(mask), "differs");
    }
    for (std::uint32_t mask : {0x10u, 0x1Fu, 0x100u, 0xFFFFFFFFu})
        expect_refused("mask " + std::to_string(mask), x2w_cb_blend::decode(0xE5040504, mask, false), "target mask");
}

// control_word against words written out by hand, and one raw word with a distinct value in each
// field decoded without control_word.
void check_words()
{
    struct Word { std::uint32_t built, raw; };
    const Word words[] = {
        {control_word(SRC_ALPHA, ADD, ONE_MINUS_SRC_ALPHA, SRC_ALPHA, ADD, ONE_MINUS_SRC_ALPHA, true, true), 0x65040504},
        {control_word(ONE, ADD, ONE, ZERO, ADD, ONE, true, true), 0x61000101},
        // SRC1_ALPHA REV_SUB INV_SRC1_ALPHA, CONSTANT_ALPHA SUB ONE_MINUS_CONSTANT_ALPHA:
        // 0x11 | 4 << 5 | 0x12 << 8 | 0x13 << 16 | 1 << 21 | 0x14 << 24 | 1 << 29 | 1 << 30.
        {control_word(SRC1_ALPHA, REV_SUB, INV_SRC1_ALPHA, CONSTANT_ALPHA, SUB, ONE_MINUS_CONSTANT_ALPHA, true, true), 0x74331291},
        {control_word(0x3F, 0xF, 0x3F, 0x3F, 0xF, 0x3F, false, false), 0x1FFF1FFF},
    };
    for (const auto& w : words) {
        ++cases;
        if (w.built != w.raw) {
            char what[64];
            std::snprintf(what, sizeof(what), "built %08x, expected %08x", w.built, w.raw);
            fail("control_word", what);
        }
    }
    expect_blend("raw 74331291 (alpha blend factor)", x2w_cb_blend::decode(0x74331291, 0xF, true),
                 {D3D12_BLEND_SRC1_ALPHA, D3D12_BLEND_INV_SRC1_ALPHA, D3D12_BLEND_OP_REV_SUBTRACT, D3D12_BLEND_ALPHA_FACTOR,
                  D3D12_BLEND_INV_ALPHA_FACTOR, D3D12_BLEND_OP_SUBTRACT});
    expect_refused("raw 74331291", x2w_cb_blend::decode(0x74331291, 0xF, false), "CONSTANT_ALPHA");
}
}  // namespace

int main(int argc, char** argv)
{
    colour_dest_alpha = argc > 2 && std::strcmp(argv[1], "--mutate") == 0 && std::strcmp(argv[2], "dest-alpha-colour") == 0;
    if (argc > 1 && !colour_dest_alpha) {
        std::printf("unknown mutation\n");
        return 2;
    }
    check_slots();
    check_combine();
    check_min_max();
    check_mask();
    check_words();
    if (failures) {
        std::printf("startup_cb_blend_decode: %d of %d FAILED\n", failures, cases);
        return 1;
    }
    std::printf("startup_cb_blend_decode: PASS (%d cases)\n", cases);
    return 0;
}
