"""Title root signatures that index descriptor heaps directly are not set on the desktop list (command-list slot 34).

The fixture is startup_root_cbv's translated draw (a pixel shader that reads four floats through root parameter 0),
run with the desktop debug layer on and the root signature created with D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_
DIRECTLY_INDEXED (0x400), SAMPLER_HEAP_DIRECTLY_INDEXED (0x800), both, or neither. For each flagged case the same
root signature is first set on a plain desktop list with no descriptor heaps; that fail-first control must produce
debug layer errors (ID 1419 for the sampler heap), or the run is a capability skip (77). Through the stand-in, no
error with a control ID or ID 1419 may appear, and the translated draw must still show the colour the title bound.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from startup_address_srv_check import prepare_draw_cache
from startup_root_cbv_check import CLEAR, HLSL

YELLOW = bytes((255, 255, 0, 255))
CASES = (
    # root CBV mode, root signature flags, colour of the drawn half
    ("bound", 0x0, YELLOW),        # unflagged: set on the desktop list as before
    ("bound", 0x400, YELLOW),      # view heap indexed
    ("bound", 0x800, YELLOW),      # sampler heap indexed
    ("bound", 0xC00, YELLOW),      # both
    ("null", 0xC00, bytes(4)),     # a cleared root CBV under a flagged root
    ("switch_root", 0xC00, bytes(4)),  # switching between two flagged roots clears the stored address
)


def run(directory, consumer, backend, mode, flags):
    folder = directory / f"{mode}-{flags:x}"
    folder.mkdir()
    modes = {"X2W_TEST_ROOT_CBV": mode, "X2W_TEST_DEBUG_LAYER": "1", "X2W_TRAP_LOG": str(folder / "trap.log")}
    if flags:
        modes["X2W_TEST_ROOT_FLAGS"] = hex(flags)
    try:
        env, args, output = prepare_draw_cache(folder, consumer, HLSL, modes, "srv 0 buf root 0")
    except subprocess.CalledProcessError as error:
        if error.returncode == 77:  # no debug layer, or the root signature flags are rejected
            return None, ""
        raise
    result = subprocess.run([*args, backend, output], env=env, capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode == 77:
        return None, result.stdout
    if result.returncode:
        raise RuntimeError(f"consumer failed ({result.returncode})\n{result.stderr[-5000:]}")
    return output.read_bytes(), result.stdout


def check(directory, consumer, backend):
    for mode, flags, drawn in CASES:
        pixels, stdout = run(directory, consumer, backend, mode, flags)
        if pixels is None:
            return 77
        expected = (drawn * 4 + CLEAR * 4) * 4
        if pixels != expected:
            raise AssertionError(f"{mode} 0x{flags:x}: actual {pixels.hex()} expected {expected.hex()}")
        # Fail-first control: one ID 1419 message per flag (view heap, sampler heap) on a plain desktop list.
        control = re.search(r"^control: (\d+) messages, ids([ \d]*)$", stdout, re.M)
        if flags and (not control or int(control.group(1)) == 0):
            raise AssertionError(f"{mode} 0x{flags:x}: no fail-first control result")
        if flags and (control.group(2).split() != ["1419"] or int(control.group(1)) != bin(flags).count("1")):
            raise AssertionError(f"{mode} 0x{flags:x}: the control did not produce one ID 1419 per flag: {control.group(0)}")
        stand_in = re.search(r"^stand-in list: (\d+) messages with a control id or id 1419, (\d+) other errors$", stdout, re.M)
        if not stand_in:
            raise AssertionError(f"{mode} 0x{flags:x}: no stand-in message count")
        if int(stand_in.group(1)):
            raise AssertionError(f"{mode} 0x{flags:x}: {stand_in.group(0)}")
        log = (directory / f"{mode}-{flags:x}" / "trap.log").read_text(errors="replace")
        deferred = re.findall(r"root signature \S+ with heap flags (0x[0-9a-f]+) not set on the desktop list", log)
        # Slot 34 calls: one, or three for switch_root (root, alternate, root); the stand-in logs the first three.
        expected_deferred = [hex(flags)] * (3 if mode == "switch_root" else 1) if flags else []
        if deferred != expected_deferred:
            raise AssertionError(f"{mode} 0x{flags:x}: deferred root lines {deferred}, expected {expected_deferred}")
        ids = control.group(2).strip() if control else "none"
        print(f"{backend} {mode} flags 0x{flags:x}: 32 pixels exact; control ids [{ids}]; "
              f"stand-in 0 matching messages, {stand_in.group(2)} other errors")
    return 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        sys.exit(check(Path(temporary), *sys.argv[1:]))
