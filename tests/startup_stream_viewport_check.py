"""A translated draw covers the render target when the title set no viewport or scissor, and never replaces one it set.

Three cases on an 8x4 RGBA8 target cleared to blue, drawn with a red pixel shader through a slot-86 wrapper:
  none:  no RSSetViewports and no RSSetScissorRects (list slots 21 and 22) -> every pixel is red.
  rect:  a viewport and scissor of 4x2 pixels at (2, 1) -> red inside the rectangle, blue outside.
  grow:  no viewport or scissor; one draw on the 8x4 target, then OMSetRenderTargets to a 16x8 target on the same
         list and a second draw -> the 16x8 target is entirely red (the default follows the new target).
  reset: the rectangle of 'rect' on one list, then the same list object Reset with no viewport or scissor
         -> every pixel red, the rectangle is not inherited.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from startup_stream_check import gfx2hlsl
from startup_stream_draw_check import pixel, vertex

WIDTH, HEIGHT = 8, 4
RED, BLUE = bytes((255, 0, 0, 255)), bytes((0, 0, 255, 255))


def expected_pixels(inside, scale=1):
    """RGBA bytes of the target; inside(x, y) says whether the pixel belongs to the draw."""
    return b"".join(RED if inside(x, y) else BLUE for y in range(HEIGHT * scale) for x in range(WIDTH * scale))


CASES = {
    "none": lambda x, y: True,
    "rect": lambda x, y: 2 <= x < 6 and 1 <= y < 3,
    "reset": lambda x, y: True,
    "grow": lambda x, y: True,
}
SCALE = {"grow": 2}


def first_difference(actual, expected, width=WIDTH):
    for index in range(0, len(expected), 4):
        if actual[index:index + 4] != expected[index:index + 4]:
            return index // 4 % width, index // 4 // width, actual[index:index + 4].hex(), expected[index:index + 4].hex()
    return None


def check(directory, consumer, backend):
    cache = directory / "cache"
    cache.mkdir()
    vs, ps, root, output = (directory / name for name in ("vs", "ps", "root", "pixels"))
    vs.write_bytes(vertex())
    ps.write_bytes(pixel(False))
    args = [consumer, vs, ps, root]
    env = dict(os.environ, X2W_D3D12_PERMISSIVE="1", X2W_D3D12_LIST_NOOP="1", X2W_D3D12_RENDER="1",
               X2W_SHADER_CACHE=str(cache))
    env.setdefault("X2W_D3D12_DEBUG", "0")
    subprocess.run(args, env=env, check=True)
    subprocess.run([sys.executable, "-B", gfx2hlsl.__file__, "--pending", cache], env=env, check=True)
    for name, inside in CASES.items():
        result = subprocess.run(args + [backend, output], env=dict(env, X2W_TEST_VIEWPORT=name),
                                capture_output=True, text=True)
        print(result.stdout, end="")
        if backend == "hardware" and result.returncode == 77:
            print("No D3D12 feature-level 12 hardware adapter")
            return 77
        if result.returncode or "-> drawn;" not in result.stderr:
            raise RuntimeError(f"{name} draw failed on {backend}: {result.returncode}\n{result.stderr}")
        actual = output.read_bytes()
        scale = SCALE.get(name, 1)
        expected = expected_pixels(inside, scale)
        difference = first_difference(actual, expected, WIDTH * scale)
        if difference:
            x, y, got, want = difference
            raise AssertionError(f"{name}: pixel ({x}, {y}) is {got}, expected {want}")
        print(f"{backend} viewport {name}: all {WIDTH * HEIGHT * scale * scale} pixels exact")
    return 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        sys.exit(check(Path(temp), *sys.argv[1:]))
