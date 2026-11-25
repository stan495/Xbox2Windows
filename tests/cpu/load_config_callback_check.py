"""Start the load-config callback EXEs (#903) under the Windows loader and pe_map; compare the results.

tests/cpu/load_config_callback.cpp, compiled with /guard:cf, calls the two entries of a read-only callback
table through the load config's dispatch slot (__guard_dispatch_icall_fptr). The second entry is null and
is skipped. The callback hands emit_trace (cpu_exe_io.dll) three words: a | b << 32, c | calls << 32 and
d - __ImageBase, which the stderr and stdout bytes carry. The exit code is the callback's result, ORed with
0x10000 if the dispatch slot no longer holds the image's own `jmp rax`, and 0x20000 for the check slot.

The record, other and invalid images are linked without /guard:cf, and cfg_record and cfg_invalid are
the same objects linked with it. other binds a different callback (the negative control); the two invalid
images bind a target inside a function, which the /guard:cf function table leaves out.

Checked from each image's own PE: the guard slots and the callback table are in a section without write
access and carry DIR64 relocations, the null entry has none, the load-config fields, and the two
`call [dispatch slot]` instructions of the caller. In pe_map's mapping, before the entry runs: the slots
hold the relocated in-image targets on read-only pages.

Windows replaces both slots of a /guard:cf image and ends a call to the invalid target before it writes
output, when CFG is enforced for the process. A child inherits the host's CFG setting, and with system CFG
off the loader keeps the image's own slots. The two /guard:cf images are therefore started with an explicit
per-process policy: CFG always on for the assertions above, and CFG always off as a negative control that
must give the result of the same objects linked without /guard:cf. Each child's effective
EnableControlFlowGuard bit is checked against the requested policy. pe_map currently does not implement CFG:
it keeps the image's own slots, so the valid callback runs without the slot bits and the invalid target
runs. The unchecked invalid result is recorded as an observation. The test also accepts an explicit
unsupported-CFG diagnostic before dispatch. An unrelated load failure or a missing output record does not
establish CFG behavior.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

import pefile

from exe_process import launch, thread_ids
from native_map_check import Mapper, STATUS

IO = "cpu_exe_io.dll"
VARIANTS = ("record", "other", "invalid", "cfg_record", "cfg_invalid")
GUARD_CF = 0x4000  # IMAGE_DLLCHARACTERISTICS_GUARD_CF
CF_INSTRUMENTED, CF_TABLE = 0x100, 0x400  # IMAGE_GUARD_CF_INSTRUMENTED, IMAGE_GUARD_CF_FUNCTION_TABLE_PRESENT
SLOT_BITS = 0x30000
PAGE_READONLY = 0x02
# PROCESS_CREATION_MITIGATION_POLICY_CONTROL_FLOW_GUARD_ALWAYS_ON / _ALWAYS_OFF
CFG_POLICY = {"on": 1 << 40, "off": 2 << 40}


class StartupInfoEx(ctypes.Structure):
    _fields_ = ([("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR),
                 ("title", wintypes.LPWSTR)]
                + [(field, wintypes.DWORD) for field in ("x", "y", "x_size", "y_size", "x_chars", "y_chars",
                                                         "fill", "flags")]
                + [("show", wintypes.WORD), ("reserved2_size", wintypes.WORD), ("reserved2", ctypes.c_void_p),
                   ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
                   ("attributes", ctypes.c_void_p)])


class ProcessInformation(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE), ("pid", wintypes.DWORD),
                ("tid", wintypes.DWORD)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


def launch_cfg(image, policy, timeout=15):
    """Start image under the Windows loader with an explicit per-process CFG policy ("on" or "off").

    A /guard:cf image otherwise inherits the host's CFG setting, and with system CFG off the loader keeps the
    image's own slots. The child's effective EnableControlFlowGuard bit is read from its handle after it ends.
    stdin is NUL; stdout and stderr go to files beside the image. The child inherits only these three handles.
    The four launches together stay well inside the test's CTest timeout; a launch that does not end records
    the child's live threads before it is terminated.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                         ctypes.POINTER(ctypes.c_size_t)]
    kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                 ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                      wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.c_void_p, ctypes.POINTER(ProcessInformation)]
    kernel.GetProcessMitigationPolicy.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    image = Path(image)
    paths = {stream: image.with_name(f"{image.stem}.{policy}.{stream}") for stream in ("stdout", "stderr")}
    inherit = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
    opens = [("NUL", 0x80000000, 3, 3)] + [(str(paths[stream]), 0x40000000, 1, 2) for stream in ("stdout", "stderr")]
    handles = []
    for name, access, share, disposition in opens:  # GENERIC_READ OPEN_EXISTING, GENERIC_WRITE CREATE_ALWAYS
        handle = kernel.CreateFileW(name, access, share, ctypes.byref(inherit), disposition, 0x80, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            error = ctypes.WinError(ctypes.get_last_error())  # read before any other call can replace it
            for opened in handles:
                kernel.CloseHandle(opened)
            raise OSError(f"opening {name} for the child: {error}")
        handles.append(handle)
    size = ctypes.c_size_t()
    kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    value = ctypes.c_uint64(CFG_POLICY[policy])
    inherited = (wintypes.HANDLE * len(handles))(*handles)
    info, process = StartupInfoEx(), ProcessInformation()
    info.cb, info.flags = ctypes.sizeof(info), 0x100  # STARTF_USESTDHANDLES
    info.stdin, info.stdout, info.stderr = handles
    try:
        if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x20007, ctypes.byref(value), 8, None, None):
                raise ctypes.WinError(ctypes.get_last_error())  # PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, inherited, ctypes.sizeof(inherited),
                                                    None, None):
                raise ctypes.WinError(ctypes.get_last_error())  # PROC_THREAD_ATTRIBUTE_HANDLE_LIST
            info.attributes = ctypes.addressof(attributes)
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW; the parent's error mode is inherited.
            if not kernel.CreateProcessW(str(image), None, None, None, True, 0x00080000 | 0x08000000, None,
                                         str(image.parent), ctypes.byref(info), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.DeleteProcThreadAttributeList(attributes)
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
    live_threads = None
    try:
        signaled = kernel.WaitForSingleObject(process.process, timeout * 1000) == 0
        if not signaled:
            live_threads = thread_ids(process.pid)
            kernel.TerminateProcess(process.process, 1)
            kernel.WaitForSingleObject(process.process, 5000)
        code, flags = wintypes.DWORD(), wintypes.DWORD()
        kernel.GetExitCodeProcess(process.process, ctypes.byref(code))
        if not kernel.GetProcessMitigationPolicy(process.process, 7, ctypes.byref(flags), 4):
            raise ctypes.WinError(ctypes.get_last_error())  # ProcessControlFlowGuardPolicy
    finally:
        kernel.CloseHandle(process.thread)
        kernel.CloseHandle(process.process)
    return {"process_signaled": signaled, "code": code.value, "cfg": bool(flags.value & 1), "pid": process.pid,
            "live_threads": live_threads, **{stream: {"bytes": path.read_bytes()} for stream, path in paths.items()}}


def words(a, b, c, calls, d):
    return a | b << 32, c | calls << 32, d


def layout(image):
    """Static facts from the image's own PE."""
    with pefile.PE(str(image)) as pe:
        base = pe.OPTIONAL_HEADER.ImageBase
        config = pe.DIRECTORY_ENTRY_LOAD_CONFIG.struct
        dir64 = {e.rva for block in pe.DIRECTORY_ENTRY_BASERELOC for e in block.entries
                 if e.type == pefile.RELOCATION_TYPE["IMAGE_REL_BASED_DIR64"]}
        check, dispatch = (config.GuardCFCheckFunctionPointer - base, config.GuardCFDispatchFunctionPointer - base)
        table = check - 16  # callback_table precedes the two slots in .const
        slots = {"table[0]": table, "table[1]": table + 8, "check": check, "dispatch": dispatch}
        facts = {"base": base, "slots": slots, "size": config.Size, "flags": config.GuardFlags,
                 "guard_cf": bool(pe.OPTIONAL_HEADER.DllCharacteristics & GUARD_CF),
                 "values": {name: pe.get_qword_at_rva(rva) for name, rva in slots.items()},
                 "relocated": {name: rva in dir64 for name, rva in slots.items()}}
        sections = {}
        for name, rva in slots.items():
            section = pe.get_section_by_rva(rva)
            sections[name] = (section.Name.rstrip(b"\0").decode(), section.Characteristics)
        facts["sections"] = sections
        text = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".text")
        code, start = text.get_data(), text.VirtualAddress
        facts["dispatch_calls"] = sum(
            1 for i in range(len(code) - 5) if code[i:i + 2] == b"\xff\x15"
            and start + i + 6 + struct.unpack_from("<i", code, i + 2)[0] == dispatch)
        fids = []
        if config.GuardCFFunctionCount:
            stride = 4 + (config.GuardFlags >> 28)
            data = pe.get_data(config.GuardCFFunctionTable - base, config.GuardCFFunctionCount * stride)
            fids = [struct.unpack_from("<I", data, i * stride)[0] for i in range(config.GuardCFFunctionCount)]
        facts["fids"] = fids
        facts["data"] = {s.Name.rstrip(b"\0").decode(): (s.VirtualAddress, s.Misc_VirtualSize) for s in pe.sections}
        return facts


