#!/usr/bin/env python3
"""Run several `emit` variants of one CPU fixture check script in a single Python process.

usage: emit_variants.py <script.py> <image> <variant>=<output> [<variant>=<output> ...]

Each pair does what `python <script.py> emit <image> <output> <variant>` does, in the order given, so the
interpreter, the lifter modules and the PE reader load once instead of once per variant. The script is
loaded as a module (its `__main__` block does not run) and its `emit(image, output, control)` is called.
"""
import importlib.util
import sys
from pathlib import Path


def load(script):
    path = Path(script).resolve()
    sys.path.insert(0, str(path.parent))  # as when the script itself is the program
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    script, image, *pairs = argv
    jobs = []
    for pair in pairs:
        variant, separator, output = pair.partition("=")
        if not separator or not variant or not output:
            raise SystemExit(f"expected <variant>=<output>, got {pair!r}")
        jobs.append((variant, Path(output)))
    emit = load(script).emit
    image_path = Path(image)
    for variant, output in jobs:
        emit(image_path, output, variant)


if __name__ == "__main__":
    main(sys.argv[1:])
