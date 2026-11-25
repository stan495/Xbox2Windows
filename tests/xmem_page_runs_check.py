"""Exercise large batches of physical pages in the production xmem shim.

Pages are slots of shared sections, and adjacent pages of one batch share a view (a run). This
check covers a batch that spans two sections, holes punched into it, pages moved and reused, what
VirtualQuery reports before and after a run is split, two views of the same pages, failures while
a run is being taken apart, and a reader on a page whose neighbour is unmapped again and again.

usage: xmem_page_runs_check.py <xmem.dll> <xmem.dll built with its test controls> [pages]
The deployed shim has no test switches. The cases that refuse a step of an unmap, or run without
the fault handler, load the second build (X2W_XMEM_TEST_CONTROLS). Cases that have to end in an
access violation, and the lifetime cases, run in a child process (--child ...) whose outcome the
parent checks after the child has announced the step it is about to take.
"""
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

PAGE = 65536
PHYSICAL_64K = 0x20400000
STARTUP_RESERVE = 0x20402000
WIN_RELEASE = 0x8000
MEM_COMMIT, MEM_RESERVE, MEM_MAPPED = 0x1000, 0x2000, 0x40000
ACCESS_VIOLATION = 0xC0000005
ERROR_INVALID_ADDRESS = 487


class MemoryBasicInformation(C.Structure):
    _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p), ("AllocationProtect", C.c_ulong),
                ("PartitionId", C.c_ushort), ("RegionSize", C.c_size_t), ("State", C.c_ulong), ("Protect", C.c_ulong),
                ("Type", C.c_ulong)]


def bind(library, name, args, result):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


class Shim:
    def __init__(self, path):
        library = C.WinDLL(str(Path(path).resolve()), use_last_error=True, winmode=0x900)
        kernel = C.WinDLL("kernelbase.dll", use_last_error=True)
        pointer, number, dword = C.c_void_p, C.c_size_t, C.c_ulong
        self.pointer, self.number = pointer, number
        self.allocate_pages = bind(library, "XMemAllocatePhysicalPages", [number, C.POINTER(number), C.POINTER(number)], C.c_int)
        self.release = bind(library, "XMemFreePhysicalPages", [number, C.POINTER(number)], C.c_int)
        self.contiguous = bind(library, "XMemMapPhysicalPages", [pointer, number, C.POINTER(number)], pointer)
        self.scatter = bind(library, "XMemMapPhysicalPagesScatter", [C.POINTER(pointer), number, C.POINTER(number)], C.c_int)
        self.reserve_startup = bind(library, "XMemVirtualAlloc", [pointer, number, dword, number, dword], pointer)
        self.free = bind(kernel, "VirtualFree", [pointer, number, dword], C.c_int)
        self.query_raw = bind(kernel, "VirtualQuery", [pointer, C.POINTER(MemoryBasicInformation), number], number)
        self.library, self.name = library, Path(path).name
        self.whole = self.reserve_startup(None, 16 << 30, STARTUP_RESERVE, 0x21, 4)
        assert self.whole, C.get_last_error()

    def close(self):
        self.free(self.whole, 0, WIN_RELEASE)

    def free_library(self, release_reservation=True):
        """FreeLibrary on the shim. Returns True when the module is gone afterwards."""
        if release_reservation:
            self.close()
        kernel32 = C.WinDLL("kernel32", use_last_error=True)
        free_library = bind(kernel32, "FreeLibrary", [C.c_void_p], C.c_int)
        loaded = bind(kernel32, "GetModuleHandleW", [C.c_wchar_p], C.c_void_p)
        assert free_library(self.library._handle), C.get_last_error()
        return loaded(self.name) is None

    def pages(self, count):
        got, tokens = self.number(count), (self.number * count)()
        assert self.allocate_pages(PHYSICAL_64K, C.byref(got), tokens) and got.value == count
        return tokens

    def unmap(self, addresses):
        array = (self.pointer * len(addresses))(*addresses)
        return self.scatter(array, len(addresses), None)

    def query(self, address):
        info = MemoryBasicInformation()
        assert self.query_raw(address, C.byref(info), C.sizeof(info)) == C.sizeof(info)
        return info.AllocationBase, info.BaseAddress, info.RegionSize, info.State, info.Type