def check_layout(name, facts):
    if facts["size"] != 0x138:
        raise AssertionError(f"{name}: load config size {facts['size']:#x}")
    for slot, (section, characteristics) in facts["sections"].items():
        if section != ".rdata" or characteristics & pefile.SECTION_CHARACTERISTICS["IMAGE_SCN_MEM_WRITE"]:
            raise AssertionError(f"{name}: {slot} is in {section} ({characteristics:#x}), expected read-only .rdata")
    expected = {"table[0]": True, "table[1]": False, "check": True, "dispatch": True}
    if facts["relocated"] != expected:
        raise AssertionError(f"{name}: DIR64 relocations {facts['relocated']}, expected {expected}")
    if facts["values"]["table[1]"] != 0:
        raise AssertionError(f"{name}: the absent callback entry is not null")
    text_rva, text_size = facts["data"][".text"]
    for slot in ("table[0]", "check", "dispatch"):
        rva = facts["values"][slot] - facts["base"]
        if not text_rva <= rva < text_rva + text_size:
            raise AssertionError(f"{name}: {slot} does not point into .text")
    if facts["dispatch_calls"] != 2:
        raise AssertionError(f"{name}: {facts['dispatch_calls']} calls through the dispatch slot, expected 2")
    cfg = name.startswith("cfg_")
    if facts["guard_cf"] != cfg:
        raise AssertionError(f"{name}: GUARD_CF characteristic {facts['guard_cf']}")
    target = facts["values"]["table[0]"] - facts["base"]
    if cfg:
        if facts["flags"] & (CF_INSTRUMENTED | CF_TABLE) != CF_INSTRUMENTED | CF_TABLE:
            raise AssertionError(f"{name}: guard flags {facts['flags']:#x}")
        if (target in facts["fids"]) != (name == "cfg_record"):
            raise AssertionError(f"{name}: callback target {target:#x} in function table: {target in facts['fids']}")
    elif facts["flags"] & CF_TABLE or facts["fids"]:  # 0x100 with link 14.50; only the table bit is checked
        raise AssertionError(f"{name}: guard flags {facts['flags']:#x} with {len(facts['fids'])} table entries")
    print(f"{name}: slots in read-only .rdata with DIR64 relocations (null entry has none), "
          f"guard flags {facts['flags']:#x}, GUARD_CF {facts['guard_cf']}, {len(facts['fids'])} function table "
          f"entries, 2 calls through the dispatch slot")


