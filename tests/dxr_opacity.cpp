#include "dxr_fixture.h"
#include "dxr_opacity_reference.h"
#include <cmath>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>

static_assert(dxr_opacity::forceOpaque == D3D12_RAY_FLAG_FORCE_OPAQUE);
static_assert(dxr_opacity::forceNonOpaque == D3D12_RAY_FLAG_FORCE_NON_OPAQUE);
static_assert(dxr_opacity::acceptFirst == D3D12_RAY_FLAG_ACCEPT_FIRST_HIT_AND_END_SEARCH);
static_assert(dxr_opacity::cullOpaque == D3D12_RAY_FLAG_CULL_OPAQUE);
static_assert(dxr_opacity::cullNonOpaque == D3D12_RAY_FLAG_CULL_NON_OPAQUE);
static_assert(dxr_opacity::geometryOpaque == D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE);
static_assert(dxr_opacity::instanceForceOpaque == D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_OPAQUE);
static_assert(dxr_opacity::instanceForceNonOpaque == D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_NON_OPAQUE);

// Each ray result is sorted into these failure categories.
enum Category { Status, Committed, Anomaly, Missing, Extra, AfterShaderCommit, AfterOpaqueCommit, CategoryCount };
static const char* const categoryNames[CategoryCount]{
    "status", "committed_hit", "candidate_out_of_range", "missing_candidate", "unexpected_candidate",
    "after_shader_commit", "after_opaque_commit"};

static bool matches(double actual, double wanted) {
    return std::abs(actual - wanted) <= 1e-5 * wanted;
}

static bool committed_mismatch(bool identity, bool candidateMatches, double distance, double wanted) {
    return !identity || !candidateMatches || !matches(distance, wanted);
}