def word(address):
    return C.c_uint32.from_address(address)


def mark(base, count, tag):
    for i in range(count):
        word(base + i * PAGE).value = tag + i
        word(base + i * PAGE + PAGE - 4).value = i


def marked(base, count, tag, skip=()):
    return all(i in skip or (word(base + i * PAGE).value == tag + i and word(base + i * PAGE + PAGE - 4).value == i)
               for i in range(count))


def large_batch(shim, base, total):
    """A batch across the 4096-page section boundary: holes, moved pages, reuse. Returns the map time."""
    assert total > 4200, "the batch has to span two 4096-page sections"
    tokens = shim.pages(total)
    started = time.perf_counter()
    assert shim.contiguous(base, total, tokens) == base, C.get_last_error()
    mapped_in = time.perf_counter() - started
    mark(base, total, 0xC0DE0000)
    assert marked(base, total, 0xC0DE0000)
    holes = [100, 101, 4095, 4096, total - 1]
    assert shim.unmap([base + i * PAGE for i in holes]), C.get_last_error()
    assert marked(base, total, 0xC0DE0000, skip=set(holes)), "a page beside a hole lost its bytes"
    elsewhere = base + (total + 16) * PAGE
    moved = (shim.number * len(holes))(*[tokens[i] for i in holes])
    assert shim.contiguous(elsewhere, len(holes), moved) == elsewhere, C.get_last_error()
    for slot, i in enumerate(holes):
        assert word(elsewhere + slot * PAGE).value == 0xC0DE0000 + i, f"moved page {i}"
    assert shim.contiguous(elsewhere, len(holes), None) == elsewhere
    addresses = (shim.pointer * len(holes))(*[base + i * PAGE for i in holes])
    assert shim.scatter(addresses, len(holes), moved), C.get_last_error()
    assert marked(base, total, 0xC0DE0000)
    assert not shim.release(total, tokens), "released a mapped grant"
    assert shim.contiguous(base, total, None) == base
    assert shim.release(total, tokens)
    fresh = shim.pages(64)
    assert shim.contiguous(base, 64, fresh) == base, C.get_last_error()
    for i in range(64):
        assert word(base + i * PAGE).value == 0 and word(base + i * PAGE + PAGE - 4).value == 0, f"reused page {i} is not zero"
    assert shim.contiguous(base, 64, None) == base and shim.release(64, fresh)
    return mapped_in, len(holes)


def query_before_and_after_split(shim, base):
    """What VirtualQuery reports for a run of eight pages, and for its pieces after page 3 is unmapped.
    For an address inside a view it answers the view's base as AllocationBase and the bytes from the
    queried page to the end of the view as RegionSize."""
    tokens = shim.pages(8)
    assert shim.contiguous(base, 8, tokens) == base

    def view(page, first, last):
        at = base + page * PAGE
        return shim.query(at) == (base + first * PAGE, at, (last + 1 - page) * PAGE, MEM_COMMIT, MEM_MAPPED)

    for i in (0, 3, 7):
        assert view(i, 0, 7), f"whole run, page {i}: {shim.query(base + i * PAGE)}"
    assert shim.unmap([base + 3 * PAGE])
    # Pages 0-2 and 4-7 are now two views with their own allocation bases; page 3 is a placeholder.
    for i in (0, 2):
        assert view(i, 0, 2), f"lower piece, page {i}: {shim.query(base + i * PAGE)}"
    for i in (4, 7):
        assert view(i, 4, 7), f"upper piece, page {i}: {shim.query(base + i * PAGE)}"
    hole = shim.query(base + 3 * PAGE)
    assert hole[1] == base + 3 * PAGE and hole[2] == PAGE and hole[3] == MEM_RESERVE, hole
    # Mapping the page again does not join the pieces: three views.
    assert shim.contiguous(base + 3 * PAGE, 1, C.byref(shim.number(tokens[3]))) == base + 3 * PAGE
    assert view(3, 3, 3) and view(2, 0, 2) and view(4, 4, 7)
    assert shim.contiguous(base, 8, None) == base and shim.release(8, tokens)