def child(mapper_path, image, io, report):
    """Map the EXE in this process, record the slots, start it, and write the observation to report."""
    mapper = Mapper(mapper_path)
    run_entry = ctypes.CDLL(str(mapper_path)).pe_map_run
    run_entry.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    run_entry.restype = ctypes.c_int
    base, status, detail, requests = mapper(Path(image), {IO: ctypes.CDLL(str(io))})
    result = {"status": status, "detail": detail, "requests": requests}
    if base:
        facts = layout(image)
        query = ctypes.WinDLL("kernel32").VirtualQuery
        query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]

        class Info(ctypes.Structure):
            _fields_ = [("base", ctypes.c_void_p), ("allocation", ctypes.c_void_p), ("allocation_protect",
                        wintypes.DWORD), ("partition", wintypes.WORD), ("size", ctypes.c_size_t),
                        ("state", wintypes.DWORD), ("protect", wintypes.DWORD), ("type", wintypes.DWORD)]
        slots = {}
        for name, rva in facts["slots"].items():
            info = Info()
            query(base + rva, ctypes.byref(info), ctypes.sizeof(info))
            value = ctypes.c_uint64.from_address(base + rva).value
            stored = facts["values"][name]
            slots[name] = {"protect": info.protect,
                           "relocated": value == (stored - facts["base"] + base if stored else 0)}
        result["slots"] = slots
        code = ctypes.c_uint32(0xdeadbeef)
        result["started"] = run_entry(base, ctypes.byref(code))
        result["exit"] = code.value
        mapper.release(base)
    Path(report).write_text(json.dumps(result))


def mapped(mapper_path, image, io, folder):
    report = Path(folder) / (Path(image).stem + ".json")
    run = subprocess.run([sys.executable, "-B", __file__, "--child", str(mapper_path), str(image), str(io),
                          str(report)], capture_output=True, timeout=60)
    if run.returncode:
        raise AssertionError(f"mapped {Path(image).name}: child exited {run.returncode & 0xffffffff:#x}: "
                             f"{run.stderr[-400:]!r}")
    return json.loads(report.read_text()), run.stdout, run.stderr


def trace(stderr, stdout):
    if len(stderr) != 16 or len(stdout) != 8:
        return None
    return struct.unpack("<QQ", stderr) + struct.unpack("<Q", stdout)


def show(row):
    code, values = row
    return f"exit {code:#x} trace " + ("none" if values is None else " ".join(f"{v:#x}" for v in values))


