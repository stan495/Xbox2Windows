#pragma once
// CPU reference for triangle opacity, opacity culling and first-hit search in
// inline DXR queries. It uses only the standard library so it can be compiled
// and checked without Direct3D.
#include <array>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <vector>

namespace dxr_opacity {
// RAY_FLAG_* values.
constexpr std::uint32_t forceOpaque = 0x1, forceNonOpaque = 0x2, acceptFirst = 0x4,
                        cullOpaque = 0x40, cullNonOpaque = 0x80;
constexpr std::uint32_t geometryOpaque = 0x1; // D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE
// D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_OPAQUE and _FORCE_NON_OPAQUE.
constexpr std::uint32_t instanceForceOpaque = 0x4, instanceForceNonOpaque = 0x8;
// Fixture convention, not a ray flag: bits 16-19 of the flags word select the
// geometries whose non-opaque candidates the shader commits.
constexpr int acceptShift = 16;
constexpr std::uint32_t geometryCount = 4;
constexpr float defaultTMin = 0.25f;
// Keep TMin below the closest intersection at distance 1 in either direction.
constexpr float maxTMin = 0.5f;

using Vec = std::array<double, 3>;
using Vertex = std::array<float, 3>;
struct Instance {
    std::uint32_t id;
    float translateX;
    std::uint32_t flags;
    // Hand-derived: for each entry of rayFlags, the opacity of geometries 0-3
    // as O (opaque), N (non-opaque) or - (culled).
    const char* opacity;
};
struct Ray {
    std::array<float, 3> origin, direction;
    float nearT, farT;
    std::uint32_t flags; // Ray flags, plus the accept mask at acceptShift.
};
struct Hit {
    bool hit = false;
    std::uint32_t instance = 0, geometry = 0;
    double distance = 0;
};
// Everything the shader result may be compared against for one ray.
struct Expected {
    Hit closest;                 // Closest committed hit without the first-hit flag.
    std::array<double, geometryCount> distance{}; // Per geometry on the ray's instance; 0 when missed.
    std::uint32_t committable = 0; // Geometries a first-hit search may legally end on.
    std::uint32_t nonOpaque = 0;   // Geometries that may be presented as candidates.
    std::uint32_t required = 0;    // Candidates that must have been presented.
};

// One triangle per geometry, stacked along z. Geometries 1 and 3 are opaque.
inline std::vector<Vertex> vertices() {
    std::vector<Vertex> result;
    for (std::uint32_t g = 0; g < geometryCount; ++g) {
        const float z = static_cast<float>(g + 1);
        result.insert(result.end(), {{0, 0, z}, {0, 1, z}, {1, 0, z}});
    }
    return result;
}
inline std::vector<std::uint32_t> geometryFlags() { return {0, geometryOpaque, 0, geometryOpaque}; }

constexpr std::uint32_t rayFlags[]{0, forceOpaque, forceNonOpaque, cullOpaque, cullNonOpaque,
                                   acceptFirst, acceptFirst | forceNonOpaque};
constexpr std::uint32_t acceptMasks[]{0x0, 0xf, 0x4};
inline std::vector<Instance> instances() {
    return {
        {1, 0, 0, "NONO OOOO NNNN N-N- -O-O NONO NNNN"},
        {2, 8, instanceForceOpaque, "OOOO OOOO NNNN ---- OOOO OOOO NNNN"},
        {3, 16, instanceForceNonOpaque, "NNNN OOOO NNNN NNNN ---- NNNN NNNN"},
    };
}

// 42 rays per instance: both directions along z, each ray flag set and each accept mask.
inline std::vector<Ray> rays(float nearT) {
    std::vector<Ray> result;
    for (const auto& instance : instances())
        for (int reversed = 0; reversed < 2; ++reversed)
            for (const auto flags : rayFlags)
                for (const auto accept : acceptMasks)
                    result.push_back({{instance.translateX + .25f, .25f, reversed ? 5.f : 0.f},
                                      {0, 0, reversed ? -1.f : 1.f}, nearT, 8, flags | accept << acceptShift});
    return result;
}

enum class Opacity { opaque, nonOpaque, culled };
// Ray flags override instance flags, which override geometry flags. The cull
// flags then remove primitives by the resulting opacity.
inline Opacity opacity(std::uint32_t geometry, std::uint32_t instance, std::uint32_t ray) {
    bool opaque = (geometry & geometryOpaque) != 0;
    if (instance & instanceForceOpaque) opaque = true;
    if (instance & instanceForceNonOpaque) opaque = false;
    if (ray & forceOpaque) opaque = true;
    if (ray & forceNonOpaque) opaque = false;
    if (ray & (opaque ? cullOpaque : cullNonOpaque)) return Opacity::culled;
    return opaque ? Opacity::opaque : Opacity::nonOpaque;
}

inline Vec sub(const Vec& a, const Vec& b) { return {a[0]-b[0], a[1]-b[1], a[2]-b[2]}; }
inline Vec cross(const Vec& a, const Vec& b) {
    return {a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]};
}
inline double dot(const Vec& a, const Vec& b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }

// Brute force over every instance and geometry in double precision.
inline Expected evaluate(const std::vector<Vertex>& mesh, const std::vector<std::uint32_t>& geometries,
                         const std::vector<Instance>& scene, const Ray& ray) {
    Expected result;
    const std::uint32_t accept = ray.flags >> acceptShift & 0xf;
    struct Candidate { std::uint32_t instance, geometry; double t; Opacity opacity; };
    std::vector<Candidate> found;
    for (const auto& instance : scene) {
        const Vec origin{ray.origin[0] - instance.translateX, ray.origin[1], ray.origin[2]};
        const Vec direction{ray.direction[0], ray.direction[1], ray.direction[2]};
        for (std::uint32_t g = 0; g < geometries.size(); ++g) {
            Vec v[3];
            for (int corner = 0; corner < 3; ++corner)
                for (int axis = 0; axis < 3; ++axis) v[corner][axis] = mesh[3*g + corner][axis];
            const Vec e1 = sub(v[1], v[0]), e2 = sub(v[2], v[0]), normal = cross(e1, e2);
            const double facing = dot(direction, normal);
            if (facing == 0) continue;
            const Vec delta = sub(origin, v[0]), q = cross(delta, direction);
            const double t = -dot(delta, normal) / facing;
            const double u = -dot(e2, q) / facing, w = dot(e1, q) / facing;
            // Fixture rays stay inside the open distance interval and the triangle interior.
            if (!(t > ray.nearT && t < ray.farT && u > 0 && w > 0 && u + w < 1)) continue;
            const Opacity kind = opacity(geometries[g], instance.flags, ray.flags);
            if (kind != Opacity::culled) found.push_back({instance.id, g, t, kind});
        }
    }
    // Every fixture ray crosses one instance only, so geometry masks identify candidates.
    for (const auto& c : found) {
        if (c.instance != found.front().instance) return {};
        result.distance[c.geometry] = c.t;
        const bool committed = c.opacity == Opacity::opaque || (accept >> c.geometry & 1);
        if (committed) result.committable |= 1u << c.geometry;
        if (c.opacity == Opacity::nonOpaque) result.nonOpaque |= 1u << c.geometry;
        if (committed && (!result.closest.hit || c.t < result.closest.distance))
            result.closest = {true, c.instance, c.geometry, c.t};
    }
    // A non-opaque triangle in front of the closest hit must be offered before
    // traversal can finish, and so must a non-opaque triangle that is committed.
    for (const auto& c : found)
        if (c.opacity == Opacity::nonOpaque && (!result.closest.hit || c.t <= result.closest.distance))
            result.required |= 1u << c.geometry;
    // A first-hit search may end on any committable hit, so only a miss requires
    // every non-opaque candidate. A committed candidate is checked per result.
    if (ray.flags & acceptFirst) result.required = result.closest.hit ? 0 : result.nonOpaque;
    return result;
}

// Returns the first ray where the geometric reference and the hand-derived
// table disagree, or the ray count when they agree everywhere.
inline std::size_t first_reference_mismatch(const std::vector<Ray>& all) {
    const auto mesh = vertices();
    const auto geometries = geometryFlags();
    const auto scene = instances();
    constexpr std::size_t perFlags = std::size(acceptMasks), perDirection = std::size(rayFlags) * perFlags;
    for (std::size_t i = 0; i < all.size(); ++i) {
        const auto& instance = scene[i / (2 * perDirection)];
        const bool reversed = i % (2 * perDirection) >= perDirection;
        const std::size_t flagSet = i % perDirection / perFlags;
        const std::uint32_t accept = acceptMasks[i % perFlags];
        const char* letters = instance.opacity + 5 * flagSet;
        // Walk the stack in ray order; geometry g lies at distance g + 1 or 4 - g.
        Hit closest;
        std::uint32_t committable = 0, nonOpaque = 0;
        for (std::uint32_t step = 0; step < geometryCount; ++step) {
            const std::uint32_t g = reversed ? geometryCount - 1 - step : step;
            const bool commit = letters[g] == 'O' || (letters[g] == 'N' && (accept >> g & 1));
            if (letters[g] == 'N') nonOpaque |= 1u << g;
            if (commit) committable |= 1u << g;
            if (commit && !closest.hit) closest = {true, instance.id, g, static_cast<double>(step + 1)};
        }
        const Expected e = evaluate(mesh, geometries, scene, all[i]);
        if (e.closest.hit != closest.hit || e.closest.instance != closest.instance ||
            e.closest.geometry != closest.geometry || e.closest.distance != closest.distance ||
            e.committable != committable || e.nonOpaque != nonOpaque) return i;
    }
    return all.size();
}
}