def two_views(shim, base):
    """The same six pages in two runs: writes through either are seen through the other, and
    unmapping a page of one run leaves the other run's page of the same backing in place."""
    tokens = shim.pages(6)
    first, second = base, base + 32 * PAGE
    assert shim.contiguous(first, 6, tokens) == first and shim.contiguous(second, 6, tokens) == second
    assert shim.query(first)[2] == 6 * PAGE and shim.query(second)[2] == 6 * PAGE, f"each mapping is one view: {shim.query(first)} {shim.query(second)}"
    for i in range(6):
        word(first + i * PAGE + 8).value = 0xA0000000 + i
        assert word(second + i * PAGE + 8).value == 0xA0000000 + i, f"first to second, page {i}"
        word(second + i * PAGE + 12).value = 0xB0000000 + i
        assert word(first + i * PAGE + 12).value == 0xB0000000 + i, f"second to first, page {i}"
    assert shim.unmap([first + 2 * PAGE])
    word(second + 2 * PAGE + 8).value = 0x5EC0
    for i in (1, 3):
        word(first + i * PAGE + 16).value = 0xC0000000 + i
        assert word(second + i * PAGE + 16).value == 0xC0000000 + i, f"after the split, page {i}"
    assert shim.contiguous(first + 2 * PAGE, 1, C.byref(shim.number(tokens[2]))) == first + 2 * PAGE
    assert word(first + 2 * PAGE + 8).value == 0x5EC0, "bytes written through the other view while this page was unmapped"
    assert not shim.release(6, tokens), "released pages that two views still show"
    assert shim.contiguous(first, 6, None) == first
    assert not shim.release(6, tokens), "released pages that one view still shows"
    assert shim.contiguous(second, 6, None) == second and shim.release(6, tokens)


def two_runs(shim, base, tag):
    """Two runs of eight pages with a gap between them."""
    a, b = base, base + 16 * PAGE
    ta, tb = shim.pages(8), shim.pages(8)
    assert shim.contiguous(a, 8, ta) == a and shim.contiguous(b, 8, tb) == b
    mark(a, 8, tag)
    mark(b, 8, tag + 0x100)
    return a, b, ta, tb


def failures(shim, base):
    """One step of an unmap refused (X2W_XMEM_TEST_FAULT): what is mapped and what the shim records must agree."""
    # An unmap refused after one run is already gone: that run comes back and nothing changes.
    a, b, ta, tb = two_runs(shim, base, 0x11110000)
    os.environ["X2W_XMEM_TEST_FAULT"] = "1:1"
    C.set_last_error(0)
    assert not shim.unmap([a + 3 * PAGE, b + 3 * PAGE]), "a refused unmap reported success"
    assert C.get_last_error() == ERROR_INVALID_ADDRESS, C.get_last_error()
    del os.environ["X2W_XMEM_TEST_FAULT"]
    assert marked(a, 8, 0x11110000) and marked(b, 8, 0x11110100), "pages changed although the unmap was refused"
    assert shim.query(a)[3] == MEM_COMMIT and shim.query(a + 3 * PAGE)[3] == MEM_COMMIT
    assert not shim.release(8, ta), "the refused unmap dropped the views from the record"
    assert shim.unmap([a + 3 * PAGE, b + 3 * PAGE]), "the same unmap without the fault"
    assert marked(a, 8, 0x11110000, skip={3}) and marked(b, 8, 0x11110100, skip={3})
    assert shim.contiguous(a, 3, None) == a and shim.unmap([a + i * PAGE for i in range(4, 8)])
    assert shim.contiguous(b, 3, None) == b and shim.unmap([b + i * PAGE for i in range(4, 8)])
    assert shim.release(8, ta) and shim.release(8, tb)

    # Surviving pages cannot be mapped again: the page asked for is unmapped, the survivors are
    # forgotten, not left recorded without a view, and the call fails.
    base += 64 * PAGE
    a, b, ta, tb = two_runs(shim, base, 0x22220000)
    os.environ["X2W_XMEM_TEST_FAULT"] = "2:0"
    assert not shim.unmap([a + 3 * PAGE]), "a failed remap reported success"
    del os.environ["X2W_XMEM_TEST_FAULT"]
    for i in range(8):
        assert shim.query(a + i * PAGE)[3] != MEM_COMMIT, f"page {i} of the broken run still has a view"
    assert marked(b, 8, 0x22220100), "the other run changed"
    # The record agrees: those pages are free to be mapped, here or elsewhere, and kept their bytes.
    assert shim.contiguous(a, 8, ta) == a, C.get_last_error()
    assert marked(a, 8, 0x22220000), "forgotten pages lost their bytes"
    forgotten = a
    assert shim.contiguous(a, 8, None) == a and shim.contiguous(b, 8, None) == b
    assert shim.release(8, ta) and shim.release(8, tb)

    # An unmap refused and the restore refused as well: the run that was already unmapped is
    # forgotten, the other run is untouched, and the call fails with the unmap's error.
    base += 64 * PAGE
    a, b, ta, tb = two_runs(shim, base, 0x33330000)
    os.environ["X2W_XMEM_TEST_FAULT"] = "3:1"
    C.set_last_error(0)
    assert not shim.unmap([a + 3 * PAGE, b + 3 * PAGE])
    assert C.get_last_error() == ERROR_INVALID_ADDRESS, C.get_last_error()
    del os.environ["X2W_XMEM_TEST_FAULT"]
    assert shim.query(a)[3] != MEM_COMMIT and marked(b, 8, 0x33330100)
    assert shim.release(8, ta), "the forgotten run's pages are still recorded as mapped"
    assert shim.contiguous(b, 8, None) == b and shim.release(8, tb)
    return forgotten