def main(mapper_path, io, *images):
    images = dict(zip(VARIANTS, images))
    # cfg_invalid fails fast on purpose: no error dialog or debugger prompt in the inherited error mode.
    kernel = ctypes.WinDLL("kernel32")
    kernel.SetErrorMode(kernel.GetErrorMode() | 0x0001 | 0x0002)  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
    with TemporaryDirectory() as temp:
        shutil.copyfile(io, Path(temp) / IO)
        staged_io = Path(temp) / IO
        observed = {}
        for name, image in images.items():
            staged = Path(temp) / Path(image).name
            shutil.copyfile(image, staged)
            facts = layout(staged)
            check_layout(name, facts)
            marker = facts["data"][".data"][0] + 8  # marker follows __security_cookie
            expected = {
                "record": (0x55, words(0x11, 0x22, 0x33, 1, marker)),
                "other": (0x99, words(0x22, 0x11, 0x33, 1, marker)),
                "invalid": (0x77, words(0, 0, 0, 0, 0)),
            }
            if name.startswith("cfg_"):
                row, control = launch_cfg(staged, "on"), launch_cfg(staged, "off")
                for policy, launched in (("on", row), ("off", control)):
                    if not launched["process_signaled"]:
                        raise AssertionError(f"{name}: Windows launch with CFG {policy} did not end: {launched}")
                    if launched["cfg"] != (policy == "on"):
                        raise AssertionError(f"{name}: requested CFG {policy}, but the child reports "
                                             f"EnableControlFlowGuard={int(launched['cfg'])}")
                uncontrolled = (control["code"], trace(control["stderr"]["bytes"], control["stdout"]["bytes"]))
            else:
                row = launch([staged], b"", timeout=30)
                if not row["process_signaled"]:
                    raise AssertionError(f"{name}: Windows launch did not end: {row}")
            native = (row["code"], trace(row["stderr"]["bytes"], row["stdout"]["bytes"]))
            result, out, err = mapped(mapper_path, staged, staged_io, temp)
            started = result["status"] == STATUS["ok"] and result.get("started") == 1
            if not started and name != "cfg_invalid":
                raise AssertionError(f"mapped {name}: {result}")
            if started:
                for slot, state in result["slots"].items():
                    if state["protect"] != PAGE_READONLY or not state["relocated"]:
                        raise AssertionError(f"mapped {name}: {slot} {state}")
            mapped_row = (result["exit"], trace(err, out)) if started else None
            observed[name] = native, mapped_row

            base = name.removeprefix("cfg_")
            note, control_note = "mapped slots read-only and relocated", ""
            if name.startswith("cfg_"):
                # Negative control: without CFG the loader keeps the image's own slots, so the /guard:cf
                # image behaves like the same objects linked without /guard:cf.
                if uncontrolled != expected[base]:
                    raise AssertionError(f"{name}: Windows with CFG off {uncontrolled}, expected the {base} "
                                         f"result {expected[base]}")
                control_note = f"; Windows with CFG off {show(uncontrolled)} (same as {base})"
            if name in expected:
                if native != expected[name] or mapped_row != expected[name]:
                    raise AssertionError(f"{name}: Windows {native}, pe_map {mapped_row}, expected {expected[name]}")
            elif name == "cfg_record":
                code, words_ = expected[base]
                if native != (code | SLOT_BITS, words_):
                    raise AssertionError(f"{name}: Windows {native}, expected both slots replaced and {code:#x}")
                if mapped_row != (code, words_):
                    raise AssertionError(f"{name}: pe_map {mapped_row}, expected its own slots kept and {code:#x}")
            else:  # cfg_invalid
                if native[0] == 0 or native[1] is not None or row["stdout"]["bytes"] or row["stderr"]["bytes"]:
                    raise AssertionError(f"{name}: Windows {native}, expected the process to end before writing output")
                if (not started and result["status"] == STATUS["unsupported"]
                        and "cfg" in result["detail"].casefold() and not out and not err):
                    note = f"unsupported before dispatch ({result['detail']})"
                elif mapped_row == expected[base]:
                    note = "unchecked target (current observation); mapped slots read-only and relocated"
                else:
                    raise AssertionError(f"{name}: pe_map {mapped_row}, status {result['status']}, "
                                         f"expected the current observation or an explicit CFG refusal: {result}")
            shown = show(mapped_row) if mapped_row is not None else "not started"
            print(f"{name}: Windows {show(native)}; pe_map {shown}; {note}{control_note}")

        if observed["other"][0] == observed["record"][0]:
            raise AssertionError("the control binding was not distinguished")
    print("load-config dispatch callback: PASS (valid callback and layout asserted; pe_map CFG boundary recorded)")


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(*sys.argv[2:6])
    else:
        main(*map(Path, sys.argv[1:]))
