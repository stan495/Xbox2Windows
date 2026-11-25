#!/usr/bin/env python3
"""tools/emit_variants.py writes what one `emit` process per variant writes, and fails the same way.

The fixture script here is synthetic: its emit(image, output, control) keeps a per-process call counter
and raises for the control "broken". Running every variant through the driver must give the same files as
one process per variant, and a failing variant must make the driver exit non-zero.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tools" / "emit_variants.py"

FIXTURE = '''\
import sys
from pathlib import Path

CALLS = []


def emit(image, output, control="lifted"):
    CALLS.append(control)
    if control == "broken":
        raise AssertionError("control broken changed nothing")
    output.write_text(f"{image.name}:{control}:{len(CALLS) > 0}", encoding="ascii")
    print(f"control {control}")


if __name__ == "__main__":
    mode, first, second, *rest = sys.argv[1:]
    emit(Path(first), Path(second), *rest)
'''


def run(*args):
    return subprocess.run([sys.executable, "-B", *map(str, args)], capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as raw:
        work = Path(raw)
        script = work / "fixture_emit.py"
        script.write_text(FIXTURE, encoding="ascii")
        image = work / "image.dll"
        image.write_bytes(b"MZ")
        variants = ["lifted", "pc64", "double"]

        single = work / "single"
        batch = work / "batch"
        single.mkdir()
        batch.mkdir()
        for variant in variants:
            done = run(script, "emit", image, single / f"{variant}.cpp", variant)
            assert done.returncode == 0, done.stderr
        done = run(DRIVER, script, image, *[f"{v}={batch / (v + '.cpp')}" for v in variants])
        assert done.returncode == 0, done.stderr
        for variant in variants:
            a, b = (single / f"{variant}.cpp").read_bytes(), (batch / f"{variant}.cpp").read_bytes()
            assert a == b, f"{variant}: batch wrote {b!r}, one process per variant wrote {a!r}"

        one = work / "one"
        one.mkdir()
        done = run(DRIVER, script, image, f"lifted={one / 'lifted.cpp'}")
        assert done.returncode == 0, done.stderr
        assert (one / "lifted.cpp").read_bytes() == (single / "lifted.cpp").read_bytes()

        failing = work / "failing"
        failing.mkdir()
        done = run(DRIVER, script, image, f"lifted={failing / 'lifted.cpp'}", f"broken={failing / 'broken.cpp'}")
        assert done.returncode != 0, "a failing variant must fail the batch"
        assert "control broken changed nothing" in done.stderr, done.stderr

        for bad in ("no-separator", f"={failing / 'empty.cpp'}", "lifted="):
            done = run(DRIVER, script, image, bad)
            assert done.returncode != 0, f"malformed pair {bad!r} must be refused"
            assert "expected <variant>=<output>" in done.stderr, done.stderr

        done = run(DRIVER, script, image)
        assert done.returncode != 0, "missing variant pairs must be refused"
    print("emit_variants: batch output equals one process per variant; failures and bad pairs are refused")


if __name__ == "__main__":
    main()