def reader_while_toggling(shim, base, seconds, announce=None):
    """A second thread reads one page while the main thread unmaps and maps its neighbour, which
    shares its view: every unmap takes the reader's view away for a moment."""
    tokens = shim.pages(64)
    assert shim.contiguous(base, 64, tokens) == base
    mark(base, 64, 0x44440000)
    reader_page, toggled = 20, 21
    stop, seen, wrong = threading.Event(), [0], [0]

    def read_loop():
        at = base + reader_page * PAGE
        while not stop.is_set():
            if word(at).value != 0x44440000 + reader_page:
                wrong[0] += 1
            seen[0] += 1

    if announce:
        print(announce, flush=True)
    reader = threading.Thread(target=read_loop)
    reader.start()
    one_address, one_page = (shim.pointer * 1)(base + toggled * PAGE), (shim.number * 1)(tokens[toggled])
    toggles, deadline = 0, time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        assert shim.scatter(one_address, 1, None), C.get_last_error()
        assert shim.scatter(one_address, 1, one_page), C.get_last_error()
        toggles += 1
    stop.set()
    reader.join(10)
    assert not reader.is_alive() and seen[0] > 0 and wrong[0] == 0, (seen[0], wrong[0])
    assert marked(base, 64, 0x44440000)
    assert shim.contiguous(base, 64, None) == base and shim.release(64, tokens)
    return toggles, seen[0]


def window_events(name):
    """The two events of the test build's X2W_XMEM_TEST_HOLD_WINDOW=<name> hook: the unmap sets
    <name>.unmapped, then waits for <name>.release."""
    kernel = C.WinDLL("kernel32", use_last_error=True)
    create = bind(kernel, "CreateEventW", [C.c_void_p, C.c_int, C.c_int, C.c_wchar_p], C.c_void_p)
    wait = bind(kernel, "WaitForSingleObject", [C.c_void_p, C.c_ulong], C.c_ulong)
    signal = bind(kernel, "SetEvent", [C.c_void_p], C.c_int)
    unmapped, release = create(None, 1, 0, name + ".unmapped"), create(None, 1, 0, name + ".release")
    assert unmapped and release, C.get_last_error()
    return unmapped, release, wait, signal


def reader_in_window(shim, base, name, announce):
    """The reader waits, without the GIL, until the unmap holds the neighbouring run apart, then
    reads a page of it: the read always falls into the window where the view is gone. Returns
    False when the unmap never reached the window within ten seconds."""
    unmapped, release, wait, signal = window_events(name)
    tokens = shim.pages(64)
    assert shim.contiguous(base, 64, tokens) == base
    mark(base, 64, 0x44440000)
    reached = [False]

    def read_once():
        if wait(unmapped, 10000) != 0:
            return
        reached[0] = True
        print("child: reader reads the unmapped page", flush=True)
        word(base + 20 * PAGE).value
        signal(release)

    print(announce, flush=True)
    reader = threading.Thread(target=read_once)
    reader.start()
    one_address, one_page = (shim.pointer * 1)(base + 21 * PAGE), (shim.number * 1)(tokens[21])
    while reader.is_alive():
        assert shim.scatter(one_address, 1, None), C.get_last_error()
        assert shim.scatter(one_address, 1, one_page), C.get_last_error()
    return reached[0]


