"""Live debug console for a run: follows the run's logs and prints what the title is doing.

usage: live_view.py <rundir> [<package root>]      (started in its own console window by debug_launch.py)

It only reads the log files the debugger and the stand-in DLLs write; it does not touch the game.
File opens are shown for paths on the G: mount and under the package root, if one is given.
"""
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

rundir = sys.argv[1]
# The package root the run uses as its working directory (run.ps1 -DataDir), next to the G:\ mount.
roots = ["G:\\"] + [os.path.join(os.path.abspath(sys.argv[2]), "")] if len(sys.argv) > 2 else ["G:\\"]
game_file = re.compile(r"Nt(?:CreateFile|OpenFile) (?:\\\?\?\\)?((?i:" + "|".join(map(re.escape, roots)) + r").+)(?= from (?:[^+]*\+)?0x[0-9a-fA-F]+ )")
os.system("title Xbox2Windows debug log - " + os.path.basename(os.path.abspath(rundir)))
start = time.time()
counts = {"modules": 0, "files": 0, "refused": 0, "chunks": 0, "gpu calls first seen": 0}
last_status = 0.0
state = "?"
frames = "0"


def say(tag, text):
    print(f"{time.time() - start:7.1f}s  {tag:<9} {text}", flush=True)


def on_event(line):
    global state
    m = re.match(r"\s*[\d.]+ load (\S+) at", line)
    if m:
        counts["modules"] += 1
        say("module", m.group(1))
        return
    if " debug-output " in line:
        say("runtime", line.split(" debug-output ", 1)[1].strip()[:150])
    elif " watch " in line:
        say("watch", line.split(" watch ", 1)[1].strip())
    elif " exception " in line and "first-chance" in line and any(
            f" at {m}+" in line for m in ("d3d12_xs.dll", "xg_xs.dll", "GameInput.dll", "xmem.dll", "XCRDAPI.dll")):
        # A stand-in DLL probing whether an address is readable: caught and handled there (expected).
        counts["handled probes"] = counts.get("handled probes", 0) + 1
        if counts["handled probes"] == 1:
            say("probe", "stand-in read probes fault on purpose and are handled; counted in the status line from now on")
    elif " exception " in line and "C++ exception" not in line and "thread name" not in line:
        say("EXCEPTION", line.split(" exception ", 1)[1].strip()[:150])
    elif re.search(r" exit 0x", line):
        say("EXIT", line.strip())


def on_file(line):
    if "DENIED (offline)" in line:
        counts["refused"] += 1
        target = line.split("DENIED (offline)", 1)[1].split(" from ")[0].strip()
        say("offline", "refused network open: " + target[:100])
        return
    if "Bink" in line:
        say("movie", re.sub(r" from .*", "", line.split(None, 1)[1]).strip()[:140])
        return
    if " REDIRECT " in line:
        say("EXPERIMENT", "forced code path: " + line.split(" REDIRECT ", 1)[1].strip()[:120])
        return
    if " HIT " in line:
        say("hit", line.split(" HIT ", 1)[1].strip()[:140])
        return
    m = game_file.search(line)
    if m:
        counts["files"] += 1
        result = line.rsplit("->", 1)[-1].strip()
        say("file", m.group(1) + ("" if result == "0x00000000" else f"   (failed {result})"))


