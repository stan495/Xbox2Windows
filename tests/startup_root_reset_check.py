"""Check root table and constant lifetime through translated rendering (list slots 34, 36 and 40)."""
from pathlib import Path
import sys
import tempfile

from startup_address_srv_check import draw_texture

HLSL = (
    "Texture1D<float4> tex : register(t0);\n"
    "cbuffer Entry : register(b0) { uint4 entry; };\n"
    "float4 main(float4 p : SV_Position) : SV_Target {\n"
    "  float4 value = tex.Load(int2(uint(p.x) % 4, 0));\n"
    "  return float4(value.r, value.a, float(entry.w) / 255.0, 1);\n"
    "}\n"
)
CLEAR = bytes((0, 0, 255, 255))
TEXELS = bytes(b for x in range(4) for b in (10 + 20 * x, 200 - 30 * x, 173, 255))
NO_BINDINGS = bytes((0, 0, 0, 255)) * 4


def run(directory, consumer, backend, mode):
    folder = directory / mode
    folder.mkdir()
    log = folder / "trap.log"
    pixels = draw_texture(folder, consumer, backend, HLSL, {"X2W_TEST_ROOT_RESET": mode, "X2W_TRAP_LOG": str(log)},
                          binding="srv 0 tex1d table 0 0\nudsrc 3 rootconst 1 0")
    lines = [line for line in log.read_text(errors="replace").splitlines() if "root constants" in line] if log.exists() else []
    return pixels, lines


def check(directory, consumer, backend):
    # The left half of the 8x4 target is drawn, the right half keeps the clear colour.
    cases = (
        ("bound", TEXELS, 0),
        ("same_root", TEXELS, 0),
        ("switch_root", NO_BINDINGS, 0),
        ("out_of_range", TEXELS, 4),
    )
    for mode, drawn, logged in cases:
        pixels, lines = run(directory, consumer, backend, mode)
        if pixels is None:
            return 77
        expected = (drawn + CLEAR * 4) * 4
        if pixels != expected:
            raise AssertionError(f"{mode}: actual {pixels.hex()} expected {expected.hex()}")
        if len(lines) != logged:
            raise AssertionError(f"{mode}: {len(lines)} root constants log lines, expected {logged}:\n" + "\n".join(lines))
        if mode == "out_of_range":
            kinds = [("dropped" if "dropped" in line else "truncated" if "truncated" in line else "?") for line in lines]
            if kinds != ["dropped", "dropped", "dropped", "truncated"]:
                raise AssertionError(f"{mode}: log lines {kinds}:\n" + "\n".join(lines))
        print(f"{backend} {mode}: 32 pixels exact, {logged} log lines")
    return 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        sys.exit(check(Path(temporary), *sys.argv[1:]))
