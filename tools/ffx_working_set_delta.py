"""Translate the counter updates following two upscaler working-set queries."""
import argparse
from pathlib import Path

import pefile

from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, UPSCALER, checked_image, lower, region


def generate(image: Path, package: bool) -> str:
    source = [INTEGER_HEADER]
    with (checked_image(image, UPSCALER) if package else pefile.PE(str(image), fast_load=True)) as pe:
        for name, start, end, sample_offset in (("create_delta", 0x74d8, 0x74f8, 0x78),
                                                ("release_delta", 0x75b7, 0x75d7, 0x28)):
            if package:
                initial = ["rsi = load<word>(input);", "rax = load<uint32_t>(input + 16);",
                           f"store<word>(rsp + {sample_offset}, load<word>(input + 8));",
                           "rbx = counters - 0x40;"]
                if name == "create_delta":
                    initial.append("store<uint32_t>(rsp + 0x30, load<uint32_t>(input + 20));")
                else:
                    initial.extend(["r14 = rsp;", "store<uint8_t>(r14 + 0x18, load<uint8_t>(input + 20));"])
                body = lower(region(pe, start, end), initial, {}, {}, {end: "return 0;"})
            else:
                (bounds,) = function_ranges(pe, export_rva(pe, name))
                body = lower(region(pe, *bounds), ["rcx = input; rdx = counters;"], {}, {}, {})
            source.append(f'extern "C" __declspec(dllexport) word {name}(word input, word counters) {{\n'
                          + body + "\n}\n")
    return "\n".join(source)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--package", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != ".cpp" or args.output.resolve() == args.image.resolve():
            parser.error("choose a distinct .cpp output")
        args.output.write_text(generate(args.image, args.package), encoding="ascii")
    except (OSError, ValueError, pefile.PEFormatError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