def handled_exception():
    """An access violation that has nothing to do with the shim, raised and handled the usual way."""
    try:
        C.string_at(1, 1)
    except OSError:
        return True
    return False


def default_unload(path):
    """The default mode (X2W_XMEM_SHARED_VIEWS unset): pages have a view each, a page can be
    unmapped without touching its neighbours, and after ordinary mapping the shim unloads."""
    assert "X2W_XMEM_SHARED_VIEWS" not in os.environ
    shim = Shim(path)
    base = shim.whole + PAGE
    tokens = shim.pages(16)
    assert shim.contiguous(base, 16, tokens) == base
    mark(base, 16, 0x44440000)
    for page in (0, 7, 15):
        assert shim.query(base + page * PAGE)[2] == PAGE, "pages share a view without the opt-in"
    assert shim.unmap([base + 5 * PAGE]) and marked(base, 16, 0x44440000, skip={5})
    assert shim.scatter((shim.pointer * 1)(base + 5 * PAGE), 1, (shim.number * 1)(tokens[5])) and marked(base, 16, 0x44440000)
    assert shim.contiguous(base, 16, None) == base and shim.release(16, tokens)
    assert shim.free_library(), "the default shim stayed loaded after FreeLibrary"
    assert handled_exception(), "the access violation was not delivered after the default shim was unloaded"
    # A second load starts clean and can still choose either mode before its first map.
    shim = Shim(path)
    tokens = shim.pages(4)
    base = shim.whole + PAGE
    assert shim.contiguous(base, 4, tokens) == base and shim.query(base)[2] == PAGE
    assert shim.contiguous(base, 4, None) == base and shim.release(4, tokens)
    assert shim.free_library(), "the reloaded default shim stayed loaded after FreeLibrary"
    print("child: default shim unloaded after mapping, twice", flush=True)


def lifetime(path):
    """The opt-in mode (X2W_XMEM_SHARED_VIEWS=1). Until the first mapping call the shim unloads
    like any DLL. From then on the module is pinned: FreeLibrary leaves it loaded, an unrelated
    handled exception is still delivered to its own handler, and the shim and its handler keep
    working."""
    shim = Shim(path)
    assert shim.free_library(), "a shim that never mapped a page stayed loaded after FreeLibrary"
    assert handled_exception(), "the access violation was not delivered after an unused shim was unloaded"
    print("child: unused shim unloaded", flush=True)

    shim = Shim(path)
    base = shim.whole + PAGE
    tokens = shim.pages(16)
    assert shim.contiguous(base, 16, tokens) == base and shim.query(base)[2] == 16 * PAGE, "pages do not share a view"
    assert shim.unmap([base + 5 * PAGE])
    assert shim.scatter((shim.pointer * 1)(base + 5 * PAGE), 1, (shim.number * 1)(tokens[5]))
    for attempt in range(3):
        assert not shim.free_library(release_reservation=False), "the shim was unloaded with its fault handler registered"
    print("child: FreeLibrary left the pinned shim loaded", flush=True)
    assert handled_exception(), "the access violation was not delivered to its handler"
    print("child: handled exception delivered", flush=True)
    # Still the same shim: the views are there, and the handler still covers a split.
    mark(base, 16, 0x55550000)
    assert shim.contiguous(base, 16, None) == base and shim.release(16, tokens)
    toggles, reads = reader_while_toggling(shim, base, 0.5)
    assert toggles > 0 and reads > 0
    assert handled_exception()
    print("child: shim and handler work after FreeLibrary", flush=True)