def on_shim(line):
    global state, frames
    line = line.strip()
    if not line:
        return
    if line.startswith("x2w-trap"):
        say("TRAP", line[9:200] + "   <- unimplemented console function: the run stops here")
    elif "Factory::OpenFile" in line:
        m = re.search(r'path="([^"]+)"', line)
        say("storage", "opened " + (m.group(1) if m else line[:120]))
    elif "Queue::EnqueueStatus" in line:
        m = re.search(r"inflated=(\d+) failed=(\d+)", line)
        if m:
            counts["chunks"] = int(m.group(1))
            say("storage", f"read batch complete: {m.group(1)} compressed chunks decompressed so far, {m.group(2)} failed")
    elif "EnqueueRequest" in line or "options=" in line or "Queue::Submit" in line or "IsComplete" in line:
        return
    elif line.startswith("x2w-dstorage heartbeat "):
        m = re.search(r"\binflated=(\d+)", line)
        if m:
            counts["chunks"] = int(m.group(1))
        say("storage", line)
    elif line.startswith("x2w-render totals:"):
        say("render", line)
    elif "heartbeat" in line:
        m = re.search(r"heartbeat: (\d+) frames presented in ([\d.]+) s \(([\d.]+) fps", line)
        if m:
            frames = m.group(1)
            say("frames", f"{m.group(1)} frames presented, {m.group(3)} fps")
    elif "title main state" in line:
        m = re.search(r"state (-?\d+) -> (-?\d+)", line)
        if m:
            state = m.group(2)
            meaning = {"0": "starting", "1": "loading", "3": "start-up flow, waiting"}.get(state, "")
            say("STATE", f"title main state {m.group(1)} -> {m.group(2)} {meaning}")
    elif line.startswith("x2w-audio"):
        say("audio", line[10:170])
    elif line.startswith("x2w-xmem"):
        say("memory", line[9:170])
    elif line.startswith("x2w-noop"):
        counts["gpu calls first seen"] += 1
        m = re.search(r"!(\S+)", line)
        say("gpu", "first use of an untranslated call: " + (m.group(1) if m else line[9:80]))
    elif "DEVICE REMOVED" in line or "failed" in line.lower() and "failed=0" not in line:
        say("WARNING", line[:170])
    elif line.startswith("x2w-d3d12") and ("placeholder" in line or "Create" in line or "slot" in line):
        say("gpu", line[10:170])
    elif line.startswith("x2w-xg"):
        say("texture", line[7:150])


sources = {"events.txt": on_event, "files.txt": on_file, "trap.log": on_shim}
positions = {name: 0 for name in sources}
partial = {name: b"" for name in sources}  # bytes: a character can be split between two reads
print("Xbox2Windows local startup experiment: live log of " + os.path.abspath(rundir))
print("Columns: time since this window opened, category, detail. Network refusals are shown when observed.")
print("-" * 110, flush=True)
finished = os.path.join(rundir, "finished.txt")
ended = False
while True:
    progressed = False
    for name, handler in sources.items():
        path = os.path.join(rundir, name)
        try:
            with open(path, "rb") as handle:
                handle.seek(positions[name])
                chunk = handle.read()
                positions[name] += len(chunk)
        except OSError:
            continue
        if not chunk:
            continue
        progressed = True
        lines = (partial[name] + chunk).split(b"\n")
        partial[name] = lines.pop()
        for raw in lines:
            try:
                handler(raw.decode("utf-8", errors="replace").rstrip("\r"))
            except Exception as error:  # a viewer bug must not hide the rest of the log
                say("viewer", f"could not format a line: {error}")
    if time.time() - last_status >= 10 and not ended:
        last_status = time.time()
        say("status", f"state {state}, {frames} frames, {counts['modules']} modules, {counts['files']} game-file opens, "
                      f"{counts['chunks']} chunks decompressed, {counts['refused']} network opens refused, "
                      f"{counts.get('handled probes', 0)} handled probes")
    if ended and not progressed:
        break
    if os.path.exists(finished):
        ended = True
        continue
    time.sleep(0.25)
print("-" * 110)
try:
    with open(finished, encoding="utf-8", errors="replace") as handle:
        print(handle.read().strip())
except OSError:
    pass
print("\nRun ended. Full logs are in " + os.path.abspath(rundir) + " (events.txt, files.txt, trap.log).")
# Closes by itself (the full logs stay in the run folder).
for left in range(10, 0, -1):
    print(f"\rThis window closes in {left} s ", end="", flush=True)
    time.sleep(1)