bool check_dxr_opacity(ID3D12Device* device, bool warp, bool usePipeline, float nearT) {
    std::printf("dxr_opacity_tmin=%.9g\n", nearT);
    // The geometric reference must reproduce the hand-derived table on every
    // machine, including those that skip the GPU comparison.
    const auto scene = dxr_opacity::instances();
    const auto probes = dxr_opacity::rays(nearT);
    const auto mesh = dxr_opacity::vertices();
    const auto flags = dxr_opacity::geometryFlags();
    if (dxr_opacity::first_reference_mismatch(probes) != probes.size())
        throw std::runtime_error("Opacity reference disagrees with its hand-derived table");
    if (committed_mismatch(true, true, 2.0, 2.0) || !committed_mismatch(true, true, 2.0 + 0.25, 2.0))
        throw std::runtime_error("Committed distance control failed to distinguish an injected TMin offset");
    const auto backend = usePipeline ? dxr_fixture::Backend::pipeline : dxr_fixture::Backend::inline_query;
    if (!dxr_fixture::supported(device, backend)) return false;
    std::vector<dxr_fixture::Vertex> vertices(mesh.begin(), mesh.end());
    std::vector<D3D12_RAYTRACING_GEOMETRY_FLAGS> geometries;
    for (const auto value : flags) geometries.push_back(static_cast<D3D12_RAYTRACING_GEOMETRY_FLAGS>(value));
    std::vector<D3D12_RAYTRACING_INSTANCE_DESC> instances;
    for (const auto& source : scene) {
        auto desc = dxr_fixture::instance(source.id, 255, source.translateX);
        desc.Flags = source.flags;
        instances.push_back(desc);
    }
    std::vector<dxr_fixture::Ray> rays;
    for (const auto& probe : probes)
        rays.push_back({probe.origin[0], probe.origin[1], probe.origin[2], probe.nearT,
                        probe.direction[0], probe.direction[1], probe.direction[2], probe.farT, 255, probe.flags});
    const auto words = dxr_fixture::trace(device, vertices, instances, rays, backend, nullptr, &geometries);
    const bool allowWarpContinuation = warp && !usePipeline;
    UINT hits = 0, shaderCommits = 0, firstHitRays = 0, mismatches = 0, warpAfterCommit = 0, plusTMin = 0;
    UINT counts[CategoryCount]{};
    for (UINT i = 0; i < rays.size(); ++i) {
        const auto expected = dxr_opacity::evaluate(mesh, flags, scene, probes[i]);
        const bool firstHit = (probes[i].flags & dxr_opacity::acceptFirst) != 0;
        const UINT* value = words.data() + 4 * i;
        const bool hit = value[0] & 1;
        const UINT seen = value[0] >> 8 & 0xf, geometry = value[0] >> 12 & 0xf, primitive = value[0] >> 16 & 0xff;
        const bool shaderCommitted = value[0] >> 4 & 1;
        float distance, commitT;
        std::memcpy(&distance, value + 3, sizeof(distance));
        std::memcpy(&commitT, value + 2, sizeof(commitT));
        bool failed[CategoryCount]{}, tPlusTMin = false;
        failed[Status] = (value[0] & 0xff0000e0u) != 0 || hit != expected.closest.hit;
        // A closest-hit search has one answer. A first-hit search may end on any committable hit.
        if (hit && geometry < dxr_opacity::geometryCount) {
            const double wanted = expected.distance[geometry];
            const bool allowed = firstHit ? (expected.committable >> geometry & 1) != 0
                                          : geometry == expected.closest.geometry;
            const bool identity = allowed && primitive == 0 && value[1] == expected.closest.instance;
            // Only the shader commits a non-opaque hit. It records the
            // candidate distance at that commit, which must be the true one.
            const bool shaderHit = (expected.nonOpaque >> geometry & 1) != 0;
            const bool commitTMatches = !shaderHit || (shaderCommitted && matches(commitT, wanted));
            // Known driver deviation: CommittedRayT of a shader-committed hit
            // equals the candidate distance plus TMin.
            tPlusTMin = shaderHit && identity && commitTMatches && !matches(distance, wanted) &&
                        matches(distance, wanted + probes[i].nearT);
            failed[Committed] = committed_mismatch(identity, commitTMatches, distance, wanted);
        } else {
            failed[Committed] = hit || value[1] != 0 || value[3] != 0 || shaderCommitted || value[0] >> 12 != 0;
        }
        failed[Anomaly] = (value[0] >> 2 & 1) != 0;
        UINT required = expected.required;
        if (hit && geometry < dxr_opacity::geometryCount && (expected.nonOpaque >> geometry & 1)) required |= 1u << geometry;
        failed[Missing] = (seen & required) != required;
        failed[Extra] = (seen & ~expected.nonOpaque) != 0;
        // A first-hit search must present no candidate after its first commit.
        failed[AfterShaderCommit] = firstHit && (value[0] >> 3 & 1) != 0;
        failed[AfterOpaqueCommit] = firstHit && (value[0] >> 1 & 1) != 0;
        const bool afterCommit = failed[AfterShaderCommit] || failed[AfterOpaqueCommit];
        bool other = false;
        for (int c = 0; c < AfterShaderCommit; ++c) other = other || failed[c];
        // Known inline WARP deviation: WARP does not end a first-hit search at a
        // committed hit, automatic or shader. It is reported, not failed, only
        // when the final hit is committable and every other check passes.
        plusTMin += tPlusTMin;
        if (!other && (!afterCommit || allowWarpContinuation)) {
            warpAfterCommit += afterCommit;
            continue;
        }
        if (other || afterCommit) {
            std::string names;
            for (int c = 0; c < CategoryCount; ++c)
                if (failed[c]) { ++counts[c]; names += names.empty() ? "" : ","; names += categoryNames[c]; }
            if (mismatches < 16)
                std::fprintf(stderr, "ray=%u flags=0x%x [%s] expected hit=%u instance=%u geometry=%u distance=%g "
                             "committable=0x%x candidates required=0x%x allowed=0x%x actual word0=0x%x instance=%u "
                             "geometry=%u primitive=%u distance=%g shader_commit_t=%g\n", i, probes[i].flags, names.c_str(),
                             expected.closest.hit, expected.closest.instance, expected.closest.geometry,
                             expected.closest.distance, expected.committable, required, expected.nonOpaque,
                             value[0], value[1], geometry, primitive, distance, commitT);
            ++mismatches;
        }
    }
    for (UINT i = 0; i < rays.size(); ++i) {
        const auto expected = dxr_opacity::evaluate(mesh, flags, scene, probes[i]);
        hits += expected.closest.hit;
        shaderCommits += expected.closest.hit && (expected.nonOpaque >> expected.closest.geometry & 1);
        firstHitRays += (probes[i].flags & dxr_opacity::acceptFirst) != 0;
    }
    std::printf("dxr_opacity rays=%zu hits=%u shader_commits=%u misses=%zu first_hit_rays=%u mismatches=%u",
                rays.size(), hits, shaderCommits, rays.size() - hits, firstHitRays, mismatches);
    for (int c = 0; c < CategoryCount; ++c)
        if (counts[c]) std::printf(" %s=%u", categoryNames[c], counts[c]);
    if (allowWarpContinuation) std::printf(" warp_after_commit=%u", warpAfterCommit);
    std::printf(" committed_t_plus_tmin=%u", plusTMin);
    std::printf("\n");
    if (mismatches) throw std::runtime_error("DXR opacity or candidate handling disagrees with the CPU reference");
    if (hits != 94 || shaderCommits != 42 || firstHitRays != 36)
        throw std::runtime_error("Opacity fixture lost its expected coverage");
    return true;
}
