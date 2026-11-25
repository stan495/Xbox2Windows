"""Test source-built root signatures and optionally measure layouts in a local call inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile

import pefile
from call_evidence import NAMES
from probe_roots import PROJECT, parts


def measure(command, blob):
    result = subprocess.run(command, input=blob, stdout=subprocess.PIPE, check=True, timeout=30)
    return [json.loads(line) for line in result.stdout.splitlines()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path, nargs="?", help="Omit to run just the two source-built controls")
    parser.add_argument("--warp-only", action="store_true")
    parser.add_argument("--bin", type=Path, default=PROJECT / "out/build/dev")
    args = parser.parse_args(argv)
    command = [args.bin / "root_device_probe.exe"]
    backends = ["warp"]
    if args.warp_only:
        command.append("--warp-only")
    else:
        backends.append("hardware")
    result = {"windows": sys.getwindowsversion().build, "controls": {}, "images": {}}
    try:
        for name in ("compute_root", "compute_tables"):
            rows = measure(command, (args.bin / (name + ".cso")).read_bytes())
            if (len(rows) != 1 or not isinstance(rows[0], dict)
                    or rows[0].get("cleared") != 0 or rows[0].get("remaining_flags") != 0
                    or any(rows[0].get(key) != "0x00000000" for key in ["serialize", *backends])):
                raise ValueError(f"{name} control failed: {rows}")
            result["controls"][name] = rows
        if args.build is None:
            return 0

        dest = PROJECT / "out/calls" / ("root-devices-warp.json" if args.warp_only else "root-devices.json")
        if dest.resolve().is_relative_to(args.build.resolve()):
            parser.error("Choose an input directory separate from the report directory")
        inventory = json.loads((dest.parent / "six-apis.json").read_bytes())
        if not isinstance(inventory, dict):
            raise ValueError("Invalid six-apis.json inventory")
        for name in NAMES:
            try:
                entry = inventory[name]
                containers = entry["containers"]
                if not containers:
                    continue
                expected_sha256 = entry["sha256"]
                container_specs = [(int(item["rva"], 16), item["bytes"], item["rva"]) for item in containers]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"Invalid six-apis.json entry for {name}: {exc}") from exc
            image = (args.build / name).read_bytes()
            if hashlib.sha256(image).hexdigest() != expected_sha256:
                raise ValueError(f"Regenerate six-apis.json for {name}")
            layouts = {}
            with pefile.PE(data=image, fast_load=True) as pe:
                for rva, size, rva_text in container_specs:
                    blob = pe.get_data(rva, size)
                    container_parts = parts(blob)
                    if b"RTS0" not in container_parts:
                        raise ValueError(f"Container at {rva_text} in {name} is missing RTS0")
                    key = hashlib.sha256(container_parts[b"RTS0"]).hexdigest()
                    if key not in layouts:
                        layouts[key] = {"rvas": [], "outcomes": measure(command, blob)}
                    layouts[key]["rvas"].append(rva_text)
            result["images"][name] = {"sha256": expected_sha256, "layouts": layouts}
            print(f"{name}: {len(layouts)} distinct layouts")
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", dir=dest.parent, delete=False, encoding="utf-8") as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, indent=2)
                stream.write("\n")
            temporary.replace(dest)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(dest)
    except (OSError, ValueError, struct.error, pefile.PEFormatError,
            subprocess.SubprocessError) as exc:
        parser.exit(1, f"probe_root_devices: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
