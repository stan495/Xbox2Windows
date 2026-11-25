"""Translate the packaged upscaler's device and context-slot initialization."""
import argparse
from pathlib import Path
import struct
from uuid import UUID

from iced_x86 import Mnemonic, Register
import pefile

from cpu_ranges import function_ranges
from ffx_dispatch_profile import dispatch_slices
from ffx_provider_loop import generate_loop
from ffx_recurrent import generate_recurrent
from ffx_version_query import INTEGER_HEADER, UPSCALER, checked_image, lower, region


def generate(image):
    start, end = 0x27f8, 0x2938
    methods = {8: "probe.retain(rcx)", 0x70: "probe.create(rcx, rdx, r8, r9)"}
    with checked_image(image, UPSCALER) as pe:
        if not any(lo <= start < end <= hi for lo, hi in function_ranges(pe, 0x2720)):
            raise ValueError("device calls outside backend callback")
        body = region(pe, start, end)
        calls = {}
        for ins in body:
            if ins.mnemonic != Mnemonic.CALL:
                continue
            if ins.memory_base != Register.RAX:
                raise ValueError(f"unexpected device call at {ins.ip:#x}")
            method = methods.get(ins.memory_displacement)
            if method is None:
                raise ValueError(
                    f"unexpected device call displacement {ins.memory_displacement:#x} at {ins.ip:#x}"
                )
            calls[ins.ip] = f"rax = {method};"
        guid = pe.get_data(0x482e0, 16)
        if UUID(bytes_le=guid) != UUID("8efb471d-616c-4f49-90f7-127bb763fa51"):
            raise ValueError("unexpected heap interface")
        code = lower(body, ["rdi = reinterpret_cast<word>(probe.device);",
                            "rbx = reinterpret_cast<word>(probe.scratch.data());"],
                     {0x482e0: "reinterpret_cast<word>(heap_iid)"}, calls, {end: "return;"})
        # R9 is formed by the provider and saved in R14 by the callback. The
        # first-use prefix through the fifth heap call executes in run_device_slice.
        slots = lower(region(pe, 0xcf94, 0xcf9b) + region(pe, 0x27a7, 0x27b0) +
                      region(pe, 0x2938, 0x2aa1),
                      ["rdi = reinterpret_cast<word>(&provider);",
                       "rbx = reinterpret_cast<word>(scratch); rsi = reinterpret_cast<word>(sizes);",
                       "rbp = 0x14;"], {}, {},
                      {0xcf9b: "r14 = r9; goto block_27a7;",
                       0x27b0: "goto block_2938;", 0x2aa1: "return static_cast<uint32_t>(rax);"})
        recurrent = generate_recurrent(pe) + generate_loop(pe) + dispatch_slices(pe)
    return INTEGER_HEADER + '\n#include "ffx_device_init.h"\n' + (
        "static const unsigned char heap_iid[] = {" + ", ".join(map(str, guid)) + "};\n"
        "void run_device_slice(FfxDeviceProbe& probe) {\n" + code + "\n}\n"
        "uint32_t run_backend_slots(uint8_t* scratch, FfxSlotOutput& provider, const uint32_t* sizes) {\n"
        + slots + "\n}\n" + recurrent)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != ".cpp" or args.output.resolve() == args.image.resolve():
            raise ValueError("choose a distinct .cpp output")
        args.output.write_text(generate(args.image), encoding="ascii")
    except (OSError, ValueError, pefile.PEFormatError, struct.error) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