def concurrent_shutdown(path, exit_while_running):
    """FreeLibrary from the main thread, again and again, while one thread reads a page and another
    unmaps and maps its neighbour: nobody may fault for good or see wrong bytes. With
    exit_while_running the process then ends with both threads still at it."""
    shim = Shim(path)
    base = shim.whole + PAGE
    tokens = shim.pages(64)
    assert shim.contiguous(base, 64, tokens) == base
    mark(base, 64, 0x66660000)
    stop, reads, toggles, wrong, errors = threading.Event(), [0], [0], [0], []

    def read_loop():
        at = base + 20 * PAGE
        try:
            while not stop.is_set():
                if word(at).value != 0x66660000 + 20:
                    wrong[0] += 1
                reads[0] += 1
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    def toggle_loop():
        one_address, one_page = (shim.pointer * 1)(base + 21 * PAGE), (shim.number * 1)(tokens[21])
        try:
            while not stop.is_set():
                assert shim.scatter(one_address, 1, None) and shim.scatter(one_address, 1, one_page)
                toggles[0] += 1
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=read_loop, daemon=True), threading.Thread(target=toggle_loop, daemon=True)]
    for thread in threads:
        thread.start()
    print("child: threads running", flush=True)
    frees, deadline = 0, time.perf_counter() + 1.5
    while time.perf_counter() < deadline:
        assert not shim.free_library(release_reservation=False), "the shim was unloaded under running threads"
        frees += 1
    assert not errors and wrong[0] == 0 and reads[0] > 0 and toggles[0] > 0, (errors, wrong[0], reads[0], toggles[0])
    print(f"child: {frees} FreeLibrary calls under {reads[0]} reads and {toggles[0]} toggles", flush=True)
    if exit_while_running:
        print("child: exiting with threads running", flush=True)
        sys.stdout.flush()
        sys.exit(0)  # the daemon threads are still reading and toggling when the process ends
    stop.set()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads) and not errors and wrong[0] == 0
    assert marked(base, 64, 0x66660000)
    print("child: threads stopped cleanly", flush=True)


def child(path, mode):
    if mode == "default-unload":
        default_unload(path)
        return 0
    if mode == "lifetime":
        lifetime(path)
        return 0
    if mode in ("concurrent-shutdown", "concurrent-exit"):
        concurrent_shutdown(path, mode == "concurrent-exit")
        return 0
    shim = Shim(path)
    base = shim.whole + PAGE
    if mode == "reader-without-retry":
        # The parent set X2W_XMEM_NO_FAULT_RETRY=1 and X2W_XMEM_TEST_HOLD_WINDOW: the unmap waits in
        # the window until the reader has read, and the reader's fault ends the process.
        if not reader_in_window(shim, base, os.environ["X2W_XMEM_TEST_HOLD_WINDOW"], "child: reader starts"):
            print("child: the unmap never held the window for the reader (hook missing or timed out)", flush=True)
            return 3
        print("child: reader survived", flush=True)
    elif mode == "hold-ignored":
        # The deployed shim has no hold: the same variables change nothing and the unmap never waits.
        unmapped, release, wait, signal = window_events(os.environ["X2W_XMEM_TEST_HOLD_WINDOW"])
        reader_while_toggling(shim, base, 3.0, announce="child: reader starts")
        assert wait(unmapped, 0) != 0, "the deployed shim held an unmap in the window"
        print("child: reader survived", flush=True)
    elif mode == "read-forgotten":
        # A page inside the range of the last break that the shim has forgotten: the handler must
        # pass the fault on instead of retrying it for ever.
        forgotten = failures(shim, base)
        print("child: reading a forgotten page", flush=True)
        word(forgotten).value
        print("child: the read returned", flush=True)
    return 0


def run_child(path, mode, environment=()):
    env = dict(os.environ)
    env.update(environment)
    return subprocess.run([sys.executable, "-B", __file__, "--child", mode, str(path)], capture_output=True, text=True,
                          timeout=120, env=env)


def expect_access_violation(path, mode, marker, environment=()):
    result = run_child(path, mode, environment)
    code = result.returncode & 0xFFFFFFFF
    assert "never held the window" not in result.stdout, f"{mode}: the hold hook did not run: {result.stdout!r}"
    assert marker in result.stdout, f"{mode}: the child stopped before its step: {result.stdout!r} {result.stderr[-400:]!r}"
    for finished in ("survived", "returned", "delivered"):
        assert finished not in result.stdout, f"{mode}: no fault: {result.stdout!r}"
    assert code == ACCESS_VIOLATION, f"{mode}: exit code {code:#x}, expected an access violation: {result.stderr[-400:]!r}"


def expect_clean(path, mode, marker):
    result = run_child(path, mode)
    assert result.returncode == 0 and marker in result.stdout, (
        f"{mode}: exit code {result.returncode & 0xFFFFFFFF:#x}: {result.stdout!r} {result.stderr[-600:]!r}")


def main(path, controls, total=6000):
    # The mode is chosen before the first mapping call. Everything below is the opt-in mode except
    # the default-unload child, which runs without the variable.
    os.environ["X2W_XMEM_SHARED_VIEWS"] = "1"
    shim = Shim(path)
    base = shim.whole + PAGE
    try:
        mapped_in, holes = large_batch(shim, base, total)
        query_before_and_after_split(shim, base)
        two_views(shim, base)
        toggles, reads = reader_while_toggling(shim, base, 2.0)
        # The deployed shim ignores the test switches: with one set, the unmap below must succeed.
        a, b, ta, tb = two_runs(shim, base + 256 * PAGE, 0x77770000)
        os.environ["X2W_XMEM_TEST_FAULT"] = "1:0"
        assert shim.unmap([a + 3 * PAGE]), "the deployed shim honoured X2W_XMEM_TEST_FAULT"
        del os.environ["X2W_XMEM_TEST_FAULT"]
        assert marked(a, 8, 0x77770000, skip={3}) and marked(b, 8, 0x77770100)
    finally:
        shim.close()
    # Refused steps of an unmap, in the build that has the control.
    test_shim = Shim(controls)
    try:
        failures(test_shim, test_shim.whole + PAGE)
    finally:
        test_shim.close()
    # Controls that must end in an access violation, each in a child process, on the test build:
    # shared views without the handler, and a read of a page the shim has forgotten.
    hold = {"X2W_XMEM_NO_FAULT_RETRY": "1", "X2W_XMEM_TEST_HOLD_WINDOW": f"x2w-xmem-hold-{os.getpid()}"}
    expect_access_violation(controls, "reader-without-retry", "child: reader reads the unmapped page", hold)
    expect_access_violation(controls, "read-forgotten", "child: reading a forgotten page")
    # The deployed shim ignores X2W_XMEM_NO_FAULT_RETRY and the hold: the reader survives there and
    # no unmap waits in the window.
    result = run_child(path, "hold-ignored", hold)
    assert result.returncode == 0 and "child: reader survived" in result.stdout, (
        f"the deployed shim honoured a test switch: {result.returncode & 0xFFFFFFFF:#x} {result.stdout!r} {result.stderr[-400:]!r}")
    # Lifetime of the handler, on the deployed shim, each in a process of its own.
    default = {k: v for k, v in os.environ.items() if k != "X2W_XMEM_SHARED_VIEWS"}
    result = subprocess.run([sys.executable, "-B", __file__, "--child", "default-unload", str(path)], capture_output=True,
                            text=True, timeout=120, env=default)
    assert result.returncode == 0 and "child: default shim unloaded after mapping, twice" in result.stdout, (
        f"default-unload: exit code {result.returncode & 0xFFFFFFFF:#x}: {result.stdout!r} {result.stderr[-600:]!r}")
    expect_clean(path, "lifetime", "child: shim and handler work after FreeLibrary")
    expect_clean(path, "concurrent-shutdown", "child: threads stopped cleanly")
    expect_clean(path, "concurrent-exit", "child: exiting with threads running")
    print(f"xmem_page_runs: PASS ({total} pages mapped in {mapped_in * 1000:.0f} ms, {holes} holes, VirtualQuery before and "
          f"after a split, two views, 3 refused steps, {toggles} neighbour toggles under {reads} reads, default mode unloads after mapping, "
          f"opt-in stays pinned, "
          f"FreeLibrary and process exit under running threads, 2 access-violation controls)")


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        sys.exit(child(sys.argv[3], sys.argv[2]))
    main(sys.argv[1], sys.argv[2], *(int(a) for a in sys.argv[3:4]))
