# Experiment notes and reproduction commands

Run commands from the repository root. Start with the [project overview](../README.md) and [contribution guide](../CONTRIBUTING.md). Package-specific observations below used local inputs. Community regression tests use fixtures built from this repository.

## Local tools

For optional static dependency inspection, build Microsoft's pinned version of xbdepends from PowerShell with x64 Visual Studio Build Tools, CMake and the Windows SDK.

```powershell
.\tools\setup-xbdepends.ps1
```

Load the toolchain in each new development shell, then configure, build and test.

```powershell
.\tools\dev.ps1
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
```

The tests exercise desktop D3D12 root signature serialization and deserialization. Use the resulting test binary to try the static inspection tools.

```powershell
xbdepends -v .\out\build\dev\d3d12_sdk_check.exe
dumpbin /imports .\out\build\dev\d3d12_sdk_check.exe
```

Build outputs and downloaded tool sources live under `out/`. [Microsoft xbdepends documentation](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/xbdepends/)

## Two-record archive reader from standard startup

The [archive reader](../tests/cpu/archive/reader.zig) uses Zig 0.13.0's standard single-threaded Windows console startup. From the linked entry the translator follows the call into the record loop and the final call to RtlExitUserProcess. It recognizes six-byte linker jump thunks for explicitly bound IAT slots, models termination without fallthrough and propagates a narrow return value through a wider register copy. A control changes the startup to consume EAX instead of AL and must fail translation.

Each record contains a one-byte compressed length and a PKWare DCL stream. The [native codec DLL](../tests/cpu/archive/codec.cpp) builds the unmodified blast.c and blast.h from [Mark Adler's blast 1.3 in zlib v1.3.1](https://github.com/madler/zlib/tree/v1.3.1/contrib/blast), retaining their license notices. The callback stages up to 6144 decoded bytes per record. The translated reader drives allocation, decode, publication, cleanup and the completed count. All three processes share the native codec.

Two distinct eight-byte streams produced 26 identical bytes, two completed records, two allocations and frees, and exit 0 across Windows, pe_map and AOT. With only four bytes of the second payload the decoder requested more input, then delivered one tentative output byte. Only the first record's 13 bytes reached stdout. All three completed one record, freed both allocations and exited 2. The [comparison](../tests/cpu/archive_check.py) checks the full event trace against fixed expectations.

Install requirements-dev.txt before building. On x64 Windows this includes the pinned Zig compiler (about 80 MB download).

```powershell
cmake --build --preset dev --target archive_reader_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_archive_reader$' -V
```

### Recompiling the blast decoder

The `cpu_native_archive_blast` test starts from the same archive reader and builds the unmodified blast 1.3 C file into a separate scalar x64 DLL with MSVC /Od. The [fixture translator](../tests/cpu/archive/translate_blast.py) follows direct calls from its PE export through the bit reader, Huffman table construction, symbol decoder and decompression loop. It reuses the x64 instruction lifter and frame analysis. Only generated C++ is compiled into the translated decoder. Its initialized data, including relocated table pointers, is separate from the native module. This profile uses per-thread tables, a bounded invocation stack, the six-argument blast ABI and two typed callback targets. The host supplies setjmp/longjmp semantics for a single active recovery point. The file I/O and allocation callbacks remain native.

On the MSVC 19.51 build, tracing the generated callback sites for the two streams reached the input call at RVA `0x11e0` and the final output call at `0x115d`. With the second stream truncated, the input calls returned 4 then 0 bytes, the call at `0x1206` took the longjmp path, and the final output callback received one tentative byte. This byte remained unpublished. The two short streams exercise the final partial-window callback.

The window cases retain a 13-byte first record and use explicit DCL length/distance pairs in the second. Eight 512-byte overlapping copies at distance three span the 4096-byte decoder window. The decoder then reads a distinct final literal. On Windows x64, the native, mapped and recompiled programs all delivered callbacks of 4096 and 4 bytes for this record, published all 4113 archive bytes in order, completed two records and exited 0.

With sixteen copies, the adapter stages the first 4096 bytes and refuses the next 4096 because they exceed its 6144-byte per-record limit. The second record publishes zero bytes, including the previously staged window. All three runners retained the first record's 13 bytes, completed one record, and exited 2. Both cases check the full callback trace and one allocation and release per record.

Windows loads the original decoder for the native run. The mapped child separately maps the original decoder, codec adapter and reader, binding each import to the preceding mapping's export. The AOT reader imports an adapter linked to the translated decoder; PE import checks guard against falling back to the original. Both archive outcomes match the byte and event traces above. A direct callback control on both decoder DLLs refuses output and checks both context pointers, the delivered bytes, and blast's error return of 1.

```powershell
cmake --build --preset dev --target archive_all_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_archive_blast$' -V
```

## CRC-32 console program from the PE entry

`cpu_crc_line` reads the first text line from stdin and writes eight uppercase hexadecimal digits and a newline. Its [MASM entry](../tests/cpu/crc_line/entry.asm) handles input, formatting and exit status. MSVC compiles the [CRC update](../tests/cpu/crc_line/crc_step.cpp) as a separate optimized object. The linker imports the real Windows Universal CRT stdio functions through `ucrt.lib`. The source PE uses the shared [native startup wrapper](../tests/cpu/exe_startup.asm) and exports the returning body as `native_entry`. The wrapper calls `ExitProcess` when Windows launches the EXE and returns the body status when a host maps the image.

The [console translator](../tools/cpu_stdio_exe_aot.py) lifts `native_entry` with no defined argument registers. Its generated EXE embeds the SHA-256 of the input PE, which the comparison checks against the image it launches and maps. On MSVC 19.51 the first refusal before enabling imports was at RVA `0x103d`, the call through the IAT to `getchar`. The translator binds near RIP-relative calls by their IAT slots, DLL and symbol, with the argument widths and defined return bytes of `getchar`, `putchar` and `fflush`. It follows the direct call to the compiled CRC routine and emits both functions from the linked instructions. Only the generated C++ is compiled into the AOT executable. The host supplies the UCRT calls. The test rejects a copy that names a different provider.

The [Windows comparison](../tests/cpu/crc_line_check.py) launches the native EXE, a child running the same PE with `pe_map_run`, and the AOT EXE. The mapped child restores normal UCRT text-mode pipes before entry. All three produced `CBF43926\r\n` for `123456789` and exited 0. They also agreed on CRLF, a final line without a newline, an empty line and a 13,000-byte line. EOF before any input produced no output and exit 1. A read-only stdout handle left the sink unchanged and produced exit 2. A separately linked and retranslated Castagnoli polynomial control produced `E3069283\r\n` on the same standard vector. With a worker held on an event at the startup boundary, the piped cases check the native process handle, EOF on both pipes, output bytes and exit code. The read-only stdout case also checks exit 2 with the worker held. A marker emitted after `pe_map_run` returns confirms that the mapped host regains control.

```powershell
cmake --build --preset dev --target cpu_crc_line_lifted cpu_crc_castagnoli_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_crc_line$' -V
```

## EXE startup through the desktop runtime shims

The source-built `cpu_shim_startup` EXE calls a companion DLL that links
against the desktop `xmem.dll` and `d3d12_xs.dll`. The DLL checks that its
process attach ran once before the call. It allocates, writes, queries and frees
a page-backed XMem block, then checks that the freed range is no longer committed
or registered as graphics memory under the desktop policy. It also checks the
unsupported working-set reply and round-trips a tagged root signature. The
resulting descriptor retains its root constants and the desktop flag.

`cpu_native_shim_startup` runs the same EXE and companion under Windows and
under `pe_map`. The mapped host loads both desktop shims from the build and
maps the companion before resolving its entry export for the EXE. The test
checks all six shim requests, the ordered output, exit status 42 and the
mapped host's return marker. Withholding either provider identifies the first
unbound import from that DLL before guest entry.

```powershell
cmake --build --preset dev --target cpu_shim_startup pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_shim_startup$' -V
```

The same EXE drives a compute dispatch when the test supplies a size. The
shared x64 translator starts at the PE entry and follows the internal call
through allocation, the null check, dispatch and release. The AOT runner
records the input image SHA-256. All three runners call the same native
providers. The compute provider submits the repository's HLSL on a D3D12
compute queue, waits for a fence, then copies the GPU readback into the XMem
allocation. The test checks both the bytes and the sequence of import calls.

| Compute request | Readback (hex, in memory order) | Compute frees | Exit |
| --- | --- | ---: | ---: |
| 4,096 bytes | `594634123d463412e546341209463412` | 1 | 42 |
| 8,192 bytes | `597634123d763412e576341209763412` | 1 | 42 |
| `SIZE_MAX` | empty, allocation returned null before dispatch | 0 | 17 |

The verifier checks the size, attributes (`0xa8004000`), seed and word
count against an independent expectation. Virtual addresses may differ per
process. Within each process the returned, 256-byte-aligned pointer must reach
dispatch and release unchanged, and its pages must be free afterward. The
counts above cover the compute allocation. The companion also performs its
existing startup allocation and release in every case.

```powershell
cmake --build --preset dev --target cpu_shim_startup_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_shim_compute_(warp|hardware)$' -V
```

The same EXE drives a raster draw when the test requests it. The draw branch
calls the new `cpu_shim_draw` provider, which renders the repository's 64x64
test triangle offscreen on a D3D12 direct queue, waits for a fence, copies the
render target to a readback buffer and publishes the raw RGBA bytes. The
guest exits 42 only when every one of the 16,384 bytes matches the CPU pixel
rule (`x >= 8 && y >= 8 && x + y <= 63` green, black otherwise, opaque). The
check compares the native EXE, the `pe_map` mapping and the AOT build byte for
byte. A mirrored-X shader variant must exit 20 with a different frame that
still agrees across runners; an unknown draw case must exit 21 with no frame.
The non-mirrored shader object is unchanged, so the existing triangle draws are
unaffected.

For the two-frame case, the guest calls the same draw import twice, with
arguments 6 and 7. The provider keeps one device, direct queue, graphics
pipeline, render target and readback resource. It waits for the first fence
before resetting the command allocator and rebinding the pixel shader's root
constants. All three runners must publish the green triangle followed by a
red triangle. The check compares all 16,384 RGBA bytes per frame with an
independent raster reference and requires completed fence values 1 and 2.
The stale-input control changes the second import argument to 8 and must
return two green frames and exit 20. Argument 9 fails before the second
submission. That control must publish just the first frame, report just fence
1 and exit 24. The mapped host must regain control in all three cases.

`cpu_shim_texture_reuse` extends the same EXE with three fenced draws. One
compute dispatch fills a 2×2 `R32_UINT` texture with packed RGBA8 colors.
The second dispatch changes only texel (1, 0), from green to magenta. The
other three texels serve as guards. Both draws load from the same texture
SRV. The second dispatch transitions that resource from pixel-shader-resource
to UAV, then back before rasterization. The third draw leaves the texture in
pixel-shader-resource state and uses a second pipeline to swap red and blue.

Each runner checks the full 64×64 readback after fence values 1, 2 and 3.
Pixels (12, 12), (30, 12), (12, 30) and (30, 30) observe the four texels.
The stale control skips the partial dispatch. Comparing it against the
updated reference must report the first mismatch at stage 2, pixel (24, 8),
with expected magenta and actual green. The control also checks the full
unchanged second frame and the channel-swapped third frame.

```powershell
cmake --build --preset dev --target cpu_shim_startup cpu_shim_startup_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_shim_(draw|texture_reuse)_(warp|hardware)$' -V
```

The same draw contract also runs the translated compute, mesh and pixel chain
from the [compute-to-mesh texture fixture](#single-group-mesh-entry-specialization).
The draw cases `chain0`, `chain1` and `chain-stale` send the frame to
[`shim_chain_gpu.cpp`](../tests/cpu/shim_chain_gpu.cpp). On one direct queue it
dispatches translated gfx1013 producers that write two packed RGBA8 rows to a
raw UAV, copies them into a 2x2 texture, and draws with the translated indexed
mesh and `image_sample_lz` pixel stages into a 32x32 target. The EXE, the AOT
import table and the `draw_request`/`draw_frame` signatures are unchanged. The
provider publishes all 4,096 RGBA bytes and the 80-byte mesh export trace, and
the guest exits 42 only when both match the case reference.
[The check](../tests/cpu/shim_chain_check.py) builds the shaders and references
from the generators that `compute_mesh_texture_check.py` uses. It requires the
native EXE, `pe_map` and the AOT build to agree on the shim bindings, the exit
status and all 4,176 published bytes, and compares them with the Python
reference.

| Case | Producers dispatched | Bound texture | Exit | Published bytes |
|---|---|---|---|---|
| `chain0` | palette A | A | 42 | match A's reference |
| `chain1` | A, then B | B | 42 | match B's reference |
| `chain-stale` | A, then B | A | 20 | equal to A's frame; against B's reference, 276 covered pixels differ, while all 748 clear pixels and the trace match |

Mesh shader tier 1 is required. Without it the test exits 77, reported as a
skip. `XBOX2WINDOWS_ADAPTER` selects the hardware adapter by part of its name.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_shim_chain_(warp|hardware)$' -V
```

## Source-built EXE entry to a DLL import

MSVC assembles a small CRT-free [EXE entry body](../tests/cpu/exe_entry.asm) and links it with the existing `call_mix` fixture. The [translator](../tools/cpu_exe_aot.py) takes the fixture's `native_entry` export through `--entry-export`, reads its unwind-bounded instruction range, follows the relative call to `call_mix`, and resolves its IAT slot against `cpu_import_provider.dll!mix_args`. It emits both functions as C++. The translated EXE uses ordinary Windows C++ startup to enter the generated `main`. Both versions use the same native provider and the same I/O DLL.

The [Windows test](../tests/cpu/exe_entry_check.py) sends one byte to each child. The provider records its two incoming integers and writes the computed word through its third argument. The I/O DLL sends the recorded integers to stderr and the eight output bytes to stdout. The test reads both pipes and the process exit code.

| Input byte | Integers received by the DLL | Output bytes in write order | Exit code |
| --- | --- | --- | --- |
| `00` | `0x100000303`, `0xfffffffd` | `11 03 00 00 00 00 00 00` | `0x60d` |
| `01` | `0x30`, `0x06` | `35 00 00 00 00 00 00 00` | `0x57` |

A second build flips the selector's conditional jump. The same translator compiles that EXE, and both inverted programs must exchange the two rows in all three observations. Empty input returns 2 with both pipes empty in all four programs. The ordinary process launch and the first exit code follow the contributor's [mapped-process experiment](https://github.com/R0Y00/Xbox2Windows/issues/165). That experiment covers TLS startup separately.

```powershell
cmake --build --preset dev --target cpu_exe_original_lifted cpu_exe_inverted_lifted cpu_exe_pair_lifted cpu_exe_swapped_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_exe_(entry|call_targets)$' -V
```

The two-target fixture gives each branch its own direct call site. One caller adds seven to the first import argument and the other adds nine. Swapping the targets while keeping the selector and incoming arguments fixed changes the imported arguments, output bytes and process status for each input. Both native and translated variants are checked against these expected observations.

The [nested-call fixture](../tests/cpu/exe_nested.asm) sends the same inputs through an extra internal caller before reaching `call_mix` and its DLL import. The translator follows direct dependencies by RVA and emits each function before its callers. Internal functions use the same two integer arguments, trace pointer and return value as the original fixture. Recursive calls are rejected. The control bypasses `call_mix` and calls the import directly, changing the import arguments, output bytes and exit status for both inputs.

```powershell
cmake --build --preset dev --target cpu_exe_nested_lifted cpu_exe_nested_bypass_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_exe_nested_calls$' -V
```

### Running the same EXE under pe_map

`cpu_native_exe_chain` gives the original EXE and its AOT build the same selector byte as above. A third child maps that exact original EXE, binds its ordinary imports to the two source-built Windows DLLs and calls `pe_map_run`. The child passes the guest's returned DWORD back as its process exit code. All three paths produce the same 16 argument bytes, eight output bytes and exit codes in the table.

`cpu_native_exe_frame_chain` adds a direct call inside the EXE before the selector branch. The existing `call_bare` fixture saves RBX, reserves home space, calls the unexported leaf `bare_mix`, writes its result through the trace pointer and restores its frame. The linker map supplies the leaf bounds for this EXE's SHA-256. The AOT build compiles the switch translator's generated caller and leaf alongside the translated entry. With `y = 5`, the leaf returns `5 * selector - 5`. On Windows x64, selectors 0 and 1 therefore took the opposite paths to the original fixture on all three runners, producing exit codes `0x57` and `0x60d` respectively. Bypassing the framed call changes the import arguments, eight output bytes and exit code for both inputs.

```powershell
cmake --build --preset dev --target cpu_exe_framed_lifted cpu_exe_original_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_(frame_)?chain$' -V
```

`cpu_native_exe_image_chain` extends this same call path into writable image data. The no-unwind leaf reads a RIP-relative bias, stores `5 * selector - 5 + bias` in the image, and reloads the result to select the DLL call. The AOT EXE uses a live mapping of the source EXE away from its preferred base. It checks the complete 64-bit value in that mapping before release. Only the generated code executes the entry and leaf in this runner.

The Windows process, the mapped original and the AOT EXE match the two DLL arguments, eight output bytes and process exit. With the linked bias of 5, selectors 0 and 1 return `0x60d` and `0x57`. Changing just the writable bias to 0 in a copy of the original reverses those results with the same AOT binary. Both the range manifest and generated sources come from the unmodified build.

```powershell
cmake --build --preset dev --target cpu_exe_image_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_image_chain$' -V
```

The first host dependency appears during IAT binding, before any guest instruction runs. Before the startup wrapper was added, the tested MSVC build with an empty binding table stopped at `cpu_exe_io.dll!emit_trace`. Supplying just the I/O DLL stops at `cpu_import_provider.dll!mix_args`. The first import invoked at runtime is `read_selector`.

The PE entry points to a [startup wrapper](../tests/cpu/exe_startup.asm). It calls the same return-based body in both cases. After it returns, the I/O DLL compares the fixture's mapped base with the main process module. A native process exits with the body's DWORD via `ExitProcess`. A host mapping returns that DWORD to `pe_map_run`. The regression starts a worker that signals readiness and stays alive. It checks process termination, both pipe EOFs and exact output and exit status for both selectors and empty input. A separate marker proves that the host gets control back after running the exact same image. A temporary copy with its startup RVA set directly to the body is the negative control, whose live worker and open pipes are sampled before cleanup. [Issue #281](https://github.com/R0Y00/Xbox2Windows/issues/281)

`cpu_exe_start_original` and `cpu_exe_start_no_tls` use the same wrapper. Their PE entry is the unchanged [`exe_startup.asm`](../tests/cpu/exe_startup.asm), which calls the body (`native_entry`) and passes its DWORD to `cpu_exe_io.dll!finish_exe`. The body also stores that DWORD in the exported `body_status` (sentinel `0xFEEDFACE` until it has returned), so a check can read the body's result from a process that never ends. Besides `cpu_import_provider.dll!mix_args` the fixtures import `finish_exe`; `cpu_native_exe_start` compares the sorted import requests of both mapped EXEs and of the lifetime control's mapped run, so the wrapper's binding is checked while the `mix_args` request stays checked. TLS callback order and the no-TLS control are unchanged.

The lifetime control copies each EXE next to `cpu_exe_io.dll` and a `cpu_import_live` provider installed under the imported name `cpu_import_provider.dll`. Inside `mix_args` that provider starts a worker. The worker signals readiness, outlives the body's return by about 2 s and then ends by itself with code 0. The test observes three things separately: the body's return, the native process handle becoming signaled with its exit code (`0x60d` with TLS digits 1, 2 for the original, `0x20d` with digit 2 without TLS, because `finish_exe` ends the process while the worker is alive; in the run on 22 November 2026 the original exited after 42.3 ms and the no-TLS EXE after 35.5 ms, so the worker's 2 s lifetime is not waited for), and the TLS digits decoded from that code. A successful native run also requires both pipes to reach EOF without output. It also checks that the mapped host regains control with the same value from `pe_map_run`. If the process does not end, `exe_process.launch(..., on_unfinished=...)` calls a hook while the process is still alive. The hook locates the EXE among the process's modules by file path and polls `body_status` with `ReadProcessMemory` for up to 10 s until it leaves the sentinel, so the message names the body's return or reports a lingering process (with the entry thread's state and the live threads) next to it. A process that ends with a different code is reported with that code, the elapsed time and the decoded TLS digits next to the expected ones. `finish_exe` runs on the main thread until `ExitProcess`, and a worker that ends does not set the exit code while that thread is alive, so the worker's 2 s lifetime does not make a correct build fail.

The unwrapped control accepts two diagnostics, `process exited 0x0` and `body returned 0x60d` with a lingering process. The second relies on the loader not calling `ExitProcess` after the entry point returns, which was measured only on Windows 11 build 26200. The simple reference starts (original, no TLS) go through the same observation without a worker, and a fifth control (synthetic) feeds the late-exit report a launch row that ended after the hook, and a sixth runs the hang build of `finish_exe` without a worker (`body returned 0x60d`, lingering process). If the process ends between the deadline and the diagnostic hook, the message reports the late exit code instead of a lingering process.

Four negative controls run with the same live worker. In each, the check must fail with the named diagnostic: a temporary copy of the EXE whose PE entry point is the `native_entry` export, so nothing ends the process (`process exited 0x0` after about 2 s, the worker's code, or in 1 of 25 repeated runs a real lingering process: `body returned 0x60d`, entry thread ended, process handle not signaled after 10010 ms with two live threads); a `cpu_exe_io_hang` build of `exe_io.cpp` whose `finish_exe` sleeps forever, installed as `cpu_exe_io.dll` (`body returned 0x60d` and a lingering process); the same process with `body_status` read at an unmapped address (`body state unknown` and a lingering process); and `CPU_EXE_TEST_BODY_HANG=1`, which makes the live provider's `mix_args` block (`body did not return` and a lingering process). The last three simulate failures that were not seen in a real fixture; the unreadable-address control tests the diagnostic branch rather than a natural read failure.

Measured on 22 November 2026, Windows 11 build 26200, MSVC 19.51, CPU-only test, one `ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' -V` process per run. Unpatched `5889876` (clean detached worktree, no local change): 78 of 80 runs passed and 2 failed. Both failures were in the native launch before any mapped run, with `native launch: original 0x0, without TLS 0x0` (run 9, 60.2 s) and `native launch: original 0x0, without TLS 0x20d` (run 68, 30.2 s), followed by `native launch does not run the TLS callback (null reserved) before the entry point`; a passing run takes about 1.2 s. With this change, `ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' --repeat until-fail:40` passed 40 of 40, about 15 s per run. That bounds the failure rate only to roughly 7 % at 95 % confidence and does not show it is gone. Control without the fix: the same checker against fixtures that keep `body_status` but start at the body (`/ENTRY:native_entry`, no wrapper) in a scratch worktree failed with `original: process exited 0x0 after 2061.6 ms, expected 0x60d (TLS/entry digits [], expected [1, 2])`, the reported symptom of an EXE that ends with exit code 0 instead of the entry result. The committed controls run on every pass, for example `unwrapped entry control: rejected as expected: original: process exited 0x0 after 2041.8 ms, expected 0x60d (TLS/entry digits [], expected [1, 2])` and `hanging finish_exe control: rejected as expected: original: body returned 0x60d (TLS/entry digits [1, 2]); native process handle not signaled after 3001.9 ms (lingering process; ...)`.

The cause of the exit code 0 on the unpatched fixtures was examined with a throw-away loop of `exe_process.launch()` over both unmodified EXEs (4626 launches in three runs, 2 failures, both in the no-TLS EXE). In one failure, with the launch deadline at 10 s, the process was still alive, its entry thread had ended with `0x20d` (the correct entry result) and exactly one other thread was alive. That thread started at the same ntdll address as an ordinary Windows thread-pool worker and had been created 3.6 ms after the process, before user code. In the other failure the process ended by itself with exit code 0 after 30.03 s. This fits an idle loader or thread-pool worker that keeps the process alive after the entry point returns without `ExitProcess`, with the worker's code 0 becoming the exit status after its idle time of about 30 s. It is a hypothesis: the worker was identified only by its start address, the two observations come from different failures, and the earlier 60.2 s failure was not reproduced.

`cpu_native_exe_two_providers` exercises two ordinary named imports from the same internal caller. The caller feeds the result from `cpu_import_provider.dll!mix_args` into `cpu_exe_second_provider.dll!accumulate_args`. The translator accepts the second provider through an explicit `--provider DLL SYMBOL` binding and verifies each used import against the corresponding DLL. Both calls use two integer arguments and the same three-word trace ABI. Windows, `pe_map`, and AOT are compared against independent expected arguments, output bytes, and exit status for both selectors. Removing the second mapping binding stops before entry, and the translator reports the missing import when its binding is omitted or names the wrong DLL.

```powershell
cmake --build --preset dev --target cpu_exe_two_providers_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_two_providers$' -V
```

```powershell
cmake --build --preset dev --target pe_map cpu_exe_original_lifted
ctest --preset dev --no-tests=error -R '^cpu_native_exe_(chain|termination)$' -V
```

## Pipeline readiness from shader manifests

`tools/pso_readiness.py` joins the native SHA-256 and stage in the shader extraction manifest with explicit pipeline references. It counts archive records, records with a native hash and unique stage/hash pairs separately. Multiple archive records can refer to the same native code. A hash from one stage does not establish coverage in another stage.

Export the archive manifest with `extract_shaders.py --format native` or `--format both`. Supply a separate JSON file containing pipeline IDs, their effective root signature status and shader references. Here's a synthetic example. The zero hash stands for a missing archive entry.

```json
{
  "pipelines": [
    {
      "id": "example-pixel-pipeline",
      "effective_root_signature": "unknown",
      "shaders": [
        {
          "stage": "pixel",
          "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
        }
      ]
    }
  ]
}
```

Set `effective_root_signature` to `present`, `missing` or `unknown` after accounting for both the embedded signature and any signature provided by the caller. The report accepts normalized references from an inspection tool or a trace and assigns no references from archive proximity.

```powershell
python -B tools/pso_readiness.py out/shaders/manifest.json out/pipelines.json
cmake --build --preset dev --target pso_root_check
ctest --preset dev --no-tests=error -R '^pso_(readiness_check|root_signature_warp)$' -V
```

Add `--llvm <AMDGPU LLVM library>` to disassemble referenced native pixel code and run the existing pixel scanner and lowering probe. Each unique stage/hash pair is assessed once. A known missing root signature takes precedence over shader diagnostics. Missing references, unassessed stages and unknown signature status require more evidence. Passing the pixel probe leaves the pipeline awaiting PSO validation, since the scanner uses synthetic bindings.

The WARP control compiles the source-built compute shader with its root signature stripped. D3D12 rejects creation without a root signature. The same shader creates a PSO when given the matching signature from the embedded build. The control measures PSO creation on the source-built fixture.

### Shader census tiers and what each percentage divides by

Three counts describe different populations, so each one is named here with its source and its key. A percentage is only comparable with another that divides by the same one.

| Count | Population | Source | Key |
| --- | --- | --- | --- |
| 26,503 accepted archive records | Named records in the plaintext archive, before the PSO cache | Independent byte-by-byte enumeration of named records with validated DEFLATE and DXBC framing; `tools/shader_profile_audit.py` reproduces the accepted count through `resource_inventory.scene_records` | End offset plus the 16-byte DXBC digest at container bytes 4..20 |
| 25,889 distinct DXBC header digests | The same 26,503 records, counted by container header | Same enumeration | Container bytes 4..20 alone |
| 21,522 cache index entries | Programs in the console PSO cache, a separate population behind the archive | The 8-byte entries that follow the 0x80 header and the 25,471 × 0x70, 2,693 × 0x18 and 54,576 × 8 tables on cache page 0 | The entry itself: a 4-byte offset from a code-area base followed by a 4-byte allocation span |

The profile tables in this document divide by the 26,503 accepted archive records, and each per-stage row divides by that stage's own count (PS 12,101, CS 2,689, MS 9,282, VS 2,236, GS 195). `tools/shader_profile_audit.py` counts those records and caches classification by the full-container SHA-256. Enumerating a start at every byte yields 26,721 accepted candidates; 218 of them are re-detections, leaving 26,503 records and 25,889 digests. Swapping 26,503 records for 25,889 digests would move a denominator by 614 without measuring anything new, so the two keys stay apart: one selects records, the other selects container bytes.

The 21,522 cache entries sit on the other side of the archive. The last named record ends 170,812 B before the cache begins, and the two disk ranges do not overlap. 21,044 was the earlier extractor result. It was revised because that scan assumed a fixed 40-byte `S_CODE_END` tail and a terminating `S_ENDPGM`, and 478 of the entries fell outside that scan's assumptions: 419 carry the standard 8-byte gate and a normal `S_ENDPGM` yet 407 of them end with a 20-byte tail and 12 carry no `S_CODE_END` fill at all, 58 begin directly with the program body without the gate, and one has no terminator. A further 4,485 programs are not aligned to any old record at all: each starts in the gap between two old records and its tail runs on into the next old record, so the record that follows it is bounded wrongly at both ends. Boundaries now come from the index, which gives 21,522 programs. 26,091 is the number of `S_ENDPGM` instructions in the code area and is not a count of records.

The 218 re-detections are suffix aliases. Each one starts inside the name of the previous record, at a byte whose value equals its distance to the name's terminating NUL, and shares that record's terminator, header and payload. The count does not change either way, because de-duplication keeps the earlier start whenever the end offset and the digest match.

### Reporting shader census populations

`tools/shader_census.py` reports accepted record occurrences, distinct header
digests and full-container SHA-256 counts without running LLVM or changing
`shader_profile_audit.py` output. It reuses the same `scene_records` acceptance
and scan order as the profile audit. With an already decoded cache from the
same build, it also reports index entries, distinct `(code_offset,
allocation_size)` spans and alias counts. The supported cache profile starts
with bytes `50 53 00 09`; the four u32 counts at byte 80 describe 0x70-byte,
0x18-byte, 8-byte and index-entry tables following a 128-byte header. Each index entry is
a little-endian offset and size pair, relative to a caller-established code-area
base. The tool does not guess that base or decompress cache streams.

```powershell
python -B tools/shader_census.py local.archive
python -B tools/shader_census.py local.archive --cache-blob out/decoded.cache --code-base 0x2100000
ctest --preset dev --no-tests=error -R 'shader_metadata|dxcontainer_part_offsets' -V
```

The base in the example is illustrative: replace it with the independently
established offset for that cache. Missing cache input reports `null`, not zero.
The parser checks table extents, a limit of 1,048,576 index entries, nonzero
in-range allocations, 16-byte relative offset alignment and distinct-span
overlaps. Exact aliases remain separate index entries. It does not validate the
programs inside allocations or prove that two supplied inputs share a build.

Run `python -B tests/shader_census_check.py` without local archives or a GPU.
Its source-built framing fixture has four accepted records, two deliberately
colliding header digests, three complete-container hashes and five cache index
entries. It is not executable DXIL. Rejection controls cover malformed cache
tables, allocation bounds and overlaps, invalid archive framing and CLI input.
Separate controls verify alias accounting, missing-cache output and that the
unchanged profile audit still counts all four occurrences while classifying
only the three full-container hashes.

Observed with Python 3.14.4 on Linux: all seven census test methods and all
seven DXContainer controls passed. On Windows 11 build 26300.9974, MSVC
19.44.35230 and Python 3.12.8, the CTest filter above passed 4/4 tests, including
the existing native metadata probe, with no skips. No archive totals were
remeasured for this change.

### Fixed-profile archive rescan

The fixed profiles previously verified 861 records. The Windows readback run at `1aafb0f` revisited all 26,503 records and added a packed RGB PS. Reclassification at `f94c254` reproduced the same 862 passes. A final full scan at `26544c6` reproduced every previous accepted source byte for byte and added one texture-sampling PS. The table incorporates its separate Windows readback.

| Stage | Records | Desktop DXIL | D3D12 pipeline | Output matched on both adapters | Change in each verified column |
| --- | ---: | ---: | ---: | ---: | ---: |
| PS | 12,101 | 849 | 849 | 849 | +2 |
| MS | 9,282 | 2 | 2 | 2 | 0 |
| CS | 2,689 | 12 | 12 | 12 | 0 |
| VS | 2,236 | 0 | 0 | 0 | 0 |
| GS | 195 | 0 | 0 | 0 | 0 |
| Total | 26,503 | 863 | 863 | 863 | +2 |

Each archive record counts once across profiles, inputs and adapters. Generated variants and repeated copies of the same native program can serve multiple records. The graphics checks use controlled partner stages, descriptors and draw inputs. The compute checks use a fixed raw UAV with an independent expected word array.

- **PS.** The accepted sets were disjoint in this input and covered 336 general color, 35 interpolated attribute, 354 two-table depth and 124 sampled coverage. Of the remaining records, 837 stopped at the native program boundary or its control-flow checks and 10,415 reached lowering without matching a binding profile. For those 10,415 records, the first general configuration alone (table s8, interpolation s9, fixed sampler, no CB) first rejected 2,927 for an unresolved constant buffer and 2,007 at `s_setreg_imm32_b32`. A different binding can expose a different next instruction.
- **CS.** The first rejection was unsupported thread-group dimensions for 1,054 records, an unverified multi-exit program for 1,002 and the fixed store lowering for 621. The 12 successes were tested with 320-word readbacks in 14 dispatches on each adapter.
- **MS.** All 9,282 programs supplied a bounded span. As in the preceding survey, the point profile targeted the shortest program (832 bytes) and the indexed profile examined the 531 programs at most 1,200 bytes long. The length-first survey counted 8,751 longer programs. The audit CLI checks the entry before applying the size filter. It rejected 309 of those longer programs for multiple exits. The other 8,442 reached the size filter. All 531 shorter entries passed entry validation. Within the 531, 427 records (24 native streams) first read entry register `s23` without an established binding. One indexed record reached a visible triangle. The shortest, degenerate case was checked using exported positions and indices as well as raster pixels.
- **VS and GS.** Neither stage has a package translator in these profiles. Their first stop is stage dispatch.

Windows 11 build 26200.9974, D3D12 debug layer enabled, NVIDIA GeForce RTX 4060 Laptop GPU 32.0.16.1656 and WARP 10.0.26100.9818. Both adapters completed every accepted record. The sampled coverage group used two separate selector bindings. The mesh and depth checks retained their indexed-export and swapped-table controls.

To repeat the inventory with a local archive and an LLVM 15 library built with AMDGPU support, run the following from the repository root. Use a fresh output directory for each revision. The optional source directory contains local HLSL indexed by profile and decoded container hash. The JSON counts HLSL acceptance; DXC, pipeline construction and readback are separate measurements.

```sh
python3 -B tools/shader_profile_audit.py "$ARCHIVE" --llvm libLLVM-15.so.1 --source-dir out/profile-sources > out/profile-counts.json
```

The first additional PS reads a packed word from the first constant buffer. Its low three bytes select RGB and each byte goes through the piecewise sRGB-to-linear conversion before a round-toward-zero binary16 export. The new readback reference evaluates the scalar transfer function independently. Inputs include byte values 10 and 11 on opposite sides of the threshold, distinct channel values and a change to the unused high byte. All six positive draws across the two adapters matched. All six rotated-channel references failed at pixel comparison.

With the archive scan complete and `offscreen_check` configured, run the following in Developer PowerShell. The source selector identifies the three-byte conversion in this measured corpus and rejects a missing or ambiguous match. The other profiles use the draw and dispatch fixtures documented in their experiment sections.

```powershell
$packed = @(Get-ChildItem out/profile-sources/general/*.hlsl | Where-Object {
    (Get-Content -Raw $_.FullName).Contains('(asuint(params[0].x) >> 16u) & 0xffu')
})
if ($packed.Count -ne 1) { throw "Expected one packed-color PS for this input" }
dxc -Gis -T ps_6_0 -E main -Fo out/packed-rgb.dxil $packed[0].FullName
if ($LASTEXITCODE -ne 0) { throw "DXC failed" }
cmake --build --preset dev --target offscreen_check
python -B tests/packed_rgb_case.py out/build/dev/offscreen_check.exe out/packed-rgb.dxil hardware
python -B tests/packed_rgb_case.py out/build/dev/offscreen_check.exe out/packed-rgb.dxil warp
```

The second additional PS samples RGB and supplies a constant 1 for alpha. The color ramp check covered 4,096 pixels, and the transparent black input produced opaque black for all 1,024 pixels. Both adapters caught red/blue exchange on the ramp and rejected a reference that copied the input alpha. To repeat on the final scan, select the local source with that three-channel sample export and run the existing draw modes:

```powershell
$opaque = @(Get-ChildItem out/profile-sources/general/*.hlsl | Where-Object {
    (Get-Content -Raw $_.FullName) -match 'return float4\(export16\(sample\d+\.x\), export16\(sample\d+\.y\), export16\(sample\d+\.z\), export16\(alu\d+\)\);'
})
if ($opaque.Count -ne 1) { throw "Expected one sampled RGB / constant alpha export" }
dxc -Gis -T ps_6_0 -E main -Fo out/opaque.dxil $opaque[0].FullName
if ($LASTEXITCODE -ne 0) { throw "DXC failed" }
python -c "import struct,pathlib; p=pathlib.Path('out'); (p/'opaque.case').write_bytes(bytes(256)+struct.pack('<4f',0,0,0,1)*1024); (p/'copied-alpha.case').write_bytes(bytes(256+16*1024))"
foreach ($adapter in 'hardware','warp') {
    out/build/dev/offscreen_check.exe $adapter --check-pixel out/opaque.dxil rgba
    out/build/dev/offscreen_check.exe $adapter --check-pixel-attribute out/opaque.dxil out/opaque.case
    # Each control below should exit 1 with pixel_mismatch.
    out/build/dev/offscreen_check.exe $adapter --check-pixel out/opaque.dxil bgra
    out/build/dev/offscreen_check.exe $adapter --check-pixel-attribute out/opaque.dxil out/copied-alpha.case
}
```

### First unresolved constant-buffer read

At `26544c6` the first general profile stopped at an unresolved constant buffer for 2,927 PS records after all profiles had been tried. These records contained 1,925 distinct native streams. All first stops were 16-byte scalar descriptor loads. Tracking scalar moves and the zero high word back to entry gave 1,896 records using s8, 1,011 using s9 and 20 using s10.

| First descriptor location (entry pointer + bytes) | Records |
| --- | ---: |
| s9 + 0 | 565 |
| s8 + 0xa80 | 430 |
| s8 + 0xac0 | 379 |
| s8 + 0x6e0 | 268 |
| s9 + 0x20 | 229 |
| Other locations | 1,056 |

Across the original cohort, the straight-line trace established a completed first consumer for 1,765 records. Immediate-offset reads comprised 508 one-word, 69 two-word, 604 four-word and four eight-word reads. Another 580 read one word with an SGPR offset. For 1,162 records the trace stopped before it could establish a completed first consumer.

In the largest group, **529 of 565** records load the descriptor at byte zero of the table supplied by s9, wait for it, then read four DWORDs from that buffer at bytes 16, 20, 24 and 28. The Windows probe binds that slot to root CBV b0 and a distinct buffer to b1. These are explicit desktop bindings. The PSV0 declarations vary across the group. They do not establish one original HLSL register and space for the traced native slot.

The diagnostic PS exposes the four traced words as repeating depth columns. Its diagnostic export makes each word observable independently of the remaining shader. All other words contain a sentinel. Changing b0 changes the expected picture, changing b1 alone preserves it, and swapping the GPU addresses produces 1,024 mismatches against the old picture and zero against the swapped reference. Both RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818) passed the 32×32 D32 readbacks on Windows 11 build 26200.9974 with the D3D12 debug layer enabled.

The full-record scan at `c849f7a` used the original bindings. It had 2,928 records in the first-profile unresolved-CB group. Binding s9 and descriptor byte zero in the existing unsampled profile yields zero complete PS from the largest group. Its most frequent next stop is `v_cvt_u32_f32_e32`. The diagnostic is counted separately.

| Stage | Records | DXIL | D3D12 pipelines | Matched output on both adapters | Delta from `26544c6` |
| --- | ---: | ---: | ---: | ---: | ---: |
| PS | 12,101 | 851 | 851 | 851 | +2 |
| CS | 2,689 | 12 | 12 | 12 | 0 |
| MS | 9,282 | 2 | 2 | 2 | 0 |
| VS | 2,236 | 0 | 0 | 0 | 0 |
| GS | 195 | 0 | 0 | 0 | 0 |
| Total | 26,503 | 865 | 865 | 865 | +2 |

Both additional PS already translated on the new base, using explicit level-zero texture sampling. Each passed the color ramp and rejected a red/blue exchange on each adapter. All previous accepted identities remained present. Across the previous 986 distinct profile/container source files (including selector variants), 616 generated sources were byte-identical and the remaining 370 produced byte-identical desktop DXIL with the same DXC flags. Their existing readbacks therefore cover the same executables.

At `4c3b573` the first-profile unresolved-CB group held 4,411 records in the same input, because the scratch-base register writes no longer stop these programs earlier. A local linear trace of scalar moves and loads found that 81 of them read one 16-byte descriptor through `s_buffer_load_*`, 140 read two, 90 read three and 4,099 read four or more (at most 32), and that 3,711 take their descriptors from three entry registers, 606 from two and 93 from one. With one bound descriptor, whichever table and offset is chosen, 3,565 records next stop at a descriptor load from another entry register and 144 at another offset of the same table.

Binding every entry-table descriptor that the trace saw read through `s_buffer_load_*`, with the `extra_constants` contract described under scalar buffer loads below, takes 3,463 of the 4,411 records past every constant-descriptor stop. Two complete, both with a single bound descriptor at an offset outside the fixed audit profiles. The others next stop at a read of an unbound entry SGPR (1,553), a `v_mov_b32` whose source is not a tracked numeric value (692), an unbound pixel input at `v_cvt_u32_f32_e32` (624), `v_interp_p1_f32_e32` outside the accepted contexts (162), a register-offset `s_buffer_load_dword` (97), an unbound texture descriptor offset (96) and smaller groups. Of the remaining 948, 582 stop at a 16-byte descriptor that no `s_buffer_load_*` reads (a candidate vector-buffer descriptor), 293 at a descriptor whose table address the tracker cannot follow to an entry register, 72 at an immediate read beyond byte 256 and one has no traced descriptor. The trace is linear, 4,250 of these programs branch, and the trace only proposes bindings: `lower` still verifies each load. The fixed audit profiles are unchanged, so the audit totals do not move. The counts say how far lowering proceeds under explicit bindings. They do not establish what a title binds to any of these slots.

Run the fixed scan with LLVM 15, then pass the emitted diagnostic to the Windows readback runner. The CTest exercises the same access using a source-built fixture.

```sh
python3 -B tools/shader_profile_audit.py "$ARCHIVE" --llvm libLLVM-15.so.1 --cb-reads --source-dir out/cb-scan > out/cb-counts.json
```

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_cb_read_' -V
foreach ($adapter in 'hardware','warp') {
    python -B tests/pixel_cb_read_check.py dxc out/build/dev/offscreen_check.exe $adapter out/cb-scan/s9-slot0-read-probe.hlsl
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
```

### Unsigned pixel conversion and entry inputs

The plain gfx1013 `v_cvt_u32_f32_e32` path uses the binary32-to-uint helper from the FFX translator. It truncates positive fractions toward zero, maps NaN and negative inputs to zero, and saturates at `0xffffffff` for inputs at or above 2^32. Sources are a defined VGPR, a ready SGPR or a hexadecimal literal word. Whole-quad execution requires a source defined for helper lanes. A varying conversion cannot establish an affine texture coordinate. [AMD RDNA 2 ISA](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf) · [Shared conversion implementation](../src/ffx_float_to_int.hlsli)

On Windows 11 build 26200.9974, RTX 4060 Laptop 32.0.16.1656 and WARP 10.0.26100.9818 each passed 27 input words in both the entry and whole-quad cases. The fixture extracts four bytes from the result and renders one byte per color channel. All 256 byte values are exactly representable even through the compressed color export. The renderer compares the readback bits with four independently expected bytes at all 4,096 pixels. Inputs include signed zero, subnormals, negative fractions, quiet and signaling NaNs, infinities and both sides of the upper limit. The binary32 immediately below 2^32 yielded `0xffffff00`. Replacing truncation with nearest-even rounding made both 1.875 and 3.75 mismatch on both adapters. LLVM 15 also assembles and disassembles both fixtures in the binary regression.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_u32_from_f32_' -V
```

With the previous fixed bindings and the `--cb-reads` command above, all 530 records (459 distinct native streams) reached the conversion with an unbound source on the `92e80ed` base plus this change. The first conversion reads v3 in 447 records and v2 in 83. **Zero advanced past that instruction and zero completed.** All 530 input signatures report reads of both X and Y of `SV_Position`. The signature identifies logical inputs; assigning either component to a native VGPR still requires entry-layout evidence. In a separate diagnostic, supplying two distinct quad-defined fixture values let all 530 cross both conversions. Their next missing scalar inputs were s17 (505), s15 (23) and s14 (2).

The fixed-profile full scan covered 26,503 records. Output-verified totals stayed at 851 PS, 12 CS, 2 MS, 0 VS and 0 GS (865 overall). All 988 previous profile/container files, including selector variants, recurred. Of those, 616 had identical generated source; recompiling the remaining 372 old/new pairs with the same DXC produced identical desktop DXIL. The existing pipeline and readback results apply to the same binaries. The new conversion draws are counted as source-built fixtures.

### SPIR-V physical addresses in a D3D12 buffer window

An optional offline experiment accepts validated Vulkan 1.2 SPIR-V assembly with the named user-data and physical DWORD pointer types emitted by [Leonida at `f85582e`](https://github.com/ItsNotRexed/Leonida/blob/f85582e23d4ab9e19acbf694358f137d6b8531b1/tools/rdna2spirv.py). [SPIRV-Cross at `57e4efc`](https://github.com/KhronosGroup/SPIRV-Cross/tree/57e4efcc84112abcd426dc5ec248b5339bee04f2) rejects the physical addressing model on its HLSL path. The [window adapter](../tools/spirv_address_window.py) converts direct DWORD loads, stores and array element addresses to logical storage-buffer accesses. It validates the input and the result before publishing the output module.

The host contract supplies a read-only raw SRV at `t0, space1` containing the original packed user-data bytes and a raw UAV at `u0, space2` covering the address window. Physical addresses supplied to the adapter are byte offsets relative to this view; an upstream heap base added to these offsets must be zero. The descriptor must have at most 1,073,741,823 DWORDs. The final byte offset `0xfffffffc` lies outside the view. An address above the window, an unaligned access, or an overflowing array-index addition selects this offset. D3D12 descriptor bounds then provide a zero read or discarded write. A production runtime still needs to locate each guest allocation, establish its lifetime and supply its real descriptors. This profile makes no claim about arbitrary GPU virtual addresses or atomics.

The [source-built compute shader](../tests/spirv_window/subtract.spvasm) reads two distinct addresses, subtracts their words and writes to a third. Windows checks all 16 words of the arena, including guards, for eight cases. Swapping the two reads reverses the result; moving the store changes which guard is replaced. High addresses, misalignment, 64-bit index overflow and a write past the actual descriptor exercise the window bounds.

The SPIR-V bridge GPU tests (`spirv_{address_window,sampler_views,ballot_extract,compare_sampler}_{hardware,warp}`)
need `spirv-as`, `spirv-val` and `spirv-cross`. `tools/setup-spirv-tools.ps1` builds them from pinned
SPIRV-Tools, SPIRV-Headers and SPIRV-Cross revisions into `out/tools/spirv/bin`, where CMake looks
first. It also searches `PATH` and `$env:VULKAN_SDK\Bin`. Without the tools, the tests are still
registered and report a skip. Both Windows CI jobs build the pinned tools, cached by revision and MSVC
toolset, and set `X2W_REQUIRE_SPIRV_TOOLS=1`, which turns a missing tool or a skip into a failure.
A separate step then requires all four `_warp` tests to have passed. Physical-GPU runs stay manual.

```powershell
.\tools\setup-spirv-tools.ps1
cmake --preset dev
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_address_window_' -V
```

With SPIRV-Tools on Linux, run `python3 -B tests/spirv_window/check.py` to assemble and validate the fixture and exercise the rejection paths. This also validates a combined fixture where a lowered ballot bit controls the value stored through the address window. The adapter reserves `%x2w_` identifiers in the input and gives address and ballot temporaries separate prefixes. The GPU check can also use WSL tools: generate a logical module with `python3 tools/spirv_address_window.py tests/spirv_window/subtract.spvasm out/window.spv`, then generate HLSL with `spirv-cross out/window.spv --hlsl --shader-model 66 --output out/window.hlsl`. Compile it on Windows with `dxc -T cs_6_6 -E main -Fo out/window.dxil out/window.hlsl` and run `out/build/dev/offscreen_check.exe hardware --spirv-address-window out/window.dxil`, then repeat with `warp`.

## Offline CPU leaf recompilation

With the Python-enabled Windows dev preset configured, run:

```powershell
cmake --build --preset dev --target cpu_leaf_original_lifted cpu_leaf_inverted_lifted
ctest --preset dev --no-tests=error -R '^cpu_leaf_aot_branches$' -V
```

MSVC's x64 assembler and linker build [the original function](../tests/cpu/branch.asm) into a DLL. The [translator](../tools/cpu_leaf_aot.py) reads its exported entry RVA and the matching runtime-function interval from the PE. It translates those bytes into C++, which MSVC compiles to a separate x64 object and links into a second DLL. The intermediate source is in `out/build/dev/cpu_leaf_original_lifted.cpp`.

The supported signature takes an unsigned 32-bit integer in ECX and a writable word pointer in RDX, and returns a 32-bit integer in EAX. The decoder covers the fixture's register move, immediate compare (`imm8` sign-extended and `imm32`) paired with unsigned/equality (`JB`/`JAE`, `JE`/`JNE`, `JBE`/`JA`) and signed (`JL`/`JGE`, `JLE`/`JG`) short conditional branches, short forward jump, immediate add/subtract, output store and return. It rejects unknown encodings and branches outside the decoded instruction boundaries. Leaf functions can omit unwind metadata. This experiment uses an explicit interval provided by the assembler.

The Windows test calls both DLLs with the same 367 inputs covering `<`, `==`, and `>` at every unsigned and signed threshold, including the signed/unsigned boundary (`0x7fffffff`, `0x80000000`) and wraparound. It compares the returned word, the stored word and guards on either side, and checks the original against an independent reference model. A control assembles the same source with every conditional jump inverted (`INVERT_BRANCH`) and runs that PE through the same translator. Its results must differ from the original on every input. [Direct-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/22)

## Chained function ranges and early returns

The range reader groups runtime-function entries by their complete unwind-chain parent records. For chained trailer lookup it accepts both version 1 and version 2 `UNWIND_INFO` headers when `UNW_FLAG_CHAININFO` is the only flag set, without decoding general version-2 unwind codes. The early-return translator uses iced-x86 to decode each owned interval separately. It follows instruction-aligned branches and fallthrough, models stack slots and preserved registers, and checks their restoration at each return. A neighboring function can occupy the gap. The single-interval translators report split functions explicitly. [Microsoft chained unwind information](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64#chained-unwind-info-structures)

```powershell
cmake --build --preset dev --target cpu_chained_lifted cpu_chained_jne_lifted cpu_chained_zero_lifted cpu_chained_imm_lifted cpu_chained_frame_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_chained_ranges$' -V
```

The source-owned fixture has a primary interval, two levels of chaining and a neighboring export, built in `JE`, `JNE`, self-`XOR` zeroing, general 32-bit immediate (`edx`/`r8d`), and 32-bit immediate stack-frame (`sub`/`add rsp, 136`) variants. Both null inputs return 7 (or 0 in the `XOR`-zeroing variant), and two non-null inputs return 21. A Windows probe compares native returns with the generated result, physical RBX/RSI, and guards. A patched copy verifies version-2 chained header resolution. Negative controls cover broken and cyclic parent links, unsupported chained header versions and combined flags, an unsupported conditional branch after `TEST`, a non-self `XOR`, clobbering an unsaved callee-saved register with a 32-bit immediate, a stack frame exceeding 4096 bytes, jumps into a neighbor or instruction interior, and an indirect call.

The translator reads each `MOV` memory operand once, in guest order, into a local declared at that point, through a `volatile` load: the original faults on an inaccessible operand even when the value is overwritten before use, and with an ordinary load an optimized MSVC build drops such a read. A load whose value nothing uses is written as the declaration followed by `static_cast<void>(...)`. Reads of the modelled stack slots are not emitted, and the table and allocator words that a bound callback or destroy call compares keep their ordinary read, since the comparison uses the value.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_aot_chained_dead_reads$' -V
```

The [dead-read fixture](../tests/cpu/chained_dead_read.asm) exports `read_dead` (a load overwritten by `MOV RAX, RDX`), `disp_dead` (a load at RCX + 8 into a register nothing reads), `pair_dead` (two loads, neither used), `read_skip` (a load skipped when y is zero) and the control `read_live`, which returns the value read. Each export is translated on its own and built by the project's build and again by a separate `cl /O2` command. `cpu_aot_chained_dead_reads` calls the original and both translations inside `__try` on a fresh page followed by an inaccessible page, and compares the structured exception code and the result. A readable operand returns the reference. A null operand, and an operand whose last read crosses one byte into the inaccessible page, raise an access violation in the original and in both translations; reads that end exactly at the page boundary return. On the path that skips the read, the same inaccessible addresses return normally. A source control checks that each memory operand is read once through the `volatile` load. An unoptimized build performs an ordinary load as well, so the `/O2` builds are the ones that show the difference.

For the local FidelityFX loader, run `cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_context.cpp --partial` using the Python dev environment. Compile the generated C++ as a Windows x64 DLL. Its `try_lifted(first, second, output)` returns 1 and writes the guest result for a completed path. Status 0 leaves the output untouched at an unsupported indirect call. Pointer arguments must reference readable fixture storage on paths that dereference them. The local inspection recovered four owned intervals. Running the generated code produced 6 for a null first argument and for a pointer to a null context. A valid non-null control stopped at the first indirect call with its output sentinel intact. The original package DLL was inspected as a file. Native differential results above are from the repository fixture. [Callback follow-up](https://github.com/R0Y00/Xbox2Windows/issues/29)

### Object-table callbacks on Windows

The source-built caller follows a handle to its context and loads a table pointer from that context. Slot 1 takes the context address and a 64-bit input and returns a 64-bit value. The test selects two tables with the providers in opposite orders. It compares the observed provider, both arguments, return and output guards with native Windows execution, including inputs with bit 63 set. Both null paths return 6 without calling either provider.

```powershell
cmake --build --preset dev --target cpu_callback_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_object_callback$' -V
```

For this fixture, `cpu_chained_aot.py` takes `--callback-offset 8` and emits `try_lifted_callback(first, second, output, binding)`. The host supplies readable fixture storage and a binding containing the expected table address and typed function pointer. Dispatch requires both the table address and the selected slot's target to match. A missing or mismatched binding returns status 0 and leaves the output untouched. The existing three-argument partial profile still stops at an unbound indirect call.

### FidelityFX destroy provider and cleanup

In the inspected Xbox loader, `ffxCreateContext` masks the effect ID and calls the internal resolver at RVA `0x1000`. For effect `0x10000` the resolver gets or loads `amd_fidelityfx_upscaler_xbox.dll`. It resolves the named export `ffxDestroyContext` and stores the address at loader RVA `0x4098`, slot 1 of the table at `0x4090`. After a successful provider create, the loader puts that table pointer and the provider context in the two words of a wrapper. The packaged upscaler export directory independently identifies the destroy entry at provider RVA `0x13a0`. The frame-generation route uses another table and its module is absent from this package.

The loader passes the **address of the wrapper's provider-context field** in RCX and the optional allocation-callbacks pointer in RDX. The upscaler dereferences the first argument and saves the second in a local slot. Its internal destroy method receives the effect object, the context-field address and the address of that saved allocator pointer. On the provider-object release path the upscaler also uses the allocator for deallocation or calls CRT free. These register uses agree with AMD's two-argument destroy declaration and unsigned 32-bit status. [AMD FidelityFX API header](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

After provider destroy, the **loader** frees its own wrapper, including when the provider returned an error. Custom cleanup receives the allocator's user data and the wrapper address. The default branch calls the imported CRT free. Both branches return the saved provider status. The explicit `--ffx-destroy` profile binds a stable provider table, allocator and cleanup routines for the duration of the call. The host must supply live storage compatible with the chosen deallocator. An unresolved binding stops before invoking the provider.

```powershell
cmake --build --preset dev --target cpu_destroy_lifted cpu_destroy_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_destroy_contract$' -V
```

CI compares a source-built native caller with its static translation. On Windows the same harness also compared that native reference with the translation of all four owned ranges of the local loader export. Both null exits returned 6. With a source-built provider resolved by export name, both cleanup routes preserved 0, 6, 0x80000001 and 0xffffffff. The test checks the addresses passed to destroy and custom cleanup, and that the provider clears the inner field before the wrapper is released. The packaged upscaler's own effect teardown was inspected statically.

To repeat the local comparison in a VS x64 developer shell, translate the loader with `tools/cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_destroy.cpp --ffx-destroy`, compile it with `cl /nologo /LD /O2 /Fo:out/local_destroy.obj out/local_destroy.cpp /link /OUT:out/local_destroy.dll /IMPLIB:out/local_destroy.lib`, and run `tests/cpu/destroy_check.py` with absolute paths to `cpu_destroy.dll`, `local_destroy.dll` and `cpu_destroy_provider.dll`, in that order.

### First device calls in the packaged upscaler

For the upscaling effect (`0x10000`), the local upscaler's `ffxCreateContext` export at RVA `0x11d0` takes an output handle pointer in RCX, a descriptor chain in RDX, and an optional allocator in R8. On the inspected path, the backend extension has type `0x01000002` at offset 0, a next pointer at +8 and the device pointer at +16. The provider selection call at `0x129c` passes that pointer to a predicate that returns true without reading it. At `0x1349` the export calls the selected provider's create method at `0xc9b0`.

The provider reads the device from the extension at `0xcdca`, sets up a fresh 0x18f160-byte scratch buffer for one context and invokes its backend callback at `0x2720`. At the call site RCX points to the backend interface, EDX is `0x14`, R8 points to four 32-bit words `{0, 0, 0, 1}`, and R9 points to storage for the output context index. The callback reads the device at interface +0xd0 and scratch at +0xc0. On its first use, the device receives `IUnknown::AddRef(this)` at `0x2803`. It takes only the device in RCX. The callback stores the device at scratch +8 and continues without inspecting the returned reference count.

The first resource creation at `0x2893` uses device vtable slot 14 (+0x70). RCX holds the device, RDX points to a 16-byte `D3D12_DESCRIPTOR_HEAP_DESC`, R8 points to the IID of `ID3D12DescriptorHeap` (`8efb471d-616c-4f49-90f7-127bb763fa51`), and R9 points to the output slot. All five calls request node mask 0. [D3D12 CreateDescriptorHeap](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createdescriptorheap) · [IUnknown AddRef](https://learn.microsoft.com/en-us/windows/win32/api/unknwn/nf-unknwn-iunknown-addref)

| Call RVA | Heap type | Descriptors | Shader visible | Output in scratch |
|---|---|---:|---|---|
| `0x2893` | CBV/SRV/UAV | 512 | No | +0x28 |
| `0x28ac` | CBV/SRV/UAV | 512 | No | +0x30 |
| `0x28cd` | CBV/SRV/UAV | 512 | Yes | +0x38 |
| `0x28fd` | CBV/SRV/UAV | 167,936 | Yes | +0x48 |
| `0x2935` | RTV | 8 | No | +0x20 |

On Windows the probe starts at RVA `0x27f8` with a live device, zero-initialized scratch, a context capacity of one and R15 zero. It executes the offline translation of the package's argument construction and binds the two COM methods to desktop D3D12. The RTX 4060 Laptop (driver 32.0.16.1656) and Microsoft WARP (10.0.26100.9818) each returned `S_OK` five times. The probe verified five distinct live heap handles, their output slots and their descriptions. Both debug-layer runs reported zero errors and zero warnings. The host releases the five heaps and its device reference on exit.

After the fifth heap the test continues into the backend slot scan at RVA `0x2938`. The host stops on a failed HRESULT so that a desktop failure identifies the exact heap. The package slice itself has no intervening HRESULT branches.

To run the local translation in a VS x64 developer shell, set `$upscaler` to the full path of the package's upscaler DLL and use a separate build directory. The translator checks the image hash before generating C++.

```powershell
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE:FILEPATH=$upscaler"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_device_init_' --no-tests=error -V
```

The log identifies this run with `source=translated-package`. The default build uses a source-owned desktop control for the five D3D12 calls (`source=desktop-control`). Public CI runs that control on WARP and compares native and translated x64 heap-size arithmetic for six inputs, including 32-bit wraparound.

### Backend context slot selection

The provider takes the address of its 32-bit index field at +0xec (`0xcf94`) and passes it in R9. The backend callback saves that pointer in R14. On first use it finishes setting up the shared descriptor range at `0x2938`. Subsequent calls skip heap creation and rejoin at `0x2942`. The callback increments the 32-bit scratch counter before checking capacity, then scans from index zero. The table pointer is at scratch +0x1078, records are 96 bytes each, and bit 0 of the byte at record +0x3c means occupied.

At the first free slot, the store at `0x2980` writes the index directly to the provider. The callback writes effect `0x14`, sets flag bit 0, clears bit 1 and preserves the other six bits. It initializes two pairs of descriptor cursors to `512*i+1` and `512*i+511`. On the package's one-slot input, the four sizes `{0,0,0,1}` reserve the half-open range `[0x19000,0x19001)`. When all slots are occupied, the scan branches to the same zero-return exit at `0x2a9f` without writing a record or the output index.

| Windows input | Backend return | Provider index after call | Scratch counter | Slot change |
|---|---:|---|---|---|
| Fresh package table after five heaps | 0 | 0 | 0 → 1 | Slot 0 becomes occupied |
| Same table, second request | 0 | `0xfeedface` preserved | 1 → 2 | None |
| Controlled two-slot table, slot 0 occupied | 0 | 1 | 1 → 2 | Slot 1 becomes occupied. Its range starts after slot 0 at `0x32008` |
| Both controlled slots occupied | 0 | `0xfeedface` preserved | 2 → 3 | None |

The package translation and an independently compiled C++ control ran on the RTX 4060 Laptop and WARP using the commands above. The test compares the entire scratch buffer and provider output region, including bytes around the index. The exhaustion cases supply a new output field initialized to the sentinel. The dual-slot setup exercises a nonzero index with a controlled capacity and four nonzero sizes. Public CI runs the desktop control plus a source-built x64 scan and four-lane sum in `cpu_aot_heap_layout`. The same fixture compares 32-bit registers with sign-extended 8-bit immediates such as `cmp ecx,-1`, which the lowerer evaluates at the operand width.

The provider tests EAX at `0xcfc2`. A zero backend return continues initialization. It later loads the same index field into R8D at `0xd1f4` for the resource callback at `0x2e30`. On the default path the first descriptor identifies `FSR4UPSCALER_Recurrent` (ID 13), with its allocation mode and placed-resource flag both zero and the optional allocation callback unset. The intervening descriptor-builder call at `0xc4ff` resolves to a CPU-only helper at `0x7900`, and the import at `0x2ea1` is an XMem working-set query. The next call **on the device** is at `0x33cc`, Xbox vtable slot 76 (+0x260). Its arguments include heap properties, zero heap flags, a resource description, initial state, a null clear value, the `ID3D12Resource` IID and an output pointer. Those arguments match the shape of desktop committed-resource creation. The Windows replay below binds that call to the desktop device. [Desktop CreateCommittedResource signature](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createcommittedresource)

### Creating the first recurrent texture

For the default provider, the builder at `0xbf60` receives the output-size pair in R8. The create descriptor supplies render width/height at +0x14/+0x18 and output width/height at +0x1c/+0x20. The first resource uses the latter pair and has ID 13 and name `FSR4UPSCALER_Recurrent`. The local replay translates the first descriptor stores, the provider's index-output address calculation and the default committed-resource path. It supplies zero for the optional Xbox working-set statistics and uses the default allocator route with the optional resource allocation callback unset.

| Field | Package path | Desktop D3D12 argument |
|---|---|---|
| Dimensions | Kind 2, output width and height, one layer, one mip | Texture2D, one sample, quality 0 |
| Format | Format code 10 selects value 28 in the format switch at `0x2160` | `DXGI_FORMAT_R8G8B8A8_UNORM` |
| Flags | Usage bits 0 and 1 produce flags 0x5 | `ALLOW_RENDER_TARGET` and `ALLOW_UNORDERED_ACCESS` |
| Initial state | Provider state 2 selects value 8 at `0x325a` | `D3D12_RESOURCE_STATE_UNORDERED_ACCESS` |
| Allocation | Default heap, zero heap flags, null optimized clear value | Committed resource, default alignment, single-node masks 1/1 |
| Layout | Xbox texture layout 0x101, extension word 3 and trailing zero | Driver-chosen `D3D12_TEXTURE_LAYOUT_UNKNOWN`. The adapter checks the Xbox extension fields before omitting them from the desktop descriptor |

With a render size of 1280×720, the RTX 4060 Laptop (driver 32.0.16.1656) and WARP (10.0.26100.9818) each ran two fresh contexts. For outputs of 1920×1080 and 1600×900, all four calls returned `S_OK`. Each resource's `GetDesc` reported the requested output dimensions, format 28, flags 0x5, one layer/mip/sample, quality zero, desktop layout 0 and 65,536-byte alignment. Initial state is a creation argument. The probe transitions the texture from UAV to shader read, then copy source, then back to UAV and waits for completion. Both debug layers reported zero errors and zero warnings.

On context slot 0 the provider passes +0x17c as the output for ID 13. The resource callback takes cursor 1, advances it to 2 and writes 1 to the output **before** the device call. On success it records provider state 2 in both state fields and stores the returned COM pointer in backend record 1 at `0x3408`. The test checks that pointer against the live texture and verifies all adjacent resource indices retain their sentinels.

Use the package build directory configured above in a VS x64 developer shell. Public CI runs the source-owned WARP fixture with the same desktop mapping and readback checks. It also compares native and translated descriptor-copy instructions using the MASM fixture.

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_recurrent_' --no-tests=error -V
```

### Recurrent texture views and pixel readback

After the resource pointer is stored, the callback reads its description and builds views for the single-layer, single-mip Texture2D path. The SRV and UAV format switches both preserve `DXGI_FORMAT_R8G8B8A8_UNORM` (28). The SRV exposes mip 0 only, plane 0, minimum LOD 0 and the default RGBA component mapping (`0x1688`). Both UAV descriptors select mip 0 and plane 0 and use a null counter.

| Device call RVA | Descriptor | Heap pointer in scratch | Slot in fresh context 0 |
|---|---|---|---:|
| `0x37aa` | SRV | +0x28, CPU-only | 1 (resource index) |
| `0x3844` | UAV | +0x38, shader-visible | 1 (UAV base + mip) |
| `0x388a` | UAV mirror | +0x30, CPU-only | 1 (UAV base + mip) |

The resource record stores the UAV base at +0x34 and the count at +0x38, both 1. After creating both UAV descriptors, the callback advances the context's UAV cursor at +0x0c from 1 to 2. Each destination is the heap's CPU start plus the slot times the device-reported descriptor increment. That increment was 32 bytes on the RTX 4060 Laptop and 64 bytes on WARP.

The same `ffx_recurrent_hardware` and `ffx_recurrent_warp` commands above run the pixel check. A source-built compute shader writes a coordinate-dependent RGBA pattern through the original shader-visible UAV. A second pass writes a different pattern through a copy of the CPU UAV mirror. For shader access the probe copies the CPU-only SRV and UAV mirror into a separate, shader-visible test heap. A read shader samples the SRV at texel centers and writes packed RGBA bytes to a buffer. A separate `CopyTextureRegion` reads the original resource pointer directly. After a fence, both paths must match the CPU's expected RGBA value for every pixel. [D3D12 descriptor copying](https://learn.microsoft.com/en-us/windows/win32/direct3d12/copying-descriptors)

Both adapters passed both patterns at 1920×1080 and 1600×900. Each adapter checked 7,027,200 pixels through the SRV and the same number through direct copies, with zero mismatches, debug errors or warnings. The local build executes the package-derived view creation through the cursor update at RVA `0x389b`. Public Windows CI runs the source-owned WARP control. The test shaders exercise descriptor access. The provider loop is covered below.

### Provider resource loop and returned handles

At 1280×720 render and 1920×1080 output, the default provider visits twelve candidate descriptors in order. The builder at RVA `0xbf60` emits 104 bytes per candidate. The loop at `0xd1d0` skips ID 23 when bit 9 of the create flags is clear. The index destination is `provider + 0x148 + 4 * ID`. Three callbacks also allocate an upload resource from the same cursor.

| ID | Resource (FSR4UPSCALER_ prefix) | Shape | Default index | Index with flag 0x200 |
| ---: | --- | --- | ---: | ---: |
| 13 | Recurrent | 1920×1080 RGBA8 | 1 | 1 |
| 14 | History | 1920×1080 RGBA16F | 2 | 2 |
| 15 | HistoryReprojected | 1920×1080 RGBA16F | 3 | 3 |
| 16 | RcasIntermediary | 1920×1080 RGB9E5 | 4 | 4 |
| 5 | AutoExposure | 8-byte raw buffer | 5 | 5 |
| 17 | DefaultExposure | 1×1 RG32F | 7 | 7 |
| 6 | SpdAtomicCounter | 1×1 R32_UINT | 9 | 9 |
| 7 | Luma_Mip_5 | 80×45 R32F | 11 | 11 |
| 22 | ScratchBuffer | 21,045,760-byte raw buffer | 12 | 12 |
| 20 | InitializerBuffer | 131,072-byte raw buffer | 13 | 13 |
| 21 | InitializerBuffer_Upload | 131,072-byte upload buffer | 14 | 14 |
| 23 | DebugInformation | 1920×1080 R16_UNORM | 0 (skipped) | 15 |

The uploads for IDs 5, 17 and 6 occupy slots 6, 8 and 10 respectively. ID 21 is a named provider resource with its own index. The package row loop treats the zero height of ID 5 as zero rows to initialize, despite its eight-byte input payload. ID 17 supplies two float ones and ID 6 supplies a zero fill. The desktop binding uses committed resources, creates SRVs and paired UAVs for eligible resources, and submits the initialization copies with a fence. Default-heap buffers use desktop COMMON state and implicit promotion.

On success, the translated loop copies all 25 indices to the mirror at +0x1ac and writes the provider pointer through the output argument at RVA `0xd263`. The loader returns a separate 16-byte wrapper containing the callback table and that same provider pointer. With flags 0 and 0x200 both adapters returned 0, with next resource indices 15 and 16 respectively. Injecting a callback return of 7 at ID 20 returned 3 through the provider and loader, left both output handles null, preserved the previous resource indices, and left the mirror untouched. The test checks every live backend record against its D3D12 object, both index tables, and the loader wrapper fields. All three cases completed with zero debug errors or warnings on the RTX 4060 Laptop and WARP.

The first desktop format difference occurs at ID 16. WARP exposed typed RGB9E5 UAV stores. The RTX driver (32.0.16.1656) lacked that capability, so the binding created an R32_UINT texture and views for the packed bits. The texel test below checks the two storage formats.

The local build translates the descriptor builder, debug flag, loop and writeback, resource-slot reservation, upload-descriptor construction, and selected create-export branches. Registry selection and the backend device callbacks are bound on the host. When the pinned loader DLL is beside the upscaler DLL, the probe also executes the recompiled loader create path against that provider. Public CI runs the source-owned provider control on WARP. Configure the local package build as in the recurrent texture test above and run:

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

### RCAS intermediate texel access

The provider-loop CTests above also exercise resource ID 16 after successful context creation. The test uses the provider's UAV and SRV for the intermediate at resource index 4. A source-built compute shader writes a strip of 1,024 HDR colors into the 1920×1080 texture. Another shader loads the texels through the SRV. A separate texture copy returns the underlying 32-bit words.

On the RTX 4060 Laptop the writer packs RGB into an R32_UINT UAV and the reader unpacks it to floats. The shared HLSL helpers clamp finite inputs to [0, 65408], choose a shared exponent and round the three 9-bit mantissas to nearest even. On WARP the typed RGB9E5 views perform the conversion. A second WARP context forces the packed path to exercise the fallback in public CI. A double-precision CPU reference checks the stored bits and all four decoded components, including the implied alpha of 1. The inputs cover all 32 exponents, channel permutations, half steps, exponent carry, underflow, negative values and saturation. The native reference also accepts the documented nearest rounding alternative at ties. [Direct3D RGB9E5 conversion](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#19.3.2)

On both local adapters the storage words and decoded channels matched nearest-even on every input. Both debug layers reported zero warnings and errors. The package supplies the local resource-creation path. These shaders test integer-coordinate texel loads. The next experiment traces the first package writer and reader of this intermediate.

The integrated AMD Radeon(TM) Graphics of a Ryzen 5 7600 (RDNA 2, device `164e`, driver 32.0.21045.29003) reports typed UAV stores for RGB9E5, so the native path runs on a physical GPU. Its stores round the mantissas toward zero. In 317 of the 1,024 inputs this gives a different word from nearest-even, and every stored word matches a round-toward-zero encoding. The SRV loads decode those words exactly. The forced packed context on the same adapter still matches nearest-even. For FLOAT -> RGBE, section 19.3.2.2 of the D3D11.3 functional spec says conversion "should be done with nearest even. Round to nearest is allowed", and notes that hardware does not perform it. Its general rule for conversion to another float format, in section 3.2.2, is round-to-zero. The native check therefore accepts nearest-even, round-to-nearest or round-toward-zero and reports how many texels needed each alternative. A store that mixes the two alternatives fails. The Xbox Series X GPU is also RDNA 2; whether its native stores round the same way is untested.

```powershell
ctest --preset dev --no-tests=error -R '^ffx_provider_loop_hardware$' -V
```

### Float-to-integer conversions in translated dispatches

The immediate control feeds positive infinity (0x7f800000) directly to both FFX emitter instructions and stores the results to a UAV. With DXC 1.8.2502.11 (239921522), cs_6_0, substituting the previous bare casts caused DXIL validation to fail with `Assignment of undefined values to UAV.` The guarded shader compiled and its DXIL returned 0xffffffff and 0x7fffffff on D3D12 WARP, with all 318 guard words intact.

The dispatch lowerer maps NaN to zero, truncates finite values toward zero and saturates out-of-range inputs to the signed or unsigned endpoint. The cast receives a finite value in range even if the compiler evaluates both arms of the endpoint selection.

The LLVM 15 check assembles both conversions and compares the translated register results with 19 pairs of expected words. The same inputs run through the emitted ALU expressions and production HLSL helpers on WARP. On Windows build 26300.9974, WARP and an RTX 4060 (driver 610.88) matched all pairs. The previous bare casts also matched in this D3D11 harness on these adapters. The fix removes dependence on out-of-range cast behaviour. Whether a normal dispatch supplies such values remains unmeasured.

```powershell
cmake --build --preset dev --target ffx_pass26_lds offscreen_check
ctest --preset dev --no-tests=error -R '^ffx_(immediate_)?float_to_int_warp$' -V
```

### FSR4 dispatch through the RCAS input

With sharpening enabled, the default i8 graph first touches the physical ID 16 texture in `FFX_FSR4_PASS_26`. It writes `rw_mlsr_output_color` (logical ID 10, UAV `u2`). The following `FFX_FSR4_RCAS_PASS` reads `r_rcas_input` (ID 16, SRV `t18`) and writes the external `rw_rcas_output` (ID 11, UAV `u11`). Each RCAS lane computes four output pixels. For each pixel it loads the center and four axial neighbors at integer texel coordinates. The native program contains 20 RGB `image_load` instructions with half-precision returns, four stores and zero sampler bindings.

| Provider RVA | Evidence from the pinned upscaler |
|---|---|
| `0xed10`, `0x1220f`, `0x13278` | Dispatch copies the sharpening byte into the staged constants at offset `0x5c`. When enabled, mirror entry 10 receives primary entry 16. When disabled, mirror entries 10 and 11 both receive the external output. |
| `0x1357d`, `0x13752`, `0x1375f` | The graph builder supplies workgroup counts. The scheduling loop indexes the pipeline array with stride `0x9690` and calls `0xe1d0`. The reflection check walks all 27 graph shader records in order. Case 26 selects the container at `0xa0e90`. |
| `0xe244`, `0xe2b9`, `0xe363`, `0xe3cd` | The scheduler resolves SRVs through the primary resource array and UAVs through the mirror, sets the job type to compute (2), then calls the enqueue callback. |
| `0x137b2`, `0x13898`, `0x138ac` | The sharpening flag gates the extra RCAS job. The separate pipeline at provider offset `0x248` selects the RCAS container at `0x701b0`. The provider then calls the execute callback. |

The package build translates the x64 alias updates and the two grid calculations. At 1920×1080 they produce 60×34×1 and 120×68×1 groups. The GPU replay uses an 8×4 logical image, one group per job, on the actual provider intermediate at index 4. Pass 26 has 256 threads per group and RCAS has 64. The host binds source-owned scene and history inputs as RGB buffers, uses unit exposure, and uses the provider's history and recurrent UAVs. Eight active lanes take the producer's direct texture path. The next experiment connects the two immediately preceding graph jobs through scratch.

Both desktop shaders come from offline decoding of the pinned native streams (3,482 producer instructions before specialization and 437 RCAS instructions). LLVM 15's gfx1030 decoder covers the producer's i8 dot instructions and its gfx1013 decoder covers RCAS. The translator keeps the original packed literal DWORDs, including the zero high half of the packed offset that decrements x. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

The GPU test checks all 32 intermediate texels and 257 surrounding guard texels. It then compares all 256 final pixels, including edges between different colors, with an independent FP32 five-tap reference (per-channel tolerance 2.5% plus 0.00005 for packed-half arithmetic). Alpha must equal red. RTX 4060 Laptop passed using R32_UINT. WARP passed with native RGB9E5 and with forced R32_UINT. Both debug layers reported zero errors and warnings. These are controlled single-group results.

From a VS x64 developer shell at the repository root, set `$upscaler` to the pinned local DLL. The middle command needs the LLVM 15 C library. On Windows, the official LLVM 15.0.7 installer (`winget install --id LLVM.LLVM --version 15.0.7 --exact`) provides `LLVM-C.dll` with the AMDGPU disassembler, so no WSL is required. All extracted programs and generated shaders stay under `out/`.

```powershell
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_profile.py $upscaler out/ffx-dispatch
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm "C:/Program Files/LLVM/bin/LLVM-C.dll"
# Alternatively, in WSL with libLLVM 15 installed:
# wsl -d Ubuntu-22.04 -- python3 -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm libLLVM-15.so.1
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE=$upscaler" "-DFFX_DISPATCH_SHADER_DIR=$PWD/out/ffx-dispatch"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

The local log reports `dispatch=package-aot`. Through the native Windows route above (LLVM 15.0.7 `LLVM-C.dll`, Python 3.14.6), the translator decoded 1152, 100, 3482 and 437 instructions for pass 24, pass 25, the producer and RCAS. `ffx_provider_loop_hardware` passed on an RTX 4070 Ti (driver 32.0.16.1088) and `ffx_provider_loop_warp` passed on WARP, both with the debug layer enabled. Each reported `dispatch=package-aot` with 256 final pixels and zero mismatches for the bypass, zero and pattern scratch cases. Public Windows CI runs source-built descriptor and dispatch controls on WARP (`dispatch=desktop-control`). Linux CI assembles an independent integer-coordinate regression for the translator.

### First pass 26 LDS write block

At `0x2410`, the existing 8×4 dispatch skips to `0x26d8`. An optional D3D11 probe models wave zero of group (0,0) with 32 synchronized threads. It decodes the branch predicate and the first block's store addresses from the pinned producer. Distinct synthetic values identify each row, lane and component after the colour transform.

| Input | X scale (Y = 1) | Active lanes | Footprint | Tagged words |
| --- | ---: | ---: | --- | ---: |
| 32×4 | 243/256 | 32 | 32×6 | 576 |
| 32×4 | 244/256 | 32 | 33×6 | 0 |
| 31×4 | 243/256 | 30 | Unspecified | 0 |

On Windows build 26300.9974, NVIDIA GeForce RTX 4060 (610.88) and Microsoft WARP each matched an independent CPU predicate for 120 inputs. An inclusive horizontal limit changed only the 244/256 boundary case at 32×4 and unit vertical scale. All 576 shared-memory words matched their expected order or zero fill for each of the three rows on both adapters. D3D11 debug layer disabled. This probe covers the first gate and tagged stores. The production translator still skips the block. Later LDS reads and pixel output on this path remain unverified.

The Windows build compiles the readback host. The default CTest uses a source-owned HLSL fixture on WARP, exercising the same gate cases, inclusive-limit negative control and tagged stores against the CPU expectations. From the repository root, after configuring the dev preset:

```powershell
cmake --build --preset dev --target ffx_pass26_lds offscreen_check
ctest --preset dev --no-tests=error -R '^ffx_pass26_lds_warp$' -V
```

The physical adapter and pinned package are independent opt-ins. Use the producer generated by the dispatch profile above and a trusted LLVM 15 installation for the package comparison. Omit `--package` to run the source-owned input, or use `--backend hardware` for either input.

```powershell
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend hardware
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend warp --package out/ffx-dispatch/producer.bin C:/LLVM15/bin/LLVM-C.dll
```

### Last writers of the pass 26 scratch reads

The default FSR4-i8 graph with tier zero, unit render/output ratio and sharpening enabled schedules passes 24 and 25 immediately before pass 26. At provider RVA `0x136a7` the inclusion mask has bit 25 set. Other scheduling modes can skip the padding job. In the 8×4 case the host rounds both dimensions up to multiples of eight at `0x11f94`. Tensor 13 uses half that aligned size (4×4). The dispatch builder at `0x7e1c` and its padding helper at `0x7730` yield grids of 1×4×1 and 2×1×1 groups respectively.

Both kernels bind the provider's raw scratch UAV at `u11` (resource ID 22, index 12). Pass 24 performs its i8 convolution and stores 16 bytes at shader PC `0x218c`. With plane base `0x7f4bc0`, row stride `0x3c20` and 16 bytes per column, its output starts at column 1, row 1. The launched 64-wide groups also write beyond the four active columns. Pass 25's store at shader PC `0x200` writes four zero DWORDs. Its 44 active lanes cover the top and bottom rows, the left column and five columns of right padding around the 4×4 interior.

Pass 26 has eight active lanes with cell coordinates `(x,y)` in `[0,4) × [0,2)`. Its nine loads read at `base + (y+dy)*stride + 16*(x+dx)`. For a given address, interior columns 1–4 on rows 1–3 of the read window have pass 24 as last writer. All other addresses in that window have pass 25 as last writer.

| Pass 26 shader PC | (dx, dy) | Lanes reading pass 24 | Lanes reading pass 25 |
|---|---|---:|---:|
| `0x14c` | (0, 0) | 3 | 5 |
| `0x154` | (1, 0) | 4 | 4 |
| `0x15c` | (2, 0) | 3 | 5 |
| `0x164` | (0, 1) | 6 | 2 |
| `0x16c` | (1, 1) | 8 | 0 |
| `0x174` | (2, 1) | 6 | 2 |
| `0x17c` | (0, 2) | 6 | 2 |
| `0x184` | (1, 2) | 8 | 0 |
| `0x18c` | (2, 2) | 6 | 2 |

The reproduction commands above also translate the two upstream shaders and use four 64-byte constant ranges from the pinned model initializer. The test seeds the pass 24 input with either zero or a deterministic pattern, fills the output region with a sentinel, and uses the same GPU buffer across all three passes. It copies the 6×4 read window after each writer and records the address and all four DWORDs at each of the 72 pass 26 loads. A third control bypasses both writers and supplies zero scratch directly to pass 26. Model biases can produce nonzero activations even from a zero upstream input.

A separate Python evaluator checks the scratch words and the four history-blend logits per lane using signed-byte dot products. Spatially constant, distinct scene and history colors give a per-pixel blend reference for resource 16. The existing five-tap FP32 reference checks all 256 final RCAS pixels, including the surrounding pattern. Switching the upstream input changed all 32 active pixels at both stages on RTX 4060 Laptop, native RGB9E5 WARP and forced R32_UINT WARP. Both adapters reported zero debug warnings and errors. The tensor feeding pass 24 remains a fixture; the full-wave LDS branch of pass 26 is a separate open path.

### FidelityFX context creation and ownership

The Windows test calls the translated loader create function, then passes its returned handle directly to the existing destroy translation. Create binds a source-owned upscaler provider to the two table slots. On success, the loader allocates a 16-byte wrapper and stores the table pointer and provider context in it. A provider create error propagates its 32-bit return code, clears the caller's handle and skips wrapper allocation. Destroy calls the provider with the address of the inner field before releasing the wrapper, including on a provider destroy error.

Fourteen cases cover default CRT allocation and custom allocation with user data. For each route the test checks success, two destroy errors, three create errors, and failure to allocate the wrapper after the provider succeeds. It observes allocation and release addresses, order, sizes and the live-allocation count. In the allocation-failure case the host adapter calls provider destroy once and returns memory error 5. This rollback is a Windows adapter behavior. The inspected loader proceeds to a wrapper store after its allocator returns. The provider used in this comparison owns real CRT memory and has controlled return codes; creation of a live upscaling effect on a graphics device remains open.

```powershell
cmake --build --preset dev --target cpu_create_lifted cpu_destroy_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_context_lifecycle$' -V
```

CI compares an independently assembled native create caller and its translation, each paired with the fixture's lifted destroy. To check the package translation from a VS x64 developer shell, set `$loader` to the path of the local FidelityFX loader and run:

```powershell
New-Item -ItemType Directory -Force out/ffx-lifecycle | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_create_context.py $loader out/ffx-lifecycle/create.cpp
./out/pe-env/Scripts/python.exe -B tools/cpu_chained_aot.py $loader ffxDestroyContext out/ffx-lifecycle/destroy.cpp --ffx-destroy
foreach ($unit in 'create','destroy') {
    $prefix = "out/ffx-lifecycle/$unit"
    cl /nologo /O2 /EHsc /LD "/Fo:$prefix.obj" "$prefix.cpp" /link "/OUT:$prefix.dll" "/IMPLIB:$prefix.lib"
    if ($LASTEXITCODE) { throw "Compilation failed for $unit" }
}
./out/pe-env/Scripts/python.exe -B tests/cpu/context_lifecycle.py out/build/dev/cpu_create.dll out/ffx-lifecycle/create.dll out/ffx-lifecycle/destroy.dll
```

### FidelityFX version enumeration

The Windows query returned one upscaler, version ID `0xf5a5ca1e01001001` and name `4.1.1`. The first call requested the count. The second supplied one ID slot and one name slot. Both returned status 0. The descriptor uses AMD's null-context, type-4 version query. [AMD FidelityFX query descriptor](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

`tools/ffx_version_query.py` checks both input DLL hashes, decodes the selected x64 query intervals and the provider's two filter functions, and emits their integer operations and branches as C++. The generated Windows DLL combines both modules' selected paths. Its host binding specializes module resolution to one available upscaler and no frame-generation module. It reconstructs the single provider entry from the initializer's stores, including the full ID and the referenced name. Static storage keeps the returned name valid for the lifetime of the DLL.

On this build, capacity zero reports the available count without writing array elements. ID-only and name-only calls both succeed. Capacity two writes one result and leaves the second slot untouched. A missing frame-generation module returns 4 and an unknown effect returns 2. In both cases the loader clears the count **pointer in the descriptor** and leaves the caller's count value untouched. This entry point covers version enumeration only.

Run these commands in an x64 VS developer shell with the Python development environment available. Set `$loader` and `$upscaler` to the two local input paths.

```powershell
New-Item -ItemType Directory -Force out/ffx-query | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_version_query.py $loader $upscaler out/ffx-query/query.cpp
cl /nologo /O2 /EHsc /LD /Fo:out/ffx-query/query.obj out/ffx-query/query.cpp /link /OUT:out/ffx-query/query.dll /IMPLIB:out/ffx-query/query.lib
./out/pe-env/Scripts/python.exe -B tests/cpu/version_query_check.py out/ffx-query/query.dll query_versions 0xf5a5ca1e01001001 4.1.1
```

The public CTest `cpu_aot_version_query` runs the same output checks against an independent MASM fixture and its translation. The fixture uses a different name and 64-bit ID. The two proprietary inputs and generated code stay local.

The integer lowering also handles MOVBE, the byte-reversing move between memory and a 16-, 32- or 64-bit register, in both directions. It changes no flags. A 16-bit register destination keeps bits 16 to 63, and a 32-bit destination clears its upper half. `cpu_aot_movbe` exits 77 (capability skip) when CPUID leaf 1 ECX bit 22 (`MOVBE`) is clear. Otherwise it builds ten exports from [MASM source](../tests/cpu/movbe.asm), covering loads, stores, REX registers, displacements, a scaled index and a load stored back. It compares RAX, ZF/CF, the data bytes and 64 guard bytes on each side, at nine offsets including misaligned ones, against the native DLL and an independent reference. Three controls mutate the generated C++ and must mismatch: `noswap` copies the bytes in memory order (2592/2880 cases), `clear16` zero-extends a 16-bit destination (360/2880), and `flags` clears ZF and CF (2160/2880). Run `ctest --preset dev --no-tests=error -R '^cpu_aot_movbe$' -V`.

The shared integer lowering in this tool emits flat loads and stores, so it rejects FS or GS segment overrides and LOCK prefixes instead of dropping them. A thread-block access such as `gs:[0x30]` would otherwise read absolute address 0x30, and a locked read-modify-write would lose its atomicity. Segment overrides without effect in 64-bit mode and `rep ret` lower like the bare instruction. A host operation that replaces an instruction bypasses the check. `ctest --preset dev --no-tests=error -R '^cpu_lower_prefixes$' -V` checks 11 rejected encodings and 6 accepted controls with synthetic bytes.

**XCHG.**
- **Register forms:** register-register `xchg` reads both operands before writing either. In every 32-bit form both registers are zero-extended, so `87 C0` (`xchg eax, eax`) clears the upper half of RAX. The one-byte `90` and `66 90` stay NOPs.
- **Memory forms:** these are atomic on x86 without a LOCK prefix. The lowering therefore emits the host's `_InterlockedExchange8`, `16`, plain or `64` on the flat address, not a load and a store. The ordering of the flat accesses around the exchange is not modelled. An explicit, redundant `lock xchg` is still rejected by the prefix check above.
- **Value fixture:** `cpu_aot_xchg` builds 18 value exports from MASM source (`tests/cpu/xchg.asm`). They cover the 8-, 16-, 32- and 64-bit register forms (including `ah`, `ch`, `r8b` and the one-byte RAX forms), `87 C0`, and memory forms at several offsets, including a misaligned qword. The check compares RAX, RCX, R8, R9 and the memory slot with a reference for 64 value pairs per export.
- **Contention check:** two token exports run a loop of `xchg` on one shared qword or dword. Eight threads run them at once, each exchanging 100000 distinct tokens. The values the exchanges return, plus the final slot, must be exactly the tokens and the initial value, each once. The native DLL and the translation lose or duplicate none in three rounds.
- **Negative controls:** two mutated translations must each mismatch. One swaps through a plain load and store: every single-threaded value case still matches, but tokens are lost or duplicated under contention. The other writes only one register of a register swap.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_xchg$' -V`.

A 67h address-size prefix selects 32-bit addressing. The lowering wraps base + index × scale + displacement at 32 bits and zero-extends the result, ignoring the upper halves of the registers as the hardware does. In this mode iced reports a displacement of -8 as `0xfffffff8`, so the wrap is also needed when the upper halves are zero. EIP-relative operands are rejected because the hardware adds the low 32 bits of the runtime RIP, which an RVA binding cannot represent. `cpu_aot_address_size` builds `lea`, load and store forms such as `lea rax, [ecx+edx*8-8]` and `mov [ecx+edx], r8d` from MASM source. It translates them and compares both images with a 32-bit reference over wrapped sums and upper-half noise. The loads and stores use a page committed below 4 GiB, and each store must leave the rest of the page unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_address_size$' -V`.

XLATB replaces AL with the byte at RBX + AL. AL is zero-extended, and bits 8 to 63 of RAX and the flags are unchanged. With a 67h prefix the base is EBX and the sum wraps at 32 bits, so the upper half of RBX is ignored. A REX.W prefix (`48 d7`) does not change the operation. `cpu_aot_xlatb` builds four exports from [MASM source](../tests/cpu/xlatb.asm): the plain, REX.W and 67h forms, and two lookups in a row. Each export runs on a 256-byte permutation table in a page committed below 4 GiB. The check compares RAX and ZF/CF for every AL value, with noise in the upper bits of RAX and, for the 67h form, in the upper half of RBX, against the native DLL and an independent reference. Three controls mutate the generated C++ and must mismatch: `signed` sign-extends AL, `noaddr32` keeps the upper half of RBX in the 67h sum (whose reads fault), and `clear` writes the loaded byte over all of RAX. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_xlatb$' -V`.

The RCX-counted branches `loop`, `loope`, `loopne` and `jrcxz` lower to a decrement and a conditional `goto`, and so do their 67h forms, which count in ECX. The behaviour is as follows:
- None of them changes a flag.
- `loope` and `loopne` test ZF after the decrement.
- `jrcxz` and `jecxz` only test the count.
- The ECX forms decrement ECX as a 32-bit write, which clears the upper half of RCX. The native fixture confirms this on the cases with noise in the upper half.

`cpu_aot_rcx_branches` builds eight exports from MASM source (`tests/cpu/rcx_branches.asm`) and compares 7712 cases against the native DLL, the translation and a Python reference. Each case records RAX, RCX, the final pointer and ZF/CF. The exports are:
- iteration counts with each ZF/CF preset, which must survive `loop`;
- byte scans under `loope` and `loopne` guarded by `jrcxz`, which exit on the first element, a later one or at count exhaustion;
- `jrcxz` and `jecxz` on counts that differ only in the upper half of RCX.

Three mutated translations must each mismatch:
- the ECX decrement keeping the upper half of RCX, with `jecxz` testing all of RCX;
- `loope` and `loopne` ignoring ZF;
- `jrcxz` inverted.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_rcx_branches$' -V`.

An immediate SHL, SHR or SAR whose masked count is zero leaves the flags unchanged, but a 32-bit destination is still written and its upper half clears. This covers `shl eax, 0` and `shl eax, 32`, because the count is masked to five bits. The lowering keeps the flags and emits the zero-extending write. A 64-bit destination keeps every bit. `cpu_aot_shift_count` builds these forms from MASM source, together with a nonzero control. It compares the native and translated images with a reference and checks that `sete` after a zero-count shift still observes the preceding compare. Before this change the translation returned `0xdeadbeef12345678` for `shl_zero(0xdeadbeef12345678)` where the hardware returns `0x12345678`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_shift_count$' -V`.

The shared lowering also handles 32-bit and 64-bit integer bit-scan, bit-count, byte-swap, and register bit-test forms (`bsf`, `bsr`, `bswap`, `popcnt`, `lzcnt`, `bt`, `bts`, `btr`, `btc`), including zero-extending 32-bit writes, ZF/CF updates, and rejection of memory-operand bit tests. `cpu_aot_bit_ops` probes CPUID for POPCNT and LZCNT before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on capable hosts it builds all 19 exports from MASM source (`tests/cpu/bit_ops.asm`), translates them, and compares values and flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_bit_ops_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bit_ops$' -V`.

The shared lowering also handles `clc`, `stc` and `cmc`, and the rotations through the carry flag `rcl` and `rcr` in 8-, 16-, 32- and 64-bit register and memory forms, including `ah`, with a count of 1, an immediate or CL. The rotation steps one bit at a time, as in the Intel SDM pseudocode. The count is masked to five bits, or six for a 64-bit operand. The 8- and 16-bit forms rotate 9 and 17 bits, so reducing their counts modulo 9 and 17 only bounds the loop. A zero step count keeps CF, and ZF is never affected. OF, which the SDM defines only for a count of 1, is not modelled, like the other flags the lowering omits. A 32-bit register destination is written and its upper half cleared even when the masked count is zero, as for the zero-count shifts above. The native fixture confirms this.

`cpu_aot_carry_rotates` builds 102 exports from MASM source (`tests/cpu/carry_rotates.asm`, generated by `tests/cpu/carry_rotates.py asm`):
- every destination with counts 1 and CL;
- immediates 0, 5, 9, 17, 33 and 65 for `al`, `ax`, `eax`, `rax` and a dword in memory;
- the flag instructions alone;
- the 128-bit idioms `shl` + `rcl` and `shr` + `rcr`.

Each export starts from one of four ZF/CF presets. The check compares RAX, the memory operand, ZF and CF for 84000 cases against the native DLL, the translation and a Python reference. CL counts run from 0 to 70, plus 96, 128 and 255. Three mutated translations must each mismatch:
- the count mask widened to eight bits;
- the rotated-out bit fed back instead of CF, which turns the instructions into `rol` and `ror`;
- `stc` clearing CF.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_carry_rotates$' -V`.

The shared lowering also handles 32-bit and 64-bit BMI1 bit-manipulation forms (`andn`, `bextr`, `blsi`, `blsmsk`, `blsr`, `tzcnt`), including their zero-extending 32-bit writes and ZF/CF updates. `cpu_aot_bmi1` probes CPUID leaf 7 EBX bit 3 (`BMI1`) before invoking the native fixture and exits 77 (capability skip) on hosts without BMI1; on BMI1-capable hosts it builds all twelve 32-bit and 64-bit exports from MASM source (`tests/cpu/bmi1.asm`), translates them, compares both values and observed ZF/CF flags against the native DLL and an independent reference, and checks that 16-bit `tzcnt` is rejected. Run `cmake --build --preset dev --target cpu_bmi1_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bmi1$' -V`.
The shared lowering also handles 32-bit and 64-bit rotates and double-precision shifts (`rol`, `ror`, `shld`, `shrd`) with both immediate and `cl` counts, preserving flags on zero masked counts while zero-extending 32-bit destinations. `cpu_aot_rotates_shifts` builds all 12 exports from MASM source (`tests/cpu/rotates_shifts.asm`), translates them, and compares values and ZF/CF flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_rotates_shifts_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_rotates_shifts$' -V`.

The string instructions `movs`, `stos`, `lods`, `scas` and `cmps` lower in byte, word, dword and qword widths, bare or with REP (`movs`, `stos`, `lods`), REPE or REPNE (`scas`, `cmps`), together with `cld` and `std`. The lowering selects them by iced `Code`, because the SSE2 scalar `movsd` and `cmpsd` share their mnemonics. Each iteration moves or compares one element and then steps RSI and RDI by the width, downward when DF is set. Overlapping copies therefore replicate as on the hardware, which a whole-range `memmove` would not. A repeated form tests RCX before each element and ZF after it. A zero count leaves the pointers, memory and flags unchanged. `lodsb` and `lodsw` merge into RAX and `lodsd` zero-extends. `cmps` compares the source element with the destination element and `scas` compares the accumulator with the destination; only ZF and CF are modelled, like the rest of the lowering. DF starts clear in each translated function. The [x64 software conventions](https://learn.microsoft.com/en-us/cpp/build/x64-software-conventions#x64-register-usage) expect DF clear on function exit and on entry to runtime and system calls, so a conforming caller never enters with it set. The 67h forms that walk ESI, EDI and ECX, REPNE on `movs`, `stos` and `lods`, and FS or GS sources are rejected. `cpu_aot_string_ops` builds 62 exports from MASM source (`tests/cpu/string_ops.asm`). In 13 of them the instruction runs with DF set, and one sets DF and clears it again before the instruction. Each export takes its pointers, count, RAX and a flag preset from the caller and reports memory, RAX, RSI, RDI, RCX, ZF and CF. The check runs 39984 cases against the native DLL, the translation and a Python reference. The cases cover overlaps of one byte and of less than an element, zero counts under each flag preset, and REPE and REPNE exits on the first element, on a later one and at count exhaustion, with both CF results. Three mutated translations must each mismatch: a `memmove` for REP `movs`, swapped `cmps` operands, and STD ignored. A 1 KiB guard on each side of the buffer is compared too. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_string_ops$' -V`.

The shared lowering also handles baseline x64 sign-extension (`cbw`, `cwde`, `cdqe`, `cqo`, `movsx`) and add-with-carry (`adc`) forms, preserving upper bits on 16-bit writes, zero-extending 32-bit writes, and propagating carry-in/carry-out and ZF on `adc`. `cpu_aot_sign_ext_adc` builds all 11 exports from MASM source (`tests/cpu/sign_ext_adc.asm`), translates them, and compares values and flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_sign_ext_adc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sign_ext_adc$' -V`.

The shared lowering also handles the memory-ordering fences `lfence`, `mfence` and `sfence`, `pause`, and the cache hints `prefetcht0`, `prefetcht1`, `prefetcht2`, `prefetchnta`, `prefetch`, `prefetchw`, `clflush`, `clflushopt` and `clwb`, each through the MSVC intrinsic that emits the same instruction. A fence has no effect a single thread can observe, so `cpu_aot_fences_hints` checks that each of its 7 exports keeps the loads and stores around the instruction, including prefetches of a null and an unmapped address, which never fault. It compares return values and the buffer against the native DLL and a reference model over 76 cases. A segment-relative prefetch and the invalid `lock lfence` stay rejected. Fences, PAUSE and the standard PREFETCHT*/PREFETCHNTA cases run in the baseline test. AMD prefetch, CLFLUSH, CLFLUSHOPT and CLWB each have a separate CTest; a native illegal instruction skips only that test (exit 77). Run `cmake --build --preset dev --target cpu_fences_hints_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fences_hints($|_)' -V`.

The shared lowering also handles 32-bit and 64-bit BMI2 forms (`bzhi`, `mulx`, `pdep`, `pext`, `rorx`, `sarx`, `shlx`, `shrx`), preserving flags on the flag-free forms, updating ZF/CF on `bzhi`, and resolving same-destination `mulx` to the high half. `cpu_aot_bmi2` probes CPUID leaf 7 EBX bit 8 (`BMI2`) before invoking the native fixture and exits 77 (capability skip) on hosts without BMI2; on BMI2-capable hosts it builds all 17 exports from MASM source (`tests/cpu/bmi2.asm`), translates them, compares values and flags against the native DLL and a reference model, and verifies that malformed 0x66-prefixed VEX sequences for all eight instructions are rejected. Run `cmake --build --preset dev --target cpu_bmi2_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bmi2$' -V`.

All seven SHA-NI forms (`sha1msg1`, `sha1msg2`, `sha1nexte`, `sha1rnds4`, `sha256msg1`, `sha256msg2`, `sha256rnds2`) are also lowered for legacy XMM encodings. `cpu_aot_crypto_sha` probes CPUID leaf 7 subleaf 0 EBX bit 29 (`SHA`) before invoking the native fixture and exits 77 on hosts without SHA-NI; on SHA-NI-capable hosts it builds all 10 exports from MASM source (`tests/cpu/crypto_sha.asm`, covering all four `sha1rnds4` round-function selectors), translates them, compares exact 16-byte outputs against the native DLL, and checks that unmodeled registers are rejected. Run `cmake --build --preset dev --target cpu_crypto_sha_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_crypto_sha$' -V`.

The shared lowering also handles 32-bit and 64-bit `mul`, `div`, `idiv`, `xadd`, and `cmpxchg` (with `LOCK` still rejected by the prefix guard), using portable 128-bit arithmetic for 64-bit widening multiply/divide. `cpu_aot_mul_div` builds all 10 exports from MASM source (`tests/cpu/mul_div.asm`), translates them, and compares results and ZF/CF flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_mul_div_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_mul_div$' -V`.

The shared lowering also handles all six unsigned/equality `cmovcc` (`cmove`, `cmovne`, `cmova`, `cmovae`, `cmovb`, `cmovbe`) and `setcc` (`sete`, `setne`, `seta`, `setae`, `setb`, `setbe`) forms, unconditionally zero-extending 32-bit `cmovcc` destinations and preserving upper bytes on `setcc`. `cpu_aot_cmov_setcc` builds 13 exports from MASM source (`tests/cpu/cmov_setcc.asm`), translates them, and compares results against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_cmov_setcc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_cmov_setcc$' -V`.

The shared lowering also handles 32-bit and 64-bit ADX forms (`adcx`, `adox`) and `seto`, tracking `OF` independently from `CF` so interleaved `adcx`/`adox` chains preserve each other's carry flag. `cpu_aot_adx` probes CPUID leaf 7 EBX bit 19 (`ADX`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on ADX-capable hosts it builds all five exports from MASM source (`tests/cpu/adx.asm`), translates them, and compares sums and `(CF << 1) | OF` against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_adx_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_adx$' -V`.

The shared lowering also handles 128-bit and 256-bit AVX2 integer broadcasts (`vpbroadcastb`, `vpbroadcastw`, `vpbroadcastd`, `vpbroadcastq`) and variable per-lane shifts (`vpsllvd`, `vpsrlvd`, `vpsravd`, `vpsllvq`, `vpsrlvq`), zeroing upper 128 bits on 128-bit VEX destinations and clamping out-of-range shift counts to match hardware. `cpu_aot_avx2_shifts` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 7 EBX bit 5 `AVX2`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on AVX2-capable hosts it builds all 44 exports from MASM source (`tests/cpu/avx2_shifts.asm`), translates them, and compares outputs against the native DLL and a reference model across 128-bit and 256-bit register/memory operands, register-aliasing forms (`dst == src1`, `dst == src2`, `dst == src1 == src2`), and boundary shift counts while checking deterministic rejections for untracked registers, EVEX encodings, segment prefixes, and EIP-relative operands. Run `cmake --build --preset dev --target cpu_avx2_shifts_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_shifts$' -V`.

The shared lowering also handles AVX2 128-bit lane operations, dword blends, and cross-lane permutes (`vbroadcasti128`, `vextracti128`, `vinserti128`, `vpblendd`, `vperm2i128`, `vpermd`, `vpermps`, `vpermq`, `vpermpd`). `cpu_aot_avx2_permutes` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 7 EBX bit 5 `AVX2`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on AVX2-capable hosts it builds all 17 exports from MASM source (`tests/cpu/avx2_permutes.asm`), translates them, and compares outputs against the native DLL and a reference model. The `vpblendd` memory tests check that native and translated code both fault before writing output when either only masked-off lanes cross into an inaccessible page or all lanes are masked off and the whole operand is inaccessible. Run `cmake --build --preset dev --target cpu_avx2_permutes_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_permutes$' -V`.

The shared lowering also handles 128-bit legacy `movdqu` and 14 SSE4.1 integer vector instructions (`pminsb`, `pmaxsb`, `pminuw`, `pmaxuw`, `pminsd`, `pmaxsd`, `pminud`, `pmaxud`, `pmulld`, `pmuldq`, `pcmpeqq`, `packusdw`, `pblendw`, `pblendvb`). `cpu_aot_sse41_int` probes CPUID leaf 1 ECX bit 19 (`SSE4.1`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSE4.1-capable hosts it builds all 14 exports from MASM source (`tests/cpu/sse41_int.asm`), translates them, and verifies bit-for-bit agreement with the native DLL. Run `cmake --build --preset dev --target cpu_sse41_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_int$' -V`.

The shared lowering also handles all 12 SSE4.1 sign/zero-extending vector moves (`pmovsxbw/bd/bq/wd/wq/dq`, `pmovzxbw/bd/bq/wd/wq/dq`) and legacy `movdqu`, reading only the narrow source lane extent for memory operands. `cpu_aot_sse41_extend` probes CPUID leaf 1 ECX bit 19 (`SSE4.1`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSE4.1-capable hosts it builds all 13 exports from MASM source (`tests/cpu/sse41_extend.asm`), translates them, and verifies bit-for-bit agreement with the native DLL. Run `cmake --build --preset dev --target cpu_sse41_extend_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_extend$' -V`.

Baseline 128-bit SSE2 packed saturating add/sub, multiply, min/max, average, and sum-of-absolute-differences forms (`movdqu`, `paddsb`/`sw`/`usb`/`usw`, `psubsb`/`sw`/`usb`/`usw`, `pmulhw`, `pmulhuw`, `pmullw`, `pmaddwd`, `pmuludq`, `pminub`, `pmaxub`, `pminsw`, `pmaxsw`, `pavgb`, `pavgw`, `psadbw`) are also lowered for legacy XMM encodings while preserving upper YMM bytes and rejecting 64-bit MMX forms. `cpu_aot_sse2_sat_mul` builds all twenty exports from MASM source (`tests/cpu/sse2_sat_mul.asm`), translates them, compares all 16 output bytes against the native DLL and an independent reference (including saturation extremes and `pmaddwd` signed-product wrapping), and checks that 64-bit MMX `paddsb mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse2_sat_mul_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_sat_mul$' -V`.

Legacy and 128-bit VEX AES-NI and carryless-multiply forms (`aesenc`, `aesenclast`, `aesdec`, `aesdeclast`, `aesimc`, `aeskeygenassist`, `pclmulqdq`, and their `v*` 128-bit VEX counterparts) are also lowered using portable FIPS-197 GF(2^8) and GF(2) arithmetic while rejecting 256-bit VAES encodings. `cpu_aot_crypto_aes_pclmul` probes CPUID leaf 1 ECX bits 1 (`PCLMULQDQ`), 25 (`AES`), 27 (`OSXSAVE`), and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without those features; on capable hosts it builds all 15 exports from MASM source (`tests/cpu/crypto_aes_pclmul.asm`), translates them, compares exact 16-byte outputs against the native DLL, and checks that 256-bit `vaesenc` is rejected. Run `cmake --build --preset dev --target cpu_crypto_aes_pclmul_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_crypto_aes_pclmul$' -V`.

The shared lowering also handles all 24 VEX-encoded scalar FMA3 instructions (`vfmadd`/`vfmsub`/`vfnmadd`/`vfnmsub` × `132`/`213`/`231` × `ss`/`sd`) using single-rounding `std::fmaf`/`std::fma`, preserving upper XMM bytes of `dst`, zeroing the upper 128 bits of YMM, and rejecting EVEX forms. `cpu_aot_fma_scalar` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 1 ECX bit 12 `FMA`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on FMA3-capable hosts it builds all 24 exports from MASM source (`tests/cpu/fma_scalar.asm`), translates them, and verifies bit-for-bit agreement with the native DLL (including exact-cancellation inputs). Run `cmake --build --preset dev --target cpu_fma_scalar_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fma_scalar$' -V`.

Baseline 128-bit SSE2 packed integer ALU forms (`movdqu`, `paddb`/`w`/`d`/`q`, `psubb`/`w`/`d`/`q`, `pand`, `pandn`, `por`, `pxor`, `pcmpeqb`/`w`/`d`, `pcmpgtb`/`w`/`d`) are also lowered for legacy XMM register operands while preserving upper YMM bytes and rejecting 64-bit MMX forms. `cpu_aot_sse2_int` builds all eighteen exports from MASM source (`tests/cpu/sse2_int.asm`), translates them, compares all 16 output bytes between the native and translated DLLs, and checks that 64-bit MMX `paddb mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse2_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_int$' -V`.

SSSE3, SSE4.1, and SSE4.2 integer, insert/extract, `ptest`, and `crc32` forms (`pextrb`/`w`/`d`/`q`, `pinsrb`/`w`/`d`/`q`, `pmaddubsw`, `pmulhrsw`, `phminposuw`, `ptest`, `mpsadbw`, `pcmpgtq`, `crc32`) are also lowered for legacy XMM and GPR encodings. The packed binary ALU and PTEST paths accept tracked XMM register sources. The fixture retains the aligned native MOVNTDQA check and verifies that the translator rejects MOVNTDQA until its alignment and memory contract is supported. `cpu_aot_sse41_42_misc` probes CPUID leaf 1 ECX bits 9 (`SSSE3`), 19 (`SSE4.1`), and 20 (`SSE4.2`) before invoking the native fixture and exits 77 on hosts without those features; on capable hosts it builds the fixture exports from MASM source (`tests/cpu/sse41_42_misc.asm`), translates them, verifies all four `(ZF, CF)` outcomes of `ptest`, 8/32/64-bit `crc32`, and exact vector/scalar outputs against the native DLL, and checks that 64-bit MMX `pmulhrsw mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse41_42_misc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_42_misc$' -V`.

The shared lowering also handles 128-bit legacy `movdqu` and 14 SSSE3 integer vector instructions (`pabsb`, `pabsw`, `pabsd`, `psignb`, `psignw`, `psignd`, `pshufb`, `palignr`, `phaddw`, `phaddd`, `phsubw`, `phsubd`, `phaddsw`, `phsubsw`), with tracked XMM register sources. The SSSE3 operations reject 64-bit MMX forms, untracked XMM sources, and memory sources. `cpu_aot_ssse3_int` probes CPUID leaf 1 ECX bit 9 (`SSSE3`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSSE3-capable hosts it builds all 15 exports from MASM source (`tests/cpu/ssse3_int.asm`), translates them, and verifies bit-for-bit agreement with the native DLL plus rejection controls for all three unsupported source forms. Run `cmake --build --preset dev --target cpu_ssse3_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_ssse3_int$' -V`.

Baseline 128-bit SSE2 packed shifts, packs, unpacks, shuffles, and movemask forms (`movdqu`, `psllw`/`d`/`q`, `psrlw`/`d`/`q`, `psraw`/`d`, `pslldq`, `psrldq`, `packsswb`, `packssdw`, `packuswb`, `punpcklbw`/`lwd`/`ldq`/`lqdq`, `punpckhbw`/`hwd`/`hdq`/`hqdq`, `pshufd`, `pshuflw`, `pshufhw`, `pmovmskb`) are also lowered for legacy XMM encodings while preserving upper YMM bytes. The new operations support register and immediate operands and reject legacy memory sources until their alignment requirements are modeled. `movdqu` retains its existing unaligned move semantics. `cpu_aot_sse2_shifts_pack` builds all 37 exports from MASM source (`tests/cpu/sse2_shifts_pack.asm`), translates them, compares outputs against the native DLL and an independent reference across immediate, in-range, boundary, and out-of-range shift counts, and checks that MMX `packsswb` and memory forms of `packsswb`, `psllw`, and `pshufd` are rejected. Run `cmake --build --preset dev --target cpu_sse2_shifts_pack_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_shifts_pack$' -V`.

The shared lowering also handles all 36 VEX-encoded packed FMA3 instructions (`vfmadd`/`vfmsub`/`vfnmadd`/`vfnmsub`/`vfmaddsub`/`vfmsubadd` × `132`/`213`/`231` × `ps`/`pd`) in 128-bit and 256-bit forms using single-rounding `std::fmaf`/`std::fma` and rejecting EVEX forms. `cpu_aot_fma_packed` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 1 ECX bit 12 `FMA`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on FMA3-capable hosts it builds all 44 exports from MASM source (`tests/cpu/fma_packed.asm`), translates them, and compares raw result bits with the native DLL across finite inputs, quiet and signaling NaNs, and pairs or triples with distinct NaN signs and payloads. The NaN result follows the arithmetic operand order for that encoding before any sign change. Extra cases cover 128-bit writes clearing the high YMM half and a 256-bit memory source. Finite-operand cases add a `132` memory form, 128-bit `vfnmadd`, `vfmaddsub` and `pd` forms, a destination that is also the first source, and a `vfmadd132pd` whose single rounding gives -2^-104 where separate rounding gives 0. These comparisons check result bytes under the host's default masked floating-point environment. Run `cmake --build --preset dev --target cpu_fma_packed_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fma_packed$' -V`.

## Recompiling a direct x64 call

The [caller and callee](../tests/cpu/direct.asm) each accept two unsigned 64-bit integers and an output pointer. The [translator](../tools/cpu_call_aot.py) starts at the caller export, follows the signed displacement in its E8 call and reads the target's runtime-function interval. It generates two C++ functions from the linked machine code. MSVC emits a separate x64 DLL with a non-inlined call. The Windows test checks the relative call against the two export addresses in both the input and output DLLs.

```powershell
cmake --build --preset dev --target cpu_call_original_lifted cpu_call_altered_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_direct_call$' -V
```

The caller changes both arguments and keeps its original inputs live across the call. The callee records its arguments and arithmetic result through R8. The caller uses the returned RAX and both live inputs to compute its result. A separate assembly probe seeds physical RBX and RSI and reads them immediately after each invocation. Tests exercise the whole call and the callee in isolation using the same 249 input pairs, including wraparound and high-word differences. They compare the return, three output words, adjacent guards and both register sentinels with the original and independent arithmetic.

A rebuilt callee that increments only its return after storing the result exposes a mismatch for every pair. A register-clobber control checks the probe itself. Targets redirected into the middle of the callee or back to the caller are rejected during translation.

A [second caller](../tests/cpu/repeat_call.asm) calls the same callee twice. Only RBX and RSI survive each call, so it parks the trace pointer in RSI and copies it back to R8 for the second call, whose arguments depend on the first result. The translator decodes 64-bit moves between R8 and the profile registers in both the opcode 8B form that ML64 emits and the opcode 89 form. A repeated direct call to the same target lifts that callee once, and a repeated import call through the same IAT slot binds that import once. It tracks which registers still hold the entry R8 value, so R8 can only be reloaded from such a copy and every store stays inside the trace.

```powershell
cmake --build --preset dev --target cpu_repeat_original_lifted cpu_repeat_altered_lifted cpu_repeat_dropped cpu_repeat_import_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_repeated_call$' -V
```

For the same 249 input pairs, the caller and callee match the original and independent arithmetic (for both the direct-call fixture and the repeated-import `IMPORT_MIX` build). The comparison covers the return, the trace written by the second call, the guards and the RBX/RSI sentinels. With a callee that returns one more, the caller's return and trace change on every pair. Both the original and the lifted DLL contain two rel32 calls to their callee. Replacing both R8 moves with their opcode 89 encodings leaves the translation unchanged. Translation rejects mixed direct/import calls, two distinct IAT slots, and six patched or rebuilt copies:
- one assembled without the R8 reload, as an undefined register;
- the first or the second call redirected into the callee interior, which owns no function interval;
- both calls redirected there;
- both calls redirected back to the caller;
- R8 reloaded from RBX, which holds an input word.

Hand-built callers that combine a direct call and an import call are rejected in either order, as mixed import and direct calls. A caller that calls two different IAT slots is rejected as a call to a second IAT slot.

A [third caller](../tests/cpu/call_pair.asm) calls two different callees, `mix_args` and then `mix_other`, which combines its arguments with XOR so the two calls do not commute. The translator lifts each distinct direct target once, in call order, as `lifted_callee`, `lifted_callee2` and so on. Each target must own a function interval that overlaps neither the caller nor another callee.

```powershell
cmake --build --preset dev --target cpu_pair_original_lifted cpu_pair_swapped_lifted cpu_pair_first_lifted cpu_pair_second_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_call_pair$' -V
```

For the same 249 input pairs, the caller and all four lifted callees match the original and independent arithmetic, with guards and RBX/RSI sentinels. A copy assembled with the two calls swapped follows its own reference and changes all 249 results, and its lifted DLL names `mix_other` first. Copies with only `mix_args` or only `mix_other` returning one more each change the caller's return on every pair. Each original and lifted caller contains exactly one rel32 call to each of its callees. Translation rejects three redirected calls: either call into the other callee's interior, and the second call back to the caller. A copy whose `mix_args` runtime-function entry is extended into `mix_other` is refused earlier, by the range reader, as an invalid runtime-function interval.

Coverage is limited to this integer ABI, its two fixed frame layouts and the decoded register operations. The fixtures exercise two distinct callees at most, and not a caller that returns to an earlier callee after calling another. They exercise the R8 moves only through RSI, and import calls are still limited to one IAT slot per caller. [Windows x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention) · [Import-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/24)

## Calling another DLL through a named PE import

The source-built caller uses an x64 RIP-relative indirect call (`FF 15 disp32`). The translator computes the import address table (IAT) slot RVA from the end of the instruction and looks up the exact slot in the ordinary import descriptors. The build supplies an explicit binding for the fixture's integer ABI. The DLL name and case-sensitive symbol must match the slot, and the provider must have a local executable export for that name. MSVC compiles the generated caller and links the provider's import library. The test independently checks that the call in each linked caller addresses its own slot for `cpu_import_provider.dll!mix_args`. [PE import address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#import-address-table)

```powershell
cmake --build --preset dev --target cpu_import_original_lifted cpu_import_altered cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_import_call$' -V
```

Each provider configuration runs in a fresh Windows process. The provider records the two arguments it received and its arithmetic result in three output words. For 249 pairs the test compares both callers with independent arithmetic, checks the adjacent guard words and samples RBX and RSI. Replacing the provider with one that increments its return changes the caller's result on every pair and preserves the output writes.

With the provider absent, both callers fail to load with error 126. With a DLL of the expected name that lacks the requested export, both fail with error 127. The translator also diagnoses the missing provider or export before compilation and rejects a call displaced one byte into the IAT slot. The verified profile covers one ordinary named import with two 64-bit integers and an output pointer. [Ordinal-only import follow-up](https://github.com/R0Y00/Xbox2Windows/issues/26)

## Calling an ordinal-only export

The source-built provider exposes only ordinal 77 through a NONAME export. The build binds `#77` with the local import-library symbol `mix_args`. The translator checks the DLL and ordinal from the call's IAT slot against the actual export address table. The test reads both linked callers' import lookup words, checks the ordinal flag and value, and verifies the provider has no exported name for the entry.

```powershell
cmake --build --preset dev --target cpu_ordinal_original_lifted cpu_ordinal_altered cpu_register_probe cpu_import_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_ordinal_import$' -V
```

For 249 input pairs both callers match the provider's observed arguments, guarded writes, register sentinels and expected return. Replacing the provider changes the return on all inputs. Both callers fail to load with error 126 when the DLL is absent and error 182 when the DLL exports the same link name but lacks ordinal 77. The static translator diagnoses a mismatched binding, a missing link name, a missing ordinal and a displacement into the middle of a slot. When the provider is renamed `ws2_32.dll`, pefile names ordinal 77 from its built-in table. The translator still requires the `#77` binding and rejects that name.

The local Bink DLL imports ordinal 1 from XAudio2_9.dll. A read-only check confirmed an executable export at that ordinal in this workstation's System32 provider. The Bink call's argument and result contract remains unverified.

## x87 in the shared lowering

`lower(..., x87=True)` in `tools/ffx_version_query.py` accepts an opt-in x87 leaf-function profile. The default (`x87=False`) still rejects every x87 instruction, so existing callers are unchanged. The profile accepts `fld` and `fstp` with an m32, m64 or m80 memory operand, `fst` with an m32 or m64 operand, `fld st(i)`, `fst st(i)`, `fstp st(i)`, `fxch st(i)`, `fchs`, `fabs`, `fsqrt`, `frndint`, `fxtract` and `fscale`, the constant loads `fld1`, `fldz`, `fldpi`, `fldl2t`, `fldl2e`, `fldlg2` and `fldln2`, `fild`, `fistp` and `fisttp` with an m16, m32 or m64 integer operand, `fist` with m16 or m32, `fcomi`, `fcomip`, `fucomi` and `fucomip` with `st, st(i)`, `fcmovb`, `fcmove`, `fcmovbe`, `fcmovnb`, `fcmovne`, `fcmovnbe`, `fcmovu` and `fcmovnu` with `st, st(i)`, `fcom` and `fcomp` with `st(i)` or an m32 or m64 operand, `fcompp`, `fucom` and `fucomp` with `st(i)`, `fucompp`, `fnstsw ax`, and five forms of each of `fadd`, `fsub`, `fsubr`, `fmul`, `fdiv` and `fdivr`: an m32 or m64 operand, `st, st(i)`, `st(i), st`, and the popping form (`faddp`, `fsubp`, `fsubrp`, `fmulp`, `fdivp`, `fdivrp`) with `st(i), st` for `i` from 1 to 7. It also accepts `fiadd`, `fisub`, `fisubr`, `fimul`, `fidiv` and `fidivr` with an m16 or m32 integer operand, `fnstcw` and `fstcw` with an m16 operand, which store the fixed control word `0x027F`, and `wait`, which does nothing because every exception is masked. A popping form with `st(0)` as the destination is rejected. A function enters with control word 0x027F (53-bit precision, round to nearest-even, all exceptions masked), TOP 0, all eight registers empty and a clear status word, and it must return with the stack empty. The stack depth before each instruction is fixed at translation time (`tools/x87_lower.py`), so each register becomes a C++ slot and no TOP exists at run time. Translation rejects any other x87 instruction, including `fldcw` (which only the checked entry below accepts), `ffree`, `fnstsw` with a memory operand, and environment access; a ninth push; a pop, an `st(i)` operand or `faddp` without enough values; depths that differ where control flow joins; an x87 instruction that is not reachable from the entry through direct edges; and a nonempty stack at a return, a call, an indirect branch or an external edge.

The helpers in `tools/x87_soft.h` compute in integers and do not use host floating point. A register holds the full 80-bit encoding. m80 loads and stores copy all 80 bits, including NaN payloads, denormals, pseudo-denormals and unnormals. m32 and m64 loads convert exactly and quiet a signaling NaN. Under the fixed control word a sum rounds to 53 significand bits within the extended exponent range; a result below that range is denormalized first and then rounded at the same bit positions. The sum rounds again when it is stored to m32 or m64. NaN operands follow the x87 column of Intel SDM Vol. 1, Table 4-7: a QNaN beats an SNaN, and otherwise the larger significand wins. When two NaNs have equal significands, the positive one wins in either operand order. This tie rule was measured, not taken from the table. Infinities of opposite sign, unnormals, pseudo-infinities and pseudo-NaNs give the indefinite. An exact zero sum of nonzero addends is +0 (-0 when rounding toward negative infinity), and -0 + -0 is -0. Every rounding helper takes the control word and follows its precision and rounding control. The fixed profile passes `0x027F`, so only the checked entry below reaches the other words. Exceptions are not modelled, and of the status word only C0, C2, C3 and TOP are (see `cpu_aot_x87_status`). This is safe only because translation rejects every reader of the other status-word bits, and the checked entry stops at a word that unmasks an exception.

`cpu_aot_x87_add` builds 16 exports from generated MASM source (`tests/cpu/x87_add.asm`, from `tests/cpu/x87_add.py asm`) and translates them. The exports cover the nine load/store width pairs, additions stored to each width, and a three-value chain that uses `faddp st(2), st`. A native-only harness (`tests/cpu/x87_harness.asm`) saves the caller's whole x87 state with `fnsave` (environment and register values), which also leaves TOP 0 and an empty stack. It then loads control word 0x027F, calls the export, records the control, status and tag words the export left, and restores the caller's state with `frstor`. A harness check enters each export with a live caller value (pi with all 64 significand bits) in the register the export reuses and control word 0x0F7F loaded. It requires the value bytes, the control word, TOP and the tags to come back unchanged. A control harness that keeps only the environment (`fnstenv`/`fldenv`) must lose the value around every export. Native and translated exports receive the same input bytes and run through that harness. The check compares each output block and the 64 guard bytes on each side of the input and output against an independent `Fraction` model. It requires every export to return with control word 0x027F, TOP 0 and all tags empty. The 8085 cases include all pairs of 34 special encodings, halfway sums that separate 53-bit from 64-bit precision, double rounding through m64, sums beyond the binary64 range, extended-range overflow and underflow, denormal results, cancellation, NaN ties and 700 random pairs. Six mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, m80 loads through binary64, a dropped sticky bit, the first NaN operand always winning, and -0 for an exact cancellation. On an AMD Ryzen 7 7700 (Windows 11 build 26200), every case matched in the native and translated DLLs, and the controls gave 1089, 3845, 1485, 48, 58 and 18 mismatches. On an Intel Core Ultra 7 358H (Windows 11 build 26300), every case likewise matched in the native and translated DLLs, and the controls gave 1089, 3845, 1485, 48, 58 and 18 mismatches. The check also confirms that the default lowering rejects `fld` and that the profile rejects 16 sequences outside it. Only `faddp st(1), st` and `faddp st(2), st` are exercised. Run `cmake --build --preset dev --target cpu_x87_add_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_add$' -V`.

`cpu_aot_x87_mul` checks the multiplication forms the same way, through the same harness, guards and live-value check, with 12 exports and 14951 cases. The product of the two 64-bit significands is exact in 128 bits and rounds like a sum. Its sign is the exclusive or of the operand signs, including for zero and infinite results, and infinity times zero gives the indefinite. An m32 or m64 operand of `fmul` converts exactly but keeps a signaling NaN signaling, so the NaN rules see the operand's own kind. A plain `fld` quiets it on load. The cases cover every pair of the 34 special encodings, each special m80 value times each of 17 m32 and 18 m64 special encodings, products that are exact halfway cases at 53 bits, products whose upper 64 bits sit exactly on a tie so that only the low 64 bits decide, `x * 1` rounding, products beyond the binary64 range, extended overflow and underflow thresholds, denormal results, the square through `fmul st, st(0)`, a three-value chain through `fmulp st(2), st`, a product followed by an add, and random operands. Six mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, dropping the low 64 bits of the product, the first operand's sign, infinity times zero giving infinity, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4836, 8059, 62, 4409, 73 and 36 mismatches. The check also confirms that translation rejects nine sequences, including `fmulp st(0), st`, `fldenv`, `fdecstp`, `fprem1`, `fincstp`, and multiplications without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_mul$' -V`.

`cpu_aot_x87_div` checks the division forms in the same way, with 17 exports and 23939 cases. `fdivr` and `fdivrp` divide the source by the destination. Each export's decoded x87 instructions must match the forms its sequence names, so an assembler that exchanged the `fdiv` and `fdivr` register encodings would fail before the run. The helper divides the 64-bit significands into a 64-bit quotient and folds a nonzero remainder into a sticky bit. That is enough to round to 53 bits, and the result goes through the same rounding as the sum and the product. Zero by zero and infinity by infinity give the indefinite. A finite nonzero value by zero gives a signed infinity (the masked divide-by-zero response). A finite value by infinity, and zero by a finite value, give a signed zero. The cases cover every pair of the 34 special encodings and every special m80 value against the m32 and m64 special encodings, in both orders. They also cover exact quotients halfway between 53-bit neighbours, quotients whose 64-bit significand ends exactly on a tie so that only the remainder decides, `x / x` through `fdiv st, st(0)`, quotients beyond the binary64 range, the extended overflow and underflow thresholds, denormal results, two three-value chains through `fdivp st(1), st`, `fdivp st(2), st` and `fdivrp st(1), st`, and random operands. Seven mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, an ignored remainder, the indefinite for a finite value by zero, `fdivr` in the `fdiv` order, an unsigned zero quotient, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 every case matched in the native and translated DLLs, and the controls gave 8857, 14373, 253, 602, 6829, 551 and 54 mismatches; on the Intel Core Ultra 7 358H (Windows 11 build 26300) every case likewise matched, with the controls giving 8785, 14373, 253, 602, 6829, 551 and 54 mismatches. Translation still rejects `fdivp st(0), st`, `fdivrp st(0), st`, `fldenv`, `fdecstp`, `fnsave`, `fprem`, `fincstp` and divisions without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_div$' -V`.

`cpu_aot_x87_sub` checks `fsub`, `fsubr`, `fsubp`, `fsubrp` and the `fadd` forms besides `faddp` in the same way, with 22 exports and 35023 cases. `fsubr` and `fsubrp` subtract the destination from the source. The helper computes `a - b` as `a + (-b)`, but it selects a NaN or invalid operand before negating, so a NaN result keeps its own sign. `x - x` is +0, and -0 - +0 is -0. The cases reuse the addition pairs of `cpu_aot_x87_add`, and again with the second operand negated. They also cover `x - x` and `x + x` through `st(0)`, m32 and m64 operands against the special encodings, `float` and `double` differences, a three-value chain through `fsubp st(2), st` and `fsubrp st(1), st`, and random operands. Seven mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, negating a NaN subtrahend, `fsubr` in the `fsub` order, -0 for an exact cancellation, a dropped sticky bit, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 15680, 24289, 2022, 7753, 777, 108 and 81 mismatches. Translation still rejects `fsubp st(0), st`, `fsubrp st(0), st`, `fldenv`, `fdecstp`, `fxsave`, `fnstenv`, `fcos` and arithmetic without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_sub$' -V`.

`cpu_aot_x87_moves` checks the register data movement in the same way, with 26 exports and 20224 cases. `fld st(i)` pushes a copy of `st(i)`, `fst st(i)` copies `st(0)` into `st(i)`, and `fstp st(i)` does the same and then pops. `fxch st(i)` exchanges `st(0)` and `st(i)`. These copy the full 80-bit encoding, so a signaling NaN stays signaling and an unnormal keeps its bits. `fchs` and `fabs` change only the sign bit, of every encoding, including zeros, NaNs, unnormals and pseudo-denormals. `fst` to m32 or m64 rounds and stores `st(0)` like `fstp` and leaves the register unchanged. The model moves raw encodings and shares no code with `tools/x87_soft.h`. The exports cover `fld st(0)`, `st(1)`, `st(2)` and `st(6)` at depth 7, `fst st(1)` and `st(2)`, `fstp st(0)`, `st(1)` and `st(2)`, `fxch st(0)`, `st(1)`, `st(2)` and `st(7)` at depth 8, and `fchs` and `fabs` stored to m80, m64 and m32. They also cover an `fst` to m64 and one to m32 before the final `fstp`, and movement combined with arithmetic: `a + (-b)`, `|a| * |b|` and a subtraction after `fxch`. The cases run every pair of the 34 special encodings through the two-value exports and 300 triples of them through the three-value exports, plus random values and 300 blocks of eight m64 values. Seven mutated translations must each mismatch: `fchs` as `0 - x`, `fabs` that leaves -0 and negative NaNs unchanged, `fxch` that does nothing, `fld st(i)` that always copies `st(0)`, `fstp st(i)` that only pops, `fst` to m32 or m64 that also writes the rounded value back to `st(0)`, and register copies that quiet a signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 969, 225, 3116, 2212, 1922, 1357 and 267 mismatches. Translation rejects an `st(i)` operand beyond the stack depth, a push at depth 8, `fchs` and `fabs` on an empty stack, `fbstp`, `fsin`, `ffree` and `fdecstp`. It also rejects the undocumented alias encodings of `fstp st(i)` (`DF D0+i`, `DF D8+i`) and `fxch st(i)` (`DD C8+i`, `DF C8+i`), which assemblers do not emit and the fixture does not measure. Only `st(0)` to `st(2)`, `fld st(6)` and `fxch st(7)` are exercised as register operands. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_moves$' -V`.

`cpu_aot_x87_constants` checks the constant loads in the same way, with 30 exports and 2863 cases. Each constant is rounded to 64 significand bits by the rounding control, which the profile fixes at round to nearest-even, and the 53-bit precision control does not apply to it. The translation emits the encodings from a table in `tools/x87_lower.py`. The model does not read that table: it computes pi (Machin's formula), ln 2 and ln 10 from integer series with 320 fraction bits, derives log2(10), log2(e) and log10(2) from them, and rounds each value itself. The exports store each of the seven constants to m80, m64 and m32, store `fldpi` through `fst` m64 and then `fstp` m80, and use the constants as operands: `x + 1`, `1 / x`, `0 - x`, `x * ln 2`, `pi` times an m64 value and `log2(e)` times an m32 value. Two exports push `fldpi` or `fldlg2` as the eighth value. Four mutated translations must each mismatch: the constants rounded to 53 bits, the constants rounded toward zero, `fldz` loading -0, and the `fldlg2` and `fldln2` values exchanged. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 170, 40, 25 and 829 mismatches. Translation rejects each constant load at depth 8, a constant left on the stack at the return, and `f2xm1`, `fyl2x` and `fnstenv`. The other rounding modes are not measured, because the profile rejects `fldcw`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_constants$' -V`.

`cpu_aot_x87_integers` checks the integer conversions in the same way, with 18 exports and 11126 cases. `fild` converts a 16-, 32- or 64-bit two's-complement integer exactly, zero gives +0, and the 53-bit precision control does not apply to it. `fist` and `fistp` round `st(0)` to nearest-even and `fisttp` rounds toward zero. A NaN, an infinity, an unsupported encoding or a rounded value outside the destination range stores the integer indefinite (only the sign bit set), the masked invalid-operation response. `fist` keeps `st(0)`. The model converts with `Fraction` arithmetic. The exports load each integer width to m80, a 64-bit integer to m64 and m32, and a 32-bit integer added to an m80 value. They store every width through `fistp` and `fisttp`, and m16 and m32 through `fist`. They also cover round trips through `fild`/`fistp` m64 and `fild`/`fisttp` m16, two stores from a two-value stack, and `fild` as the eighth value. The integer inputs include each width's extremes, small values, values just past the binary64 and binary32 significand widths, and random values. The m80 inputs include the special encodings, integers, halfway values, quarter steps and values a small fraction past a half around zero and each width's limits, and 400 random magnitudes from 2^-3 up to just below 2^67. Six mutated translations must each mismatch: `fist` and `fistp` truncating, `fisttp` rounding to nearest, saturation instead of the integer indefinite, a NaN converting to 0, `fild` m16 and m32 zero-extending, and `fild` keeping only 53 significand bits. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 1510, 676, 1049, 60, 621 and 170 mismatches. Translation rejects `fild` at depth 8, the stores on an empty stack, `fist` that leaves a value at the return, `fldenv`, `fyl2xp1`, `fbld` and `fbstp`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_integers$' -V`.

`cpu_aot_x87_compare` checks `fcomi`, `fcomip`, `fucomi` and `fucomip` in the same way, with 12 exports and 12230 cases. A comparison sets the integer lowering's ZF and CF and clears OF: equal gives ZF 1 and CF 0, less gives 0 and 1, greater 0 and 0, and unordered (a NaN or an unsupported encoding) 1 and 1. With invalid operations masked, `fcomi` and `fucomi` give the same flags. All 64 significand bits count, +0 equals -0, and a pseudo-denormal equals the normal value with the same significand at biased exponent 1. In a function with `jp`, `jnp`, `fcmovu` or `fcmovnu` they also set PF, which is 1 only for unordered (`cpu_aot_x87_parity`). The integer lowering rejects the other readers of PF and the readers of SF and of the whole flags register. `seto` and `adox` read the cleared OF. After each comparison a record reads the flags through the integer lowering's flag readers, with only flag-neutral `mov`s in between: `je`, `jne`, `ja`, `jae`, `jb` and `jbe` as taken bytes, `cmove`, `cmovne` and `cmovbe` as dwords, `sete`, and last `rcl` of a zeroed register, which rotates CF in and changes it. CF is therefore observed on its own. `loope` and `loopne`, which also read ZF, are not used. The model compares with `Fraction` arithmetic. The exports cover all four mnemonics with `st(1)`, `fcomi st, st(2)`, `fucomip st, st(2)`, a value against itself through `fucomi st, st(0)` and `fcomip st, st(0)`, m64 and m32 operands, two comparisons in a row, and `seto` and `adox` after an `add` of two input dwords and `fcomi` (OF was set before the comparison in 211 of 317 cases), and they store the values left on the stack. The cases cover every pair of the 34 special encodings and values against themselves, their neighbours in the lowest significand bit and bit 11, their negation and the next exponent. They also cover pseudo-denormal and normal twins, denormals one bit apart, random values, and the m64 and m32 special encodings. Seven mutated translations must each mismatch: unordered reported as equal, the operands exchanged, -0 below +0, the low 11 significand bits ignored, the raw biased exponent for pseudo-denormals, unnormals compared by their bits, and OF left as it was. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 3023, 7260, 13, 745, 33, 812 and 211 mismatches. Translation rejects `jp` and `jnp` after `fucomi` and `inc eax`, which writes PF last, and `setp`, `cmovp`, `js`, `jl`, `lahf`, `cmovs` and `pushfq` after `fucomi`, and comparisons without enough stack values. It also rejects the forms that stay outside the profile (`fcom` in the DC D1 alias encoding, `fucompp` with one value, `fprem1`, `f2xm1`, `fptan` and `fbstp`), and `fnstsw` with an m16 operand. On a CPU without ADX the native `add_fcomi_of` export, whose `adox` would fault, is not run; the test runs every other check and then exits 77. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_compare$' -V`.

`cpu_aot_x87_sqrt` checks `fsqrt` in the same way, with 9 exports and 13083 cases. The helper rounds the exact square root to 53 significand bits within the extended exponent range. It takes the 64-bit integer root of the significand scaled by 2^63 or 2^64, whichever makes the exponent even, and folds a nonzero remainder into the sticky bit. sqrt(-0) is -0 and sqrt(+inf) is +inf, a NaN is quieted, and any other negative operand or an unsupported encoding gives the indefinite. The model rounds an integer square root with about 160 bits, scaled to the operand's magnitude, and replaces an inexact root by the midpoint of its unit interval, which rounds the same way. The exports take the root of an m80 value and store it to m80, m64 and m32. They also take two roots in a row, the roots of m64 and m32 loads, `sqrt(b) + a`, `sqrt(a * a + b * b)` and a root at depth 8. The cases cover the special encodings, perfect squares at both exponent parities, and 600 values next to the squares of 53-bit midpoints, whose roots fall within a few units of the last bit of a rounding tie. They also cover signaling NaNs, random values over the whole exponent range, denormals, and the m64 and m32 special encodings. Six mutated translations must each mismatch: rounding to 64 bits, rounding in the binary64 exponent range, an ignored remainder, +0 for sqrt(-0), the root of a negative operand's magnitude, and an unquieted signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4863, 9027, 168, 7, 741 and 104 mismatches. Translation rejects `fsqrt` on an empty stack and `fsin`, `fsincos`, `fnsave` and `frstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_sqrt$' -V`.

`cpu_aot_x87_rndint` checks `frndint` in the same way, with 8 exports and 7760 cases. `frndint` rounds `st(0)` to an integral value to nearest-even and keeps it in the extended format. The 53-bit precision control does not apply: integral results between 2^53 and 2^63 keep all their bits, which the native runs confirm. Zeros, infinities and values of 2^63 and above are unchanged, a value that rounds to zero keeps its sign, a NaN is quieted and an unsupported encoding gives the indefinite. The model rounds with `Fraction` arithmetic. The exports round an m80 value and store it to m80, m64 and m32, round m64 and m32 loads, compute the fractional part `x - rndint(x)`, store a rounded value through `fistp` m64, and round at depth 8. The cases reuse the m80 inputs of `cpu_aot_x87_integers` (integers, halves, quarter steps and values just past a half around zero and each integer width's limits). They add values with a fraction between 2^53 and 2^63, signaling NaNs, and m64 and m32 values from 2^-2 up to just below 2^53 and 2^24. Five mutated translations must each mismatch: truncation, ties away from zero, +0 for a value that rounds to zero, the result cut to 53 significand bits, and an unquieted signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 2236, 208, 321, 1207 and 42 mismatches. Translation rejects `frndint` on an empty stack, `fnstenv`, `fldenv`, `fbld` and `fxsave`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_rndint$' -V`.

`cpu_aot_x87_xtract` checks `fxtract` in the same way, with 8 exports and 5895 cases. `fxtract` replaces `st(0)` by its unbiased exponent and pushes its significand, which keeps the sign and gets biased exponent 16383, so it lies in [1, 2). Both results are exact and the precision control does not apply. A denormal or pseudo-denormal is normalized first, so its exponent is that of its value. A zero keeps its sign and gives an exponent of -inf, the masked divide-by-zero response. An infinity keeps its sign and gives an exponent of +inf. A NaN is quieted into both results, and an unsupported encoding gives the indefinite in both. The model splits each value with `Fraction` arithmetic. The exports split an m80 value and store both results to m80, m64 and m32, split m64 and m32 loads, split the significand again (its exponent is +0), add the two results, and split at depth 8. The cases cover the m80 singles of `cpu_aot_x87_add`, 400 random values over the whole exponent range, two values of exponent 0 for each leading-bit position (denormals, and pseudo-denormals at bit 63), values at small and extreme exponents, signaling NaNs, 30 each of pseudo-denormals, unnormals and pseudo-infinities or pseudo-NaNs, and the m64 and m32 special encodings and random bit patterns. Six mutated translations must each mismatch: +inf as the exponent of a zero, a denormal split without normalizing, the exponent left biased, the significand without its sign, an unquieted signaling NaN, and the two results pushed in the other order. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 16, 660, 5296, 2692, 84 and 4448 mismatches. Translation rejects `fxtract` on an empty stack and at depth 8, `fxtract` with only one result stored, `frstor` and `fxrstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_xtract$' -V`.

`cpu_aot_x87_scale` checks `fscale` in the same way, with 8 exports and 43003 cases. `fscale` multiplies `st(0)` by 2^n, where n is `st(1)` truncated toward zero, and keeps `st(1)` on the stack. The precision control does not apply: a result in the normal range keeps all 64 significand bits, and a result below it is denormalized and rounded to nearest-even at the same bit positions. A result above the range overflows to infinity. `st(1)` = -inf gives a zero and +inf an infinity, both with the sign of `st(0)`. An infinite `st(0)` scaled by -inf and a zero scaled by +inf give the indefinite. A zero or infinite `st(0)` is otherwise unchanged, and NaN and unsupported operands follow the rules of `fadd`. The helper clamps the truncated scale to 2^20 in magnitude before adding it to the exponent. The model scales with `Fraction` arithmetic. The exports scale an m80 value by an m80 value and store the result to m80, m64 and m32, scale m64 values, scale by a 32-bit integer from `fild`, scale twice in a row, rebuild a value through `fxtract` and `fscale`, and scale at depth 8. Every export also stores `st(1)`. The cases cover every pair of the 34 special encodings, and values with tie and sticky patterns in their low bits, denormals, pseudo-denormals and random values scaled by integers from -70 to 70, by the exponents at the edges of the normal and denormal ranges, by huge values, by non-integers of both signs and by denormals. They also scale values with tie patterns into each part of the denormal range, and cover m64 pairs and 32-bit integer scales. Six mutated translations must each mismatch: rounding to 53 significand bits, rounding toward zero, a negative non-integer scale rounded toward -inf, +0 for every scale by -inf, a zero scaled by +inf kept as zero, and the operands exchanged. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 11395, 1063, 3068, 26, 10 and 37337 mismatches. Translation rejects `fscale` on an empty stack and with one value, `fscale` that leaves `st(1)` at the return, `fnsave` and `fxrstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_scale$' -V`.

`cpu_aot_x87_fcmov` checks `fcmovb`, `fcmove`, `fcmovbe`, `fcmovnb`, `fcmovne` and `fcmovnbe` in the same way, with 16 exports and 13290 cases. Each copies `st(i)` into `st(0)` when its condition on the integer lowering's CF and ZF holds: CF, ZF, CF or ZF, and their negations. The copy moves the whole 80-bit encoding, so a signaling NaN stays signaling, which the native runs confirm. The flags come from an integer `cmp` of two input dwords or from `fcomi`. The model evaluates each condition and copies raw encodings. The exports run each form after `cmp` with `st(1)`, `fcmovb` with `st(2)`, a maximum (`fcomi; fcmovb`) and a minimum (`fcomi; fcmovnbe`) of two values, two moves under the same flags, `fcmove st, st(0)`, and `fcmovnb` with `st(7)` at depth 8. Four more read the CF of `cmp` before the move: `adc` and `sbb` set ZF from it, `cmc` inverts it, and `rcl` carries it through EAX to the ZF of `test`. Every export stores every value. The two-value `cmp` exports run every seventh pair of the 34 special encodings with dword pairs that compare below, equal and above, and random values with random dword pairs. The three-value exports run random triples of the special encodings with the same three dword pairs. The `fcomi` exports run every pair of the special encodings and random values. The depth-8 export runs random m64 values and compares the halves of the seventh. In 5807 of the cases a move changes the output. Eight mutated translations must each mismatch: `fcmovb` on a clear CF, `fcmovbe` that reads only CF, `fcmovnbe` that moves when either flag is clear, `fcmovne` that never moves, moves that ignore their condition, a copy that quiets a signaling NaN, a dropped `cmc`, and `adc` and `sbb` that ignore the incoming CF. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 3448, 185, 960, 1055, 5851, 276, 696 and 510 mismatches. Translation rejects `fcmovu` and `fcmovnu` after `cmp`, whose PF the integer lowering does not model (`cpu_aot_x87_parity` covers these two moves), and the forms with an `st(i)` beyond the stack depth. In a function with a move, or with `fnstsw`, `sahf`, `jp` or `jnp` (`cpu_aot_x87_status`), it also rejects any instruction that reads a status flag that no earlier instruction writes on every path from the entry, because the lowered function starts with both flags false and has no input for the caller's flags. Every reader counts, not only the moves, because `adc`, `sbb`, `rcl`, `rcr` and `cmc` pass an undefined CF on into the flags and registers they write, and a later writer of ZF can carry it to a move. The same holds for the other status flags. The integer lowering reads only PF of them, through `jp`, `jnp`, `fcmovu` and `fcmovnu`. `DF` is not tracked, because the Windows x64 calling convention clears it. A forward pass over the function computes which of the six status flags are defined before each instruction:

- Writes come from iced's flag information. Flags left undefined, as ZF after `imul` or after `stc`, do not count.
- A join keeps only the flags defined on all of its incoming paths.
- A call leaves every status flag undefined.
- A CL-count shift and a `rep` compare may change nothing, so they define no new flag.
- An 8- or 16-bit `shl`, `shr` or `sar` by the operand width or more leaves CF undefined.

Seven native-only exports move on the caller's flags. `fcmovb` and `fcmove` move right after two loads. `mov eax, 0FFFFFFFFh; adc eax, 0; fcmove` and `mov eax, 0; sbb eax, 0; fcmove` move on the ZF that the caller's CF produces. `adc eax, 0` or `rcl eax, 1` from EAX = 0, then `test eax, eax; fcmovne`, carry the caller's CF through EAX. `cmc; fcmovb` inverts it. They run through `x87_call_flags` in `tests/cpu/x87_harness.asm`, which loads the caller's CF and ZF with `sahf` just before the call. For 40 value pairs and all four flag pairs, each export's move followed the caller's flag in all 1120 runs. Translation rejects each export at its first reader of the undefined flag: the move, or the `adc`, `sbb`, `rcl` or `cmc`. Nine more rejection cases cover the move before any writer, a join with one undefined predecessor, `stc`, a call, `imul`, `shl eax, cl`, `shl ax, 16`, and a `cmc` or a `seto` before the `cmp` that defines the move's flags. The integer lowering would also reject `seto`, but the flag rule comes first. Eight sequences whose flags are written on every path must still translate: after `clc`, after a join where both paths write ZF, after `xor eax, eax`, after `fcomi`, `clc` before `adc`, `stc` before `sbb`, `cmp` before `cmc`, and `clc` before `rcl` and `test`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fcmov$' -V`.

`cpu_aot_x87_iarith` checks `fiadd`, `fisub`, `fisubr`, `fimul`, `fidiv` and `fidivr` with an m16 or m32 integer operand in the same way, with 15 exports and 28512 cases. The integer is converted exactly, as `fild` converts it, and the operation then follows its floating-point form with `st(0)` as the destination: the result rounds to 53 bits under the precision control, a zero integer is +0, and the NaN, infinity and divide-by-zero rules are those of `fadd`, `fsub`, `fmul` and `fdiv`. `fisubr` and `fidivr` subtract `st(0)` from the integer and divide the integer by `st(0)`. The model combines the conversion of `cpu_aot_x87_integers` with the `Fraction` models of the add, subtract, multiply and divide tests. The exports run each operation at each width on an m80 value, `fimul` on an m64 value stored back to m64, `fiadd` with an m32 integer followed by `fisub` with an m16 integer, and `fidivr` at depth 8. The cases pair every special encoding with zero, small values of both signs, powers of two and the extremes of each width, and add values whose low significand bits sit at or beside a 53-bit rounding tie, full 64-bit significands, values at the edges of the exponent range, denormals and random values. The two bytes after each m16 integer are never its sign extension. Six mutated translations must each mismatch: `fisub` computed as `fisubr`, `fidiv` computed as `fidivr`, an m16 or an m32 integer zero-extended, an m16 integer read as 32 bits, and 64-bit rounding. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4565, 3980, 4593, 5609, 9857 and 12275 mismatches. Translation rejects `fbld` and `fbstp`, and the forms without a value on the stack. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_iarith$' -V`.

`cpu_aot_x87_fnstcw` checks `fnstcw`, `fstcw` and `wait` in the same way, with 6 exports and 1776 cases. The profile fixes the control word at `0x027F`, so `fnstcw m16` stores that word, and `wait` does nothing because every exception is masked and none can be pending. iced decodes `fstcw` as `wait` followed by `fnstcw`, and the check requires both. The exports store the word alone, between loads and a division, at the first and last two bytes of the output block next to the guards, and beside the values of a full stack. One export runs `wait` between loads, a division and a store. Every export stores every value. Four mutated translations must each mismatch: a store of `0x037F`, a four-byte store, no store, and a one-byte store. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and each control gave 1068 mismatches, every case of the five exports that store the word. The fixed profile still rejects `fldcw`, also after `fstcw`, `fnstsw` with m16, `fninit` and `fdecstp`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fnstcw$' -V`.

`cpu_aot_x87_examine` checks `ftst`, `fxam`, `ficom` and `ficomp` with an m16 or m32 integer, `fnop`, `fnclex` and `fclex` in the same way, with 4 exports and 782 cases. `ftst` compares ST(0) with +0.0 and `ficom` with the integer, converted exactly as for `fild`; both set C0, C2 and C3 like `fcom`, and `ficomp` pops. `fxam` writes C3, C2 and C0 from the 80-bit encoding: unsupported, NaN, normal, infinity, zero, empty and denormal, where a pseudo-denormal is a denormal, as the native run confirms. An empty ST(0) is known at translation time. C1, which `fxam` sets to the sign, stays unknown, as for every instruction in the profile. `fnop` and `fnclex` change nothing the profile models, and `fclex` decodes as `wait` and `fnclex`; like every other x87 instruction they leave C0, C2 and C3 undefined. The inputs cover every class, both signs, the integer bounds and values on either side of an integer. Three mutated controls, which report a denormal as normal, convert the integer as unsigned, and compare `ftst` with 1.0, gave 5, 136 and 7 mismatches. Translation rejects `ftst` and `ficomp` on an empty stack, and a reader of C0 after `fnop` or `fnclex`. The rejection cases that used these instructions in the earlier x87 tests now use instructions that stay outside the profile. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_examine$' -V`.

`lower(..., x87=x87_lower.Checked(reject))` is the checked entry of the profile. It also accepts `fldcw m16`, and keeps the control word in a local of the lowered function. The arithmetic, `fsqrt`, `fscale`, `frndint`, `fist` and `fistp`, the m32 and m64 stores, `fnstcw` and the five inexact constant loads all follow that word. The precision control applies only to the arithmetic and `fsqrt`. `fldcw` accepts a word with all six exceptions masked and precision control 00, 10 or 11 (24, 53 or 64 bits). It keeps the word as `fnstcw` reads it back: bit 6 reads as 1, bits 7 and 13 to 15 as 0, and bit 12 is kept. The lowered function leaves through the caller's `reject` statement with a status in these cases:

- status 1, at an `fldcw` whose word unmasks an exception;
- status 2, at an `fldcw` whose word has the reserved precision control 01;
- status 3, at a return whose control word is not `0x027F`.

Guest stores before that point stay done. The fixed profile (`x87=True`) is unchanged and its generated code is byte-identical. A function that loads the control word must not contain a call, an indirect branch or an external edge. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H, under every precision control, the constants round down for toward negative infinity and toward zero, and up for toward positive infinity. `fscale` truncates its scale toward zero under every rounding control.

`cpu_aot_x87_fldcw` checks the checked entry with 15 exports and 15944 cases. The translation is a checked entry `uint32_t name(word input, word output, word* result)`, which returns the status and writes `*result` only for status 0. The exports cover:

- each rounding site under all twelve supported words;
- the MSVC truncation idiom (`fnstcw`; `movzx`; `or eax, 0C00h`; `fldcw`; `fistp`; `fldcw` of the saved word);
- `fnstcw` and `fstcw` after words with each reserved-bit pattern;
- two returns that restore from a saved home-space slot and from an input word;
- guest stores before, between and after two loads.

The model rounds with exact rational arithmetic under the word's precision and rounding control, and uses the fixed-word models of the earlier x87 tests for special operands. At `0x027F` it agrees with those models on 13231 operations. Native runs never load a word with an exception unmasked, which would trap. The 570 such cases ran only translated. The 380 cases with precision control 01 ran natively as an observation only. All of them matched a 64-bit model on this part, but the SDM leaves the value reserved. The translation matched the model in every case: status, output block, guards and `*result`. The statuses were 14718 accepted, 570 status 1, 380 status 2 and 276 status 3. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300), the 14994 native cases matched the model, including the control word each export left. Ten mutated translations must each mismatch: rounding control ignored (5277), precision control ignored (5420), `fist` ignoring the rounding control (562), constants ignoring it (75), the raw word kept (524), unmasked words accepted (570), precision control 01 accepted (380), no return check (276), the check on only one of the two returns (123), and a rejection that records its status but runs on (950). Translation rejects `fldcw` in the fixed profile. In the checked entry it rejects `fldcw` in a function with a call, an indirect jump or an external edge, or with a value left at the return, and it rejects `fnstsw` with an m16 operand. It still translates a function with a call and no `fldcw`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fldcw$' -V`.

`cpu_aot_x87_status` checks the comparisons into the status word and `fnstsw ax` with 22 exports and 17843 cases. `fcom` and `fcomp` with `st(i)` or an m32 or m64 operand, `fcompp`, `fucom` and `fucomp` with `st(i)`, and `fucompp` set C3, C2 and C0 to 0 0 1 for less, 1 0 0 for equal, 0 0 0 for greater and 1 1 1 for unordered, with the order of `fcomi`. The P forms pop once, and `fcompp` and `fucompp` pop twice. `fnstsw ax` writes those bits and TOP to AX. TOP is the depth subtracted from 8, modulo 8, because a function enters with TOP 0. A forward pass over the function decides what a reader of AX may see, following the SDM:
- C0, C2 and C3 are known after a comparison on every path from the entry, and unknown at the entry.
- `fcomi`, `fcomip`, `fucomi` and `fucomip` leave them as they were.
- Every other x87 instruction leaves them undefined, including `wait`, `fnstcw` and `fnstsw` itself, and so does a call. `fstsw ax` assembles as `wait` and `fnstsw ax`, so after a comparison it exposes no known bit. This follows the SDM, although iced lists `wait` as changing no condition bit.
- C1, the exception flags, the stack fault and the busy bit are never known.

`sahf` copies C0 to CF, C2 to PF, C3 to ZF and the busy bit to SF. Each flag is defined only when its bit is known, and AF is left undefined. `test ah, imm8` and `and ah, imm8` define ZF, PF and SF only when the immediate selects known bits, and `and` makes the bits it clears known. Any other reader of AL, AH, AX, EAX or RAX that sees an unknown bit is rejected. So are a return, a call, an indirect branch and an external edge while AX holds one. The flag rule of `cpu_aot_x87_fcmov` then applies to every function with `fnstsw`, `sahf`, `jp` or `jnp`. The integer lowering sets PF only after `sahf`, the `fcomi` family (`cpu_aot_x87_parity`) and an 8-bit `test` or `and`, and `jp` and `jnp` are rejected when any other instruction may have written PF last. The lowering adds the status variable and PF only to functions that need them. Every other generated source of the build is byte-identical to the one built from the parent commit.

After each `fnstsw ax` a record reads AH through every accepted consumer, with only flag-neutral `mov`s in between:
- `sahf`, then `je`, `jne`, `jb`, `jae`, `ja`, `jbe`, `jp` and `jnp`;
- `test ah` with 01h, 04h and 40h (C0, C2 and C3) and with each TOP bit, then `jne`;
- `test ah` with 45h, 44h, 05h and 41h, then `jp`;
- `and ah, 45h`, stored, then `jp`, then `cmp ah` with 40h, 01h and 00h and `je`.

The record then clears EAX. The model compares with the `Fraction` model of `cpu_aot_x87_compare`. The exports run each register form with `st(1)`, `fcompp` and `fucompp`, `fcom st(2)`, `fucomp st(2)`, a value against itself, the four memory forms, and `fucom st(7)` at depth 8, where TOP is 0 again. They also cover a comparison followed by `fcomi` or `fucomip`, two comparisons each read through its own `fnstsw`, a join of `fcom st(1)` and `fucom st(2)` chosen by an input dword, and a branch on C3 whose successors test C0 and C2. Two exports run on all 256 byte values: the record on AH loaded by `mov`, and `jp` after `test al, al`, `test byte ptr [m], 0A5h` and `and al, 3Ch`. Every export stores the values left on the stack. The cases cover every pair of the 34 special encodings, values against themselves, their neighbour in the lowest significand bit, their negation and the next exponent, pseudo-denormal and normal twins, and random values, together with the m64 and m32 special encodings. At `fnstsw` they gave 4856 less, 2265 equal, 4914 greater and 5296 unordered, at TOP 0, 5, 6 and 7. Seven mutated translations must each mismatch: an unordered result without C2 (5340), C0 and C3 exchanged (7263), TOP stored as 0 (10895), PF loaded from bit 4 by `sahf` (8707), odd parity for `test` and `and` (16084), odd parity for the 22 `and` statements alone (16084, every case that reads PF), and `fcomi` clearing C0, C2 and C3 (779). On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs.

Two native-only exports read bits that the profile does not know. One stores the whole status word after `fcom`. Over 1759 runs its known bits matched the model, and the other bits took 0x0000, 0x0001 (IE) and 0x0002 (DE). The other is the branch export with `test al, 1` (IE) on the C3 successor, after `fucom`. An unordered `fucom` left IE clear for a quiet NaN and set for a signaling NaN, so the byte does not follow from C0, C2 and C3. Translation rejects both, at the store of AX and at `test al, 1`.

Translation rejects 32 more sequences:
- condition bits that are unknown at the reader: `fnstsw ax` without a comparison, or after `fadd`, `fld` and `fstp`, `fnstcw`, `wait` (`fstsw ax`), a second `fnstsw ax` or a call, and after a join where one path skips the comparison;
- the review's control, a branch whose other successor tests C1;
- direct reads of unknown bits: `test al, 1`, `test ax, 4500h`, `and ah, 47h` with a store of AH, a store of AX, a return (also in the checked entry after `fldcw`) and an external edge with the status word in AX;
- PF: `jp` after `fucomi` and `inc eax`, after `cmp` or after `test eax, eax`, or before any flag writer, `sahf` in a function without x87 instructions, and `lahf`;
- forms outside the profile: `fnstsw` with an m16 operand, the `fcom` and `fcomp` alias encodings DC D1, DC D9 and DE D1, `fsincos`, `fpatan` and `fbstp`, and comparisons without enough stack values.

Eight twins must still translate:
- `sahf; jb` after `fcom`;
- `fcomi` between `fcom` and `fnstsw`;
- the branch whose successors test C0 and C2;
- `test al, al; jp`;
- `and ah, 45h` with a store of AH;
- a return after `xor eax, eax`;
- `sahf` on AH from `mov`;
- `fnstsw ax` after `fcom` in the checked entry after `fldcw`.

Memory-form status stores, sticky exception flags, `fninit`, `fclex`, `ftst`, `fxam` and `ficom` are not part of this test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_status$' -V`.

`cpu_aot_x87_parity` checks the PF of `fcomi`, `fcomip`, `fucomi` and `fucomip`, and `fcmovu` and `fcmovnu`, with 16 exports and 17672 cases. In a function with `jp`, `jnp`, `fcmovu` or `fcmovnu`, each of the four comparisons sets the integer lowering's PF along with ZF and CF: 1 for unordered, 0 otherwise. `fcmovu` copies `st(i)` into `st(0)` when PF is set and `fcmovnu` when it is clear, moving the whole 80-bit encoding as the other moves do. A function without such a reader keeps its generated code. Every other generated source of the build is byte-identical to the one built from the parent commit, except the eight of `cpu_aot_x87_status`. They differ in two `fcomi` statements each, in functions whose `jp` reads the PF of a later `sahf`. A record stores whether `jp`, `jnp`, `je` and `jb` are taken, through flag-neutral `mov`s. The exports cover:
- each comparison with `st(1)`, read through the record;
- `fcmovu` and `fcmovnu` after `fucomi`, `fcomi`, `fucomip st, st(2)` and `fcomip`, and `fcmovu` with `st(7)` at depth 8;
- the equality test that compilers emit, `fucomip; fstp st(0); jp; jne`, and a classification by `jnp` and `je`;
- the last writer of PF: `fucomi` after `sahf`, `sahf` after `fucomi`, and `test al, al` after `fucomi`;
- `fcmovu` after `sahf` on all 256 AH values loaded by `mov`;
- a join, chosen by an input dword, of a `fucomi` path and an `fcom; fnstsw ax; sahf` path.

The model compares with the `Fraction` model of `cpu_aot_x87_compare` and copies raw encodings. The cases cover every pair of the 34 special encodings, values against themselves, their neighbour in the lowest significand bit and their negation, random values, triples and random m64 values. In the four records `jp` was taken in 2124 of 6624 cases, and the moves read PF set in 2361 and clear in 4675 cases. Five mutated translations must each mismatch, each in a named export:

| Control | Mutation | Mismatches | Export |
|---|---|---|---|
| `pfnever` | the comparisons clear PF | 4959/17672 | 531 in `equal_branch` |
| `pfequal` | PF is set for equal instead of unordered | 6244/17672 | 660 in `classify` |
| `pfkeep` | the comparisons leave PF as it was | 4904/17672 | 127 in `sahf_fucomi` |
| `uinvert` | `fcmovu` moves on a clear PF | 4050/17672 | 1522 in `fucomi_fcmovu` |
| `nuinvert` | `fcmovnu` moves on a set PF | 2122/17672 | 1522 in `fcomi_fcmovnu` |

`pfkeep` mismatches in `sahf_fucomi` only where the `sahf` before `fucomi` left a different PF. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs. Translation rejects 10 sequences: `fcmovu` before any flag writer, `fcmovnu` after `test eax, eax`, `fcmovu` after `fucomi` and `inc eax`, `jp` after `fucomi` and `add eax, 1`, `fcmovu` after a call, a join of a `fucomi` path and a `cmp` path, `sahf` on `fnstsw ax` without a comparison before `fcmovu`, `fcmovu st, st(2)` with two values, `fcmovnu st, st(1)` with one value, and `setp` after `fucomi`, which the integer lowering does not implement. It translates 5 twins: `fcmovu` after `fucomi`, `fcmovnu` after `fucomi` and a `mov`, `jp` after `fucomip` and `fstp st(0)`, a join of a `fucomi` path and a `sahf` path, and `fcmovu` after `fucomi` and `lea`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_parity$' -V`.

## Mapping an image without LoadLibrary

`tools/pe_map.cpp` maps an x64 PE32+ image into memory it allocates itself, away from the preferred base, and applies its DIR64 and HIGHLOW base relocations. It binds ordinary and delay imports through a resolver supplied by the host, sets section protections, records the actual base in the mapped headers and registers `.pdata` with `RtlAddFunctionTable`. The original machine code runs unchanged. Header fields are checked against the file and image size before use. It then calls TLS callbacks and the entry point of a DLL; `pe_map_run` starts an EXE. TLS data templates are rejected. [Issue #46](https://github.com/R0Y00/Xbox2Windows/issues/46)

```powershell
cmake --build --preset dev --target pe_map cpu_reloc_original cpu_import_original_lifted cpu_ordinal_original_lifted cpu_import_altered cpu_ordinal_altered cpu_register_probe cpu_two_imports
ctest --preset dev --no-tests=error -R '^cpu_native_map$' -V
```

The test maps the named and ordinal import fixtures, `reloc_route` and a damaged copy of the register probe. In `reloc_route`, `call_mix` reaches the provider through an absolute code pointer in `.data`, which gives the image exactly one DIR64 relocation; the test checks that the mapped pointer equals its linked value plus the load delta. The two import fixtures have no relocation entries. For every image the test checks the following:

- the base differs from the preferred base and is recorded in the mapped headers;
- the IAT slot holds the export the resolver returned;
- `RtlLookupFunctionEntry` finds `call_mix` in the registered table until the image is released.

The mapped `call_mix` is compared over 249 input pairs on arguments, guarded writes, RBX/RSI sentinels and return value. It matches the reference and the same image loaded by Windows, and for the import fixtures it also matches the lifted DLL. If the import is bound to the altered provider instead of the linked one, only the return changes. An unbound import is rejected with its DLL and symbol or ordinal. Copies patched to have an entry point outside any executable section or an import directory that runs past the image are rejected before any import is requested. Like the Windows loader, `pe_map` finds the import table by its address and walks it to the null descriptor without using the directory `Size`; each descriptor is bounds-checked against the image. `two_imports` imports `mix_args` from the named provider and `probe` from the register probe. With its import directory `Size` left at 60 or patched to 0, 19 or 20 (ending after the first descriptor), `LoadLibraryExW` and `pe_map` both bind both IAT slots; the test only reads the slots and runs nothing in the image. The Windows loader does not read the delay-import directory at all: copies of the named fixture with an empty table, an address of 0, an address past the image or a descriptor naming a DLL all load. `pe_map` maps the first two, rejects a directory outside the image as a format error and one that lists a DLL as unsupported. In a copy of the register probe with the `clobber_control` export address slot zeroed, that name no longer resolves while `probe` still does.

With the preferred base reserved, Windows x64 and pe_map both mapped the source-built, entry-free relocation fixture elsewhere when its relocation directory RVA was zero. The two sizes tested were the linked size (12) and SizeOfImage + 1 (28673). Both loaders left the DIR64 slot at its linked value, 0x180001000. The fixture reads the slot without calling through it. A zero RVA is treated as an absent directory; the separate stripped-relocations control still rejects mapping away from the preferred base.

`cpu_native_seh` checks exception dispatch through a mapped image. In `seh_route`, `guarded_load(address, mode, trace)` names its own exception handler in its unwind data. The handler resumes an access violation raised by the load instruction at a recovery label with a fixed value in RAX, and declines every other exception. The cases run in one child process for the Windows-loaded original and another for the mapped copy, so a broken unwind crashes only the child and is reported as a failed check:

- a valid load returns the value;
- a load from address zero is caught by the image's own handler and returns the fixed value, with RBX and RSI restored;
- an illegal instruction, reached after the first trace write, is declined and unwinds through the mapped frame to the ctypes caller, which reports exception code `0xc000001d`;
- a later valid call still succeeds.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_seh$' -V
```

`cpu_native_section` compares every section of the register probe after `LoadLibraryExW` and after `pe_map`, byte for byte, on the original and on copies with one section header patched. The probe has no entry point and no imports, so the Windows load runs none of its code. On Windows 11 build 26300.9974, the loader reads section data in whole `0x200`-byte file sectors: it rounds `PointerToRawData` down and the end of the raw data, `PointerToRawData + SizeOfRawData`, up to a multiple of `0x200`. It copies that range up to the section's virtual size, and bytes past the end of the file stay zero. `pe_map` now reads the same range. The test covers a start inside a sector, a start in the first sector, which reads the headers from offset 0, a raw size below one sector and raw data that crosses a sector boundary. In each case Windows differs from an exact read of `SizeOfRawData` bytes at `PointerToRawData`. A last case shortens the file so that the rounded end of `.rsrc` lies past it.

The section test compares the original fixture's final page protections against Windows. On Windows 11 build 26200 the contributor observed that Windows mapped a copy with an unreadable `.pdata` section. The mapper applies final protections, validates the complete exception table, and returns `MAP_UNSUPPORTED` if any table page is unreadable. Only a readable table is registered for unwind lookup. The fixture also checks section alignment and rejects unsupported sub-page protection layouts.

A `PointerToRawData` of zero is the exception: Windows leaves that section zero-filled even when `SizeOfRawData` is set, and so does `pe_map`. The rounding cases patch `.rdata`, because in `.rsrc` a rounded start can put other data where the resource directory should be, and `LoadLibraryExW` then fails with `ERROR_BAD_EXE_FORMAT`. These results were measured with a `FileAlignment` of `0x200` and `0x400`. With `0x1000`, Windows filled more of a section whose raw size is below one sector than the rounded sector end allows, which remains open.

Against the end of the file, Windows checks raw data by two rules only. `LoadLibraryExW` fails with `ERROR_BAD_EXE_FORMAT` if any section's `PointerToRawData + SizeOfRawData` reaches 4 GiB, or if the last section in the table has raw data that ends past the file, compared without rounding. A section whose `SizeOfRawData` is zero is not checked. If the last section has no raw data, no section is checked against the file size. The raw data of any other section may run past the end of the file or start after it: Windows maps that section, and the missing bytes read as zero. `pe_map` now applies the same two rules. Before, it refused every section whose raw data ran past the file and accepted both copies that Windows refuses. The test adds three copies that Windows loads: `.rdata` raw data that runs past the file, `.pdata` raw data that starts after it, and `.pdata` with no raw data and an offset after the file. It also adds two copies that both loaders must refuse: `.pdata` with a raw range that reaches 4 GiB, and the file shortened by one byte so that the last section, `.rsrc`, runs past it.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_section$' -V
```


### HIGHLOW and mixed-width base relocations

The AMD64 mapper accepts `IMAGE_REL_BASED_HIGHLOW` as a four-byte addition of the
low 32 bits of the load delta, with unsigned wraparound. `DIR64` remains an
eight-byte addition. Unaligned entries are handled with byte copies. Bounds and
overlap checks use the actual fixup width; IAT writes retain their eight-byte
checks. Unknown types, header writes, writes into the relocation directory and
fixups extending past the image are rejected.

`cpu_native_highlow` modifies the relocation table of the source-built
`reloc_route` DLL and compares seven cases with `LoadLibraryExW` using
`DONT_RESOLVE_DLL_REFERENCES`. Both loaders are forced away from the preferred
base. The check compares relocated values and adjacent bytes, normalizing for
the different bases. Cases include zero/high-bit/all-one inputs, unaligned
entries, mixed DIR64/HIGHLOW entries, the final image dword and a dword ending
exactly before the relocation directory. Five malformed or unsupported controls
must fail. The patched pointer is never called. The original fixtures and test
are MIT source; no game inputs are included.

```powershell
cmake --build out/native --target pe_map cpu_reloc_original cpu_import_provider --parallel 4
ctest --test-dir out/native --no-tests=error -R '^cpu_native_highlow$' -V
```

Measured on Windows 11 build 26300.9974 with MSVC/ML64 19.44.35230 and
Python 3.12.8: the seven comparison cases and five rejection controls passed
with Debug and Release (`/O2`) builds. The selected existing native-map,
mutation and relocation-free tests also passed in both configurations.

[Microsoft PE relocation definitions](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#base-relocation-types)
define the four-byte HIGHLOW operation. This adds a mapper capability; no title
startup or use of this relocation by a particular title is established.

### TLS callbacks and the entry point

`tls_route` has an entry point and a TLS directory whose callback list holds one routine and whose data template is empty. Both routines report each call to `cpu_tls_sink.dll`, which outlives the image, so calls made during release stay visible. `cpu_native_tls` loads the image with `LoadLibraryW` and maps it with `pe_map`, each in its own child process, and compares the process notifications:

- Attach: with both loaders the TLS callback runs before the entry point, each with `DLL_PROCESS_ATTACH`, the module base and a null reserved argument.
- Detach: on Windows 11 build 26300, `FreeLibrary` called the TLS callback first and then the entry point, both with `DLL_PROCESS_DETACH`. `pe_map_release` follows that measured order. No Microsoft documentation specifies it.
- Failed attach: in `cpu_tls_refuse` the entry point returns FALSE. Windows then sends a full detach in the same order, and `LoadLibraryW` fails with error 1114. In the same case `pe_map` sends the same detach and returns `MAP_INIT_FAILED`.

Like the Windows loader, `pe_map` finds the TLS directory by its address and does not use its `Size`. On Windows 11 build 26200, copies with `Size` 0, 1 or 39 still run the callback before the entry point, under both loaders; with the address set to 0 only the entry point runs; a directory past the end of the image fails `LoadLibrary` with error 998 and `pe_map` with "TLS directory outside image". Between attach and release the test starts and joins one thread whose start routine is `Sleep`. On that thread both loaders call the TLS callback and then the entry point with `DLL_THREAD_ATTACH`, then the same pair with `DLL_THREAD_DETACH`, all with a null reserved argument. `pe_map.dll` is itself loaded by Windows, and its `DllMain` forwards these notifications to every mapped image: attach in mapping order and detach in reverse, which is what two `LoadLibraryW` copies receive on Windows 11 build 26300. The sink records the calling thread, so notifications from unrelated threads in the process are ignored. A thread created between an image's process attach and its registration receives the detach without the attach, as a thread that existed before mapping does. Each mapped image holds one reference for its registration and one per thread notification in flight. `DllMain` takes those references under the registry lock and calls the guest callbacks after releasing it, so a callback may map or release any image, its own included. `pe_map_release` unregisters the image and returns 1 at once. The image's process detach and unmapping run when its last reference goes, inside `pe_map_release` or on the thread that finishes the last notification, and happen exactly once. A notification is admitted when its thread takes the image's reference under the registry lock, and none is admitted once release has unregistered the image. An admitted notification may still run after release returns; it skips an image already marked released before calling it, but one whose callback has started finishes, so an image whose TLS callback releases it still has its entry point called for that thread attach. `cpu_native_lifetime` compares the two-image order with two native loads, and releases an image from inside a thread-attach callback, both another image and the image itself, through a hook armed for that thread only. A second release returns 0. Null, an interior address, a foreign `PAGE_NOACCESS` page and a foreign read-write buffer also return 0. The buffer keeps its content and protection, and the unreadable page shows that release does not read its argument. Without a notification in flight, release finishes the process detach, the removal of the image's `.pdata` table and the unmapping before it returns: `RtlLookupFunctionEntry` finds the entry point's function before the release and nothing after it, and the base is no longer committed. In 50 rounds, eight native threads spin on a shared flag and then release the same image at once; each round has exactly one 1 and one process detach. A stale pointer cannot identify an earlier instance: once the address is reused by a new mapping, releasing it releases that new mapping. When another thread releases an image whose TLS callback is blocked inside a thread attach, release returns 1 at once while the unwind entry is still present and no process detach has run. Once the callback returns, the entry point still receives that attach, the worker performs the detach and the table removal once, and the thread detach is not delivered. Damaged copies are rejected before any notification: one with a nonzero `SizeOfZeroFill` as TLS data, one with the entry point moved into `.data`, and one with its TLS callback pointing into `.data`. The entry point and callbacks must lie in an executable section. The TLS directory and each callback slot are read after section protections are applied, so `pe_map` checks the live page protection with `VirtualQuery` before every such read, on attach and on release. In `tls_route` the callback list has its own read-only `.tlscb` section. Copies with `.data` or `.tlscb` made inaccessible are rejected as "TLS directory not readable" or "TLS callback list not readable". A list made `PAGE_NOACCESS` after mapping is skipped on release while the entry point is still detached. `pe_map_export` applies the same rule to the export directory, the address and name tables and each name string, and accepts an export target on an execute-only page; a copy whose export section is mapped without read access resolves no export instead of returning a pointer into it. A list longer than 256 callbacks is rejected as unsupported. With `IMAGE_FILE_DLL` cleared, with or without an entry point, `LoadLibraryW` loads and frees the copy without calling the TLS callback or the entry point, and `pe_map` does the same. `pe_map` still applies the executable-section checks to such an image, where Windows would load it and call nothing. The callback list collected before attach is reused for the detach that follows a failed attach. The list is re-read on release; if it was overwritten with a non-executable address or made unreadable after mapping, only the entry point is detached. Implicit TLS data would need a slot in each thread's `ThreadLocalStoragePointer` array, which ntdll owns. `pe_map` therefore leaves `AddressOfIndex` as linked. In the test process, Windows wrote index 8 for this image even though its template is empty.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_tls$' -V
```

### Implicit TLS values on the main and worker threads

`cpu_tls_value` is a source-built DLL with one `__declspec(thread)` value (`7`). Its TLS callback (caller 1) and entry point (caller 2) each report the observed value in the sink's reserved slot. `cpu_native_tls_value` loads the image with `LoadLibraryW` and maps it with `pe_map`, each in a fresh child process, reads the value on the main thread, runs one worker thread whose start routine returns its own TLS value, joins it with a 10 s limit, and compares the sink events with raw thread IDs translated to semantic identities (`main`/`worker`) so two processes can be compared without comparing raw OS thread IDs. A worker timeout, a missing callback, or an unexpected value fails the test. The empty-template `cpu_tls_original` fixture is the control: it must load under both loaders.

The Windows comparison verifies that the Windows loader initializes implicit TLS and runs the callbacks on both threads with value 7. `pe_map_image` rejects the non-empty TLS data template with `MAP_UNSUPPORTED` ("TLS data") before any notification, so the mapped report carries that expected failure and no events, and the CTest passes by verifying exactly that differential. Any other mapped failure, a worker timeout, a missing native callback, or a native value other than 7 is a hard test failure. A repeat mapping must fail identically. This pins two missing mechanisms without changing the loader: `pe_map` leaves `AddressOfIndex` as linked and has no slot in each thread's `ThreadLocalStoragePointer` array (see the TLS paragraph above), and `pe_map_run` never sends thread notifications to an EXE (`notify` returns early for non-DLL images, so only `DLL_PROCESS_ATTACH` runs on the calling thread). This DLL test addresses the thread-local-value part of #165; it does not cover EXE startup: an EXE with implicit TLS data, an EXE's TLS callbacks on a second worker thread under `pe_map_run`, C-runtime startup in a mapped EXE, `GetModuleHandle(NULL)`, and the PEB remain untested and unsupported. No callbacks are manufactured and no production code is changed. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_tls_value cpu_tls_original cpu_tls_sink
ctest --preset dev --no-tests=error -R '^cpu_native_tls_value$' -V
```

### Starting a mapped EXE

`pe_map_image` maps an EXE without notifying it, as `LoadLibrary` does. `pe_map_run(mapped, &exit_code)` starts it: it calls the TLS callbacks in table order with `DLL_PROCESS_ATTACH` and a null reserved argument, then the entry point on the calling thread, and stores the entry point's return value in `exit_code`. It returns 0 and runs nothing for an image with `IMAGE_FILE_DLL` set, for an image without an entry point and for an unreadable callback list. `mapped` must be a live base returned by `pe_map_image`; the headers are read without validation, as in `pe_map_export`. The entry point and callbacks are arbitrary code running in the host process on the calling thread, so an image that crashes, hangs or calls `ExitProcess` takes the host with it; run unknown images in a child process, as the test does. Start each mapping at most once. The caller keeps the mapping live until the call returns and prevents any thread, TLS callback or entry point from starting or releasing the same mapping during that call. The PEB argument that Windows passes to an EXE's entry point is not forwarded, and no process-detach notification follows, because the host process goes on running.

`cpu_exe_start_original` is a source-built EXE without a C runtime. Its TLS callback and its body (`native_entry`) each append one base-4 digit to a log, then the body calls the named import `cpu_import_provider!mix_args(5, 3, out)`, which returns 13, and returns `(log << 8) | 13`. The PE entry point is the startup wrapper `native_startup` ([`exe_startup.asm`](../tests/cpu/exe_startup.asm)), which passes the body's value to `finish_exe`; a process launch of the EXE makes that value its exit code. `cpu_native_exe_start` compares the launch with `pe_map_image` and `pe_map_run` in a child process:

- Original: both give `0x60d`: the callback (digit 1, reserved null) ran before the body (digit 2), and the import returned 13.
- `cpu_exe_start_no_tls` has no TLS directory. Both give `0x20d`, so the log reflects the callback.
- The original bound to `cpu_import_altered`, whose `mix_args` returns one more, gives `0x60e` under `pe_map`.
- With `IMAGE_FILE_DLL` set in a copy, `pe_map_run` returns 0 and leaves the exit code untouched.
- With the PE entry point (the wrapper) moved into `.data` in a copy, the Windows launch faults with `0xc0000005`, while `pe_map_image` refuses it as "entry point outside executable section" and nothing runs.

As a one-off manual check (not in CI), I removed the callback loop from `pe_map_run`; the test then failed with `mapped original 0x20d != native 0x60d`.

This shows one EXE with an ordered start log and an import bound through the resolver. It does not run a C runtime startup, which would call `ExitProcess` in the host. It does not cover implicit TLS data, a second thread's TLS callbacks, static initializers in imported DLLs, `GetModuleHandle(NULL)` (which still returns the host) or the PEB. Those remain untested or unsupported. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_exe_start_original cpu_exe_start_no_tls cpu_import_provider cpu_import_altered cpu_exe_io cpu_import_live cpu_exe_io_hang
ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' -V
```

### Starting an EXE with two DLLs

`cpu_startup_exe` and two DLLs, all built without a C runtime, import each other in a known order. `cpu_startup_a` has no fixture DLL dependencies and keeps an event log and a value set by its initializer (`7`). `cpu_startup_b` imports `cpu_startup_a`, and its initializer publishes `a_value() * 2`, so it reads zero if it runs first. The EXE imports both. These initializers are the DLLs' `DLL_PROCESS_ATTACH` entry points, called by the loader, not C++ static constructors. The EXE's TLS callback logs `t` and its entry point logs `e`. The entry point then runs two worker threads one after the other and joins each with a 10 s limit. A's TLS callback allocates an explicit kernel32 `TlsAlloc` slot and sets `1` on the main thread. The workers set `2` and `3` in their respective copies of the slot. Both images use empty static TLS data templates. The entry point writes the event log and the callback trace to standard output and returns `b_value() | first worker << 8 | second worker << 16 | main << 24` as its exit code.

`cpu_native_startup` launches the EXE with the Windows loader in a directory holding the two DLLs, and starts it with `pe_map_run` in a child process. The harness reads the import tables, orders the images dependencies first, maps each DLL, serves the imports of the DLLs with `pe_map_export` on the mapped images and the kernel32 imports with the host. `pe_map` does the mapping, relocation, section protection, each DLL's attach notification and the start of the EXE; it does not read import tables to find dependencies. On Windows 11 build 26300:

```text
native: LOG:abtew2w3m1 exit 0x103020e
mapped: LOG:abtew2w3m1 exit 0x103020e
```

- The startup event logs and exit codes match, and both standard error streams are empty. Under both loaders `a` and `b` ran once and in dependency order, the TLS callback ran after the DLL initializers and before the entry point, the entry point ran once, and `b_value()` was `14`. The three threads read their own `TlsAlloc` values.
- Without B's initializer (`cpu_startup_b_noinit` under B's name) both loaders give `LOG:atew2w3m1` and exit code `0x1030200`. With another A under A's name (`cpu_startup_a_alt`, whose value is 9) both give the original log and `0x1030212`. Both controls differ from the original under both loaders.
- Mapping the EXE before its DLLs fails with `unresolved import cpu_startup_b.dll!b_value`.
- On Windows 10 build 19045.6456, A's TLS callback read `1` on process attach using the main thread ID. For each joined worker it read `0` on thread attach, then the worker's value (`2` or `3`) on thread detach. The harness checks the recorded IDs against the actual main and worker IDs in each process. Both loaders produce the same observations by thread role, including with B's initializer disabled and with the alternate A. The test measures callbacks of the dependency DLL. [Thread forwarding](https://github.com/R0Y00/Xbox2Windows/pull/139)

Not covered: `DLL_PROCESS_DETACH` ordering at exit, DLLs built with the C runtime (C++ constructors), implicit TLS and `GetModuleHandle(NULL)`. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_startup_exe cpu_startup_a cpu_startup_a_alt cpu_startup_b cpu_startup_b_noinit cpu_startup_exe_timeout cpu_startup_exe_create
ctest --preset dev --no-tests=error -R '^cpu_native_startup$' -V
```

### Lifecycle of two DLLs with the static C runtime

`cpu_life_a` and `cpu_life_b` are built with the static MSVC runtime (`/MT`, linked without `/INCREMENTAL`, so the PE entry point is the runtime's `_DllMainCRTStartup` and not an incremental-link thunk). A real global constructor runs before `DllMain(DLL_PROCESS_ATTACH)` and a real global destructor after `DllMain(DLL_PROCESS_DETACH)`. Nothing calls them by hand. B imports A. A's constructor sets its state to 7. B's constructor stores `a_value() * 2`, and its destructor reads A's state again. `b_via_a()` returns `a_value() + b_value()` at the time of the call. Every constructor, destructor and `DllMain` call logs a token into `cpu_life_sink.dll`, a small CRT-free DLL that the test host loads first and keeps for the whole child process, so it is valid during every destruction and sees the last destructor. `DllMain` only calls into the sink, which takes no lock and needs nothing from the loader.

Each constructor also records its callers with `RtlCaptureContext`, `RtlLookupFunctionEntry` and `RtlVirtualUnwind`. The test reads the entry-point function from `.pdata`, follows its closing `jmp rel32` to the function that dispatches the process attach and detach (this is why the entry point itself is not on the stack), and requires that function on the constructor's stack with the next frame outside the image. That frame is the loader's under Windows and `pe_map`'s under `pe_map`. `RtlCaptureStackBackTrace` is not used: on an image that `pe_map` mapped it returns only the first frame.

`cpu_native_crt_lifecycle` runs one scenario in fresh child processes with a 30 s timeout: through `LoadLibraryExW` and `FreeLibrary`, where the loader finds A as B's dependency and decides every order, and through `pe_map_image`, `pe_map_export` and `pe_map_release`. For `pe_map` the harness orders the images from the import tables, maps A before B, serves B's import of A with `pe_map_export` and releases B before A; `pe_map` itself keeps no dependency graph. The scenario loads B, calls the exports, changes A's state with `a_set(99)`, unloads, reloads, unloads, and then runs two more load and unload cycles. Three fresh processes per loader must report identical stages. On Windows 11 build 26300 both loaders give identical events, values and residency at every stage:

```text
load:           A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_value 14, b_via_a 21, a_ctor_runs 1
a_set(99):      (no events)                                 a_value 99, b_via_a 113
unload:         B:detach, B:dtor a=9, A:detach, A:dtor      A and B no longer mapped
reload:         A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_via_a 21, a_ctor_runs 1
unload again:   B:detach, B:dtor a=7, A:detach, A:dtor      A and B no longer mapped
cycles 3, 4:    the load and unload lines above
```

The reload starts from fresh state (7, not 99) and runs each constructor once more. B's destructor still sees A alive, with A's state at that time (`a=9`, then `a=7`). Residency is `GetModuleHandleW` for Windows and `VirtualQuery` on the mapped base right after the releases for `pe_map`.

- Without A's constructor (`cpu_life_a_noctor`) both loaders log `A:attach, B:ctor, B:attach`, every value is 0 and A's constructor is not among the callers. Without B's destructor (`cpu_life_b_nodtor`) both log `B:detach, A:detach, A:dtor` on unload. Both controls differ from the original in the events under both loaders.
- Base addresses: Windows loaded B at the same address in all three processes, as the system randomizes a DLL's base once per boot. `pe_map` mapped it at a different address every time and never at the preferred base, so the relocation path ran on every mapping.
- Responsibilities: Windows counts references. A second `LoadLibrary` of B returns the same module and runs nothing, and A stays loaded while B or the harness holds it. `pe_map` keeps no module table and no reference counts. Mapping B twice gives a second image that runs its constructor again. The order of mapping and release and the import of A are the harness's work.
- Mutation checks, not in CI: making `pe_map_release` skip the process detach, or not free the image, each made `cpu_native_crt_lifecycle` fail.
- Not covered here: a double release, null and foreign or interior pointers, which `cpu_native_lifetime` already checks for `pe_map_release` (it returns 1 once and 0 otherwise); and releasing A before B, which would touch released memory. Neither this test nor the harness depends on freed memory, apart from asking `VirtualQuery` about the released range right after the release.

```powershell
cmake --build --preset dev --target pe_map cpu_life_sink cpu_life_a cpu_life_a_noctor cpu_life_b cpu_life_b_nodtor
ctest --preset dev --no-tests=error -R '^cpu_native_crt_lifecycle$' -V
```

### An empty relocation directory away from the preferred base

`cpu_import_original` contains no absolute addresses. Its base-relocation directory has Size 0 and `IMAGE_FILE_RELOCS_STRIPPED` is clear. The issue used `cpu_register_probe`, which has the same properties; this test uses `cpu_import_original` because it exports `call_mix`, so a call can be checked after loading, and the register probe is the harness.

`cpu_native_reloc_free` runs each case in its own child process. It reserves the preferred base (`0x180000000`) for `SizeOfImage` and checks with `VirtualQuery` that the whole range is held by that reservation. It then loads the image with `LoadLibraryExW` and maps it with `pe_map`. A second copy has `IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE` cleared.

The discriminating case is Windows with the `DYNAMIC_BASE`-cleared copy. With nothing reserved, Windows loads that copy at `0x180000000`. With the base reserved, it loads it elsewhere and `call_mix` matches the reference on all 249 input pairs. The as-built copy also moves under Windows, but ASLR would move it anyway. `pe_map` always allocates away from `ImageBase`, so for it the reservation only shows that an empty relocation directory is accepted at another base and that the image runs there.

The bases vary between runs, so the test checks only that they differ from the preferred base. With the base reserved, a copy that sets `IMAGE_FILE_RELOCS_STRIPPED` fails in `LoadLibraryExW` with error 487 (`ERROR_INVALID_ADDRESS`) and is rejected by `pe_map` as "relocations stripped"; `pe_map` would reject it with or without the reservation.

`pe_map` therefore does not reject an empty relocation directory when it maps away from `ImageBase`, because Windows loads such images. An image that contains absolute addresses but omits its relocations is malformed and would fail at run time under either loader. Measured on Windows 11 build 26300. [Issue #77](https://github.com/R0Y00/Xbox2Windows/issues/77)

```powershell
cmake --build --preset dev --target pe_map cpu_import_original cpu_import_provider cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_native_reloc_free$' -V
```

### Mutated images

`cpu_native_mutation` applies a fixed list of single-field edits to the header, the first section header, and the export, import and base-relocation directories of `cpu_register_probe` and `cpu_import_original`, plus three truncations. Each case runs in its own process and must print either `MAP_OK` or a named `MapStatus` with a detail string; a crash or a timeout fails the case. Across 46 cases the outcomes were 26 `format`, 16 `ok` and 4 `relocation`, with no crash. A three-line mapper that dereferences null makes the parent report `child exited 0xc0000005`, so the harness detects the failure mode it covers.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_mutation$' -V
```

### Forwarded exports

`cpu_forwarder` exports `local_value` and forwards `mix_args` to `cpu_import_provider.mix_args`; the linker records that string in the export directory. `pe_map_export` keeps returning null for the forwarder. `pe_map_export_resolved` follows the string through the host resolver, and the returned address matched `GetProcAddress` on a `LoadLibrary` copy in `cpu_native_forwarder`. `local_value` resolves to the same RVA under both loaders. An absent name and a resolver that returns null give null. The `Module.#ordinal` form is parsed but has no fixture yet.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_forwarder$' -V
```

## VEX.128 16-bit packed-integer leaf functions

The [translator](../tools/cpu_vex_aot.py) lifts Windows x64 leaf functions that use VEX.128 word forms. It supports:
- `vpaddw`, `vpsubw`, `vpmaxsw`, `vpminsw`, `vpmulhw` and `vpavgw`;
- `vpsraw`, `vpsllw` and `vpsrlw` with an immediate count;
- `vpunpcklbw`, `vpunpckhbw`, `vpunpcklwd` and `vpunpckhwd`;
- `vpackuswb`;
- the 16-byte moves `vmovdqu` and `vmovdqa`, the logic ops `vpxor`, `vpand` and `vpor`, and `vzeroupper`.

The ABI belongs to this profile: RCX and RDX hold the addresses of two 16-byte inputs, R8 the address of a 16-byte output, and no return bits are defined. Each XMM register becomes a 16-byte value and each form an explicit per-lane loop, so the generated C++ states the wraparound, the unsigned saturation of signed words in `vpackuswb`, the shift-count limits and the `vpavgw` rounding instead of using intrinsics. Memory operands must be exactly `[RCX]`, `[RDX]` or `[R8]` with a 16-byte access, and stores go only to `[R8]`. Only VEX encodings and the volatile XMM0-XMM5 are accepted. Any other instruction, an EVEX form such as a masked `vpaddw`, a legacy SSE encoding, XMM6-XMM15, a VEX.256 operand or another operand shape is rejected with its RVA. Decoding uses iced-x86, and function bounds come from `cpu_leaf_aot.bounded_code`.

```powershell
cmake --build --preset dev --target cpu_vex_words_original_lifted cpu_vex_words_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_vex_words$' -V
```

The [fixture](../tests/cpu/vex_words.asm) has five functions. The test runs each through the native and lifted DLLs on the same 4096 input pairs:
- every pairing of eight edge words, including 0x7fff, 0x8000, 0xffff and 0x00ff, broadcast to all lanes;
- the edge words rotated across lanes;
- seeded random lanes.

Each call runs in one 16-byte-aligned arena in which the two inputs and the output are separated by 0xcc guard zones. The whole arena must match bit for bit, and the native call must leave everything outside its output unchanged.

**Alignment.** `vmovdqu` accepts any address, and the four functions that use only `vmovdqu` also match on buffers 8 bytes off alignment. `vmovdqa` requires 16-byte alignment. `aligned_ops` is called only with aligned buffers, because the generated C++ does not check the precondition that the native instruction would fault on.

**Aliases.** The output may alias either input exactly. Reads and the store then happen in program order, each as one 16-byte copy. With the output aliasing A and then B, and with RCX equal to RDX, all five functions match on 512 pairs. `store_reload` stores, reads the inputs again and stores a second result; with the output aliasing an input, its result differs from the non-aliased one in 955 of these cases, so the order is observed. Partial overlaps are not tested.

**Controls.** A second fixture build replaces one `vpsraw` with `vpsrlw` in `shift_edges`. Its lifted DLL matches its own native build and differs from the original in 4044 of 4096 outputs, while the other functions are unchanged. Translation rejects three patched copies: a VEX.256 load, a VEX.256 store, which uses a three-byte VEX prefix because of R8, and a `vpaddw` turned into `vpaddd`. It also rejects eight hand-encoded bodies, each first checked against its iced disassembly: an EVEX `vpaddw xmm0{k1}`, a legacy `paddw`, `xmm6` as destination and as source, `[rcx+10h]`, `[rax]`, a store to `[rcx]` and `vpsraw` with an XMM count.

Sixteen mutations of the translator each fail the test:
- `vpavgw` without rounding;
- `vpmulhw` keeping bit 15;
- `vpsraw` counts above 15 not clamped;
- `vpsllw` and `vpsrlw` counts above 15 not cleared;
- `vpackuswb` without saturation;
- `vpunpckhbw` and `vpunpckhwd` reading the low half;
- `vpmaxsw` comparing unsigned;
- either YMM check removed;
- the VEX-encoding check removed;
- either nonvolatile-register check removed;
- stores deferred to the end of the function;
- input reads cached.

Only the alias checks catch the last two.

## String memory transfers in the control-flow CPU lifter

`tools/cpu_switch_aot.py` lowers MOVS and STOS, with and without REP, to copy and fill 1-, 2-, 4- and 8-byte elements in either direction. These instructions
can occur inside branches, loops and translated callees; they use the existing
register-definedness and nonvolatile-save proofs. A zero REP count touches no memory
and leaves the pointers unchanged. The loop updates RSI/RDI and RCX at their architectural widths.

Each element is read before it is written. Forward overlapping copies deliberately
observe bytes written by preceding iterations, unlike `memmove`. Volatile guest
reads and writes preserve access faults and iteration order under `/O2`. Multi-byte accesses allow unaligned addresses and keep the original element width.
Arithmetic flags survive a transfer and remain available to the existing bounded
flag-consumer proof. STD/CLD are represented by a local direction flag. A control-flow proof requires DF clear on every path to calls, tail jumps and returns, including joins with different direction states. The backwards path clears DF before leaving its copy region. Non-repeated forms preserve RCX.
Address-size overrides, segment prefixes, REPNE, LODS, CMPS and SCAS remain rejected
in this control-flow profile. The shared straight-line translator has its separate
string-instruction coverage. Guest exception resumption, native REP register values
at a fault, concurrent memory observation and throughput equivalence are not modeled.

The fixtures, reference and implementation are original MIT source. They use
source-built buffers, not title code. An assembly harness checks physical RSI/RDI
preservation and DF on return. The independent byte-array model checks complete
buffers and their surrounding bytes, pointer updates, counts, results and carry
from a compare before the transfer. Macro-generated instruction forms are recorded
from the decoded functions only after all comparisons and controls succeed.

```powershell
cmake --build out/native --target cpu_switch_strings_lifted cpu_switch_strings_lifted_o2 cpu_switch_strings_mutants cpu_string_probe cpu_guarded_call --parallel 4
ctest --test-dir out/native --no-tests=error -R '^cpu_aot_switch_strings$' -V
```

Windows 11 build 26300.9974, MSVC/ML64 19.44.35230 and Python 3.12.8:
43 exports, 24,915 native/AOT/reference comparisons and 96 access-violation
comparisons passed, including the `/O2` translation. Compiled incorrect
implementations must fail the comparison: a bulk `memmove` on destructive overlap
and a truncated repeat count, plus ignored backwards direction.
The comparison includes inaccessible addresses with a zero count, and a read that
copies three bytes before faulting at a protected page; no later trace store executes. The same fault must occur with a repeat count of 2^32 + 1, so the full 64-bit RCX is used. Non-overlapping copies run from an even and an odd source address, and an overlapping copy runs with the destination 8 bytes past the source. `rep stos` also runs with a nonzero fill whose low byte is zero, with RCX = 2^32 (zero ECX) at a protected destination, with a two-byte `rep stosb` from the last writable byte (the first byte is stored before the fault), with a word that straddles a protected page (nothing is stored), and with a one-byte store just below a 4 GiB boundary, so RDI must carry into its upper half. It also compares native and translated memory effects when a word, dword or qword straddles the protected page.
No title startup result is claimed.

The Windows ABI boundary follows [Microsoft's direction-flag convention](https://learn.microsoft.com/en-us/cpp/build/x64-software-conventions).
MOVS/STOS semantics follow the [Intel SDM](https://cdrdv2-public.intel.com/868137/325462-089-sdm-vol-1-2abcd-3abcd-4.pdf).

## Switch dispatch through jump tables

Compilers lower a dense `switch` to a bounds check and an indirect `JMP` through a table of 32-bit entries. The [jump-table translator](../tools/cpu_switch_aot.py) recognizes the two x64 layouts in common use. MSVC stores image-relative entries and loads them through a register holding `__ImageBase`. LLVM stores signed offsets from the table and adds the table address. The translator reads the table from the PE, then emits a C++ `switch` whose cases jump to labels at the decoded targets. The host compiler builds its own dispatch from that switch.

```powershell
cmake --build --preset dev --target cpu_switch_original_lifted cpu_switch_swapped_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_jump_tables$' -V
```

The decoder starts at the export and follows branches, fallthrough and table entries, so it never decodes the table bytes that MSVC places after a function's code. An indirect jump through a register is accepted only in the sequence `load entry; add base; jmp`, reached by the fallthrough of an unsigned `JA` or `JAE` against an immediate. The table length comes from that bound. A must-agree constant pass over the control-flow graph gives the base register's RIP-relative value, which also covers a base loaded once outside a loop. Value numbering over the single-entry block that contains the bound shows that the index register holds the compared value, zero- or sign-extended from 32 bits. Every table entry must be the first byte of a decoded instruction inside the function, and the table must not overlap any decoded instruction. Flags are modelled only between a producer (`CMP`, `TEST`, `ADD`, `SUB`, `AND`, `OR`, `XOR`, `INC`, `DEC` or `NEG`) and its consumers: a conditional jump, `SETcc` or `CMOVcc`. Producer and consumer must sit in one straight-line run that no edge enters; only instructions that leave the flags unchanged (moves, `LEA`, `NOT`, `SETcc`, `CMOVcc` and the sign extensions `CBW`, `CWDE`, `CDQE`, `CDQ` and `CQO`) may lie between them. Overflow, parity, signed and carry conditions are supported for these producers as defined by their affected flags, using their operand width and the low result byte respectively (`INC` and `DEC` preserve `CF`, so carry conditions after them remain rejected). A forward pass records how many low bytes of each register (0, 1, 2 or 8) every path defines, and rejects a read of more bytes than that. Writes to AH, BH, CH and DH, 16-bit `CMOVcc` and indirect calls are rejected; direct calls are described below. Stack operations and nonvolatile registers are accepted only under the frame rules described below.

The translator also lowers `JO/JNO/JP/JNP`, `SETO/SETNO/SETP/SETNP` and
`CMOVO/CMOVNO/CMOVP/CMOVNP`, along with `ADD` carry/signed conditions
(`B/AE/BE/A/L/GE/LE/G`), `INC`/`DEC` signed conditions (`L/GE/LE/G`), and all 16
`NEG` conditions (`CF = (result != 0)`, `OF = (result == sign_bit)`). `ADD`,
`SUB` and `CMP` overflow is computed from saved unsigned operands; `INC`, `DEC`
and `NEG` use their wrapped result at the signed boundary. Logical producers
clear overflow. Parity uses only the low result byte, as defined by the [Intel SDM](https://cdrdv2-public.intel.com/843827/253665-sdm-vol-1-dec-24.pdf).
The existing straight-line and register restrictions still apply, including
rejection of 16-bit `CMOVcc`, carry conditions after `INC`/`DEC`, and flags
from shifts or across joins.

`cpu_aot_flag_conditions` compares 289 MASM exports with their generated C++
and an independent Python reference over 532,140 inputs. The fixtures cover
ten flag producers at 8/16/32/64 bits, guarded byte stores, 32- and 64-bit
conditional moves, conditional branches, and a memory-operand `NEG [mem]` whose
lifted statement reads memory once via `load_always`. Both operand registers are
clobbered before the consumers to verify that the flag inputs were saved.
Negative controls verify that a variable-count `SHL RAX, CL` producer,
`INC`/`DEC` followed by carry conditions (`JB/JAE/JBE/JA/SETB/CMOVB`), and
`NOT` without a preceding producer or with `sets_flags` are rejected.
The former `SETO` rejection control now checks successful translation.
The test also records its decoded mnemonics through `FORM_COVERAGE_RECORD`.
Coverage reads that record for this parameterized MASM fixture instead of
interpreting macro tokens. Only a passing test with a current record covers the
arithmetic flag instruction forms; failed, skipped and missing records
cannot add green cells.

```powershell
cmake --build --preset dev --target cpu_flag_conditions_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_flag_conditions$' -V
```

The supported functions take three integer words in RCX, RDX and R8 and return RAX, of which only the low bytes described below are compared. An entry can instead be translated with a fourth integer word in R9, as described below. Stack arguments (the fifth and later) and floating-point arguments are not supported. A leaf function without a `.pdata` entry is bounded by its executable section. Its walk may not enter an interval owned by another function's unwind entry.

The [source fixture](../tests/cpu/switch.asm) exports seven functions. `select_rva` uses the MSVC layout, and `select_rel` computes the same function through the LLVM layout. Both have seven cases, including two selectors that share a target, a case that falls through into the next, and a default path. `run_ops` runs a byte program in a loop and dispatches every byte through a five-entry table. It stops at the first opcode above 4. Through the register probe, the test compares each original export with the matching lifted function, a reference written in Python, and RBX/RSI sentinels. It runs 600 selector and operand pairs per layout and 138 programs. Half of the pairs carry nonzero upper register halves that must not reach the table index. A control assembles the same source with two entries swapped in each table, and its translation must follow the swapped reference. `shift_narrow` dispatches through a six-entry LLVM-layout table to byte and word `SHL`, `SHR` and `SAR` of a memory destination by `CL`. x86 masks the count to five bits for 8- and 16-bit operands as well, so counts of 8 to 31 still shift (a byte holding 1 shifted left by 8 becomes 0). The translator performs these shifts in 64 bits and truncates the stored result; 4,608 inputs, covering every count from 0 to 31 and count bytes above 31, match the original and the reference. `flag_select` dispatches through an eight-entry LLVM-layout table to `SETL`, `SETBE`, `SETG` into a memory byte, `SETNE` after a 16-bit compare, and `CMOVA`, `CMOVNS` (64-bit, after `TEST`), `CMOVGE` from memory with a `MOV` between compare and consumer, and `CMOVE` after `TEST` with an immediate. It compares EDX with bits 32 to 63 of RDX and returns RAX whole. A 32-bit `CMOVcc` therefore shows that it clears bits 32 to 63 even when the condition is false, and a byte `SETcc` shows that it keeps them. The translation reads a memory source of `CMOVcc` on both outcomes, as the processor does. 4,000 inputs at the signed, unsigned and 16-bit boundaries must match the original and the reference. The swapped-table build exchanges the `SETL` and `CMOVA` entries and must follow the swapped reference. `cmov_read` executes `CMOVNE` after `CMP ECX, ECX`, which never moves but still reads its memory source: called with a null R8 it must fault in the original and in the translation. The translator therefore performs every read of guest memory through a `volatile` load; with an ordinary load, an optimized MSVC Release build drops a read whose value is unused and the null call returns. Each instruction's memory operand is read once, at its own width, into a local declared before the rest of the statement, and every use of the operand names that local: an `ADD` whose result and flags both come from one operand performs one read. Reads of the function's own frame array, which cannot fault, stay ordinary loads. The [dead-read fixture](../tests/cpu/dead_read.asm) stores 1 to the trace before each read and 2 after it. Its exports read a value that is overwritten, compare memory with flags that are never read, read one byte, read two operands whose sum is cleared, return the value read, skip the read on a branch, and take a sum and its zero flag from one read, into a register and in place. `cpu_aot_dead_reads` builds the translation with the project's build and again with a separate `cl /O2` command, calls the original and each translation inside `__try` with fresh trace words and a fresh page, and compares the structured exception code, the return value, the trace and the page. For a readable operand all three return the reference. For a null operand, and for an operand whose read crosses into an inaccessible page, the original raises an access violation after the first marker and before the second, and both translations must do the same; an operand that ends exactly at the page boundary returns, at each width, and the second read of the pair faults when only the first is readable. On the branch that skips the read, the same inaccessible addresses return normally with the first and third markers written. A source control checks that each memory operand is read once through the `volatile` load and that the shared reads are used twice. An unoptimized build performs an ordinary load as well, so the `/O2` builds are the ones that show the difference. Stores are unchanged.

`select_rel_edges` bounds a three-entry LLVM-layout table with `JAE`, places its default before the guard, maps all three entries to one target placed more than 32 KiB before the table, and leaves a zero DWORD after the table. Selectors 0 to 2 must return the operand and selector 3 its complement, in the original, the translation and the swapped-table build.

On a copy of the linked DLL, the test patches the code, the table or `.pdata`, and expects each change below to be rejected:

- the bound jump removed or made signed;
- a sign extension that no longer comes from the compared register;
- a table entry into an instruction interior or into the neighbouring function;
- a table placed over code, or a second entry into the dispatch block;
- a base that is not the image base, or that does not come from a RIP-relative `LEA`;
- an undefined register, a read of a nonvolatile register's entry value, or an unsupported instruction;
- a leaf whose walk reaches another function's unwind interval;
- in `flag_select`, a `SETcc` into `AH`, a flag-writing `SHL` between a compare and its `CMOVGE`, and a second entry that reaches a `SETBE` with another path's flags.
- a `JGE` bound on `select_rva`, a 64-bit load of its 32-bit table entries, and a `select_rel_edges` table load whose base register differs from the register the entry is added to.

When `.pdata` no longer covers `select_rva`, the translation must stay unchanged.

On Linux, the fixture was assembled and linked with LLVM (`llvm-mc`, `lld-link`) from an equivalent GNU-syntax copy, because `llvm-ml` does not accept `IMAGEREL`. The original machine code ran in-process through an `ms_abi` call. It matched the lifted C++ built with clang on every input, for both the original and the swapped tables. The same translator also handled C switch statements compiled by clang for `x86_64-pc-windows-msvc` at `-O1` and `-O2`. Those builds have no `.pdata` for the leaves, and the loop's table base is hoisted. The generated sources compile without warnings under clang-cl `/W4 /WX`. For `flag_select`, the same Linux run matched the original and the reference on all 4,000 inputs, and the swapped table changed 762 results. The translator before this change rejected `flag_select` at its first `SETL`. Clang output at `-O1` and `-O2` for small C comparisons, selects and clamps (`SETL`, `SETE` into `R8B`, 32- and 64-bit `CMOVcc`) matched its translation on 25,000 calls per level. On Windows 11 with MSVC 14.44 and ML64, `cpu_aot_jump_tables` passed before `flag_select` was added: 600 selector pairs per table layout and 138 programs match, the swapped tables change 60, 60 and 99 results, and all 13 rejection controls and the leaf-without-unwind translation pass. The table labels are declared with `LABEL DWORD`, because ML64 rejects `IMAGEREL` of a code label inside a memory operand.

The jump-table translator wraps a 67h (32-bit) effective address at 32 bits and zero-extends it, as the hardware does. It rejects EIP- and ESP-based operands. `cpu_aot_switch_addr32` builds `lea rax, [ecx+1]`, `lea rax, [ecx-8]`, `mov eax, [ecx-8]` and `mov [ecx+8], edx` from separate MASM source. It compares the native and translated images over wrapped sums and upper-half register noise, using a page committed below 4 GiB for the load and store. Before this change the translation returned `0x100000000` for `lea_wrap(0xffffffff)` where the hardware returns 0. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_switch_addr32$' -V`.

The generated C++ follows address order, so a function that continues in code below its entry starts with a jump to the entry. A leaf without unwind data can reach such code through a jump, because it may use any unowned bytes of its section. Before this jump was emitted, the translation of such a function began at its lowest instruction and never ran the entry. `cpu_aot_entry_order` builds `inc_square`, which increments RCX and jumps to `square_tail` below it, and `pick_tail`, which branches there on a comparison. Both match the native exports on return, guarded trace, RBX and RSI over 249 input pairs. The control export `square_tail`, whose entry is its lowest instruction, still starts at the entry. A function whose chained unwind range lies below its entry takes the same path and has no fixture yet. A conditional branch whose target is its own fallthrough also emits a `goto`, so its target gets a label. Before that label was emitted, such a branch was accepted but its translation did not compile (`C2094: use of undefined label`). The export `branch_next` (`cmp rcx, rdx; jb` to the next instruction) checks that the assembled branch targets its fallthrough, that the target is labelled, and that native and translated calls match over the same 249 input pairs.

An 8- or 16-bit register write leaves the other bytes of the register unchanged, as on x86. The generated C++ merges the new byte or word into the 64-bit variable, which is declared with an initial value. The defined byte count becomes the larger of the old count and the write width, and a 32- or 64-bit write defines all eight bytes. A `SETcc` into a byte register is such a write, so it no longer needs the rest of the register to be defined; after `setl al` only AL is defined. A 16-bit `CMOVcc`, which would merge into the low word only when its condition holds, stays rejected. A 16-bit `IMUL` multiplies in `std::uint32_t`, because two `uint16_t` factors are promoted to `int` and `0xffff * 0xffff` overflows it. A three-operand `IMUL` (`imul r, r/m, imm`) writes its destination without reading it, so the destination need not be defined before it, while the two-operand form reads its destination. `RET` and `RET 0` (`C2 00 00`, which MSVC emits) no longer read RAX. The return width is the number of consecutive low RAX bytes that are definitely defined at every reachable `RET` or `RET 0`: 0, 1, 2 or 8, the lower bound over joins and over returns. It counts defined bytes and does not establish a source-language type. When no normal return (a `RET`, or a tail jump through a bound import as described below) is reachable, the width is `None` and the output says `no reachable normal return`, which describes the reachable control flow and makes no termination claim. Microsoft's [x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention#return-values) states that the state of unused bits in the value returned in RAX is undefined. The generated function still returns a 64-bit `word`, callers need no width declaration and may use only those bytes, and a comment states the width unless it is 8. The upper bytes that the generator initializes carry no guest result. A `RET` reached while RAX may hold a guest address, for example from a RIP-relative `LEA`, is rejected, so such a function is not reported with a narrower width.

`cpu_aot_narrow_returns` builds [`narrow.asm`](../tests/cpu/narrow.asm) and translates thirteen exports: byte merges into defined registers, word `ADD`, `SUB`, `IMUL` and `MOVZX`, byte `SHL` and `SAR` and word `SHR` and `SAR` by `CL`, byte `INC`, `NOT` and `DEC` and word `NEG`, a comparison that writes only AL, a function that writes EAX on one path and only AL on the other, a copy that writes only AX, `MOVZX` from AL, `SETL` with a byte `MOV` between the compare and the `SETL`, `SETB` after a byte compare followed by `MOVZX`, two functions that store a word and write no RAX byte, one ending in `RET 0`, and `self_loop`, a loop that branches to itself. `self_loop` is translated and inspected and is never called: it must report `None` and `no reachable normal return`, while the storing function with a reachable `RET` must report width 0. Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs; the shift export uses counts 0 to 31 and the count bytes 0x21, 0x3f, 0x80 and 0xff under nonzero upper halves. The original must match the reference in all of RAX, the guarded trace and RBX/RSI; the translation must match in the low return-width bytes of RAX and in the rest. The probe enters each function with RAX holding the function's own address, and the original's RAX bytes above the return width still hold that value on every path that leaves them unwritten. The test also checks the widths reported by the translator. Seven controls must be rejected: `mov al, dl` patched to `mov ah, dl`, `movzx eax, al` patched to `add eax, 0` after `mov al, dl` and after `setb al`, `RET 0` patched to `RET 8`, a join where one path defines only AL before `add eax, 1`, `cmovb ax, dx`, and `lea rax, byte_merge; ret`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 3,819 inputs matched, the two inspected exports reported `None` and 0, and all seven controls were rejected. The Windows job runs the same test. The export `imul3_fresh` writes R10, R9W and EAX with three-operand `IMUL` before any read of them and is compared the same way, and an eighth control, `imul2_undefined`, whose `imul eax, ecx` reads the undefined EAX, must be rejected with `undefined register rax`; on Windows 11 with MSVC 19.50, 4,568 inputs matched, 249 of them for `imul3_fresh`, and all eight controls were rejected. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_narrow_returns$' -V`.

Whoever invokes the translator can give an entry a fourth integer argument in R9 by listing it as `name:4` in the export list of the command line, of `translate` or of the CMake function `cpu_translation`. `name` and `name:3` keep three arguments, and any other count is rejected with `unsupported argument count`. The count is that caller's choice. The translator does not derive it from the machine code, which does not show how many parameters the source function declares. On a four-argument entry R9 is defined at entry and the generated function takes a fourth `word r9` parameter, so a read of R9 is accepted whether or not the source function declares a fourth parameter. A three-argument entry, the default, translates as it did before this option: R9 is undefined at entry, a read before a write is rejected with `undefined register r9`, and a write followed by a read is accepted with `r9` declared as a local. The host must declare and pass the same number of arguments as the entry was translated with. A four-argument entry that never references R9 has an unused parameter, which `/W4` reports.

`cpu_aot_fourth_argument` builds [`fourth_arg.asm`](../tests/cpu/fourth_arg.asm) and translates `r9_mix:4,r9_loop:4,no_r9:4,r9_scratch`. `r9_mix` computes `lea rax, [rcx+r9*2]; sub rax, rdx` and stores R9. `r9_loop` counts additions down in R9D through `CMP`/`JA`, `TEST`/`JZ` and `DEC`/`JNZ`, and stores R9 as it leaves it. `no_r9` does not touch R9. `r9_scratch`, a three-argument entry, writes R9B before any read of R9, reads only R9B and then overwrites R9. The register probe gains `probe4`, which passes its fifth argument to the function in R9 and samples RBX/RSI through its sixth. The test checks that the built source is the translation of that list. It calls each four-argument entry through `probe4` and the three-argument entry through `probe`, and compares native and translated calls with a Python reference on the return value, the guarded trace, RBX and RSI. The four-argument entries get the direct-call input pairs crossed with ten fourth arguments, among them 0, 1, 64, 65, 2^32 - 1, 2^32, 2^63 and all ones, and for `no_r9` the fourth argument must not change any result. As a control for an existing three-argument entry, `byte_merge` from `narrow.asm`, which writes R9 before reading it, keeps its three-parameter signature and its `r9` local and still matches through `probe`. `r9_mix` must be rejected with `undefined register r9` when translated with three arguments and accepted as `r9_mix:4`. The counts `5` and `x` must be rejected, and so must a copy of the image with the index register of `r9_mix` patched from R9 to R10, with `undefined register r10`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 7,719 calls per side matched and every control passed. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_fourth_argument$' -V`.

Whoever invokes the translator can have RIP-relative data translated by passing `--image-data` on the command line, `image_data=True` to `translate`, or `IMAGE_DATA` to the CMake function `cpu_translation`. Without it, RIP-relative data is refused as before and the generated C++ is unchanged. With it, the generated DLL also exports `lifted_bind_image(base)` and `lifted_unbind_image()`. The host passes the base of a live mapping of the input image, loaded by Windows or by [`pe_map`](../tools/pe_map.cpp). The translated functions keep their signatures and read the bound base once, at entry. A RIP-relative memory operand becomes a load or store at base + RVA. The translator requires its whole width to lie inside one section's virtual extent (`VirtualSize`; a mapping zero-fills the part past the file-backed bytes), a readable section for a read, and a writable, non-executable section for a write. Otherwise it reports the target as outside the image, outside every section, crossing the end of a section, or a write to a read-only or executable section. A RIP-relative `LEA` yields base + RVA for any RVA inside the image, so the register can be stored, compared, used as a base or returned. A `LEA` that loads a recognised jump-table base stays a guest address that the switch replaces, so a function whose only RIP-relative reference is a table base needs no mapping.

`lifted_bind_image` accepts a mapping only if it is the input image. It compares an FNV-1a hash, computed by the translator from the file, of the DOS signature and `e_lfanew`, the PE signature and file header (with `TimeDateStamp`), `SizeOfImage`, `CheckSum` and the section table, and returns 2 when they differ. It then hashes the file-backed bytes of every readable, non-writable section, except the 8-byte slots that DIR64 relocations rewrite and the import address table, and returns 3 when they differ. A null base returns 1 and an accepted mapping 0; a refused bind keeps the current binding. The hash detects accidental differences, not a deliberately constructed image. Writable data and relocated slots are not compared. A loader write to other read-only bytes, such as the CFG pointer slots of an image linked with `/guard:cf`, would make bind refuse the mapping; this was not measured. While nothing is bound, a translated function that addresses image data raises the noncontinuable structured exception 0xE0494D47 at entry, before any access. An address that a translated function returns or stores is valid only while that mapping stays bound and mapped, and a later bind does not move it. A relocated pointer loaded from the image, such as a vtable entry or a pointer in `.data`, holds the absolute address in that mapping, as it does for the original code. A code address loaded this way cannot be called through the translation, because indirect calls stay rejected. Bind and unbind must not run concurrently with each other or with a translated function.

`cpu_aot_image_data` builds [`image_data.asm`](../tests/cpu/image_data.asm) with a counter and a relocated pointer in `.data`, a scratch word in its zero-filled tail, and a read-only table of two relocated code addresses and a constant. Its eleven exports read the counter, add to it and return the old value, return its address, load the relocated pointer, compare an argument with the counter's address, store the table's address into an object and return the object, load a table entry, exchange the scratch word, load through a pointer, dispatch through an MSVC-layout jump table whose cases update the counter or the scratch word, and dispatch through an LLVM-layout table without data. The test checks the disassembly: 18 RIP-relative operands, no absolute memory operand. The original runs in one `pe_map` mapping, and the translation is bound to a second mapping of the same file; both start from the image's initial data. After each of 29 calls, the test compares the return value, the guarded trace, RBX/RSI and the 1,048 writable bytes of the two mappings with each other and with a Python reference. An address inside the image is compared as its RVA, the address minus the mapping's base. The exports that return an address and the relocated slot in `.data` are known from the fixture, not inferred from values. Writing through the counter address that each side returned changes what both sides then read. In one mapping, the translation reads the original's write and the original reads the translation's. After unbind and a bind to a third mapping, results follow that mapping. Bind returns 1 for a null base, 2 for another DLL and for a copy with one byte of `TimeDateStamp` changed, and 3 for a copy with one byte changed in the read-only table or in a `.text` jump table; each refusal leaves the binding unchanged. A copy with a changed initial counter in `.data` is accepted. Bind also accepts the system loader's mapping. A child process calls translated functions while nothing is bound: the data exports raise 0xE0494D47, an object passed to `set_vtable` stays unchanged, and the two exports without image data run. On copies of the image, the translator rejects a load and a `LEA` outside the image, a load in the headers, an 8-byte load 4 bytes before the end of `.data` and a write to `.rdata`. Without `--image-data`, four exports are rejected with the messages they had before, and an export named `bind_image` is rejected. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, every comparison and control passed. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_image_data$' -V`.

A function may use its stack frame. A forward pass over the control-flow graph gives RSP at every instruction as the entry RSP plus a constant. `PUSH` and `POP` of a nonvolatile register and `SUB` or `ADD RSP` by a multiple of 8 are the only changes accepted. The constants must agree where paths join, so a loop that pushes on every iteration is rejected, and every `RET` must find RSP at its entry value. Any other write of RSP (`MOV RSP, RAX`, `AND RSP, imm`, `SUB RSP, RCX`, `LEAVE`, `ENTER`) is rejected as a dynamic frame. Any use of RSP as a value (`MOV RAX, RSP`, `LEA RAX, [RSP+8]`, an RBP frame pointer set from RSP) is rejected because the frame's address would escape, and so are `PUSH` and `POP` of other operands, which MSVC did not emit in the call-free functions measured. The generated function declares one zero-initialized byte array per call. It runs from the lowest RSP the function reaches to the end of the caller's 32-byte home space at entry RSP + 40, and each RSP-based operand becomes a `load` or `store` at a constant index into it, so RSP is never a C++ value. The array is at most 4,096 bytes: MSVC calls `__chkstk` before it allocates a page or more, and that call's target has no unwind entry, so it is rejected (see below). It is declared `alignas(16)` and starts at the lowest RSP rounded down to 8 mod 16, so each byte has the address residue modulo 16 of the stack byte it stands for, given the entry alignment the ABI guarantees. Every access goes through `memcpy` and the address never escapes, so this alignment cannot change a value; what the translator checks is the exact offset of every access, the bound and that every RSP change is a multiple of 8. An access below the current RSP (Windows x64 has no red zone), of the return address, or at or above entry RSP + 40 (the fifth argument and later) is rejected. A second forward pass follows every frame byte on every path. A read needs each byte written on every path, so home space, which the caller only reserves, must be written before it is read. Bytes are tracked one by one, so an 8-byte store can be read back as its upper 4 bytes and stores of 4, 2, 1 and 1 bytes as one 8-byte value. When `ADD RSP` or `POP` raises RSP, the bytes it leaves below RSP become unwritten again, after a `POP` has read its source: Windows may change memory below RSP, so a later allocation of the same bytes must write them before it reads them, and a register saved there can no longer be restored from them. RBX, RBP, RSI, RDI and R12 to R15 may be used if every `RET` is reached with each one holding its entry value again on every path. The same pass records which register and which frame bytes hold the entry value of which register, through `PUSH` and `POP` and through a 64-bit `MOV` to and from the frame. MSVC uses both, the second to save into home space before it allocates. An entry value is not an argument: it may be saved, and its saved bytes may only be restored into the same register. Any other read of it, including a read after the restore, is rejected. The frame bytes of a saved register hold the generated local's value at the save, which is its initial 0, and are never read as data. A register byte that may hold a guest address is never defined, as above, and a store reads its source register, so a guest address cannot be stored in the frame and reloaded. The generated function is ordinary C++ that the host compiler gives its own frame. Exception unwinding through the original frame layout is not reproduced, and memory outside the frame is accessed as before.

`cpu_aot_stack_frames` builds [`frame.asm`](../tests/cpu/frame.asm), with the unwind directives each prologue needs, and translates nine exports. `push_loop` pushes and pops RBX and RSI and uses them in a loop. `spill_widths` allocates 24 bytes, stores 8, 4, 2 and 1 bytes, reads them back in overlapping pieces, adds to a slot in place, and uses `SETB` into the frame and `CMOVL` from it. `home_save` has the MSVC `/O2` shape: it saves RBX and RSI into home space with `MOV [RSP+8], RBX` and `MOV [RSP+10h], RSI` before `PUSH RDI` and `SUB RSP, 20h`, restores them with `MOV` on each of two returns, and carries the unwind codes MSVC writes for that prologue. `home_args` is the `cl /Od` output for a small C function that stores its arguments to home space, writes a local on both sides of a branch and reloads it. `two_paths` pushes RSI on one path and RDI on the other, rejoins at the same depth, and returns early on a third path after restoring RBX. `home_spill` spills two arguments into home space with no allocation of its own, and `loop_counter` keeps a counter and an accumulator in the frame across a loop's back edge. `realloc_slot` releases its allocation with `ADD RSP`, allocates the same bytes again and writes the slot before reading it. `frame_bound`, whose frame array is exactly 4,096 bytes, is translated and compiled but never called. Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs, on the return value, the guarded trace, RBX and RSI, and every path of each export is taken. The probe shows that the original saves and restores RBX and RSI and that the translation leaves the caller's RBX and RSI intact. It does not show that the translation reproduces the original's save and restore: the generated locals are not the physical registers, and the host compiler saves and restores those itself. The test checks that the built source is the translation of the export list, that each frame array has the expected size, and that `rsp` appears nowhere in the generated C++. Thirty-two controls must be rejected. Twenty-six are copies of the image with one or two instructions replaced by others of the same length: an unbalanced `POP`, two paths that join at different depths, a loop that pushes on every iteration, a nonvolatile register not restored on one path, restored from another register's slot by `MOV` or by `POP`, or loaded into RAX from its save slot, a local written on one path only, home space never written, a 4-byte store removed under a later 8-byte read, a loop counter written only on the back edge, a stack argument at entry RSP + 40, a read and a write of the return address, `LEA RAX, [RSP+8]`, `MOV RAX, RSP`, `SUB RSP, RCX`, `SUB` and `ADD RSP` by 14h, `ADD RSP` at entry, a `JAE` left reading the flags of `SUB RSP`, an access below RSP, a frame of 4,112 bytes, `realloc_slot` without the store into its second allocation, `ADD RBX, RCX` on the caller's RBX, `PUSH RAX` and `PUSH 5`. The six others are exports rejected as written: a saved RBX whose slot is released by `ADD RSP` and allocated again before `POP RBX`; a slot that `POP` read and released, allocated again and read without a new store; a RIP-relative address spilled to the frame and reloaded, refused at the store as a read of an undefined register; RBX saved twice and reloaded from the 8 bytes that straddle the two copies; RBX pushed as data after only BL was written; and a register read after `POP` restored it. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 1,992 calls per side matched and every control was rejected. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_stack_frames$' -V`.

The same translator follows direct calls and tail jumps to other functions of the image ([#317](https://github.com/R0Y00/Xbox2Windows/issues/317), step 2). A `CALL rel32`, or a direct `JMP` that leaves the function's owned ranges, must target a function entry of this image: the first byte of a verified primary runtime function (an unchained `.pdata` record with version 1 or 2 unwind data and a non-empty interval in executable code), or a non-forwarded code export in an executable section that lies outside every runtime-function interval. The start of a chained secondary range, an interior label (exported or not), a record start with any other unwind header, a forwarded export and a data export are not entries, and the message says which of these the target is. Each callee is translated once into the same source, as a static function named `fn_<rva>` that takes RCX, RDX, R8 and R9, and is defined before its first caller. A call to the caller's own entry is rejected as self-recursion and any longer cycle as mutual recursion. A caller is rejected when any callee is, and the message names the callee's RVA and its reason. A callee records how many low bytes of each argument register it may read while that register may still hold its entry value, and the caller must have those bytes defined at the call. After a call, RAX has the callee's return width of defined bytes. RCX, RDX and R8 to R11, the flags and the callee's 32-byte home space at RSP are undefined. The return address the `CALL` pushes and the callee's own frame lie below RSP, where the frame proof already holds every byte unwritten, because the bytes that `ADD RSP` and `POP` leave below RSP become unwritten; so they stay undefined if RSP later falls back over them. Nonvolatile registers keep their state, because each callee is itself checked to restore every one, and so do the other frame bytes, including saves by `MOV` into the caller's own home space above entry RSP, which the callee's home space cannot reach. RSP must be 8 mod 16 from entry at a call, so the native call is aligned, and the callee's home space must lie below the return address. A tail jump is checked like a `RET`, with RSP at its entry value and every nonvolatile register restored, and contributes the callee's return width. The callee's frame is its own array, so no frame address crosses a call. A frame address passed to a callee (`LEA RCX, [RSP+20h]`), stack arguments, indirect calls, callees with no reachable normal return, import thunks, and targets with no unwind entry or export (such as leaves that MSVC emits without `.pdata`) unless the invoker binds them as described below, stay rejected.

`cpu_aot_direct_calls` builds [`calls.asm`](../tests/cpu/calls.asm) and translates seven exports, which reach three unexported callees placed below them. `call_leaf` calls a leaf from a frame and keeps the trace pointer in RBX across the call. `chain3` calls `mid_step`, which spills RDX into its own frame above the callee's home space, calls the same leaf and reloads it, so the leaf is shared. `narrow_call` calls a callee that defines only AL and widens it with `MOVZX`. `narrow_tail` tail-jumps to that callee and reports width 1, `tail_call` tail-jumps to the leaf, and `tail_after_frame` pops its frame before tail-jumping to `mid_step`. `home_save_call` has the MSVC `/O2` save shapes around a call: RBX saved by `MOV` into its own home space before `SUB RSP, 28h`, and RSI saved by `MOV` into the allocation above the callee's home space. Through the register probe, the original must match a Python reference, and the translation must match the original on the defined return bytes, the guarded trace, RBX and RSI over 545 input pairs per export. The test checks that each callee is defined exactly once and that the reported return widths are 8 except for `narrow_tail`. Twenty copies must be rejected with their stated reason: a call to the caller's own entry, a call from `mid_step` back to `chain3`, calls into an instruction interior and to an instruction boundary that is not an entry, a target outside the image and one in `.rdata`, reads of RCX, R8 and the full RAX after a call, a second call to the leaf whose RCX and RDX the first call left undefined, a `SETE` after a call, a stack-argument read in a callee and an unsupported instruction two levels down, a spill and a `MOV` save of RSI moved into the callee's home space and reloaded after the call, a call at entry RSP - 0x30 and one at entry RSP - 0x8, a tail jump with a frame still allocated, a tail jump with RBX not restored, and, as built, `below_call`, which stores a slot at entry RSP - 0x40, raises RSP for a call whose return address overwrites it, and reloads it after RSP falls back (natively it returns the return address). Removing the volatile clobber accepts the RCX control, keeping RAX fully defined after a call accepts the narrow control, keeping the callee's home space defined accepts the spill control, dropping the callees' argument demands accepts the second-call control, translating a shared callee again for each caller emits it five times (and the C++ fails with `C2084`), and naming the wrong callee at a call fails `chain3`. On Windows 11 build 26200 with MSVC 19.50 and ML64, all 3,815 calls matched and every control was rejected. The C++ generated for the existing jump-table, `addr32`, entry-order, narrow, fourth-argument and frame fixtures is unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_direct_calls$' -V`.

`cpu_aot_call_entries` builds [`call_entries.asm`](../tests/cpu/call_entries.asm), whose `.pdata` and `.xdata` are written explicitly so the image has a primary function continued by a chained secondary range. It also exports an interior label of a primary function, a `.data` variable as `DATA` and a forwarder to `kernel32.GetTickCount`. The test reads the image's own records and checks that the translator's entries are exactly the four primary runtime functions. A framed `CALL` and a tail `JMP` to the primary leaf translate and match the original and a Python reference over 249 input pairs each. Eight retargeted copies must be rejected with the reason that names the distinction: the call retargeted to the exported interior label, the chained range's start, the data export and the forwarder; the tail jump retargeted to the interior label and the chained start; and the leaf's unwind header patched from version 1 to version 3, for both the call and the tail jump. The translator before this rule refused the first six later and for unrelated reasons (`no unique function interval`, `not in an executable section`, `edge outside owned ranges`), and accepted the last two. Removing the version check, or accepting an export at the start of an unverified record, accepts the version controls. Counting interior exports or chained ranges as entries, or classifying forwarders and data exports as code, fails the check of the entry table. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_call_entries$' -V`.

On a Release (`/O2`) build of this change with MSVC 19.50, every `.pdata` entry of the 96 readable non-generated images was translated with four arguments, each unique function body once. Of 2,343 unique functions, 78 translated with the frame translator before this change and 97 after it, and none was lost. Of the 19 new ones, 8 are in `calls.asm`, 2 in `call_entries.asm`, 8 are callers in the other MASM call fixtures and 1 is MSVC output, a tail jump in `cpu_life_a.dll`, which is translated but not run. The most common first refusals are now a call target with no unwind entry or export (806 functions), mostly MSVC leaves without `.pdata` and import thunks, and a refused callee (299). Among refused callees, the innermost reason is an indirect transfer (`unsupported control flow`) in 186 and again a target with no unwind entry in 102. A frame address passed to a callee is the first refusal of 32.

A target with no unwind entry and no export is refused by default. The message gives the target RVA and says that a range binding is needed. A missing `.pdata` entry, clean decoding, no RSP change and a path to `RET` together do not show that an address starts a distinct function: an aligned interior label has all four. The invoker can supply that evidence with `--bind-ranges MANIFEST` (or `RANGES` in the CMake function `cpu_translation`). The manifest is JSON with the image's SHA-256 and a `[start, end)` RVA pair per function, taken from trusted evidence for that exact build. [`tools/range_manifest.py`](../tools/range_manifest.py) writes one from the source labels that an MSVC `/MAP` file lists, a function's symbol and its `_end` label. The translator checks:
- that the hash matches the image;
- that each range lies in the file-backed bytes of an executable section;
- that no range overlaps another binding or any runtime-function interval.

A call or tail jump may enter a bound range only at its start. An interior target, an instruction interior included, is refused with the range it lies in. The bound callee is walked within its range alone: an edge that leaves it, reaches another function's unwind interval or another bound range, or enters unbound code is refused, and the refusal names which of these it is. The callee may not move RSP or call, may not be an import thunk, and is subject to the recursion rules above. PDB function symbols are another possible source of the same evidence and are not read yet.

`cpu_aot_bound_callees` builds [`bare_calls.asm`](../tests/cpu/bare_calls.asm) with `/MAP`, whose two leaves are declared without `FRAME`, so the image has no `.pdata` entry for them, and are not exported. The build writes the manifest from the map and translates `call_bare`, `twice_bare` (both calling the same leaf from a frame) and `tail_bare` (a tail jump to a leaf that defines only AL) with it. Through the register probe, the translation matches the original and a Python reference on the defined return bytes, the guarded trace, RBX and RSI over 545 input pairs per export. Without the manifest, the same image's call and tail jump are refused with the target RVA and the binding needed. `bare_mix_inner`, an aligned interior label from which `MOV`, `SUB` and `RET` translate, does translate when it is bound as its own range. Sixteen controls must be rejected with their stated reason:
- the manifest applied to a copy whose `TimeDateStamp` differs;
- a call to that interior label and to an instruction interior, with the leaf's range bound;
- a tail jump into the other leaf's interior;
- a range in `.rdata`, a range past the end of `.text`, a range overlapping a framed function and two overlapping ranges;
- a range that ends before the leaf's `RET`;
- the second leaf jumping into a framed function, into the first leaf's range and into unbound code;
- a bound `JMP [RIP+slot]` thunk;
- the leaf patched to call, to `PUSH` and `POP`, and to call itself.

Skipping the hash check, treating any address in a range as its start, walking a bound callee over its whole section, or allowing a range to overlap a runtime function each accepts its control. Removing the check against overlapping bindings, the executable-bounds check, the thunk check or the leaf rule leaves its control rejected by a later check (the other range as a foreign interval, the unbound target, `unsupported control flow`, the call-site alignment), so the test fails on the message. On Windows 11 build 26200 with MSVC 19.50 and ML64, all 1,635 calls matched and every control was rejected. The C++ generated for every earlier fixture of this translator is unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_bound_callees$' -V`.

The corpus walk above passes no manifest, so its counts are unchanged: 97 of 2,346 unique functions translate, before and after this option, and none is lost or gained. Unbound targets are counted only as candidates for a binding. Without one, 872 functions are refused first at a target that needs a binding (808 calls and 64 tail jumps); counting a refused callee's innermost reason, 974. They reach 421 distinct target RVAs, 129 of them shaped like an import thunk, which a binding would not admit.

One-operand `MUL` and `IMUL`, `DIV` and `IDIV` and the sign extensions `CBW`, `CWDE`, `CDQE`, `CDQ` and `CQO` read and write RAX and RDX without naming them. MSVC emits them for 64-bit multiply-high, division and modulo. Operand 0 of such a multiply or divide is a source. The translation copies every input into a `const` temporary before it writes RAX or RDX, because `mul rdx`, or a source addressed through RDX, reads a register that is also an output. A sign extension is computed in `std::uint64_t` as `(x ^ s) - s`, where `s` is the top bit of the source, and `CDQ` and `CQO` as `0 - (x >> (bits - 1))`. Unsigned arithmetic wraps by definition, so these forms involve no signed conversion and no shift of a negative value. Factors of up to 32 bits are widened to `std::uint64_t`, sign-extended for `IMUL`. Their product needs at most 64 bits, so the wrapped product is exact. An 8-bit multiply writes AX, and a 16-bit multiply merges into AX and DX; the byte-wise definedness pass counts these as 16-bit writes. 64-bit products use `_umul128` and `_mul128` from `<intrin.h>`, which the generated source now includes. Quotients use `_udiv64`, `_div64`, `_udiv128` and `_div128`. These execute the hardware `DIV` or `IDIV`, so a zero divisor or a quotient that does not fit raises the same divide error as in the original, and the translation emits no C++ `/` or `%`. The signed intrinsics take signed parameters. Converting a register value of 2^63 or more to them is implementation-defined in C++14 and C++17, which MSVC defines as wrapping, and defined as wrapping in C++20; the translator already relies on this conversion for `MOVSX`, `SAR` and the signed conditions. With MSVC 14.52 `cl /O2 /FAs`, each divide of the fixture compiles to one `div` or `idiv` and each 64-bit multiply to one `mul` or `imul`, with no call and no added check. The Debug build that CTest uses also keeps them inline. Reaching a divide raises its error even when both results are unused: the translation stores the quotient to a `volatile` local, so the compiler must perform the divide when the function overwrites RAX and RDX without reading them. Without that store, an `/O2` build removed such a divide, and a divide by zero returned in the translation and faulted in the original. `cpu_aot_dead_divides` checks this on [`dead_div.asm`](../tests/cpu/dead_div.asm), whose exports store 1 to the trace before the divide and 2 after it, then overwrite both result registers: 32-bit `DIV` and `IDIV`, a 64-bit `DIV` of RDX:RAX, a 64-bit `IDIV` after `CQO`, and `div_skip`, which branches around its divide when the divisor is zero. The translation is built twice, by the project's Debug build and by a separate `cl /O2` command, and a small C++ helper calls each function inside `__try` and returns the structured exception code. For zero divisors, quotients that do not fit, valid divides and the skipped divide, both builds must give the code, the return value and the trace that the original gives. The original is the oracle; the test itself only requires that an input expected to fault yields one of the two integer divide codes and leaves the second trace word unwritten. Inputs that do not fault are also compared through the register probe. 8-bit divides, which write AH, and 16-bit divides, which no intrinsic executes, are rejected. `MUL`, `IMUL`, `DIV` and `IDIV` set flags that the flag model does not describe, so a flag consumer after one of them is rejected. The Intel SDM lists "Flags Affected: None" for the sign extensions, so they may lie between a producer and its consumer. The constant pass for table bases and the value numbering for table indices treat these instructions as writes of RAX and RDX.

`cpu_aot_wide_arithmetic` builds [`wide_arith.asm`](../tests/cpu/wide_arith.asm) and translates thirteen exports: `div32` (EDX:EAX built from RCX, divided by EDX moved to R9D), `idiv32` (`CDQ`, then `IDIV`), `div64` (`XOR EDX`, then `DIV`), `udiv128` (RCX:RDX divided by a fixed divisor), `idiv64` (`CQO`, then `IDIV` of a memory operand), `mul64` (`mul rdx`), `imul64_1` (one-operand `IMUL` of a memory operand addressed through RDX, which it overwrites), `mul32` and `imul32`, `mul8` and `mul16` (8- and 16-bit `MUL` and `IMUL`), `widen` (`CBW`, `CWDE` and `CDQE`, alone and chained) and `sign_dx` (`CDQ` and `CQO` between a `CMP` and its `SETL`). Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs that do not fault, plus the largest quotients that fit, the minimum dividends and inputs across the sign boundaries. The reference rounds signed quotients toward zero itself, since Python's `//` rounds toward minus infinity. RAX, the guarded trace and RBX/RSI must match. Eight faulting inputs, zero divisors, unsigned quotients that do not fit, -2^31 / -1 and -2^63 / -1, must raise the same `OSError` through ctypes in the original and in the translation, and leave the same trace. The test accepts `exception: integer divide by zero` or `exception: integer overflow`, provided both sides report the same. Eleven controls must be rejected: `div r9d` patched to `div r9b` and to `div cx`, a `JE` right after `DIV`, `CDQ` patched to `CLC` between `CMP` and `SETL`, a `MUL` and a `CDQ` whose RAX is no longer written (rejected at the `MUL` and at the `CDQ`), `JC` after `MUL`, two jump tables whose base or index register a `MUL` or `CQO` overwrites before the dispatch, a store of RDX after an 8-bit `MUL`, which leaves RDX holding a guest address from a RIP-relative `LEA`, and the same function with `div ecx`, which reads that EDX. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 2,929 inputs matched, all eleven controls were rejected, and all eight faulting inputs raised `exception: integer divide by zero` on both sides; Wine reports every divide error this way. Whether Windows reports the overflow cases as `integer overflow` was not observed here. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_wide_arithmetic$' -V`.
MSVC ends a function whose last action is a call to an imported function with `jmp qword ptr [rip+slot]`, a tail jump through the import address table (IAT). The jump-table translator accepts it only through an explicit binding, in the form the [import-call translator](#calling-another-dll-through-a-named-pe-import) uses: `--bind PROVIDER_DLL SYMBOL` names the provider and the slot's import by name or by `#ordinal` (with `--link-name` for an ordinal), `--import-arguments N` gives the number of integer arguments the import takes (3 or 4), and `--return-bytes N` gives the number of guaranteed low RAX bytes of its return (0, 1, 2 or 8; 0 is a normal return with no guaranteed result bits). `cpu_translation` forwards them from `PROVIDER`, `SYMBOL`, `LINK_NAME`, `IMPORT_ARGUMENTS` and `RETURN_BYTES`, and takes `NAME` so that one image can be translated with two bindings. Like an entry's own argument count (`name:4`), these numbers are chosen by whoever invokes the translator; none of them is derived from the machine code or from the provider. The entry's count and the import's count are separate choices. The decoder accepts an indirect `JMP` through a RIP-relative 64-bit memory operand without a segment prefix (`FF 25`, or `48 FF 25` with the REX.W prefix that MSVC also emits), and the jump ends the path; the 67h (EIP-relative) form, a segment prefix and a far jump stay rejected. As for import calls, the slot must hold a unique ordinary import whose DLL name equals the provider's file name and whose name or ordinal matches the binding, and the provider must export it as executable code. One binding covers one IAT slot for all exports of a run. A tail jump without a binding, a second distinct slot, a binding that no reachable call or tail jump uses and a slot that is not the bound import are rejected. The generated C++ declares the import with exactly N `word` parameters and replaces the jump with `return import(...);`, passing RCX, RDX, R8 and, for four, R9. Each passed register must be fully defined at the jump and hold no guest address, otherwise the translation is rejected with `undefined register`. A four-argument entry therefore passes on its incoming R9, and a three-argument entry can call a four-argument import only after writing all of R9 itself. The jump ends the path, so no register or flag clobber after the call needs modelling. A tail jump is a normal return of the function: it contributes the binding's number of guaranteed bytes to the return width, which is the conservative minimum over tail jumps and reachable `RET`s and is `None` only when neither is reachable. A function whose only normal returns are tail jumps therefore has a width. For a function with a tail jump the generated comment always states the width and both of its sources, for example `// RAX: 2 low byte(s) at every reachable normal return: 2 defined at every reachable RET, 8 guaranteed after every tail jump to mix4 as the binding states` (`no RET is reachable` replaces the middle part when there is no `RET`), and the declaration of the import is preceded by `// mix4: bound import with 4 integer argument(s) and 8 guaranteed low RAX byte(s), as the binding states; neither number is read from the provider`. For such a function the CLI prints the same width text after `RAX: `.

The bound import must take three or four integer arguments and return in RAX. Stack arguments (the fifth and later) and floating-point arguments are not forwarded, and the translator cannot check the argument count or the guaranteed bytes against the provider: a wrong binding gives wrong code. The import's name must not be one that the generated prelude declares through `<cstdlib>` or `<cstring>`: with `free`, `memset` or `strlen`, MSVC 14.52 rejects the declaration with error C2733 (a second declaration of a function with C linkage). Binding C runtime functions is left for a later change.

`cpu_aot_import_tail_jump` builds [`tail_import.asm`](../tests/cpu/tail_import.asm) three times, each linked against one provider built from [`tail_provider.cpp`](../tests/cpu/tail_provider.cpp): with named exports, with the same exports returning one more, and with ordinal-only exports (`#3` and `#4`, bound with the link names `mix3` and `mix4`). `mix3(first, second, output)` and `mix4(first, second, output, fourth)` record their integer arguments in three output words and return a value that depends on each of them. Each caller is translated twice. `tail_mix`, a three-argument entry, changes RCX and RDX and jumps to `mix3`, bound with three arguments. `tail_choose,tail_pass4:4` is bound to `mix4` with four. `tail_choose`, a three-argument entry, either writes R9 and jumps to `mix4`, or stores a word, writes only AX and returns. `tail_pass4`, a four-argument entry, changes RDX and jumps to `mix4` with its incoming R9; its jump carries a REX.W prefix (`DB 48h` before the `JMP`). Both bindings state 8 guaranteed bytes, so `tail_mix` and `tail_pass4` report width 8 and `tail_choose` width 2, the bytes defined at its `RET`. The test checks in each caller that every tail jump is `FF 25` (`48 FF 25` in `tail_pass4`) with a displacement to its own import's IAT slot, and in the ordinal caller that both imports carry the ordinal flag. It checks that the built sources are the translations of those lists, that each declaration and call carry the binding's number of arguments and each signature the entry's own number, and that R9 is a local of `tail_choose` and a parameter of `tail_pass4`. It translates `tail_choose` with 8, 1 and 0 stated bytes and `tail_mix` with 8, 2 and 0 stated bytes, and inspects the widths and comments: the width is the lower bound over both kinds of exit, and a function without a reachable `RET` gets a width rather than `None`. Every native and translated export is compared with a Python reference over the direct-call input pairs, the three-argument entries through `probe` and `tail_pass4` through `probe4` with six fourth arguments, which change its result on every pair. The return value is compared whole after a tail jump and in its two defined bytes after the `RET`, together with the guarded trace, RBX and RSI. The translations bound to the altered provider must follow the altered reference and differ from the named ones only in the return value. Thirteen bindings or copies must be rejected inside the translator: no binding; the symbol `mix4` for the slot of `mix3`; the altered provider's file for the named caller; the name `mix3` for the ordinal-only slot; the jump's displacement patched to the IAT's terminating null entry; the jump patched to a far jump (`FF 2D`); an EIP-relative (`67 FF 25`) and an FS-relative (`64 FF 25`) tail jump in place of the `LEA` of `tail_guest_arg`; `tail_guest_arg` itself, which passes the address of `tail_mix` in RCX; `tail_pass4` translated as a three-argument entry with the four-argument binding, where R9 is not written; `tail_choose` with its R9 writes patched to `mov r9b, cl` and `NOP`s, so that R9 is only partly written at the jump; `tail_choose` with its AX writes patched to a tail jump through the slot of `mix3`, a second slot; and `tail_choose` with its `JAE` patched to `JMP`, so that no tail jump is reachable and the binding is unused. The test asserts no loader error codes. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 5,976 calls per side matched, 5,589 of them through a tail jump; the altered provider changed the return of all 249 `tail_mix` calls, of the 120 `tail_choose` calls that take the tail jump and of all 1,494 `tail_pass4` calls, and all thirteen controls were rejected. The providers' argument counts and return widths are known from their source, so the test does not show how a binding's numbers would be established for an import without source. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_import_tail_jump$' -V`.

An ordinary call of the bound import is translated through the same binding, one IAT slot for all functions of a run. Three forms reach the slot: `call qword ptr [rip+slot]` (`FF 15`), a direct `CALL rel32` of an import thunk, and a direct `JMP` to one. An import thunk is the `jmp qword ptr [rip+slot]` (`FF 25 disp32`) that the linker adds when the caller declares the import without `__imp_`; it counts only at a target outside every unwind interval and range binding. In each form the slot must be an IAT slot of the ordinary import directory; otherwise the instruction keeps its earlier refusal (`unsupported control flow`, or a call target that is not a function entry). The binding then checks the slot as for a tail jump: a slot that holds another import is rejected by name, and a run without a binding is rejected with `no binding for the call ...` or `no binding for the tail jump ...`. A jump to a thunk becomes the tail jump through its slot described above. The slot's eight bytes must also lie in one section with `IMAGE_SCN_MEM_READ`, a check that now applies to tail jumps too, because the translation calls the import that the binding names instead of reading the slot. The ordinary import directory and readable section flag establish the slot identity and expected initial mapping. During execution of the original and its translation, the caller keeps the image and bound target valid and ensures that the slot stays readable and contains the declared target. Delay-loaded imports, rebinding and page protection changes are outside the explicit binding contract. The call becomes `rax = import(rcx, rdx, r8);` (with `r9` for four arguments) under the rules of a direct call: RSP is 8 mod 16 from entry at the call, the 32 bytes of home space lie below the return address, and each register of the binding's argument count must be fully defined, which also excludes a guest address (both are rejected as `callee argument ... is undefined`). After the call RAX holds the binding's number of guaranteed low bytes (none for 0); RCX, RDX, R8 to R11, the flags and the 32 bytes at RSP are undefined; the nonvolatile registers and the rest of the frame keep their state. That last part is a term of the binding: the import is taken to follow the Windows x64 calling convention, which the translator cannot check against a provider. A call adds nothing to the return width except through the RAX bytes defined at a later `RET`. As for any call, a function without unwind data may not call the import, and a callee reached through the call graph may itself call or tail-jump to it. The C runtime names of the previous paragraph cannot be bound for a call either.

`cpu_aot_import_calls` builds [`import_calls.asm`](../tests/cpu/import_calls.asm) twice, linked against the named and the altered provider of `cpu_aot_import_tail_jump`, and translates each caller three times; every export takes three arguments. `call_keep,call_twice,outer_call,pass_result,read_then_call,call_then_read` is bound to `mix3` with three arguments and 8 guaranteed bytes. `call_keep` keeps x in RBX and y in a frame slot above the import's home space across `call [__imp_mix3]` and combines both with the result; `call_twice` calls the import twice and writes RCX, RDX and R8 again between the calls; `outer_call` calls the unexported `inner_call`, which calls the import; `pass_result` returns the import's RAX. `low_result`, which reads only AL of the result, is bound to `mix3` with 1 guaranteed byte. `thunk_call,thunk_tail,outer_tail` is bound to `mix4` with four arguments: `thunk_call` calls the import thunk with a direct `CALL`, `thunk_tail` jumps to it, and `outer_tail` calls `inner_tail`, which tail-jumps through the IAT. The test checks that each call through the slot is `FF 15 disp32` and that the thunk is `FF 25 disp32` to the slot of `mix4`, outside every unwind interval and not exported. It checks that the built sources are the translations of those lists, with the import declared once and the expected numbers of calls, tail jumps and callees, and that `pass_result` reports widths 8, 2, 1 and 0 for those numbers of stated bytes and `thunk_tail` 8 and 0. Through the register probe, every export except the two that read memory at x is compared with a Python reference over the 249 direct-call input pairs on the whole return value, the guarded trace, RBX and RSI, for both providers and for the named caller's three translations built again with a separate `cl /O2` command. The altered provider's translations return a different value on every pair. `read_then_call` and `call_then_read` read the word at x before and after the call. Through `guarded_call`, with x readable, null and in an inaccessible page, the original and both builds of its translation give the same exception code, return value and trace: an access violation before the call leaves the trace unwritten, and one after the call leaves the arguments the import recorded. With a null trace pointer, `pass_result` raises the same access violation inside the import on all three. Thirty-four bindings or copies must be rejected inside the translator: no binding, for a call through the slot, a call and a tail jump to the thunk, and a call in an internal callee; `mix4` bound for the slot of `mix3`, and `mix3` for the thunk's slot; the second call of `call_twice` patched to the slot of `mix4`; the call of `call_keep` patched to `mov eax, 1`, which leaves the binding unused; the section that holds the IAT without `IMAGE_SCN_MEM_READ`; reads of RCX, RDX, R8, R9, R10 and R11 after the call; a `SETE` after the call; the full RAX read with 1 guaranteed byte and AL read with 0; RDX not written again before the second call; R9 not written before the call of the four-argument import; a guest address in RCX; a value spilled into the import's home space and reloaded after the call; RSP misaligned at the call; the call of the thunk retargeted to the thunk's second byte, to the `FF 25` tail jump inside the unwind interval of `inner_tail`, and to an `FF 25` through the IAT's terminating null entry, which is not an import slot; a range-bound callee without unwind data that calls the import; a call through the IAT's terminating entry; `call qword ptr [rax]`, `call rax`, a far call through the slot (`FF 1D`), and EIP-relative (`67 FF 15`) and FS-relative (`64 FF 15`) calls; and a jump-table base loaded into R10 before the call and used after it. The test asserts no loader error codes. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 5,976 probe calls per side and the nine fault cases matched, and all 34 controls were rejected. The providers' argument counts and return widths are known from their source, so the test does not show how a binding's numbers would be established for an import without source. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_import_calls$' -V`.

On the corpus of the earlier steps (MSVC 14.52 `/O2`, `/O1` and `/Od`, three entry arguments, no binding), the translated functions and their C++ are unchanged: 655, 651 and 400. For 143, 150 and 98 functions the first refusal now says that a binding is needed, instead of naming a target with no function entry or `unsupported control flow`. A binding names one import, so the gain was estimated with a script outside the repository. It binds every IAT slot that a function and its callees reach to the import that the import directory lists there, with 8 guaranteed bytes and without checking any provider. These are upper bounds that assume correct bindings. With three import arguments, 77, 69 and 13 more functions translate, all but one at `/O2` and at `/O1` through a single slot; with four, 2, 2 and 3. Most of the three-argument gains call `memset`, `free`, `memcpy`, `memcmp` or `strlen`, and as generated only 11, 11 and 5 of them compile. Declaring the import library's slot symbol instead, `extern "C" word (*__imp_memset)(word, word, word);`, and calling through it, all 159 sources compiled and linked against the C runtime import libraries; this change does not use that declaration. The functions whose single import only reads or writes memory through its arguments (`memset`, `memcpy`, `memcmp`, `strlen`, `memchr`: 49, 52 and 7 functions) were also run against the original with the inputs, arena and driver of the corpus differential tool, both sides calling the same runtime functions. Every input that completed on both sides matched (847, 1,000 and 176 inputs). Inputs that faulted on both sides compare nothing, memory outside the arena was not compared, and functions calling `free`, `VirtualFree`, `VirtualAlloc`, mutex or wait functions, or `SetLastError` were not run.

### Delay imports

`delay_route` imports `delayed_value` from `cpu_delay_provider.dll` by name and `delayed_ordinal` by ordinal 2 with `/DELAYLOAD`, and exports one caller for each. `cpu_native_delay` compares two paths: `LoadLibrary` plus the image's own delay helper, and `pe_map` with a resolver bound to the same provider. The mapped calls return the provider values, and IAT slot checks confirm both hold the `GetProcAddress` results before the calls. Binding the altered provider returns its values instead.

Windows leaves a delay IAT slot pointing at its thunk until the first call. `pe_map` resolves every delay descriptor at map time because it has no `LoadLibrary`, and the image's helper would bypass the host resolver. An absent provider therefore fails the map with `MAP_UNRESOLVED_IMPORT`, where Windows would map and fail on the first call. Descriptors must set `dlattrRva`; a descriptor without it stores virtual addresses and is rejected as unsupported.

Like the TLS and ordinary import tables, the delay-import directory is found by its address and `Size` is not used. The table is walked to the null descriptor; a copy with `Size` zero still binds, and a directory address of zero ignores a nonzero `Size`. Copies patched to clear `dlattrRva`, point the DLL name outside the image, zero the name table, point the IAT outside the image, or move the directory address outside the image are rejected before the provider is requested.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_delay$' -V
```

### Candidate programs for a named SGD5 effect

Supply the expected effect name, the group range, the program-array offset and the effect-record range. Use physical byte offsets in decimal or hexadecimal. Both ranges end at the first byte after the selected data.

```text
python -B tools/sgd5_effects.py archive.bin EFFECT --group GROUP_START GROUP_END --arrays ARRAY_OFFSET --record EFFECT_START EFFECT_END
ctest --preset dev --no-tests=error -R "^sgd5_effect_candidates$" -V
```

The report follows the effect's stored list order. Each entry gives its stored group, zero-based program index, name, numeric DXIL shader kind and decoded-container SHA-256. Check the DXIL kind when interpreting a group label. A stored VS group can contain a mesh shader (kind 13).

Two independently located local effects yielded 5 and 81 candidates across all groups. Their pixel lists contained 2 and 41 candidates respectively, and every pixel-container digest matched an independent decode. Identifying which candidate a draw uses still requires evidence of pass selection.

## Single-group mesh entry specialization

The indexed variant, selected with `--vertex-header WORD`, specializes packed
16-bit geometry indices and the vertex header while leaving the actual float3
vertices in a 512-byte GPU SRV at t0. Table slots 0, 0x20 and 0x40 select
geometry, object transform and projection buffers. The profile binds the low/high
pointer inputs in s18/s19 and verifies the constructed four-word vertex
descriptor before accepting a read. A changed header or index stream requires
retranslation. A changed vertex position can use the same DXIL. Undefined
registers, reads before their waits and out-of-range vertex accesses are rejected.

```text
python3 -B tests/mesh_indexed_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_indexed_draw_" -V
```

The source-built `mesh_cbv_order` test binds two separate upload resources as
root CBVs and exchanges their GPU addresses for the second draw. The mesh entry
adds the float at byte 4 of b0 to the float at byte 12 of b1. The translated
pixel entry reads its color from b0 at byte 16. Binding A/B draws red at depth
0.5 and B/A draws white at depth 0.625. On RTX 4060 Laptop and WARP, the three
exported depths and all 1,024 RGBA pixels matched for both orders. Comparing
the swapped draw to the old color detected 1,024 mismatches. Comparing to the
old depth failed the mesh trace with zero pixel mismatches.

```sh
ctest --preset dev --no-tests=error -R "^mesh_cbv_order_" -V
```

The indexed attribute fixture binds three additional float3 colors in the vertex
buffer. Its mesh entry exports a four-component PARAM0 per vertex. The pixel
entry interpolates attr0, packs the channels to binary16 and exports MRT0.
The two 32×32 CPU references below differ only in the uploaded vertex colors.
Both matched every RGBA byte on RTX 4060 and WARP. Comparing the swapped
upload to the original reference produces 992 mismatches and a matching mesh
trace. The 32 equal-channel pixels lie on one diagonal.

![Two CPU reference gradients verified by GPU readback](mesh-attribute-reference.png)

```sh
python3 -B tests/mesh_attribute_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_attribute_draw_" -V
```

The textured mesh fixture passes indexed UVs through PARAM0 and samples a
source-built 2×2 RGBA8 texture with a translated `image_sample_lz` pixel shader.
The mesh buffer and pixel texture both use `t0`, with stage-specific root
signature visibility. The sample consumes only `xy` but declares a four-component
`TEXCOORD0` to match the mesh output. Point sampling, clamp addressing and the
single mip level make the reference selection deterministic.

The 32×32 readback compares a baseline, an independent texture change and a
horizontal UV reflection. Each stale baseline reference gives exactly 276
mismatches while the geometry trace still matches. The images are CPU
references checked byte for byte against each GPU readback.

![Baseline texture, changed texture, and reflected UVs](mesh-textured-reference.png)

```sh
python3 -B tests/mesh_textured_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_textured_draw_" -V
```

The compute-to-mesh texture fixture reuses that translated mesh and pixel
shader. A translated compute program writes four packed RGBA8 texels to a raw
default-heap UAV, with rows at byte offsets 0 and 256. After a UAV-to-copy-source
transition, a GPU copy places those rows in a 2×2 texture. The draw samples the
texture after its transition to pixel-shader-resource state. The dispatch,
copy, and draw share one direct command list. The test reads back the final
1,024 pixels and the mesh export trace.

Two independently generated palettes match their CPU references. In the
stale-binding control, both producers run but the draw binds the first texture
while expecting the second. All 276 covered pixels differ while the mesh
export trace and all 748 clear pixels match.

```powershell
ctest --preset dev --no-tests=error -R '^compute_mesh_texture_(warp|hardware)$' -V
```

The mesh depth-pair fixture draws two partially overlapping indexed triangles
with translated mesh and pixel stages. The two dispatches share one RGBA8
target and one D32_FLOAT depth buffer with depth writes and LESS enabled.
It clears both targets once and compares both complete 32×32 readbacks
against a CPU edge test. It also checks each dispatch's geometry trace.

Reversing draw order preserves every color and depth value. Exchanging the
two depths changes the winning color at the 190 overlapping pixel centers.
At the 172 centers covered by one triangle, the color stays fixed and the
depth changes. Separate stale-color and stale-depth references verify both
comparators. These CPU reference images matched the GPU readbacks on
RTX 4060 and WARP.

![Mesh depth-pair color references before and after the depth exchange](mesh-depth-pair-reference.png)

```powershell
ctest --preset dev --no-tests=error -R "^mesh_depth_pair_" -V
```

The lit mesh fixture reuses the indexed PARAM0 mesh program. The three vertex
attributes are now normals whose x and y equal the vertex NDC position and
whose z is 1/4. The source-built pixel program interpolates attr0. It loads a
light direction and an ambient term from b0 at byte 0x80 and computes N·L with
`v_mul_f32` and two `v_fmac_f32`. It clamps the result with `v_max_f32`, adds
the ambient term and exports gray RGB with the interpolated constant one as
alpha. The b0 bytes it reads are unused by the mesh program. The normals are not
normalized: the fixture checks the dot product, the clamp and the attribute
path, not a lighting model.

The CPU reference evaluates N·L exactly at each pixel center of the 276 covered
pixels and applies the round-toward-zero binary16 export and the 8-bit target.
At pixel centers N·L is a multiple of 1/128, so with an ambient term of exactly
1/4 every result is itself a binary16 value. On the RTX 3070, 133 of the 276
interpolated results of the base draw were one or two binary32 ulps (at most
2^-23) below that value. Round toward zero then dropped a whole binary16 step,
which changed the 8-bit byte at 17 pixels. WARP interpolated every pixel center
exactly. The ambient term is therefore 1/4 + 2^-17: every positive result lies
at least 2^-17 from both neighbouring binary16 values, 64 times the observed
error, and still exports the same binary16 value. The reference asserts this
for every pixel. On RTX 3070 and WARP, with the debug layer enabled,
three lights match every RGBA byte: the base light, a second light and the
inverted base light. Three negative controls
compare against the base reference:

- **Inverted light:** the same shaders and normals with the light negated in b0.
  All 276 covered pixels differ; the 748 clear pixels match.
- **Ignored normal:** a pixel program that lights a fixed +Z normal (the
  attribute's constant fourth component) gives 276 mismatches. It matches its
  own flat-shaded reference.
- **Missing clamp:** without `v_max_f32`, only the 132 covered pixels that face
  away from the light differ. It matches its own unclamped reference.

Each draw also checks the mesh export trace.

![Base light, second light and inverted light references](mesh-lit-reference.png)

```sh
python3 -B tests/mesh_lit_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_lit_draw_" -V
```

The shadow fixture (`tests/mesh_shadow_check.py`, `offscreen_check
--mesh-shadow`) adds a depth pass and a comparison sample to the lit fixture.
All draws use the PARAM0 mesh program of the textured fixture on a 64×64 target.
The scene is orthographic with depth z. A directional light projects (x, y, z)
to (x − z/2, y + z/2) with depth z. Every vertex lies on the pixel grid in both
views, so each receiver pixel center samples a texel center of the map.

- **Light pass:** the caster triangle at depth 1/4 and a second triangle at
  depth 7/8 go into an `R32_TYPELESS` texture through a `D32_FLOAT` view. The
  depth test is `LESS` and the map is cleared to 1. The pixel program is the
  caster's, with color writes masked. On WARP (10.0.26100.9818), a mesh
  pipeline without a pixel shader removed the device in this fixture.
- **Camera pass:** a receiver plane at depth 3/4 covers the target. Its
  source-built pixel program interpolates the map coordinate from attr0.xy and
  loads its light-space depth from b0 at byte 0x90. It compares that depth
  against the map with `image_sample_c_lz` (point, `LESS`, through the #718
  path, `SampleCmpLevelZero` on an `R32_FLOAT` view with a static comparison
  sampler from the decoded words). It exports 1/4 + result/2 as the plane color.
  The caster is then drawn at depth 1/4 with the #749 N·L program, reading its
  normal from attr0.xyz. The normal's z is 17/32, so N·L at every pixel center
  is an odd multiple of 1/128 and no result exports as 1/2. The ambient term is
  1/4 + 2^-17, as in the lit fixture.

The CPU reference rasterizes both views exactly with `Fraction` and asserts that
no pixel or texel center lies on an edge. It evaluates N·L at each caster pixel
and applies the comparison, `CLAMP` addressing and the 8-bit target. It does not
use D3D12. Every RGBA byte, every texel of the current map (576 at 1/4, 176 at
7/8, the rest 1) and the mesh trace of each of the five draws are compared. On
RTX 3070 and WARP with the debug layer, the base scene matches, with 342 plane
pixels in shadow. The second triangle is behind the plane: its 176 texels are in
the map, and the plane pixels that sample them stay lit. The scene does not tell
`CLAMP` from `WRAP`: the texels a wrapped coordinate would reach are clear or
behind the plane. Negative controls:

- **`GREATER` for `LESS`** (translated program and host sampler): every one of
  the 3,520 plane pixels flips. The draw matches its own `GREATER` reference.
- **Stale map:** the camera pass binds the map drawn in the same command list
  for a previous light, (x + z/2, y + z/2). 356 pixels differ. The draw matches
  its own reference for that map.
- **Ignored map:** a receiver program that exports the lit color without
  sampling gives 342 mismatches. It matches its own unshadowed reference.
- **Coverage instead of depth:** a reference where any written texel shadows
  differs at 52 pixels, the plane pixels that sample the triangle behind the
  plane.
- **Map without the second triangle:** the map readback differs at its 176
  texels.

![Shadow scene reference and the light-space depth map](mesh-shadow-reference.png)

```sh
python3 -B tests/mesh_shadow_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_shadow_draw_" -V
```

A separate draw binds two immutable constant buffers through a shader-visible
D3D12 table. The translated mesh reads depth from b0 and a horizontal offset
from b1. The translated pixel stage reads a packed color from b0. Both draws
reuse the same shaders and upload bytes. Exchanging the two CBV slots moves the
triangle's left edge from column 0 to column 8 and changes the covered pixels
from red to green. The test compares all 1,024 pixels and the mesh export trace.
Using the old pixel reference reports 1,024 mismatches. Using just the old mesh
reference fails the trace check while every pixel still matches.

```sh
ctest --preset dev --no-tests=error -R "^mesh_descriptor_swap_" -V
```

A local 1,028-byte entry (197 instructions) read three distinct vertex
positions and produced visible geometry with controlled indices, transforms and
vertex data. The two sets of positions cover 1,024 and 276 of the 1,024 pixel
centers. Both plain and instrumented mesh DXIL match the CPU raster reference.
The instrumented draw also checks counts, three float4 positions, the primitive
indices and guard bytes. Aliasing one vertex to another changes exactly the
276 covered pixels and fails the export check. Measured with the debug layer
enabled on Windows 26200.9974, RTX 4060 Laptop 32.0.16.1656 and WARP
10.0.26100.9818. The source-built fixture also permutes the packed indices.

`tools/mesh_entry_aot.py` takes an extracted mesh container and a JSON array of
unsigned geometry words. It checks the native extent and the PSV triangle
topology, group size and output bounds. It decodes the entry with LLVM in
wave64 mode, follows two waves of forward control flow and specializes the
geometry reads and lane masks. The resulting desktop mesh shader reads position
and transform data from b0 and b1, emits three vertices and one primitive, and
can record its exports to a diagnostic UAV when compiled with `TRACE_EXPORTS`.

The specialization assumes one 128-thread group, wave ID in bits 24–27 of s3,
a descriptor table token in s17, and zero for s20, s21 and v5. Table offsets
0x20, 0x60 and 0x80 select static geometry, point data and the transform,
respectively. Changing geometry requires retranslating the entry. The native
draw state and application bindings remain unverified. Each public test builds
its own inputs. The visible triangle control uses an independently authored
native program, and the CPU checks its 32×32 target, exported vertices,
primitive index order and guard bytes.

```text
python3 -B tests/mesh_entry_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_entry_draw_" -V
```

A local scan based on main `933cbad` selected the shortest validated native
stream among 9,282 MS records (832 bytes, 153 instructions). For the tested
geometry its three vertices receive the same transformed position. The raster
target therefore retains its clear color. Two independently calculated
point/matrix cases matched all three exported positions and the (0,1,2) index
triple. The uninstrumented shader also completed a draw for both inputs. The
source-built visible triangle filled all 1,024 pixels, while altering a
reference index caused the comparison to fail. Tested on Windows build
26200.9974, RTX 4060 Laptop driver 32.0.16.1656 and WARP 10.0.26100.9818,
with the debug layer enabled.

## Compute UAV stores from a native container

`tools/compute_store_aot.py` accepts a local extracted DXBC compute container. It checks the stage in both the DXIL and PSV0 parts, the group dimensions, the FSSM v0x101 extent and termination, then decodes the gfx1013 instructions with LLVM. The store profile translates one or more non-overlapping raw UAV stores of one to four known 32-bit constant words per invocation, including constants loaded via `s_mov_b32`, `s_movk_i32`, and `v_mov_b32` register copies, along with `s_clause` hints and combined `lgkmcnt(0)`/`vmcnt(0)` waits. A test binding maps one four-word descriptor to u0. Its table pointer has a known low register and a zero high word. Scalar loads must complete before use. For an indexed store the caller also supplies the group-X register and byte stride, v0 carries local X, and the decoded shift and add must produce the dispatch-thread index. The tests bind in-range raw buffers with no swizzle, format conversion or out-of-range accesses. The original descriptor contents and dispatch configuration require separate evidence.

```sh
python3 -B tools/compute_store_aot.py local.dxbc --llvm libLLVM-15.so.1 --table-reg 5 --descriptor-offset 64 --group-reg 6 --stride 4 --output out/store.hlsl
python3 -B tests/compute_store_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

Build the emitted source with `dxc -T cs_6_0 -E main`. The Windows fixture reads back all 320 words, including gaps between records and the untouched tail, after one and three indexed groups. A wrong stored word and a wrong guard word must both fail. Run `ctest --preset dev --no-tests=error -R '^compute_store_aot_'`.

An optional `--constant-descriptor BYTE_OFFSET` binds a second table entry to
b0. It must be distinct from the 16-byte u0 descriptor. The translator accepts
aligned immediate `s_buffer_load_dword`, `s_buffer_load_dwordx2`, and
`s_buffer_load_dwordx4` reads within the 64-byte binding. It tracks scalar waits,
register copies, and each loaded DWORD through the raw UAV store. The Windows
test dispatches one compiled shader with two different root-constant uploads and
compares all 320 output words each time. The stale reference fails on exactly
eight stored words. The binding is limited to unsigned 32-bit words and fixed
byte offsets.

### Compute programs with two exits

Compute programs (stage 5) now use the [pixel two-exit check](#pixel-programs-with-two-exits) from #477 when LLVM is supplied. Only exactly two aligned `s_endpgm` markers qualify. One marker is unchanged, three or more are rejected, and mesh programs (stage 13) with more than one marker are still rejected. `compute_store_aot.py` now passes `--llvm` to the extractor, and `shader_profile_audit.py` already did. This step extracts the code span only. The store profile does not lower branches, so a two-exit program now stops at its first unsupported instruction instead of at extraction.

The fixtures are original kernels compiled with LLVM 15 and reassembled with `llvm-mc-15`:

```sh
llc-15 -mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2 -filetype=obj kernel.ll -o kernel.o
llvm-objdump-15 -d --mcpu=gfx1013 kernel.o
```

A wave-uniform `if (gid >= n) return;` with branch weights that mark the return likely compiles to `s_cmp_ge_u32`, then `s_cbranch_scc0` over the early `s_endpgm` to the store block. That is the layout #477 verifies. Both an `amdgpu_kernel` and an `amdgpu_cs` form are extracted unchanged, and the CLI and the audit then stop at the lowering. Without branch weights, LLVM 15 branches to one shared exit instead. A per-lane bound (`local X >= n`) compiles to `s_and_saveexec_b32` and `s_cbranch_execz` over the store to a single `s_endpgm`, which already passes as a one-exit program.

`tests/compute_store_check.py binary` rejects:

- the same program without LLVM, or marked as a mesh program;
- a branch one instruction late, a branch to the final exit, a target separated from the early exit by a NOP, and a skipped non-NOP word;
- an unconditional branch that disconnects the early exit, and a missing branch that disconnects the store block;
- a third exit, or a literal word equal to `s_endpgm` that makes a third marker;
- in a one-exit program, the same literal, which gives two markers but one decoded exit;
- a branch into a literal operand or into the trailing `s_code_end` padding.

An `s_branch` that skips an alignment NOP at the start is still accepted, and the NOP stays in the span.

These are fixture results only. The 1,002 multi-exit CS records in the [fixed-profile rescan](#fixed-profile-archive-rescan) have not been split by marker count, so it is not known how many have exactly two exits or this layout. Their counts have not been remeasured after this change.

### Per-lane guards around compute stores

A per-lane bound such as `if (gid < n) store` compiles under LLVM 15 to one exit, with no early `s_endpgm`. A `v_cmp` sets `vcc_lo`, `s_and_saveexec_b32` saves `exec_lo` and masks it, and an optional `s_cbranch_execz` skips the masked block when no lane passes. `s_or_b32 exec_lo, exec_lo, sN` then restores the mask, or the program exits with the mask still applied. The store profile lowers this form as control flow: the stores issued under the mask go inside an HLSL `if`, and stores after the restore stay outside it.

The accepted subset is small:

- one guard per program, not nested;
- `v_cmp_{gt,lt,ge,le}_u32_e32 vcc_lo, a, vN`, where both operands are known words (a dispatch index, a constant word or an immediate). The operator and operand order are kept;
- `s_and_saveexec_b32` from `vcc_lo` only, and a restore from an SGPR that still holds the saved mask. With a branch, that SGPR must already hold it at the branch, so that it holds the mask when the block is skipped too;
- `s_waitcnt_depctr` as a no-op.

The CLI names each decoded `s_cbranch_execz` target as a label. The target must be forward, at a decoded instruction start, and exactly at the end of the masked block. Only `s_waitcnt_depctr` or `s_nop` may come between the target and the restore or the exit. A register written under the guard is unknown after the restore, since the lanes that did not pass, or a skipped block, keep the old value. A scalar load that was waited for only under the guard is pending again after the restore.

The two fixtures are unchanged LLVM 15 code generator output for original `amdgpu_cs` sources with a group of 32. One keeps the guard to the exit and sinks the u0 descriptor load under it. The other restores the mask before a second, unguarded store. Their bindings are `--table-reg 0 --descriptor-offset 0 --constant-descriptor 32 --group-reg 1` with `--stride 4` or `--stride 8`, and the bound `n` is `params[0].x`.

The Windows fixture reads back all 320 words with the untouched words at a sentinel value. It runs seven dispatches per program, from one group with `n` of 0, 17 and 32 to three groups with `n` of 1, 70, 96 and 200. The guarded words of lanes at or past the bound must stay untouched. On three groups with `n = 70`, a reference built for 69 or 71 must fail, and so must recompiled HLSL with the guard inverted, off by one or removed. For the second program, HLSL that keeps the second store under the guard must also fail.

The CPU controls reject a missing or wrong restore, a saved mask overwritten under the guard, a restore from a copy made only inside the skipped block, a branch target before, inside or after the end of the masked block, a branch without a label, a label without a branch, a second branch to another label, `s_cbranch_execnz`, wave64 `s_and_saveexec_b64`, a mask from another source, a compare of an unknown, pending or non-word operand, a nested or second guard, a guard around no store, and registers written or waited for only under the guard and then used after the restore. Removing both the restore and the branch is accepted with both stores inside the `if`, because the mask then holds until the exit.

`shader_profile_audit.py` is unchanged. It does not bind b0, so it does not try guards that read their bound from a constant, and it does not label branches.

### Two- and three-dimensional thread groups

The PSV0 group size can be any `numthreads(x,y,z)` within the `cs_6_0` limits
(x and y up to 1024, z up to 64, x·y·z up to 1024). The translator emits it
unchanged, so an 8×8 group stays 8×8. On gfx10, compute waves receive local X,
Y and Z in separate registers `v0`, `v1` and `v2`. LLVM 15 packs the three IDs
into `v0` only for targets with `FeaturePackedTID` (gfx90a, gfx940 and gfx11), and
`FeatureISAVersion10_1_3` does not have it. `v1` and `v2` are bound only when
that group dimension is larger than one. `--group-reg-y` and `--group-reg-z`
name the group-ID SGPRs in the same way as `--group-reg`. LLVM allocates them
as consecutive system SGPRs in X, Y, Z order, each only when the program reads
it. For an original program this is still a caller assumption.

A dispatch index is `v_lshl_add_u32` of a group ID and the local ID of the
same axis, shifted by log2 of a power-of-two group dimension. An indexed store
takes one of three forms:

- one `idxen` axis, when every other group dimension is one;
- a row axis through `idxen` and a column axis through `offen`, with a
  `v_lshlrev_b32` byte offset. The store must fit in one element, and one
  group's row must fit in the stride.
- a plane axis through `idxen` and an `offen` byte offset that folds a row
  axis and a column axis: `v_lshl_add_u32` of the row dispatch index, a shift
  and the column's `v_lshlrev_b32` byte offset. The three axes must differ.
  The store must fit in one element, one group's columns in one row
  (`1 << shift`), and one group's rows in the stride.

A byte offset can also come in the form LLVM 15 emits for gfx1013:
`s_lshl_b32` of a group ID by log2 of its power-of-two group dimension gives
the group's first dispatch index, and `v_add_lshl_u32` of that SGPR and the
local ID of the same axis, with a shift, gives the same byte offset as
`v_lshlrev_b32` of the dispatch index. The group-base SGPR is not a storable
word. The `llvm_cube` fixture keeps the address instructions that `llc-15`
produced for the 4×4×4 store below, with their registers:

```sh
llc-15 -mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2 -filetype=obj cube.ll -o cube.o
llvm-objdump-15 -d --mcpu=gfx1013 cube.o
```

The source is an `amdgpu_kernel`; only its kernel-argument load is replaced by
the descriptor-table load of the other fixtures. Its bindings are
`--table-reg 0 --descriptor-offset 0 --group-reg 2 --group-reg-y 3
--group-reg-z 4 --stride 136`.

A store with `offen` and no `idxen` can also take a row pitch:
`v_mad_u32_u24` of a row dispatch index, a literal pitch and the column's
`v_lshlrev_b32` byte offset, with the pitch in either multiplicand. LLVM 15
emits this form when the source masks the row index to 24 bits, and places the
pitch in src0. The instruction multiplies only bits [23:0] of the row, so the
HLSL masks the row with `0xffffff` before the multiply. The pitch must be a
multiple of 4 from 4 to 2^24 - 4. The store must fit in one element and one
group's columns in the pitch, and one group's rows must not wrap the 32-bit
address. Without `idxen` the descriptor stride is unused,
so `--stride` does not change the output.

All stores in a program use one layout, so no two invocations of a group write
the same bytes. A dispatch-thread index can also be stored as data. The
translator does not model out-of-range behavior for structured buffers: the
caller's stride and dispatch must keep every row in range. Addresses that need
other ALU forms are still rejected: a `v_mul_lo_u32` row pitch on an unmasked
index, a pitch read from a constant or a register, a `v_lshl_or_b32` dispatch
index and a pitch offset combined with `idxen`.

```sh
python3 -B tools/compute_store_aot.py local.dxbc --llvm libLLVM-15.so.1 --table-reg 8 --descriptor-offset 0 --group-reg 10 --group-reg-y 11 --stride 72 --output out/tile.hlsl
```

The Windows fixture covers five group shapes, one of them in two address
forms, and a row-pitch kernel, each with a full 320-word readback:

| Group | Dispatches |
|---|---|
| 8×8×1 | 1×1×1 and 1×2×1 |
| 4×1×8 | 1×1×1 and 1×1×2 |
| 4×4×1 | 1×1×1 and 3×2×1 |
| 256×1×1 | 1×1×1 |
| 4×4×4 | 1×1×1 and 1×2×2 |
| 4×4×4, LLVM 15 form | 1×1×1, 1×1×2 and 1×2×2 |
| 4×4×1, row pitch | 1×1×1 and 2×3×1 |

The 4×4×4 kernel stores dispatch Z at `tid.z * 132 + (tid.y << 4) + (tid.x << 2)`.
Its 132-byte stride covers both groups' rows in Y and leaves one guard word per
plane. The LLVM 15 form stores dispatch Z at `tid.z * 136 + (tid.y << 4) +
(tid.x << 2)`, with two guard words per plane.

The row-pitch kernel stores `(tid.x, tid.y)` at
`(tid.y & 0xffffff) * 72 + (tid.x << 3)`. Two groups of 8-byte columns in X
leave two guard words per row. The fixture is hand-assembled in the operand
order LLVM 15 emits. It does not cover row indexes of 2^24 or more, where the
mask changes the address.

The expected words come from an independent per-invocation model. For each
shape, the following must fail on the adapter:

- a flipped guard word;
- the first group count against the larger reference;
- recompiled HLSL with swapped axes;
- recompiled HLSL that ignores Z (4×1×8 and 4×4×4);
- recompiled HLSL with half the group X size;
- for the LLVM 15 form, recompiled HLSL with X and Z or Y and Z swapped;
- recompiled HLSL with a 64-byte pitch (row pitch only).

Each recompiled variant runs on the shape's largest dispatch, with one
exception: Y and Z both span 8 in the 1×2×2 dispatch, so swapping them leaves
the buffer unchanged. That swap runs on 1×1×2, where Y spans 4 and Z spans 8
(`MUTATION_DISPATCH` in the test).

The CPU controls reject:

- swapped local or group axes;
- a non-power-of-two size whose shift still matches;
- `v1` in a group whose Y is one;
- duplicate or missing group registers;
- an element or row overflow;
- an index and a byte offset taken from the same axis;
- two stores with transposed layouts;
- a plane offset whose row and column, or whose index and row, share an axis;
- a plane offset built from a raw local ID or from an index instead of a byte
  offset;
- a group's columns overflowing a row, or its rows overflowing the stride;
- a plane offset stored without its `idxen` index, or stored as data;
- a group base with the wrong shift, a non-power-of-two size whose shift still
  matches, a source that is not a group ID, a destination past `s105` or still
  pending from a descriptor load, or a group base scaled again; the Y form, with
  a group Y size different from X, checks the shift and the power of two against
  Y;
- `v_add_lshl_u32` of an unscaled group ID, of the local ID of another axis or
  into a register past `v255`, and a group base stored as data;
- a pitch that is unaligned, zero, negative, wider than 24 bits, narrower than
  one group's columns, so wide that one group's rows wrap the address, or read
  from a register;
- a pitch offset built from a raw local ID, from the column's axis or from an
  index instead of a byte offset, or written with `clamp`;
- a pitch offset combined with `idxen`, stored as data, or used next to a
  store with another pitch.

They also check that the pitch is accepted in either multiplicand, as a
decimal inline constant, with or without a stride binding, and with a column
byte offset in the LLVM 15 group-base form.

`tools/shader_profile_audit.py` passes the PSV0 size and tries the next one or
two SGPRs after the group-X candidate for group Y and Z. A CPU control in
`compute_store_check.py` checks that a 4×1×8 kernel reading group Z from the
SGPR after group X is accepted, and one reading it two SGPRs later is not.
Another checks that the audit accepts the row-pitch kernel although it always
offers a stride for groups larger than one thread. Its
record counts have not been remeasured after this change.

A local scan of 2,689 compute records accepted 12 records (10 distinct instruction streams). Ten use one-thread groups and fixed addresses. Two use 32- and 64-thread groups and indexed addresses. Tested table bindings used s4 or s5 with descriptor offsets of 0, 32 or 64. The indexed bindings used strides of 4, 8, 12 or 16 bytes, selecting the first compatible profile. On RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818), each record compiled to desktop DXIL and dispatched. Each adapter completed 14 full-buffer comparisons with zero mismatches, including two different group counts for both indexed records.
## AVX2 leaf recompilation

[The AVX2 leaf translator](../tools/cpu_avx2_aot.py) lifts bounded straight-line Windows x64 leaf functions covering all 28 AVX2 forms in `docs/coverage/x64_zen2.tsv` group `AVX2`:

- 128-bit lane broadcast and permute: `vbroadcasti128` (`m128` source), `vextracti128`, `vinserti128`, `vperm2i128`
- Element broadcasts: `vpbroadcastb`, `vpbroadcastw`, `vpbroadcastd`, `vpbroadcastq` (XMM and YMM destinations)
- Cross-lane permutes: `vpermd`, `vpermpd`, `vpermps`, `vpermq`
- Blend: `vpblendd` (XMM and YMM widths)
- Variable shifts: `vpsllvd`, `vpsllvq`, `vpsravd`, `vpsrlvd`, `vpsrlvq` (XMM and YMM widths)
- Indexed gathers (VSIB): `vgatherdps`, `vgatherdpd`, `vgatherqps`, `vgatherqpd`, `vpgatherdd`, `vpgatherdq`, `vpgatherqd`, `vpgatherqq` (all 16 VEX encodings across `vm32x`, `vm32y`, `vm64x`, `vm64y` and XMM/YMM widths)
- Masked loads and stores: `vpmaskmovd`, `vpmaskmovq` (all 8 VEX encodings across XMM/YMM load and store forms)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers and returns `void`. For transport and ABI glue the translator accepts `vmovdqu` (128- and 256-bit flat loads, stores and register copies), `vzeroupper` and terminal `ret`.

### State model, memory semantics and explicit rejections

YMM registers `ymm0`..`ymm5` are represented as a portable C++ value struct (`struct Vec256 { std::uint64_t q[4]; }`) rather than MSVC AVX2 intrinsics, so the generated C++ compiles with default x64 flags without `/arch:AVX2` and avoids C++ shift-width undefined behavior when shift counts are `>= 32` or `>= 64`. Every VEX write to an XMM destination zeroes bits 128..255 of the corresponding YMM register, and `vzeroupper` zeroes bits 128..255 of all defined YMM registers. `vpermd` and `vpermps` mask each 32-bit index to its low 3 bits; floating-point permutes and gathers transfer raw bits without canonicalization or FP exceptions. Per the AVX2 ISA, variable shifts do not wrap counts modulo lane width: unsigned lane counts `>= 32` (32-bit shifts) or `>= 64` (64-bit shifts) zero the lane in `vpsllvd`, `vpsllvq`, `vpsrlvd` and `vpsrlvq`, and fill the lane with the sign bit in `vpsravd`.

Memory-domain AVX2 instructions follow the Intel SDM lane, fault-suppression and register-state rules:

- **Indexed gathers (`vgather*` / `vpgather*`)**: each lane `i` within the instruction's gathered element count checks the MSB of `mask[i]`; when set, it loads 32 or 64 bits from `base + SignExtend(vindex[i]) * scale + disp` inside an `if` guard so masked-off lanes never dereference memory. Masked-off lanes within the gathered element count preserve their prior destination value; upper bits beyond the gathered element count are zeroed (bits `64..255` for `vgatherqps`/`vpgatherqd` with `vm64x`, bits `128..255` for other XMM-destination gathers). After every gather, the entire 256-bit mask register is zeroed.
- **Masked loads and stores (`vpmaskmovd` / `vpmaskmovq`)**: masked loads read only lanes whose mask MSB is set and zero masked-off lanes (and bits `128..255` for XMM destinations) while preserving the mask register; masked stores write only lanes whose mask MSB is set and never touch masked-off memory locations.

Memory-operand scope remains explicit: `vbroadcasti128` (`m128`), `vmovdqu` and `vpmaskmovd`/`vpmaskmovq` require flat `[base + disp]` addressing rooted in `rcx`, `rdx` or `r8`, and gathers require VSIB `[base + vindex*scale + disp]` with base in `rcx`/`rdx`/`r8`, SIB scale in `{1, 2, 4, 8}`, and pairwise distinct `dest`/`index`/`mask` vector registers. The translator explicitly rejects:

- memory operands on all 17 register-only AVX2 forms;
- overlapping gather destination/index/mask registers (including XMM/YMM cross-width aliases), illegal VSIB scales, missing/non-argument/32-bit/`rsp` bases, RIP-relative or GPR-indexed `vpmaskmov` operands, and reads of undefined `dest`/`index`/`mask`/`src` vector registers;
- EVEX encodings (including EVEX gathers), instruction prefixes, nonvolatile vector registers (`xmm6`..`xmm15` / `ymm6`..`ymm15`), branches, out-of-scope instructions, missing terminal `ret` and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx2_regs_original cpu_avx2_regs_original_lifted cpu_avx2_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx2_regs.asm) builds 68 exports covering all 28 AVX2 forms (42 register-domain exports plus 18 gather exports and 8 `vpmaskmov` load/store exports) across XMM and YMM widths, ignored immediate bits, `vperm2i128` lane-zeroing control bits, IEEE-754 special bit patterns (signed zero, subnormals, infinities, sNaN and qNaN), boundary lane indices (`0`..`7` with high-bit noise), boundary shift counts (`0`, `1`, `15`, `31`, `32`, `33`, `63`, `64`, `65`, `128`, `0x100000001`, `0x7fffffff`, `0x80000000`, `0xffffffff`, `0xffffffffffffffff`), `vzeroupper` upper-lane clearing, a chained `op_pipeline` function, full/partial/zero masks (including `0x7fffffff`/`0x7fffffffffffffff` non-MSB checks), negative and boundary signed 32-bit and 64-bit VSIB indices, scales `1`/`2`/`4`/`8`, positive/negative displacements, all four gather dest-width/index-width combinations (including `vm64x` bits `64..255` zeroing and post-gather mask zeroing), and Windows `VirtualAlloc` + `PAGE_NOACCESS` guard-page fault-suppression checks for every gather and `vpmaskmov` export. [The Windows test](../tests/cpu/avx2_regs_check.py) checks 1,120 differential cases (772 register-domain + 348 memory-domain) and 34 guard-page fault-suppression cases across all 68 exports with 16-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes the immediates or memory displacements of `op_perm2i128_mix`, `op_permq`, `op_pblendd_ymm`, `op_gatherdps_ymm` and `op_pmaskmovd_load_ymm`, and must match the altered reference while diverging from the unmodified reference on 14, 14, 16, 12 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Rejection controls cover 59 unit cases (memory-operand variants of all 17 register-only AVX2 forms, 7 out-of-scope ops, 23 gather/maskmov/EVEX violations including overlapping dest/index/mask registers and unsupported addressing, and 12 ABI/state violations), 4 direct invalid-scale checks (`0`, `3`, `5`, `16`), and 9 end-to-end PE-patched DLL cases. In `tools/form_coverage.py`, `avx2_regs.asm` accounts for all 28 of 28 forms in `x64_zen2.tsv` group `AVX2`.

Not covered by this experiment: memory operands on the 17 register-only AVX2 forms, control flow or calls mixed with AVX2 state, nonvolatile `XMM6`..`XMM15` save/restore prologues, and title CPU code execution.

## Two-table depth-only pixel shaders

### Coverage-only texture sampling

`tools/sampled_coverage_aot.py` translates a two-exit PS which selects a texture
through a buffer word, samples an interpolated UV and compares the scaled sample
with a threshold. Both exits export only coverage. The selector, table pointers
and point-clamp sampler are specialized for a supplied snapshot. The texture
and two comparison inputs remain GPU resources. Changing the selector requires
another translation. The tested inputs are finite and the textures have one mip.

The JSON bindings used by the source-built fixture are constructed by
`bindings(slot)` in `tests/sampled_coverage_check.py`. A descriptor entry lists
the table's entry SGPR, its byte offset, the descriptor kind and its value.
For a CB the value is a word offset into desktop b0. For a texture it selects
t0 or t1. `words` supplies concrete entry SGPR values and `selector_byte`
identifies the known word in the selector buffer.

```sh
python3 -B tools/sampled_coverage_aot.py local.dxbc --bindings snapshot.json --llvm libLLVM-15.so.1 --output out/coverage.hlsl
python3 -B tests/sampled_coverage_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R '^sampled_coverage_' -V
```

The Windows fixture measures a complete triangle with a D3D12 occlusion query,
then checks all 64 pixel locations with individual scissored draws. The color
target must retain its clear value. It exercises both bound textures, mixed
coverage, full survival and full rejection. A reference computed for the
opposite texture must fail at the sample comparison.

### Buffered depth export

`tools/depth_mask_aot.py` follows the two entry table pointers through scalar
moves, four-word descriptor loads and completed constant-buffer reads. Supply the
two `--table SGPR BYTE_OFFSET` pairs in desktop b0, b1 order. The destination
buffers each contain 2 KiB. The input convention supplies window x, y and depth
in v2, v3 and v4. The supported exits export depth for surviving lanes or discard
them. Unknown table addresses, outstanding reads, changed EXEC provenance and
unrecognized exit sequences reject translation.

The Windows fixture binds distinct seeds and thresholds, changes each separately,
then exchanges the two root CBV addresses. It checks all 1,024 D32_FLOAT
pixels and requires comparison against the old reference to fail after the
exchange. LLVM CI assembles an original gfx1013 program and translates it from a
synthetic container through the CLI.

```text
python3 -B tests/depth_mask_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^depth_mask_draw_" -V
```

In a local inventory based on main `933cbad`, 354 of 12,101 PS records
shared one additional native program. With the explicit bindings, the first
read followed entry s8 at byte offset 0x480 and consumed an integer at byte
offset 0x5f0 of the resulting buffer. The second followed the saved entry s9
at offset zero and consumed a float at byte zero. Physically exchanging the
buffers changed 897 of 1,024 depth samples against the old reference on both a
GeForce RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818), Windows
build 26200.9974, debug layer enabled. Matching results use controlled inputs
and the specified binding convention. Target application resource identity and
original render state remain unverified.
## AVX packed-integer leaf recompilation

[The AVX packed-integer leaf translator](../tools/cpu_avx_int_aot.py) lifts bounded straight-line Windows x64 leaf functions covering 90 AVX packed-integer and state forms in `docs/coverage/x64_zen2.tsv` group `AVX` (excluding the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)` so each form has a single translator):

- state management: `vzeroall`, `vzeroupper`
- packed absolute value: `vpabsb`, `vpabsw`, `vpabsd`
- saturated packs: `vpacksswb`, `vpackssdw`, `vpackusdw`
- packed add and subtract (wrapping and signed/unsigned saturating): `vpaddb`/`q`, `vpaddsb`/`usb`/`usw`, `vpsubb`/`d`/`q`, `vpsubsb`/`sw`/`usb`/`usw`
- align and blend: `vpalignr`, `vpblendvb`, `vpblendw`
- packed compares and min/max: `vpcmpeqb`/`w`/`d`/`q`, `vpcmpgtb`/`w`/`d`/`q`, `vpmaxsb`/`sd`/`ub`/`uw`/`ud`, `vpminsb`/`sd`/`ub`/`uw`/`ud`
- horizontal operations: `vphaddw`/`d`/`sw`, `vphsubw`/`d`/`sw`, `vphminposuw`
- multiply and multiply-add: `vpmaddwd`, `vpmuldq`, `vpmuludq`, `vpmulhrsw`, `vpmulhuw`, `vpmulld`
- sums of absolute differences: `vpsadbw`, `vmpsadbw`
- shuffles and packed sign: `vpshufhw`, `vpshuflw`, `vpsignb`, `vpsignw`, `vpsignd`
- sign-extend and zero-extend moves: `vpmovsxbw`/`bd`/`bq`/`wd`/`wq`/`dq`, `vpmovzxbw`/`bd`/`bq`/`wd`/`wq`/`dq`
- unpack/interleave: `vpunpckldq`/`hdq`/`lqdq`/`hqdq`
- packed shifts: `vpslld`/`q`, `vpsrld`/`q` (both `imm8` and `xmm` count encodings)
- movemask, extract and insert: `vpmovmskb`, `vpextrb`/`w`/`d`/`q` (register and memory forms), `vpinsrb`/`w`/`d`/`q` (register and memory forms)

Each export uses the leaf ABI `void fn(word rcx, word rdx, word r8)` where `rcx`, `rdx` and `r8` carry flat host pointers to caller-owned buffers. `vmovdqu` (128/256-bit flat load, store and register copy), flat 32/64-bit `mov` load/store for volatile scratch GPRs (`rax`, `r9`, `r10`, `r11`) and terminal `ret` serve as transport/ABI glue.

### State model, ISA semantics and explicit rejections

YMM registers `ymm0`..`ymm5` use a portable C++ value struct (`struct Vec256 { std::uint64_t q[4]; }`) so the generated C++ compiles under default x64 flags without `/arch:AVX` and avoids signed overflow or shift-width undefined behavior. Every VEX.128 write zeroes bits `128..255` of the destination YMM register. `vzeroupper` zeroes bits `128..255` of all defined YMM registers; `vzeroall` zeroes all 256 bits of all six modeled registers `ymm0`..`ymm5`.

Key ISA rules enforced by the translator:

- **VEX.128 only**: AVX1 has no 256-bit packed-integer encodings (`VEX.L=1` packed-integer instructions belong to AVX2). Every `VEX.L=1` encoding of the 88 packed-integer forms is rejected explicitly (`vzeroall` is the sole `L=1` form in scope).
- **Shift counts**: `vpslld`/`q` and `vpsrld`/`q` support both `imm8` and `xmm` count encodings. Per the Intel SDM, the `xmm` count operand reads bits `63:0` as an unsigned 64-bit count (ignoring bits `127:64`), and counts `>=` lane width zero the lane without wrapping.
- **Saturation, rounding, compares and blends**: signed/unsigned saturating add, subtract, horizontal add/sub and pack clamp at exact 8-bit/16-bit bounds; `vpmulhrsw` rounds and scales per `(((a * b) >> 14) + 1) >> 1`; `vpblendvb` selects solely on bit 7 of each mask byte; `vpcmpeq*`/`vpcmpgt*` produce all-ones or all-zero lanes; `vpsign*` negates, zeroes or preserves per the sign of the second operand; `vphminposuw` breaks ties toward the lowest word index.
- **GPR bridge and memory operands**: `vpmovmskb` and `vpextr*` register forms zero-extend results into volatile scratch GPRs (`rax`, `r9`, `r10`, `r11`). Flat `[base + disp]` memory operands rooted in `rcx`, `rdx` or `r8` are accepted only on `vpextrb`/`w`/`d`/`q`, `vpinsrb`/`w`/`d`/`q` and transport `vmovdqu`/`mov`.

The translator explicitly rejects:

- `VEX.L=1` (256-bit) packed-integer encodings;
- memory operands on all register-only AVX packed-integer instructions;
- the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)` (`vpackuswb`, `vpaddd`, `vpaddw`, `vpaddsw`, `vpsubw`, `vpand`, `vpandn`, `vpor`, `vpxor`, `vpavgb`, `vpavgw`, `vpmaxsw`, `vpminsw`, `vpmaddubsw`, `vpmulhw`, `vpmullw`, `vpshufb`, `vpshufd`, `vpunpcklbw`, `vpunpckhbw`, `vpunpcklwd`, `vpunpckhwd`, `vpsllw`, `vpsraw`, `vpsrad`, `vpsrlw`, `vpslldq`, `vpsrldq`);
- undefined vector or scratch GPR reads, nonvolatile vector registers (`xmm6`..`xmm15` / `ymm6`..`ymm15`), nonvolatile GPRs (`rbx`, `rbp`, `rsi`, `rdi`, `rsp`, `r12`..`r15`), and scalar reads/writes of pointer-argument GPRs (`rcx`, `rdx`, `r8`);
- EVEX encodings, instruction prefixes, branches, out-of-scope instructions (AVX2-only ops, AVX floating-point ops, legacy SSE/MMX, GPR ALU), non-flat/non-argument memory operands, register-to-register `mov`, missing terminal `ret`, and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx_int_regs_original cpu_avx_int_regs_original_lifted cpu_avx_int_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx_int_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx_int_regs.asm) builds 119 exports covering all 90 surviving AVX packed-integer forms across their valid encodings: both `xmm`-count and `imm8` shifts (including in-range, overflow `>=` lane width, and upper-64-bit nonzero `xmm` counts), register and flat-memory forms of `vpextrb`/`w`/`d`/`q` and `vpinsrb`/`w`/`d`/`q`, `vpalignr` across offsets `0`/`5`/`16`/`23`/`32`, `vmpsadbw` ignored immediate bits, saturation edges, full `vpsign*` sign matrices, `vpmulhrsw` rounding boundaries, `vphminposuw` tie-breaking, `vpmovsx*`/`vpmovzx*` sign/zero-extension extremes, `vpblendvb` bit-7 mask checks, `vzeroupper`, a 192-byte `vzeroall` check across `ymm0`..`ymm5`, and a chained `op_pipeline` export. [The Windows test](../tests/cpu/avx_int_regs_check.py) checks 1,568 differential cases across all 119 exports with 16-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes the immediates of `op_palignr_5`, `op_pblendw`, `op_mpsadbw_0`, `op_pshufhw` and `op_pextrb_reg`, and must match the altered reference while diverging from the unmodified reference on 12, 11, 8, 10 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Alternate-encoding lift checks cover 6 r64/W1 GPR bridge forms (`vpmovmskb`, `vpextrb`/`w` and `vpinsrb`/`w` `r64` variants that MASM does not emit by default). Rejection controls cover 110 unit cases (25 `VEX.L=1` packed-integer encodings, 25 register-domain memory operands, 32 carved-out encodings covering all 28 carved-out forms, 10 out-of-scope instructions and 18 EVEX/ABI/GPR/addressing/state violations) and 11 end-to-end PE-patched DLL cases. In `tools/form_coverage.py`, `avx_int_regs.asm` covers all 90 surviving AVX packed-integer forms (plus transport `vmovdqu`, 91 AVX forms total), advancing x64 Zen 2 `AVX` group coverage alone from 7/256 to 96/256 (+89 net new forms; total x64 Zen 2 from 55/948 to 144/948).

Not covered by this experiment: the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)`, memory operands on register-only AVX packed-integer forms, 256-bit AVX2 packed-integer forms (`VEX.L=1`), AVX floating-point/move/test/string-compare forms, control flow or calls mixed with AVX state, nonvolatile register save/restore prologues, and title CPU code execution.

## AVX bit-move, shuffle, blend and logical leaf recompilation

[The AVX bit-move leaf translator](../tools/cpu_avx_move_aot.py) lifts bounded straight-line Windows x64 leaf functions covering 50 bit-transfer, shuffle, blend, broadcast, lane, permute, unpack, logical and masked load/store forms in `docs/coverage/x64_zen2.tsv` group `AVX` (excluding the 5 move forms `vmovdqa`, `vmovups`, `vmovaps`, `vmovq` and `vmovd` carved out to `cpu_vex_aot.py (PR #325)`; `vmovdqu` remains here as transport glue):

- Logical bitwise ops: `vandpd`, `vandps`, `vandnpd`, `vandnps`, `vorpd`, `vorps`, `vxorpd`, `vxorps` (XMM and YMM widths)
- Blends: `vblendpd`, `vblendps` (`imm8`), `vblendvpd`, `vblendvps` (sign-bit mask) across XMM and YMM widths
- Broadcasts: `vbroadcastf128` (`m128`), `vbroadcastsd` (`m64` and `xmm`), `vbroadcastss` (`m32` and `xmm`, XMM and YMM destinations)
- Lane ops: `vextractf128`, `vinsertf128`, `vextractps` (`r32`/`r64`), `vinsertps`
- Permutes: `vperm2f128`, `vpermilpd` (`imm8` and variable register-control), `vpermilps` (`imm8` and variable register-control)
- Shuffles and unpacks: `vshufpd`, `vshufps`, `vunpcklpd`, `vunpckhpd`, `vunpcklps`, `vunpckhps` (XMM and YMM widths)
- Vector and scalar moves: `vmovapd`, `vmovupd` (128/256-bit load, store and register copy), `vlddqu`, `vmovntdq`, `vmovntdqa`, `vmovntpd`, `vmovntps`, `vmovddup`, `vmovshdup`, `vmovsldup`, `vmovhlps`, `vmovlhps`, `vmovhpd`, `vmovhps`, `vmovlpd`, `vmovlps`, `vmovsd`, `vmovss`, `vmovmskpd`, `vmovmskps`
- Masked loads and stores: `vmaskmovpd`, `vmaskmovps` (all 8 VEX encodings across XMM/YMM load and store forms)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers and returns `void`. For transport and ABI glue the translator accepts `vmovdqu` (128/256-bit flat load, store and register copy), flat argument-rooted 32/64-bit `mov` between volatile scratch GPRs (`rax`/`eax`, `r9`/`r9d`, `r10`/`r10d`, `r11`/`r11d`) and `[base + disp]`, `vzeroupper`, and terminal `ret`.

### State model, bit-transparent semantics and explicit rejections

YMM registers `ymm0`..`ymm5` use a portable `struct Vec256 { std::uint64_t q[4]; }` value model, so the generated C++ compiles with default x64 MSVC flags without `/arch:AVX`. All 50 instructions transfer or combine raw bits with no FP arithmetic, no NaN canonicalization and no MXCSR involvement; `vcmp*`, `vtest*`, `vcomis*` and `vucomis*` are out of scope. Every VEX write to an XMM destination zeroes bits `128..255` of the corresponding YMM register; `vmovss` (`m32`) and `vmovsd` (`m64`) zero-extend into their destination XMM register; `vmovmskpd`, `vmovmskps` and `vextractps` zero-extend to 64 bits; variable `vpermilpd` inspects bit 1 of each 64-bit control lane; `vinsertps` applies `ZMask` after inserting `CountS` into `CountD`; and non-temporal moves (`vmovnt*`) lower as plain loads/stores. `vmaskmovps` and `vmaskmovpd` check the MSB of each mask lane inside an `if` guard so masked-off lanes never dereference memory, zero masked-off load lanes and preserve the mask register.

Memory operands are allowed as flat `[base + disp]` rooted in `rcx`, `rdx` or `r8` only where the form requires memory (loads, stores, broadcasts, half/scalar memory moves, non-temporal moves, masked loads/stores and transport `vmovdqu`/`mov` glue). The translator explicitly rejects:

- memory operands on all 28 register-domain AVX forms that have memory encodings (`vand*`, `vor*`, `vxor*`, `vblend*`, `vextractf128`, `vinsertf128`, `vextractps`, `vinsertps`, `vperm2f128`, `vpermil*`, `vshuf*`, `vunpck*`, `vmovddup`, `vmovshdup`, `vmovsldup`);
- the carved-out move forms owned by `cpu_vex_aot.py (PR #325)` (`vmovdqa`, `vmovups`, `vmovaps`, `vmovq`, `vmovd`);
- out-of-scope ops (`vcmp*`, `vtest*`, `vcomis*`, `vucomis*`, FP/integer arithmetic, AVX2-only ops, legacy SSE, branches);
- EVEX encodings, instruction prefixes, nonvolatile vector (`xmm6`..`xmm15` / `ymm6`..`ymm15`) or GPR (`rbx`, `rbp`, `rsi`, `rdi`, `r12`..`r15`) registers, clobbering argument GPRs, reg-to-reg `mov`, reads of undefined vector or scratch GPR registers, RIP-relative/indexed/32-bit/`rsp`/non-argument memory operands, missing terminal `ret` and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx_move_regs_original cpu_avx_move_regs_original_lifted cpu_avx_move_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx_move_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx_move_regs.asm) builds 150 exports covering all 50 surviving AVX forms across XMM and YMM widths, memory and register encoding variants, all 4 `vbroadcastsd` (`m64`) and all 8 `vbroadcastss` (`m32`) lane offsets, ignored immediate bits, `vperm2f128` lane-zeroing bits, `vinsertps` `CountS`/`CountD`/`ZMask` interactions (including zeroing `CountD`), `vpermilpd` bit-1 vs bit-0 variable control, IEEE-754 special bit patterns (`+0`, `-0`, subnormals, infinities, sNaN and qNaN), 32-byte aligned buffers for aligned and non-temporal moves, unaligned `+1`/`+3`/`+7` byte offsets for `vmovupd`/`vlddqu`/scalar/broadcast/half-XMM paths, GPR zero-extension checks (`vmovmskpd`, `vmovmskps`, `vextractps`), `vzeroupper`, a chained `op_pipeline` function, full/partial/zero/non-MSB-noise masks, and Windows `VirtualAlloc` + `PAGE_NOACCESS` guard-page fault-suppression checks for all 8 `vmaskmovps`/`vmaskmovpd` exports. [The Windows test](../tests/cpu/avx_move_regs_check.py) checks 2,418 differential cases (1,845 standard across 137 exports + 192 unaligned across 16 exports + 192 variable-blend across 4 exports + 176 `vmaskmov` across 8 exports + 13 negative-displacement across 1 export) and 16 guard-page fault-suppression cases with 32-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes immediates or memory displacements on 10 exports (`op_blendpd_ymm`, `op_blendps_ymm`, `op_broadcastf128`, `op_insertps_s3d2_zmask`, `op_perm2f128_mix`, `op_permilpd_imm_ymm`, `op_permilps_imm_ymm`, `op_shufpd_ymm`, `op_shufps_ymm`, `op_maskmovps_load_ymm`), matching the altered reference while diverging from the original on 13, 13, 11, 10, 11, 11, 11, 11, 11 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Unit and end-to-end controls cover 7 alternate-encoding lift checks (`0F 11` reg-reg `vmovsd`/`vmovss`, `W1` 64-bit `vmovmskpd`/`vmovmskps`/`vextractps`), 78 unit rejection controls (30 register-domain memory encodings across all 28 register-only forms, 13 carved-out move encodings, 13 out-of-scope ops, 22 EVEX/ABI/state/addressing violations) and 11 PE-patched DLL rejection controls. In `tools/form_coverage.py`, `avx_move_regs.asm` accounts for all 50 surviving AVX forms (plus `vmovdqu` and `vzeroupper` ABI glue, 52 AVX forms total), advancing x64 Zen 2 `AVX` group coverage alone from 7/256 to 57/256 (+50 net new forms; total x64 Zen 2 from 55/948 to 105/948). Together, `avx_int_regs.asm` and `avx_move_regs.asm` advance x64 Zen 2 `AVX` group coverage from 7/256 to 146/256 (+139 net new forms; total x64 Zen 2 from 55/948 to 194/948).

Not covered by this experiment: the 5 move forms carved out to `cpu_vex_aot.py (PR #325)`, memory operands on the 28 register-only AVX forms, FP arithmetic/compare/convert/test instructions, control flow or calls mixed with AVX state, nonvolatile `XMM6`..`XMM15` save/restore prologues, and title CPU code execution.

## Scalar SSE and SSE2 leaf functions

Compilers keep `float` and `double` values in XMM registers and use the scalar SSE and SSE2 instructions for them. The [scalar float translator](../tools/cpu_sse_aot.py) adds these instructions to the control flow and integer instructions of the [jump-table translator](../tools/cpu_switch_aot.py), without its jump tables. It lifts `MOVSS` and `MOVSD` between registers and memory, register `MOVAPS`/`MOVUPS` copies, `ADD`/`SUB`/`MUL`/`DIV` in both precisions, `CVTSI2SS`/`CVTSI2SD` from 32- and 64-bit integers, the truncating `CVTTSS2SI`/`CVTTSD2SI`, `CVTSS2SD`/`CVTSD2SS`, the four `COMISS`-family compares, `MOVD`, `MOVQ` between XMM registers and memory, and `XORPS`/`XORPD`/`PXOR` of a register with itself.

```powershell
cmake --build --preset dev --target cpu_float_original_lifted cpu_float_swapped_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_scalar_float$' -V
```

A PE does not record argument types, so each export is named with its signature, such as `blend_ss=f:ffqf`: the result, then up to four slots, each `f` (float), `d` (double) or `q` (a 64-bit integer or pointer). Under the Windows x64 convention, slot N arrives in XMM0 to XMM3 when it is floating point and in RCX, RDX, R8 or R9 otherwise. The generated function has the matching C++ signature. XMM6 to XMM15 are nonvolatile and a leaf has no frame to save them in, so any use is rejected.

The generated C++ keeps the low quadword of each XMM register in a 64-bit integer. Every supported instruction computes that quadword from low quadwords only, so the upper half of a register never reaches a result and is not modelled. The two dwords of the quadword are tracked as defined or undefined along every path. A `float` argument defines the low dword alone, a register copy carries undefined parts with it, and a read of an undefined part is rejected. `MOVSS` between registers and every single-precision result replace the low dword and keep bits 32 to 63.

Results must match the processor bit for bit, and three rules are written out because C++ leaves them to the host compiler:

- **NaN operands.** `ADDSS xmm0, xmm1` returns the quieted NaN of its destination when that is a NaN, otherwise the quieted source NaN. A compiler may swap the operands of `a + b`, which changes the payload when both are NaN, so the generated code tests the operands before the arithmetic. An invalid operation such as `inf - inf` gives the default NaN with the sign bit set.
- **Truncation.** `CVTTSS2SI` and `CVTTSD2SI` return the integer indefinite, `0x80000000` or `0x8000000000000000`, for a NaN or a value outside the destination range. The C++ cast is undefined there, so the range is tested first.
- **Compares and integer flags.** An unordered float compare sets ZF, PF and CF together; the three flags are stored by the compare and may feed a run of conditional jumps, as in the `JP`, `JNE` pair compilers emit for equality (`JE`, `JNE`, `JB`, `JAE`, `JBE`, `JA`, `JP` and `JNP` are accepted). Integer flag producers (including `NEG`) still feed only an immediately following conditional jump (`Jcc`), whereas `SETcc` and `CMOVcc` reuse the [jump-table translator](../tools/cpu_switch_aot.py) flag-producer rules across straight-line flag-neutral instructions within a block (without crossing joins or branches, and rejecting `COMISS`/`UCOMISS` as a flag producer). An 8-bit `SETcc` to a GPR preserves bits 8..63 and requires the register to be defined beforehand, a 32-bit `CMOVcc` zero-extends into bits 32..63 even when the condition is false, and a 16-bit `CMOVcc` is rejected.

Addition, subtraction, multiplication, division and the conversions run as one host instruction each under the thread's MXCSR, as in the original, with contraction to fused multiply-add switched off. Exceptions are assumed masked.

The [source fixture](../tests/cpu/scalar_float.asm) exports 14 functions (`blend_ss`, `blend_sd`, `convert`, `from_int`, `classify_ss`, `classify_sd`, `round_trip`, `zero_xorps`, `zero_pxor`, `zero_xorpd`, `probe_ss`, `neg_jcc`, `setcc_mix`, `cmovcc_mix`): single- and double-precision arithmetic through register and memory operands, conversions in both directions and between the precisions, signalling-NaN quieting, self-XOR zeroing, two classifiers that turn compare flags into a bit mask, `probe_ss`, `NEG`-driven conditional jumps, 8-bit `SETcc` preserving high bits after `CMP` and `NEG`, and 32-bit zero-extending and 64-bit `CMOVcc` after `CMP` and `NEG`. The test passes every floating-point slot as a `double` whose bit pattern it chooses, so signalling NaNs reach XMM unchanged and the upper dword of a `float` slot holds junk. Inputs cover zeros of both signs, denormals, infinities, quiet and signalling NaNs with distinct payloads, the values around ±2^31 and ±2^63, ties in the narrowing conversions, integer flag edge cases, and random patterns. For 13,690 calls it compares the result bits and a guarded output buffer between the original DLL and its translation, and again between a control build and the control's translation. The control assembles the same source with one operand order, condition, or source changed in each function. In `blend_ss` the control computes `b + a` instead of `a + b`, and the test requires that its results differ exactly when both operands are NaN. 43 calls have results fixed in the test from the instruction set reference, including both integer indefinites, the sign of the default NaN, 8-bit `SETcc` high-bit preservation on both true and false outcomes, and 32-bit `CMOVcc` zero-extension.

On a copy of the linked DLL, the test patches one instruction at a time and expects all 20 controls to be rejected: packed `ADDPS`, `SQRTSS`, VEX-encoded `VADDSS`, `ADDSD` on `float` arguments, `XORPS` of two different registers, a write to `XMM6`, a read of an unwritten register, a RIP-relative load, `CVTSS2SI` (which rounds by MXCSR), `MOVQ` to an integer register, `JLE` after `COMISS`, a float compare without a conditional branch, a second jump after `CMP`, `SETcc` after `UCOMISS`, `CMOVcc` after `UCOMISS`, `SETcc` across a branch after `NEG`, `CMOVcc` at a join target, a second jump after `NEG`, `SETcc` partial write to an undefined GPR, and a 16-bit `CMOVcc`. Eight wrong signatures are rejected as well, for example a `double` result from a function that defines only a `float`.

Not covered: RIP-relative constants, packed operations, `SQRT`, `MIN` and `MAX`, the rounding conversions, stack frames and calls.

On Linux, `llvm-ml` assembled the fixture and `lld-link` linked it. The original machine code ran in-process through a Windows-ABI trampoline that loads each slot into both its integer and its XMM register. It matched the lifted C++ built with clang at `-O0`, `-O2` and `-O3` on every call, for both builds, and all rejection controls passed. The generated sources compile without warnings under clang-cl `/W4 /WX`. A Windows run of `cpu_aot_scalar_float` with ML64 and MSVC has not been recorded yet.

## D3D12 pixel-position inputs

`pixel_position_centers` compiles a standalone Shader Model 6.0 pixel shader. Its inputs are `float4 TEXCOORD0` and `float4 SV_Position`, in the order emitted by the existing full-screen vertex shader. Red and green depend only on `SV_Position`. The test reads an 8×8 RGBA32F target with a 5×3 viewport at (2, 1). The CPU reference expects red and green to contain the absolute pixel center coordinates, with a half-pixel offset on each axis. The other 49 pixels retain the clear color. A second shader exchanges red and green and must first disagree at (2, 1), where the expected coordinates are (2.5, 1.5). [HLSL system-value semantics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-semantics)

The optional viewport in the pixel-case trailer is enabled by bit 1 of the flags byte. Four little-endian uint32 values follow in left, top, width, height order and must fit inside the 8×8 target. Existing optional NaN masks, when present, follow those fields. The fixture measures desktop screen-space coordinates. Scarlett register assignments require separate input-layout evidence.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_position_centers_(warp|hardware)$' -V
```

## Passing compute output directly to a pixel shader

`compute_to_pixel` translates a fixed `buffer_store_dwordx4` compute producer
and the scalar-buffer pixel color fixture. The producer dispatch writes four
words at byte offset 16 of a 256-byte default-heap buffer. The runner
transitions that resource from unordered access to constant-buffer state
and waits for the dispatch fence before binding its GPU address to the
pixel shader. Only the final RGBA32F render target is read back.

Two producer variants write different exactly representable colors. The
pixel shader is shared, and the CPU comparison checks every channel in
the 64×64 target for each palette. The stale-binding control dispatches
both producers and binds the earlier buffer while keeping the later
reference. The first pixel must differ.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^compute_to_pixel_(warp|hardware)$' -V
```

## Offline pixel shader recompilation

The floating-point predicate draw uses one `--check-pixel-batch` process per
compiled shader group. All 20 reference draws still run. For the final group,
the finite-instead-of-NaN control produces a mismatch inside the batch and the
following valid draw must pass. The fixture checks each status in order and
checks the process exit code. This leaves four device creations for the
predicate groups in place of 21, without changing the number of DXC calls.

The 32-bit integer, 16-bit integer and SDWA predicate draws use the same
runner, one batch per compiled group, with the same references. The
signed-against-unsigned control of the first `i32` e32 group and the
inverted-unordered control of each SDWA group run inside their batch as
expected mismatches, each followed by a passing draw. The three 16-bit
`must_mismatch` controls, two of them on mutated shaders, keep one single-case
draw each. These three suites
go from 108 device creations to 23, and DXC compiles one shader fewer, because
the signed/unsigned control now reuses the group's compiled shader.

The remaining multi-case `--check-pixel-case` loops across `draw-core`,
`draw-predicate-rounding`, `draw-blend-scalar`, `draw-e64`, `draw-integer-nc`,
`draw-bfm`, `three-way` and the full-RGBA `buffer-color` pass now group cases
per compiled shader through `run_pixel_batch()`, preserving every reference case
and expected mismatch control (`--check-pixel`, `--check-pixel-quad-lod`,
`--dual-pixel` and `--check-pixel-case-rg` keep their single-draw paths).
Across all eight modes, total renderer launches per backend drop from 494 to
263 (`draw-core`: 127 to 65; `draw-predicate-rounding`: 147 to 74;
`draw-blend-scalar`: 126 to 61; `draw-e64`: 57 to 50; `draw-integer-nc`: 12 to
3; `draw-bfm`: 15 to 3; `three-way`: 4 to 3; `buffer-color`: 6 to 4).

The first raw buffer_load_dword path remains a proposal while
[issue #319](https://github.com/R0Y00/Xbox2Windows/issues/319) is open. The
[binding contract](pixel_mubuf_contract.md) records the separate DW0/DW1
address masks, the 14-bit stride field, OOB_SELECT = 3 whole-DWORD bounds, a
bounded space1 title-lookup table for ps_6_0 and a separately observable host
binding failure for an unmapped title address. The tests keep an aligned
partial tail apart from rejected unaligned inputs. The gfx1013 bounds rule
remains open. No vector memory instruction is accepted by this change.

#The bitwise fixture uses AND, OR, XOR, NOT, BFI and three reversed shifts. Two constant sets exercise counts above 31. Exact mantissa bits distinguish the arithmetic and logical right shifts in GPU readback.

The scalar quad reduction forms are s_quadmask_b32 and s_quadmask_b64. Each
output bit is the OR of one four-bit group. The b64 result has sixteen quad
bits in the low word and zero in the high word, and both widths set SCC from
the complete result. s_bitreplicate_b64_b32 expands every source bit into two
adjacent destination bits and preserves SCC. The fixture covers sparse and
alternating groups, the null word, bits 0 and 31 and SCC in both incoming
states. Negative references cover an AND-within-group, a missing second copy,
the wrong source word, a quad in the high destination word and recomputed SCC.
Run ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V.
On Windows build 26300.10075, the 37-case fixture passed on WARP
10.0.26100.10068 and an AMD Radeon RX 9060 XT (driver 32.0.32029.2010)
with all 4,096 pixels matching on every positive case and every negative
control mismatching. The D3D12 debug layer was unavailable.
[Leonida scalar semantics](https://github.com/ItsNotRexed/Leonida/blob/4eb6155d/tests/semantic/test_salu_quadmask_bitreplicate.py) ·
[Leonida RDNA2 measurements](https://github.com/ItsNotRexed/Leonida/pull/1685) ·
[AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

## Pixel color without a texture sample

For an unsampled pixel program, a matched P1/P2 interpolation in the entry mask can supply attr0.x/y/z/w from a four-component TEXCOORD0. The profile binds entry v0/v1 to the barycentric pair and the observed m0 source to the interpolation base. Each completed P2 produces a distinct value, including when the destination overwrites v0. The test vertex shader varies all four channels across a full-screen triangle. CPU references check all four output words at 1,024 pixel centers; an incorrect channel at an interior pixel must fail. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_attribute_'`.

A local scan found 35 additional single-exit unsampled pixel records (12 different native instruction streams). All 35 compiled to DXIL and passed readback on RTX 4060 Laptop 32.0.16.1656 and WARP 10.0.26100.9818. The two constant-buffer records used two buffer inputs, giving 37 draws per adapter. This is a controlled TEXCOORD0 binding with linear screen-space test values.

In a separate two-exit population, 2,151 records read four words at byte offset zero through a zero-extended s9 address. Tracking that register range to its first direct use found 912 single-word, five two-word, 1,222 four-word and one eight-word scalar buffer reads. Eleven first uses remained unresolved. These counts identify consumers of a descriptor in the native program; they do not establish its original bound contents or the output of those programs.

Use `--no-texture` in place of `--sampler-words` for a program that writes MRT0 without sampling. For a constant-buffer program, `--texture-sgpr` identifies the entry register holding the low descriptor-table address and `--constant-descriptor` gives the byte offset of the four-word descriptor in that table. Offset zero is valid with this profile. At a tested call site the program copies entry s9 to the low address register, sets the high word to zero, fetches the descriptor and loads color words. Bind the test buffer at b0. Other programs can copy s9 to m0 for interpolation. Select the binding from the program's use of the register.

An RG-only compressed MRT0 export produces a two-component output. Match it with a red/green render-target write mask. The Windows fixture clears all four channels to -1, draws with two different constant buffers and compares every channel, including preserved blue and alpha. A stale reference must fail. A second draw profile checks the four-component export with all channels enabled. To reproduce:

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_buffer_color_' -V
```

A local audit against the 3310cb8 baseline retained the four sampled profiles and added six zero-sample profiles (entry table register 8 or 9, interpolation register 24, and no constant descriptor or a descriptor at byte 0 or 32). Of 26,503 records, 334 pixel records compiled with DXC `-Gis -T ps_6_0` and created D3D12 pipelines, up from 2. These represent 333 distinct containers and 21 distinct native instruction streams. Of the 332 additional records, 315 exported immediate values and 17 read the constant buffer; 298 wrote RG and 34 wrote RGBA. Each was drawn on an RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818). Reference colors for the new records were computed from the decoded operands, test buffer words and truncation to binary16. Two buffer inputs per constant-buffer record plus the original copy and day/night cases gave 352 draws per adapter. Each draw matched all 4,096 float4 pixels. These results use controlled buffers and render-target masks.

### Pixel programs with two exits

For FSSM version 0x101 the pixel extractor reads the 64-bit code offset, byte count and section end at 0xC0, 0xC8 and 0xE8. Instructions start 40 bytes after the code offset. The end must agree with offset plus count. For the unset end sentinel the extractor also checks for four padding words at the computed end. It strips aligned trailing `s_code_end` words inside that extent and requires a terminal `s_endpgm`. A marker in the following metadata cannot extend or terminate the code. The field interpretation agrees with the [independent FSSM cross-check](https://github.com/ItsNotRexed/Leonida/blob/dcd8ecf10b138566d567020562b7afd853a89024/docs/fssm_crosscheck.md).

Among 6,189 pixel records previously rejected by the unique-marker rule, 5,465 had two aligned end markers, 682 had three, 38 had four and four had five. The largest layout had two markers 20 bytes apart (3,561 records). In the supported two-exit layout, a direct branch targets the block immediately after the early exit. The extractor uses LLVM instruction boundaries to follow both conditional successors, preserves both exits and permits skipped alignment NOPs. Unresolved transfers, jumps into instruction operands or padding, and disconnected executable blocks are rejected. This verifies a code span; lowering branch or resource semantics is a separate step.

The source-built binary checks include a literal word resembling a terminator, a metadata terminator, conflicting extents, a branch into a literal and a disconnected exit. Run with LLVM 15:

```bash
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

`tools/pixel_aot.py` extracts one named pixel program from a local archive, decodes gfx1013 instructions with LLVM and emits HLSL. DXC compiles the result to desktop DXIL before the draw. The lowerer tracks register writes across whole-quad interpolation of attribute 0, a scalar texture-descriptor load, a 2D RGBA sample, packed round-toward-zero binary16 conversions and one compressed color export, or an uncompressed export of four distinct computed or sampled registers whose result may instead be returned as `uint4` for a lossless integer readback. Unknown instructions, mismatched bindings and reads before the required wait cause translation to fail. [LLVM disassembler API](https://llvm.org/docs/doxygen/group__LLVMCDisassembler.html) · [Leonida public format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/fssm_unpack.py)

The caller identifies the entry registers for the texture address and interpolation base, and supplies the four sampler descriptor words to match. The generated pixel shader uses `TEXCOORD0`, texture `t0` and sampler `s0`. The test binds a point-clamp sampler, one mip and a full-screen triangle. These bindings are an explicit specialization. The test measures finite UNORM inputs under full pixel coverage. Execution with a title's original pipeline state, helper-lane behavior at triangle edges and other texture formats require separate validation.

With the entry execution mask restored, `image_sample` also accepts `dmask:0x7` for a 2D RGB sample. It writes three consecutive VGPRs and preserves the following register. The LLVM 15 fixture presets that register to 0.5 and exports it as alpha alongside the sampled RGB. On WARP and RTX 5080, all four channels of 4,096 pixels matched the CPU reference bitwise. A reference that expects the sampled alpha of 1.0 instead fails first at (0, 0) on both adapters. [AMD RDNA 2 image instruction reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

For a single 2D RGBA sample, `image_sample_lz` lowers to `SampleLevel` at level 0 of the bound texture view. Both halves of an interpolation must execute under the same tracked mask. Entry-mask interpolation supplies the covered lanes for explicit LOD, while implicit sampling requires whole-quad coordinates. The BC7 fixture draws an 8×8 texture into a 4×4 viewport with different colors at mip levels 0 and 1. On WARP and RTX 5080, the explicit sample matched level 0 across 16 pixels and 64 channels. The implicit sample chose level 1 and failed the level-0 reference at (0, 0). [HLSL SampleLevel](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-to-samplelevel)

Depth-comparison sampling covers one form: `image_sample_c_lz vD, v[a:a+2], ... dmask:0x1 dim:SQ_RSRC_IMG_2D` and its NSA spelling `[vR, vU, vV]`. In RDNA 2 the reference comes before the coordinates. The result lowers to `tex.SampleCmpLevelZero(samp, float2(u, v), reference)` on `Texture2D<float> tex : register(t0)` and `SamplerComparisonState samp : register(s0)`. The four sampler words must still match the caller binding. `comparison_sampler_state()` decodes them, and the generated HLSL names the desktop state it needs in a comment. The compare function comes from word 0 bits 14:12, in the same order as `D3D12_COMPARISON_FUNC` minus one, and the result is `reference OP texel`. One point or bilinear XY filter maps to `D3D12_FILTER_COMPARISON_MIN_MAG_MIP_POINT` or `..._MIN_MAG_LINEAR_MIP_POINT`. Other values are refused, each with its own message:
- X/Y addressing other than `CLAMP_LAST_TEXEL`;
- anisotropy, unnormalized coordinates, `MC_COORD_TRUNC` or `FORCE_DEGAMMA`;
- a min/max filter mode;
- a non-zero `MIN_LOD` or LOD bias;
- different min and mag filters;
- mip filter 3 or word 2 bits 28–31;
- word 3 bits 12–29, unnamed on gfx10 (bit 29 is `UPGRADED_DEPTH` on GFX8);
- `TRUNC_COORD` 0 with point filtering, or 1 with bilinear filtering.

`TRUNC_COORD` (word 0 bit 27) is part of the binding contract. On gfx10 it applies to X and Y for every filter ([`ac_gpu_info.h`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/common/ac_gpu_info.h)). With 1 the texel-space coordinate is truncated, so point sampling takes the floor, which is the D3D rule ([D3D11.3 §7.18.7](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#7.18.7%20Point%20Sample%20Addressing)). With 0 the coordinate is rounded to nearest even at six fraction bits and point sampling then drops the fraction ([Mesa d573d1d8](https://gitlab.freedesktop.org/mesa/mesa/-/commit/d573d1d82524b8a2e5f56938069cabc0f0176a0e)), so it can take the next texel when the coordinate is up to 1/128 texel below an edge. The accepted pairs are the ones RADV programs on gfx10 by default ([`radv_sampler.c`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/vulkan/radv_sampler.c)): point with 1 and bilinear with 0. RADV clears the bit for DXVK and vkd3d-proton ([`00-radv-defaults.conf`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/util/00-radv-defaults.conf)), so point with 0 is a setting drivers use; it is refused here. Bilinear results are in the contract where the texel-space position (u × width − ½) is a multiple of 1/64, where that rounding is exact and D3D's 8-bit subtexel precision is exact too.

The reference and the texels are in the contract within [0, 1], ends included. D3D quantizes the reference to the texture format and clamps it to the format range before comparing ([`sample_c`](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#22.4.19%20sample_c)). The lowering does not decode the image format and the draws bind `R32_FLOAT`, so references or texels outside [0, 1], and NaN, are outside the contract.

Fields that cannot change a 2D LOD-zero result under this profile are not read: `CLAMP_Z`, `DISABLE_CUBE_WRAP`, `Z_FILTER`, the anisotropy threshold and bias, `MAX_LOD`, `PERF_MIP`/`PERF_Z`, the border colour and word 0 bit 31. A program that mixes comparison and ordinary sampling, or uses a second texture, is refused, because both would need `s0`. Field positions follow `SQ_IMG_SAMP_WORD0`…`3` in Mesa [`src/amd/registers/gfx10-rsrc.json`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/registers/gfx10-rsrc.json), and the enum values follow Linux `navi10_enum.h` (`SQ_TEX_DEPTH_COMPARE`, `SQ_TEX_CLAMP`, `SQ_TEX_XY_FILTER`). [HLSL SampleCmpLevelZero](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-to-samplecmplevelzero)

The fixed root signature of `tests/pixel_aot_vs.hlsl` has one ordinary static sampler, so `offscreen_check --check-pixel-compare` uses its own root signature with a descriptor-heap sampler. The case file supplies the filter and compare function from the decoded state. `tests/pixel_compare_check.py` draws an 8×8 `R32_FLOAT` texture into a 16×16 target, so bilinear weights are 1/4 and 3/4 and the expected results are exact multiples of 1/16. Three source-built programs cover all eight compare functions with both filters:
- the reference from constant word 0, equal to some texels, so `EQUAL` and the `LESS`/`LESS_EQUAL` split are visible;
- the reference interpolated from `attr0.x`, which varies per pixel and is never equal to a texel;
- the reference computed by `v_fma_f32` from constants, in the NSA form.

Three more programs move u and v by a constant in whole-quad mode before the sample, all eight compare functions each:
- point filtering 1/256 texel below an edge at every even pixel: truncation takes the texel below, rounding at six fraction bits the one above (96 of 256 pixels differ for `LESS`);
- point filtering exactly on an edge at even pixels: the floor takes the texel above;
- bilinear filtering on texel centres (one weight 0) and halfway between them.

Two more cover the ends of the interval: constant references 0 and 1 on a texture with texels equal to 0 and 1, both filters, so the strict and inclusive functions differ.

The expected images come from an independent CPU model: compare each texel, clamp to the edge, then weight bilinearly. The sampler words are written from the field layout in the test, not by the decoder.

On Windows 11 build 26200.9974 with the D3D12 debug layer enabled, NVIDIA GeForce RTX 3070 (driver 621.01) and WARP each matched all 104 draws (256 pixels, four channels, bitwise) with no debug messages. On each adapter, all 18 negative controls failed at pixel comparison:
- expected images for `GREATER` and `GREATER_EQUAL` (and `LESS_EQUAL` for the constant reference) against a `LESS` descriptor;
- bilinear depths compared once instead of compared texels filtered (192 and 159 of 256 pixels differ);
- a point sampler in place of bilinear;
- recompiled HLSL that ignores the reference;
- recompiled HLSL that swaps u and v;
- below an edge, the image for `TRUNC_COORD` 0, and on an edge, the texel below;
- on texel centres, the image without the offset;
- at references 0 and 1, the inclusive function against the strict one.

The draws show the desktop side. They show that the accepted point rule is the floor and that D3D does not round at six fraction bits; they cannot show the console rule.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_compare_' -V
```

```bash
python3 -B tests/pixel_compare_check.py
python3 -B tests/pixel_compare_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

Not covered: implicit-LOD `image_sample_c`, `_c_l`, `_c_b`, `_c_o`, `_c_d`, `image_gather4_c*`, cube and array shadow maps, border-colour addressing, references or texels outside [0, 1] (outside the contract above) and the two refused `TRUNC_COORD` pairs. How many archive records use these forms has not been measured.

For a shader that reads constants, pass `--constant-descriptor OFFSET` to identify its four-word descriptor in the same table as the texture descriptor. The desktop test binds 256 bytes at `b0`. A four-word read may span two HLSL constant vectors. Arithmetic support includes log2, exp2, reciprocal, rounding up (`v_ceil_f32`) and to nearest even (`v_rndne_f32`, lowered to HLSL `round`, which DXC emits as `Round_ne`), two-operand min/max, three-operand minimum, maximum and median (`v_min3_f32`, `v_max3_f32`, `v_med3_f32`), addition, multiplication, subtraction in either operand order, legacy zero-preserving multiplication, multiply-add, destination accumulation and saturation. The three-operand forms are emitted as nested HLSL `min` and `max`, with the median as `max(min(a, b), min(max(a, b), c))`. Each result is one of its operands, so finite inputs involve no rounding; NaN operands are not measured and may differ from the hardware's NaN rules. `v_mov_b32` copies a sample component, a loaded constant, an earlier result or an inline or literal constant, and rejects interpolation state, packed values and source modifiers. The lowerer rejects moves and floating-point arithmetic writes to the destination registers of an outstanding sample until its result is ready. With two samples in flight, `vmcnt(1)` permits moving the older result while all four destination registers of the newer sample remain protected. The dual-texture fixture uses that move in the rendered blend. As in the other operands, an integer inline constant is a bit pattern, not a converted value. Ordered and unordered `v_cmp_*_f32` comparisons, including the always-false and always-true predicates, but excluding class and exec-writing forms, become HLSL booleans, and `v_cndmask_b32` selects between two numeric operands with one. The lowerer accepts a mask in `vcc` or a scalar register under either wavefront-width spelling and rejects a select whose mask it did not see a comparison write. For `nlg`, equality or an explicit NaN check on either operand preserves the unordered case in DXIL. The earlier ordered comparisons produced a mismatch on the [reported RX 9070 XT driver](https://github.com/R0Y00/Xbox2Windows/issues/179). The pixel draw checks cover all 16 truth tables with less-than, signed-zero equality, greater-than and a quiet NaN on either side. Replacing a NaN with an equal finite operand must fail against the saved unordered reference. Compile arithmetic shaders with DXC `-Gis`. Transcendental precision and multiply-add rounding may vary by device. [AMD RDNA instruction reference](https://gpuopen.com/wp-content/uploads/2019/08/RDNA_Shader_ISA_5August2019.pdf) · [HLSL mad precision](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/mad)

`s_movk_i32` sign-extends its 16-bit immediate, and the translator folds the result. One draw checks the rule end to end on the GPU. It moves 0x8000 and 0x7fff, extracts bytes 1 and 3 of each with `s_bfe_u32`, and widens each byte as a binary16 subnormal scaled by 2^24 to the exact integer, so the draw exports 255, 128, 127 and 0. A reference for zero extension (byte 3 of 0x8000 as 0), and one for treating 0x7fff as negative (byte 3 as 255), must each report a mismatch.

Scalar ALU support also covers the 64-bit SOP1/SOP2 family (`s_cmov_b64`, `s_not_b64`, `s_brev_b64`, `s_and_b64`, `s_or_b64`, `s_xor_b64`, `s_andn2_b64`, `s_orn2_b64`, `s_nand_b64`, `s_nor_b64`, `s_xnor_b64`, `s_lshl_b64`, `s_lshr_b64`, `s_ashr_i64`, `s_bfm_b64`) and the 32-bit scalar bitfield extracts (`s_bfe_u32`, `s_bfe_i32`), complementing the 32-bit scalar ALU, comparison and full SCC tracking scope of PR #257 without overlapping it. Each 64-bit scalar result becomes an HLSL `uint2` local bound across an even-aligned SGPR pair (`s0`..`s105`), and each 32-bit extract becomes a `uint` local; constant-buffer inputs are read with `asuint` and vector operands consume scalar results through `asfloat`. Inline decimal integers sign-extend to 64 bits, while 32-bit hex literals zero-extend for `b64` operations and sign-extend for `s_ashr_i64`. Operations that update SCC in RDNA hardware (`s_not_b64`, the 64-bit bitwise and shift forms, `s_bfe_u32` and `s_bfe_i32`) track a nonzero-result boolean so `s_cmov_b64` selects between the source and existing destination pair. The complement forms invert the 64-bit AND, OR or XOR, and each sets SCC when its result is nonzero. The draw takes their register-pair results into the hash and probes SCC in both states: `s_nand_b64 -1, -1` and `s_nor_b64` with -1 are zero, and `s_xnor_b64` of the alternate pair with -1 is zero only in the second constant set. Replacing each with its uncomplemented form, or the zeroing operand of the `s_nor_b64` probe with 0, must produce a pixel mismatch; `s_brev_b64`, `s_bfm_b64` and `s_cmov_b64` preserve SCC, and `s_wqm_b64` and the exec-restoring `s_and_b64` clear it. Pending scalar loads, unaligned or out-of-range pairs, descriptor/exec/vcc/VGPR operands, untracked SCC reads, exec save/restore ops (`s_*_saveexec_b64`) and out-of-scope scalar ops are rejected.

`s_bfm_b32` (issue #596) builds a bitfield mask with the 32-bit scalar ALU table: `D = ((1u << (S0 & 31)) - 1u) << (S1 & 31)`, so a count of 0 or 32 gives an empty mask and the offset wraps modulo 32. `s_bfm_b64` uses the same formula on 64 bits with six-bit counts (`s_bfm_b64` helper, two 32-bit halves). Neither writes SCC. The RDNA 2 ISA guide lists the five- and six-bit count fields in SOP2 opcodes 36 and 37. The HLSL masks both counts explicitly, so no 32-bit shift reaches 32. The standalone integer-export draw (`pixel_aot_bfm_warp` or `pixel_aot_bfm_hardware`) covers counts 0, 1, 31, 32, 33, 63 and 64 and offsets that wrap; one control narrows the count mask to four bits and one swaps count and offset, and each must produce a pixel mismatch. That `s_bfm_b32` leaves SCC unchanged is checked only statically: a following `s_cselect_b32` still selects on the earlier compare. No draw covers it. Dropping the mask entirely is not observable on the GPU (DXIL masks shift amounts itself); that variant is caught by the static lowering check. Vector, pair or out-of-range operands, a missing operand and an unset source are rejected. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`s_brev_b32` reverses the 32 bits of its scalar source (`D = reversebits(S0)`) and, like `s_brev_b64` above, does not write SCC; it sits in the 32-bit scalar ALU table with no SCC expression. The standalone integer-export draw (`pixel_aot_brev_warp` or `pixel_aot_brev_hardware`) reverses scalar words 0, all ones, the top bit, bit 0, `0xaaaaaaaa` and `0x12345678` (plus an inline literal) and compares them with an independent bit-loop reference. Between an `s_cmp_eq_u32` and an `s_cselect_b32` it runs two reversals, and the select channel proves SCC survived both outcomes of the compare. Three controls must produce a pixel mismatch: `reversebits` replaced by `~` in the HLSL, an `s_not_b32` (which writes SCC) inserted before the select, and the `s_brev_b32` replaced by `s_not_b32`. A VGPR source or destination, a pair operand, a missing or extra operand, an out-of-range or unset register and a pending scalar load are rejected, and so is a select without a prior compare. The form is counted as SOP1 coverage in both reference lists. Only the synthetic fixture is measured, not a real title shader. Reproduce with `ctest --preset dev -R "^pixel_aot_brev_" -V`.

The SDWA forms of the 16 `v_cmp_*_f32` predicates are lowered when both `src0_sel` and `src1_sel` are `DWORD`. With whole-dword selections they compare the operands exactly as the e32/e64 forms do; the translator emits identical HLSL for both, and a control asserts it. A byte or word selection would compare a zero-extended field as binary32 and is refused. The predicate draws are repeated with the SDWA compares for all four groups and the same five input pairs, including NaN on either side. A reference that inverts each group's unordered result must report a mismatch.

Scalar comparison lowering covers 18 RDNA2 SOPC instructions: 32-bit signed comparisons (`s_cmp_eq_i32`, `s_cmp_lg_i32`, `s_cmp_gt_i32`, `s_cmp_ge_i32`, `s_cmp_lt_i32`, `s_cmp_le_i32`), 32-bit unsigned comparisons (`s_cmp_eq_u32`, `s_cmp_lg_u32`, `s_cmp_gt_u32`, `s_cmp_ge_u32`, `s_cmp_lt_u32`, `s_cmp_le_u32`), 64-bit scalar comparisons (`s_cmp_eq_u64`, `s_cmp_lg_u64`), 32-bit bit comparisons (`s_bitcmp0_b32`, `s_bitcmp1_b32`), and 64-bit bit comparisons (`s_bitcmp0_b64`, `s_bitcmp1_b64`). Each comparison sets the scalar condition code boolean (`scc`) to the comparison outcome, making it available for subsequent conditional moves (`s_cmov_b64`). Signed integer comparisons cast operands with `asint`, 64-bit comparisons inspect both low and high dwords across even-aligned SGPR pairs, and bit comparisons mask by `src1 & 31u` (selecting low or high dword via bit 5 for 64-bit forms). Unaligned register pairs, out-of-range registers (`s106`), and unsupported comparison types (`s_cmp_gt_u64`, `s_cmp_eq_i64`) are rejected.

Scalar ALU instructions act on uniform 32-bit values: immediates, loaded constant words and earlier scalar results. Each result becomes an HLSL `uint` local, and a vector move or operand reads it through `asfloat`. Supported forms are `s_add_u32`, `s_sub_u32`, `s_add_i32`, `s_sub_i32`, `s_absdiff_i32`, `s_mul_i32`, `s_min_u32`, `s_max_u32`, `s_min_i32`, `s_max_i32`, `s_and_b32`, `s_or_b32`, `s_xor_b32`, `s_andn2_b32`, `s_orn2_b32`, `s_not_b32`, `s_nand_b32`, `s_nor_b32`, `s_xnor_b32`, `s_lshl_b32`, `s_lshr_b32`, `s_ashr_i32`, the scaled addition family (`s_lshl1_add_u32`, `s_lshl2_add_u32`, `s_lshl3_add_u32`, `s_lshl4_add_u32`), the six `s_cmp_*_i32` and six `s_cmp_*_u32` relations, and `s_cselect_b32`, besides the existing `s_mov_b32` and `s_movk_i32`. SCC follows the RDNA definitions: carry or borrow for the unsigned add, shifted additions and subtract, signed overflow for the signed ones, the comparison for min, max and `s_cmp_*`, and a nonzero result for the bitwise, shift, and absolute difference forms. For `s_lshl<K>_add_u32`, SCC records whether the 64-bit sum `(S0 << K) + S1` generated a carry out of bit 31, evaluated in 32-bit unsigned arithmetic as `(S0 >> (32 - K) != 0) || (dest < S1)`. `s_mul_i32` leaves it unchanged. A shift uses the low five bits of its count. `s_cselect_b32` is rejected unless the lowerer saw SCC written, and `s_wqm_b64` and the exec restore clear the tracked SCC. Scalar results are not folded to constants, so they cannot supply a texture address or sampler word. Carry-in, 64-bit, high-half multiply, bitfield, `s_cmpk_*`, `s_bitcmp*` and `s_cmov*` forms remain unsupported. The draw test packs 23 SCC outcomes and a 17-bit hash of the 32-bit results into four channels, ten bits each, exported as 1 + k/1024, which binary16 holds exactly. The program is drawn with two sets of constant words, including negative and sign-bit inputs, and every select takes a different arm between the sets. A CPU interpreter written from the instruction definitions supplies the expected color. Replacing `s_ashr_i32` with `s_lshr_b32`, `s_cmp_lt_i32` with `s_cmp_lt_u32`, `s_nand_b32` with `s_and_b32`, `s_nor_b32` with `s_or_b32`, `s_xnor_b32` with `s_xor_b32`, or swapping the subtraction's sources must produce a mismatch. In addition, `check_lshl_add_u32_draw` verifies `s_lshl[1-4]_add_u32` end-to-end via `R32G32B32A32_UINT` readback across small values, shift-wrap, and add-wrap arithmetic cases, alongside selective SCC carry-out cases through `s_cselect_b32`; corrupted expectation words and inverted carry flags report pixel mismatches. `check_absdiff_i32_draw` verifies `s_absdiff_i32` end-to-end via `R32G32B32A32_UINT` readback across positive, negative, zero (clearing SCC), double-negative, mixed-sign, INT_MIN edge, and (2^31, 2^32) overflow-wrap cases alongside SCC nonzero checks; corrupted expectation words report pixel mismatches.

The `s_bcnt0_i32_b32` and `s_bcnt1_i32_b32` instructions count zero or one bits in a 32-bit scalar. Their `b64` forms count across an SGPR pair. All four set SCC when the count is nonzero. The predicate and rounding draw checks the count and SCC against a Python reference and rejects a 64-bit count that ignores the upper word.

`s_bfe_u64` and `s_bfe_i64` extract a field from two scalar dwords. The control supplies the offset in bits 5:0 and the width in bits 22:16. An empty field returns zero. A field extending past bit 63 stops at bit 63, and the signed form extends the top bit of the resulting field. The lowerer reuses the two-dword shifts and sets SCC when either result word is nonzero. The source-built test uses raw UINT readback to check both words and SCC independently. Cases include an extraction across the dword boundary, a result with only its high word set, and offsets and widths at their limits. The signed and unsigned draws each require a mismatch against the other's cross-dword reference. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`s_cselect_b64` selects both words of the first source when SCC is set and both words of the second source otherwise. The pixel lowerer uses the tracked SCC and emits a `uint2` for the destination SGPR pair. It reads both operands before writing the destination, so the destination can alias a source. It preserves SCC. The source-built fixture exports both selected words and uses a second select between inline `0` and `-1` to expose SCC in the other two channels of a UINT target. Swapping the register sources must cause a mismatch on all three constant sets. The fixture runs in the LLVM binary check and in `ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

From the repository root on Linux with LLVM 15 installed, assemble the original test shaders, wrap them in synthetic records, then extract, decode and translate them. The test compares the result with lowering the source operands directly.

```sh
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

On Windows, use the Python-enabled build from the project README. Each backend draws a copy, a red/blue swap, a nonlinear curve and a compare-and-select program, the last two with two sets of constants each. It compares all four channels of 4,096 pixels bitwise with an independent CPU reference. Negative controls change the expected channel order, the bound gain or the comparison threshold and must report a mismatch. The select program uses only comparisons, one subtraction and selects, so its reference contains no transcendental or fused step and assumes only correctly rounded UNORM sampling. Its inputs are finite, so NaN operands remain unmeasured. The absolute-value fixture also draws abs(0.5 - red) and checks that removing the modifier produces a mismatch. The add, multiply and move program is drawn with two sets of constants, and replacing its reversed subtractions with forward ones must produce a mismatch. Its reference asserts that every addition, subtraction and multiplication is exact in binary32 for the test inputs, so it does not depend on rounding direction or fusion. The `scalar64` fixture exercises all 14 newly lowered scalar forms across two 12-word constant sets, plus a third drawn without mutations for a high-word-only `s_not_b64` result and an `s_bfe_i32` width of 64 (including NaN bit patterns, negative 64-bit inputs, zero/small/large/wrapped shift counts, `s_bfm_b64` width boundaries, signed/unsigned `s_bfe_*` across `offset + width < 32` and `>= 32`, and SCC preservation through `s_cmov_b64`, `s_brev_b64` and `s_bfm_b64`), folding the 64-bit accumulator and 10 flipping SCC observation bits into four exact binary16 fields (`1 + k/1024`). Four mutations (`s_ashr_i64` to `s_lshr_b64`, `s_bfe_i32` to `s_bfe_u32`, `s_andn2_b64` to `s_and_b64`, and replacing an `s_cmov_b64` `-1` source with `0`) must each report `pixel_mismatch`. What this measures is the synthetic fixture translation, DXC compilation and bitwise D3D12 readback; exec save/restore control flow, SOPP branches, scalar-computed descriptors and real title pixel-shader execution remain unverified.

`v_cubeid_f32`, `v_cubesc_f32`, `v_cubetc_f32` and `v_cubema_f32` follow LLVM's `ConstantFoldAMDGCNCubeIntrinsic` (`llvm/lib/Analysis/ConstantFolding.cpp`). The major axis is z if |z| is at least |x| and |y|, otherwise y if |y| is at least |x|, otherwise x; a face is negative only for a nonzero, non-NaN negative coordinate, and `v_cubema_f32` returns twice the signed major coordinate. The lowering computes each result inline from the three operands. One draw evaluates all four operations for six directions that cover every face, the y/x and z/x ties and -0 on the major axis, which selects face 4 and exports -0 for the major axis. Each direction has a wrong reference for the property it isolates (the tie winner, the sign of -0, the sign of the major axis, the sign of a coordinate), and each must report a mismatch. NaN coordinates are not drawn.

Signed and unsigned 32-bit integer comparisons (`v_cmp_{f,lt,eq,le,gt,ne,ge,t}_{i32,u32}` in both e32 and e64 encodings) reinterpret operands via HLSL `asint` or `asuint`, reject float source modifiers and `clamp`, and feed `v_cndmask_b32` through the same tracked lane-mask path as `v_cmp_*_f32`. The binary and draw fixtures cover all 32 forms across normal and signed-zero bit patterns (`0x80000000` vs `0x00000000`, `0xbf800000` vs `0xbf800000`, `0x3f800000` vs `0xbf800000`, and `0xc0000000` vs `0xbf800000`) to separate signed, unsigned, and floating-point orderings; evaluating the signed shader against the unsigned oracle on `(0x80000000, 0x00000000)` must report a pixel mismatch.

A focused `v_cmp_lt_u32` fixture exports both operand orders through `v_cndmask_b32`, with `vcc_lo` for e32 and `s52` for e64. Its three draws check the sign boundary (`0x80000000` vs `0x7fffffff`), equal words, and the raw patterns `0x7f800002` and `0x7fc00001`. Quieting the first NaN would reverse that pair's unsigned order. Each draw checks all four channels of 4,096 pixels against a CPU reference. Replacing both comparisons with `v_cmp_lt_i32` must produce a pixel mismatch at the sign boundary.
SDWA forms of `v_mov_b32` and `v_cvt_f32_f16` select part of the source and place the result. `src0_sel` takes a byte, a word or the whole dword. The selection is zero-extended, or sign-extended under `sext()`. `dst_sel` writes the low bits of the result into a byte or word of the destination. Following the instruction equivalences in LLVM's `SIPeepholeSDWA` (for example, `v_lshlrev_b32` by 16 is `dst_sel:WORD_1 dst_unused:UNUSED_PAD`, and `v_ashrrev_i32` by 16 is a sign-extended `src0_sel:WORD_1`), `UNUSED_PAD` zeroes the other bits and `UNUSED_PRESERVE` keeps them from the destination's tracked value and is refused when the destination holds no tracked numeric value. The SDWA `v_cvt_f32_f16` widens the selected half and applies `abs`, `neg` and `clamp` after widening, as the e64 form does. Two programs are drawn with two constant sets each. The first moves `WORD_1` and a sign-extended `BYTE_2` into the low half of a register holding 1.0, then splits `(v - 1) × 2^23` into bytes. The second moves a byte into `BYTE_3` and a word into `WORD_1` with padding, and widens both halves of one word. Wrong references must each report a mismatch: the neighbouring byte or word, a zero-extended byte, a sign-extended positive byte, and the other half. `UNUSED_SEXT` is refused, because the instruction equivalences do not pin down its effect on the bits below the field. Byte selections for half widening are also refused.

The rounding program applies `v_ceil_f32` and `v_rndne_f32` in both the VOP1 and VOP3 encodings to power-of-two multiples of the sample and to constant ties (2.5 and -3.5, which round to 2 and -4). It is drawn with two sets of constants against a CPU reference that rounds ties to even. Replacing either instruction with `v_floor_f32`, or the generated `round` with `floor(0.5 + x)`, must produce a mismatch. No input rounds to a signed zero, so the sign of a zero result remains unmeasured.
SDWA forms of `v_mov_b32` and `v_cvt_f32_f16` select part of the source and place the result. `src0_sel` takes a byte, a word or the whole dword. The selection is zero-extended, or sign-extended under `sext()`. `dst_sel` writes the low bits of the result into a byte or word of the destination. Following the instruction equivalences in LLVM's `SIPeepholeSDWA` (for example, `v_lshlrev_b32` by 16 is `dst_sel:WORD_1 dst_unused:UNUSED_PAD`, and `v_ashrrev_i32` by 16 is a sign-extended `src0_sel:WORD_1`), `UNUSED_PAD` zeroes the other bits and `UNUSED_PRESERVE` keeps them from the destination's tracked value and is refused when the destination holds no tracked numeric value. The SDWA `v_cvt_f32_f16` widens the selected half and applies `abs`, `neg` and `clamp` after widening, as the e64 form does. Two programs are drawn with two constant sets each. The first moves `WORD_1` and a sign-extended `BYTE_2` into the low half of a register holding 1.0, then splits `(v - 1) × 2^23` into bytes. The second moves a byte into `BYTE_3` and a word into `WORD_1` with padding, and widens both halves of one word. Wrong references must each report a mismatch: the neighbouring byte or word, a zero-extended byte, a sign-extended positive byte, and the other half. `UNUSED_SEXT` is refused, because the instruction equivalences do not pin down its effect on the bits below the field. Byte selections for half widening are also refused.

The SDWA forms of the exact arithmetic (`add`, `sub`, `subrev`, `mul`, `mul_legacy`, `max`, `min`, `floor`, `ceil`, `fract` and `trunc`) use the e32/e64 lowering and place the result with the SDWA move rules. Only `src0_sel:DWORD` and `src1_sel:DWORD` are accepted. A byte or word source would reach the arithmetic as a zero-extended, usually subnormal binary32, and Scarlett's binary32 denormal mode is not measured. Output modifiers (`mul:2`, `div:2`) are refused. Four programs draw every operation once, each with source modifiers, an inline constant, `clamp`, the legacy zero rule (0 × ∞ = 0) and a byte placed into `BYTE_3`. A result is kept in `WORD_0` and `BYTE_2` of 1.0 under `UNUSED_PRESERVE`, and the low word of another is padded into `WORD_1`. Every result is exact in binary32 and after the half export. Each channel has a wrong reference that must report a mismatch.

The SDWA forms of the remaining unary binary32 operations (`v_exp_f32`, `v_log_f32`, `v_rcp_f32`, `v_rcp_iflag_f32`, `v_rsq_f32`, `v_sqrt_f32`, `v_sin_f32`, `v_cos_f32`, `v_rndne_f32` and `v_frexp_mant_f32`) go through the same path: each reuses its e32/e64 lowering unchanged, with `src0_sel:DWORD` only, and the result is placed with the SDWA move rules. The translator emits the same shader for the whole-dword SDWA spelling and the e32 spelling of each operation, and the controls assert that. A byte or word source, a `src1_sel`, a second operand, an output modifier, `UNUSED_SEXT` on a partial destination and `UNUSED_PRESERVE` over an untracked destination are refused for every one of the ten. Three programs are drawn with two constant rows each. D3D allows `exp2`, `log2`, a reciprocal, `rsqrt`, `sqrt`, `sin` and `cos` an error, so the first two programs choose inputs whose true result is an integer, add 0.5 and take the floor, which returns that integer for any result within 0.5 of it: exp of `-A`, log of `|B|`, rcp of C and rcp_iflag of `-|D|` give 8, 5, 8, -4 and then 16, 3, -2, -16; rsq of `|A|`, sqrt of B, sin of C and cos of `-D` (in revolutions) give 4, 7, 1, -1 and then 2, 3, -1, 1. That checks which operation ran and its source modifiers, not last-place accuracy. The third program is exact and is compared directly: `v_rndne_f32_sdwa` of the ties 2.5 and -3.5 (2 and -4), `v_frexp_mant_f32_sdwa` of `-B` with `clamp`, the low byte of a rounded tie kept in `BYTE_2` of 1.0 under `UNUSED_PRESERVE`, and the low byte of a significand padded into `BYTE_3`. 26 wrong references, each replacing one channel, must report `pixel_mismatch`: a neighbouring operation for each approximate one (natural exponential and logarithm, rsq for rcp, sqrt and rcp for rsq, rsq for sqrt, sin and cos swapped, radians for revolutions), a copied source, a dropped negation, ties rounded up or toward zero, a dropped clamp, padding for preserving, the neighbouring byte and an unplaced result. That is 32 draws from three compiles and three renderer launches. They run as their own tests, `pixel_aot_sdwa_unary_hardware` and `pixel_aot_sdwa_unary_warp` (`tests/pixel_aot_check.py draw-sdwa-unary`), with their own coverage record, and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. Not shown: last-place accuracy of the approximate operations on any adapter, a partial destination for an approximate result (its low bits have no single reference), inputs outside these rows (zero, negative arguments to log, rsq and sqrt, infinities, NaN, denormals), the integer-divide flag of `v_rcp_iflag_f32`, and what the console computes.

Signed and unsigned 16-bit integer comparisons (`v_cmp_{eq,ne,lt,le,gt,ge}_{i16,u16}` in e32 and e64 encodings) compare only the low 16 bits of each source: the lowering sign-extends them with `asint(x << 16) >> 16` or masks them with `& 0xffff`, and the result feeds `v_cndmask_b32` through the same tracked lane mask as the other comparisons. A source may be a register, an integer inline constant from -16 to 64 or a literal up to `0xffff`. LLVM prints a float inline constant on a 16-bit integer operand as its binary16 literal (`1.0` as `0x3c00`), so float tokens, wider literals, source modifiers, `clamp` and `op_sel` are refused. The exec-writing `v_cmpx_*` forms, the 64-bit and f16 comparisons and `v_cmp_class_f16` stay rejected, and `tests/form_coverage_check.py` asserts that all 24 16-bit forms and the 32 32-bit forms are counted while those neighbours are not. The binary fixtures cover the 24 forms, and the draws (`tests/pixel_aot_check.py draw-predicate-rounding`) evaluate all six predicates per kind and encoding on six word pairs. Each word is a normal binary32 value whose upper half differs from the lower one where a 32-bit comparison would disagree, and the pairs include `0x8000` against 0, equal low halves, -1 against 1, `0x7fff` against `0x8000` and both ends of the inline-constant range. Three references must report a pixel mismatch: a signed shader against the unsigned reference, `eq` replaced by `ne`, and a 32-bit comparison against the 16-bit reference on equal low halves.

`v_cvt_f32_u32_e32` and `v_cvt_f32_i32_e32` convert one 32-bit word to binary32, reading it as unsigned or two's-complement. The e32 encoding is lowered without modifiers: source modifiers, clamp or omod suffixes on the e32 spelling, and DPP or SDWA variants are refused, as is a scalar source whose constant load is still pending. The source is consumed through the same bit-copy representation `v_mov_b32` produces, so `asuint` or `asint` of a moved literal such as `0xFFFFFFFF` or `0x80000000` recovers the exact word before the conversion; the result is emitted as a `precise float` local like the other ALU lowerings. The CPU references convert through Python's binary64 float, which holds every 32-bit integer exactly, then pack to binary32, rounding ties to even; the Scarlett pipeline's MODE.FP_ROUND is unmeasured, so nearest-even is the harness's recorded assumption (`F32_ROUND_ASSUMPTION`), following the binary32 denormal convention, and an adapter disagreeing on a row would be recorded as a mismatch rather than chosen. The inputs are shader literals, not constant-buffer words (issue #360): each draw moves two words with `v_mov_b32_e32`, converts each unsigned and signed, and exports four uncompressed binary32 channels. The words cover 0, 1, the last exactly representable integer 2^24, the ties 2^24+1 and 2^25+2, which round to the even neighbor, the non-tie neighbors 2^25+1 and 2^25+3, which round down and up, and the extremes 0xFFFFFFFF, 0x80000000 and 0x7FFFFFFF. A program that swaps the unsigned and signed conversions on the 0xFFFFFFFF/0x80000000 pair, where every channel changes, must report a mismatch. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_'`; the LLVM 15 assemble-and-disassemble round trip runs as `python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1`.

`v_cvt_f32_u32_e64` and `v_cvt_f32_i32_e64` (issue #360, stage 2) lower the same conversion with the e64 clamp modifier, bit 15 of the raw encoding's first dword: the plain e64 spelling emits exactly the e32 lowering, and with `clamp` the result is wrapped in HLSL `saturate(...)`, so the same word pairs and nearest-even references cover both encodings, ties included. omod is explicitly rejected rather than lowered, per the issue's fallback option: LLVM 15.0.7's assembler rejects every probed omod syntax on these forms — `omod:1`, `omod:2`, `omod:3`, `omod(2)`, `mul:2`, and `clamp` combined with `omod:2` — and also on `v_add_f32_e64` and `v_mul_f32_e64`, and the disassembler never prints omod for the probed raw encodings with both 2-bit fields swept over values 1 to 3, so an omod-carrying program cannot round-trip through the assembler. abs and neg on the integer source (`|s44|`, `-s44`) are refused as well; the assembler refuses them too. The clamp draws pair (0, 1), (2, 0xFFFFFFFF) and (0x80000000, 0x7FFFFFFF), expecting min(1.0, max(0.0, .)) of the u32f/i32f reference for each word: rows (0, 0, 1, 1), (1, 1, 1, 0) — 0xFFFFFFFF is 4294967295.0 unsigned, which rounds to 4294967296.0, but -1.0 signed — and (1, 0, 1, 1), where 0x7FFFFFFF rounds to 2147483648.0 before clamping. Two negative controls run on every backend: dropping `clamp` from the program exports u32 of 2 as 2.0 against the clamped 1.0 reference still on disk, and swapping the unsigned and signed conversions under clamp on the 0xFFFFFFFF/0x80000000 pair exchanges 1.0 and 0.0 on every channel. The rounding assumption is unchanged: the clamp rows contain no tie words and every conversion either is exact or clamps to exactly 0.0 or 1.0, so they do not depend on it, while the plain e64 rows keep the stage-1 tie coverage under the recorded nearest-even reference. Binary and draw fixtures additionally verify SGPR, VGPR and literal operands across negative and positive integers. Mutating `v_cvt_f32_i32` to `v_cvt_f32_u32` or dropping `clamp` produces a pixel mismatch. The reproduction commands are unchanged.

`v_cvt_u32_f32_e32` and `v_cvt_i32_f32_e32` convert one binary32 value to a 32-bit word. Only the plain e32 encoding with a VGPR, an SGPR or a hex literal source is lowered, through the shared helpers in `src/ffx_float_to_int.hlsli`, emitted once before `main`; the signed path is restricted to the entry mask; the result word is held as its bit pattern in a `precise float` local, like a `v_mov_b32` literal, and the draws read it back through the integer export. A float export of such a word is translated the same way, but no draw checks it, so whether a float render target keeps subnormal or NaN bit patterns there is untested. Decimal inline constants are refused because `fp_operand` reads them as bit patterns and the f32 reading has not been checked against the ISA. e64 spellings, source modifiers, clamp and omod suffixes, DPP and SDWA variants, `v_cvt_rpi_i32_f32`, `v_cvt_flr_i32_f32` and a pending scalar load are refused. The rule is the one in issue #360, which cites the RDNA 2 ISA; it was not re-checked against the ISA document and not measured on the console (`F32_TRUNC_ASSUMPTION`): results truncate toward zero, NaN gives 0, and out-of-range values saturate (u32: x <= 0 gives 0, x >= 2^32 gives 0xFFFFFFFF; i32: x <= -2^31 gives 0x80000000, x >= 2^31 gives 0x7FFFFFFF). Truncation is assumed to ignore MODE.FP_ROUND. The input is exact in every denormal mode, since a subnormal truncates to 0; result words 0x00000001 to 0x007FFFFF are binary32 subnormal bit patterns in a `precise float`, and the harness compiles with `-denorm preserve`. Whether an FTZ build would flush them is not claimed and not tested, and a later float instruction that reads such a result would follow the pipeline's `FLOAT_MODE`. The draws move literal words with `v_mov_b32_e32`, convert each unsigned and signed, and export through the lossless `R32G32B32A32_UINT` readback. They cover 18 words in 9 pairs: both zeros, 1.5, -2.75, -0.5, -1.0, a quiet NaN and a negative NaN with payload, both infinities, 2^32, 4294967040, 2^31, 2147483520, -2^31, the next binary32 below -2^31 (the issue's -2^31-1 is not representable and rounds to -2^31), the smallest subnormal and 0.99999994; a further draw reads 3.75, -3.75, 1e10 and -1e10 from SGPRs. A program that swaps the two conversions on -0.5 and -1.0 must report a mismatch. Measured on WARP (driver 10.0.26100.9818) and an NVIDIA GeForce RTX 3080 (driver 32.0.16.2124): every row passes on both. Dev-time mutants (not in CI): replacing the unsigned helper's guards with a bare `uint(x)` fails on both WARP and the RTX 3080, so the saturation guards matter. The helpers apply the cast only to an operand that is made NaN-free and clamped to the largest binary32 below 2^32 or 2^31, because an out-of-range or NaN cast is undefined in HLSL and poison in LLVM. NaN fails every guard compare, so it reaches the cast as 0. Dropping that NaN replacement from both helpers fails on both adapters: the NaN rows then export 0x80000000 from the signed conversion instead of 0. The LLVM 15 round trip (`pixel_aot_check.py binary`) runs in the Ubuntu CI job and also passed locally with LLVM 15.0.7 on Windows. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_'`.

The same binary and draw checks cover `v_trunc_f32` in e32 and e64 forms. The lowerer emits HLSL `trunc`, which rounds toward zero and returns a floating-point value. With two constant sets, the RTX 5080 and WARP draws matched all four channels of 4,096 pixels per draw against the CPU reference. The fixture covers both signs, signed zero, source negation and absolute value, and both clamp bounds. Replacing truncation with floor changed the first pixel on both adapters. Removing the upper clamp changed alpha from 1 to 2 in the first pixel on WARP. The source-built fixture verifies desktop readback with the bindings above. Execution with a title's own pipeline state remains unverified.

The `v_ceil_f32` fixture checks rounding toward positive infinity in e32 and e64 encodings. It compares finite integral and fractional inputs of both signs, including signed zero, and exercises e64 absolute value, negation and clamp. Replacing the e32 ceiling with a floor must produce a pixel mismatch. The translator rejects a ceiling write into a pending texture sample destination before `vmcnt(0)`. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The `v_cvt_f32_ubyte0` to `v_cvt_f32_ubyte3` fixture converts byte 0 to 3 of a source word to binary32, as an unsigned integer. The source is read as bits through `asuint`, so float modifiers do not apply and the translator does not accept them. Each output reads a different byte of a constant word in e32 and e64 encodings, and the last uses clamp. The words are normal binary32 values with four distinct bytes, so the bits survive the constant buffer and reading the wrong byte changes the result. Replacing `ubyte1` with `ubyte0` must produce a pixel mismatch.

The `v_rcp_iflag_f32` fixture lowers the reciprocal that compilers emit in integer-division sequences as an ordinary `1.0f / x`. The flag it raises for the integer divide has no desktop equivalent, and nothing in the fixture reads it. Power-of-two inputs of both signs keep every reciprocal exact. Zero and negative zero give signed infinities, and clamp turns +inf into 1, through e32 and e64 encodings with absolute value and negation. Replacing the reciprocal with a square root must produce a pixel mismatch.

The `v_cvt_off_f32_i4` fixture converts bits 3:0 of a source word, read as a signed nibble, to binary32 in units of 1/16, from -0.5 for 0x8 to 0.4375 for 0x7. The source is read as bits through `asuint`, so float modifiers do not apply and the translator does not accept them. Four draws present all 16 nibbles under the same upper bits, through e32 and e64 encodings, and the last lane uses clamp. Reading the nibble as unsigned must produce a pixel mismatch.

The `v_mad_legacy_f32` fixture lowers the legacy multiply-add as the existing legacy multiply followed by an unfused add, so a zero factor gives zero even when the other factor is infinite or NaN. The constant sets pair a zero with +inf and with NaN, and also use finite values. A finite cancellation case uses uncompressed binary32 export and rejects a fused CPU reference. The draw tests measure the generated DXIL on desktop D3D12. The e64 negation, absolute-value and clamp modifiers are included. Replacing the instruction with `v_fma_f32` must produce a pixel mismatch, because 0 * inf is NaN there.

The translator accepts the gfx10 scheduling hints `s_clause`, `s_setprio` and `s_sleep`, which affect timing only, and lowers each exactly like `s_nop 0` at the same position. Each operand must fit its RDNA 2 field: 6 bits of clause length minus one, a 2-bit wave priority and a 7-bit sleep duration. A larger value is rejected. `s_code_end` stays rejected: the native program ends at `s_endpgm`, so padding after it never reaches the translator. Any instruction after `s_endpgm` and any hint between the color export and `s_endpgm` is rejected. The hinted program is part of the LLVM assemble and disassemble round trip.

`s_setreg_imm32_b32` writes hardware state, so it is accepted only where the effect on the lowered program is known. Two targets are accepted:

- A whole-register write to `HW_REG_FLAT_SCR_LO` or `HW_REG_FLAT_SCR_HI`, with any 32-bit immediate. These hold the scratch base address. Only `scratch_*` and `flat_*` memory instructions and `s_getreg_b32` can observe it, none of them is lowered, and the fixture checks that each stays rejected after the write.
- `hwreg(HW_REG_MODE, 2, 2)` with 0 or 3. LLVM's `SIDefines.h` names bits 3:2 of MODE the rounding mode of binary16 and binary64 results; 0 is round to nearest even and 3 is round toward zero. The lowerer tracks the value. Shader entry is taken as nearest even, which the existing binary16 lowerings already assume. Under round toward zero every binary16 or binary64 form is rejected by name except `v_cvt_f32_f16`, which is exact, and `v_cvt_pkrtz_f16_f32`, which always truncates. A truncating `v_cvt_f16_f32` is not lowered.

Every other register, field, width or value is rejected, including the directed rounding modes 1 and 2, the binary32 rounding field, the denormal fields (issue #312) and a partial scratch-base write. `s_setreg_b32` and `s_getreg_b32` stay unsupported. The fixture opens with both scratch-base writes, widens under round toward zero, restores nearest even for two narrowings whose truncated results differ, and selects round toward zero again before the pack. It must translate exactly like the same program with `s_nop 0` in place of each write, is part of the assemble and disassemble round trip, and is drawn on both backends; a truncated reference in either narrowing channel must report a `pixel_mismatch`. The translation controls require that dropping the restore, or writing round toward zero before a narrowing or a packed binary16 operation, is rejected.

In the local archive used for the fixed-profile rescan, linear-order counts over the 12,101 PS records (825 not decoded by the count) gave 2,011 records with the scratch-base pair, always at the second and third instruction and never with a `scratch_*` or `flat_*` instruction in the same program; 4,187 whose first MODE write is 3, each before 16-bit interpolation (`v_interp_p1ll_f16`, `v_interp_p2_f16`) and none with a rounding binary16 operation while 3 is selected; and 996 with a rounding binary16 operation after a write of 0. No other register, field or value occurred in any stage. These are counts from one local input, not a statement about other builds.
The same binary and draw checks cover two SOPK forms, `s_addk_i32` and `s_mulk_i32`, on 32-bit scalar values. LLVM prints a negative `simm16` as an unsigned 16-bit word such as `0xfffe`; the lowerer sign-extends it to 32 bits before adding or multiplying modulo 2**32. Each SOPK instruction has one SGPR operand: the destination is also the source. It can hold an inline scalar bit pattern or one constant word from the tracked descriptor, and the result is read by a vector move through `asfloat`. The fixture copies two constant words into scratch SGPRs, multiplies the first by 3, adds -2 to the second and exports both bit patterns. Out-of-range immediates, descriptor-register sources, vector destinations and writes that overlap an outstanding constant load are rejected. `s_addk_i32` writes SCC with signed 32-bit overflow, which the lowerer tracks so a following `s_cmov_b64` can consume it; `s_mulk_i32` leaves SCC unchanged. Changing the multiply immediate from 3 to 5 must report a `pixel_mismatch`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`v_sin_f32` and `v_cos_f32` take their operand in revolutions and return sin(2πx) and cos(2πx). LLVM 15.0.7 selects them the same way, as the trigonometric instruction applied to x · 1/(2π), and applies a `fract` to the argument only on Southern, Sea and Volcanic Islands (`FeatureTrigReducedRange`), not on GFX9 or GFX10. The lowerer first removes the nearest integer number of turns, which is exact in binary32, then multiplies by 2π and calls HLSL `sin` or `cos`. That keeps the radian argument within [-π, π], where the D3D11.3 functional specification bounds the `sincos` error to 0.0008 absolute in [-100π, 100π]. Inputs of at most half a turn are passed unchanged, so a negative zero keeps its sign. Because the specification allows that error, the fixture quantizes each result to steps of 1/32 with exact operations (multiply by 32, add 0.5, floor, multiply by 1/32), and every reference value lies at least 0.1 step from a step boundary. Four constant sets cover all four quadrants and an input of millions of turns in each set. The program uses both encodings of each instruction, e64 negation and absolute value, and clamp. It is compared bitwise with a CPU reference that reduces each input in double precision. Swapping sine and cosine, and dropping the 2π scale, must each produce a pixel mismatch for all four sets. A third variant drops the reduction and multiplies the unreduced input by 2π. Its mismatch count is printed as `trig_unreduced_mismatches` rather than required. On WARP 10.0.26100.9818 and an RX 9070 XT (driver 32.0.32015.7029), the reduced program matched all four sets, and so did the unreduced variant (`0/4`). In the DXIL that DXC 1.8 emits, the multiply by 2π carries no `dx.precise` flag. One explanation is that both drivers fold it into their own scaling to revolutions, but the drivers were not inspected. The reduction therefore makes no measured difference on these two adapters. It is kept because it is exact and does not depend on that folding. The instruction's own precision on a gfx1013 GPU, infinities, NaN payloads and the sign of a zero result are not measured. [LLVM 15.0.7 trigonometric lowering](https://github.com/llvm/llvm-project/blob/llvmorg-15.0.7/llvm/lib/Target/AMDGPU/SIISelLowering.cpp#L9262) · [D3D11.3 sincos](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm)

`v_cvt_f32_f16` widens the binary16 in the low 16 bits of S0. The lowering builds the binary32 encoding from those bits, so a subnormal half stays exact as m × 2^-24 whatever the desktop denormal mode, and applies `abs` and `neg` modifiers after widening. The draw converts 1.0 with junk in the high half, -|-5.0|, the largest finite half 65504 and the smallest subnormal half 2^-24, which a multiply by the literal 2^24 turns into 1.0. References that read the high half, drop the negation, use a wrong exponent or expect the subnormal flushed must report a mismatch. NaN and infinite halves are not drawn; whether Scarlett quiets a signaling NaN when widening is not measured, and the lowering keeps the payload. SDWA selections of the high half are refused.

Signed and unsigned 32-bit integer minimum, maximum and median instructions (`v_min_{i32,u32}` and `v_max_{i32,u32}` in e32 and e64 encodings, plus `v_min3_{i32,u32}`, `v_max3_{i32,u32}` and `v_med3_{i32,u32}`) reinterpret operands with HLSL `asint`/`asuint`, store the winning 32-bit pattern via `asfloat`, and reject float source modifiers, `clamp` and writes to pending sample destinations. Binary and draw fixtures cover all 14 forms using exact binary16 float bit patterns whose signed, unsigned and floating-point orderings differ (`-2.0`, `-1.0`, `1.0` and `0.5`, `-0.5`, `4.0`), including tie inputs for `med3`. Replacing `v_min_i32` with `v_min_u32` or `v_med3_i32` with `v_med3_u32` must report a pixel mismatch.
`v_cvt_f16_f32` narrows S0 to binary16, rounding to nearest even. The lowering builds the encoding in integer arithmetic, so desktop conversion and denormal handling cannot change it. Overflow, including the 65520 tie, gives infinity. A binary16 subnormal result is kept, and a binary32 subnormal input gives a signed zero. On GFX10, a 16-bit result replaces only the low half of the destination and keeps the high half (LLVM `GCNSubtarget::zeroesHigh16BitsOfDest`). The lowering merges into the destination's tracked value, or into zero when it has none. One draw reads the whole word by narrowing into a register that holds 1.0, then splitting `(v - 1) × 2^23` into the two bytes of the encoding. It also widens `-|S0|` and a scaled subnormal back to binary32. Four constant sets cover:

- ties to even at 1 + 2^-11, 1 + 3 × 2^-11, 1.5 × 2^-24 and 2^-25;
- the largest value below overflow and the overflow tie;
- the carry from the largest subnormal into 2^-14;
- negative subnormals, -0 and a binary32 subnormal input.

Wrong references must each report a mismatch: round-half-up, truncation, early or saturating overflow, a missing carry, and a lost sign or negation. NaN inputs are not drawn; the lowering sets the quiet bit and keeps the top payload bits. Whether Scarlett titles run with binary16 denormals enabled, as the lowering assumes, is not measured. `clamp` and SDWA forms are refused.

Integer vector arithmetic covers 9 RDNA2 VOP2 instructions: logical right shift (`v_lshrrev_b32`), arithmetic right shift (`v_ashrrev_i32`), logical left shift (`v_lshlrev_b32`), bitwise operations (`v_and_b32`, `v_or_b32`, `v_xor_b32`), and 32-bit addition/subtraction without carry (`v_add_nc_u32`, `v_sub_nc_u32`, `v_subrev_nc_u32`; the VOP3 spellings are listed in the next paragraph). Shift operations preserve RDNA reverse operand ordering, shifting `src1` by `src0 & 31u`. `v_ashrrev_i32` casts `src1` with `asint` to perform signed arithmetic right shifting. Arithmetic addition and subtraction (`v_sub_nc_u32` for `src0 - src1` and `v_subrev_nc_u32` for `src1 - src0`) wrap modulo 32 bits without generating carry bits. All results preserve their 32-bit integer bit pattern in vector registers through `asfloat`. Float modifiers (`-`, `abs`) and uninitialized vector registers are rejected.

The remaining 32-bit no-carry forms lower as well, building on the e32 `v_subrev_nc_u32` lowering and its reverse-subtract draw check from #570: `v_subrev_nc_u32_e64`, `v_add_nc_u32_e64`, `v_sub_nc_u32_e64`, and the VOP3-only `v_add_nc_i32` and `v_sub_nc_i32`, in the spelling LLVM 15 prints (no `_e64` suffix for the two `_i32` forms). All of them wrap modulo 2^32 and keep the bit pattern through `asfloat`; the `_i32` forms are the same bits as the `_u32` forms. Subrev computes `src1 - src0`, following the translator's existing float `subrev` convention and the LLVM 15 AMDGPU backend, where the gfx10 `V_SUBREV_NC_U32` is an encoding of the reversed `V_SUBREV_U32`; this order is not checked against the ISA document here. These spellings are refused: ` clamp` on any of them, the hand-written `v_add_nc_i32_e64` and `v_sub_nc_i32_e64` (LLVM 15 assembles them but prints the form unsuffixed, and `docs/coverage/gfx1013.tsv` has no `_e64` row), `abs(...)` and `-` on an e64 source, and any DPP control other than `quad_perm` and `dpp8`, which the existing DPP table already lowers for the three `_u32` spellings. The scanner has no DPP pattern and reports every DPP form as unsupported. The `v_add_nc_u32_e64` and `v_sub_nc_u32_e64` spellings, which the previous check refused, are now accepted. The scanner pattern for the family is one expression over the existing coarse `INTEGER_SOURCE` class, so it also accepts operands such as `v106` that the translator refuses. Float-written inline constants are taken as their binary32 bits by both, as for the #495 forms. Three programs draw the forms bit for bit through the `R32G32B32A32_UINT` readback target, with A and B taken from constants s44 and s45: group 1 is `v_add_nc_u32_e64`, `v_sub_nc_u32_e64`, `v_subrev_nc_u32_e32` and `v_subrev_nc_u32_e64`; group 2 is `v_add_nc_i32`, `v_sub_nc_i32` and the two e32 forms; group 3 uses inline constants and register-first operands (`B - 64`, `64 + B`, `A - 64`, `A - B`). Groups 1 and 2 run on five (A, B) rows chosen to wrap unsigned, overflow signed, differ between sub and subrev, borrow and wrap through the high bit, and group 3 runs on one, which gives 11 draws from 3 compiles. Every input and result is a normal binary32 or a signed zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Group 1 with the expected sub and subrev words swapped must report `pixel_mismatch`. The draws run as `pixel_aot_integer_nc_hardware` and `pixel_aot_integer_nc_warp` (`tests/pixel_aot_check.py draw-integer-nc`), and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 3080 (driver 32.0.16.2124) and on WARP (Microsoft Basic Render Driver 10.0.26100.9818), Windows 11 (10.0.26200.9974), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated order on desktop GPUs, not that the console does.

`v_xnor_b32` lowers in the VOP2 (e32) and VOP3 (e64) encodings as `~(src0 ^ src1)` on the register bits, through the same integer table and operand rules as `v_xor_b32`; before this it was accepted only as a quad-local DPP form. The operation is taken from the LLVM 15 AMDGPU backend, where `VOP2Instructions.td` selects `V_XNOR_B32` for `not (xor a, b)`; it is not checked against the ISA document here. A float modifier, ` clamp`, a wrong operand count and an unwritten register are refused; the SDWA spelling is described in the next paragraph. The integer e64 encodings that the translator already accepted but no fixture drew are now drawn with it: `v_and_b32_e64`, `v_or_b32_e64`, `v_xor_b32_e64`, `v_not_b32_e64`, `v_lshlrev_b32_e64`, `v_lshrrev_b32_e64`, `v_ashrrev_i32_e64` and `v_cvt_f32_ubyte2_e64`. Three programs export through the `R32G32B32A32_UINT` readback target and are compared bit for bit, with A, B and C taken from constants s44, s45 and s46. The shift group computes `B << (A & 31)`, `B >> (A & 31)` logical and arithmetic, and `~B`, with both sources in SGPRs, which the e32 encoding cannot express for the second source; the bitwise group computes and, or, xor and xnor of A and B the same way; the third group draws `v_xnor_b32_e32` and `v_cvt_f32_ubyte2_e64` from an SGPR, from a VGPR and with ` clamp`. Each group runs on two rows. The shift counts are 0x3f800024 and 0x40000023, so a count that is not reduced modulo 32 gives a different word, and B is negative in the first row, so the logical and arithmetic shifts differ. Every input and every expected result is a normal binary32 or zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Fourteen wrong references, each replacing one channel on the first row, must report `pixel_mismatch`: swapped shift operands, an unreduced shift count, arithmetic for logical shift and the reverse, a dropped not, or for and, and for or, xnor for xor and the reverse (in both encodings), byte 1 and byte 3 for byte 2, the wrong source register, and a dropped clamp. That is 20 draws from 3 compiles and 3 renderer launches. They run as their own tests, `pixel_aot_e64_integer_hardware` and `pixel_aot_e64_integer_warp` (`tests/pixel_aot_check.py draw-e64-integer`), with their own coverage record, and the LLVM 15 round trip covers the three programs; LLVM 15 prints all twelve instructions as written for gfx1013 and gfx1030. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated operations on desktop GPUs, not that the console does.

The SDWA forms of fifteen 32-bit integer operations lower as well: `v_and_b32`, `v_or_b32`, `v_xor_b32`, `v_xnor_b32`, `v_not_b32`, `v_lshlrev_b32`, `v_lshrrev_b32`, `v_ashrrev_i32`, `v_add_nc_u32`, `v_sub_nc_u32`, `v_subrev_nc_u32`, `v_min_i32`, `v_max_i32`, `v_min_u32` and `v_max_u32` (`_sdwa`). Each is the e32 operation applied to the selected source words, with the result placed by the SDWA move rules above (`sdwa_source` and `sdwa_dest`). Unlike the SDWA float arithmetic, byte and word sources are accepted, zero-extended or sign-extended under `sext()`, on either source, including the shift count: an integer operation has no denormal mode, so the reason given above for whole-dword float sources does not apply. The source for these semantics is the same as for the SDWA moves, the instruction equivalences in LLVM 15's `SIPeepholeSDWA.cpp`, read at tag `llvmorg-15.0.7`. That pass matches `v_lshrrev_b32` or `v_ashrrev_i32` by 16 or 24, `v_bfe_u32` or `v_bfe_i32` of a whole byte or word, and `v_and_b32` with 0xff or 0xffff, and rewrites whichever SDWA-capable instruction uses the extracted value so that the matching `src0_sel` or `src1_sel` (with the sext modifier for the signed matches) replaces the extract; it has no list of consumer opcodes and no check that the consumer is a move. It folds `v_lshlrev_b32` by 16 or 24 of a result into `dst_sel` with `UNUSED_PAD` on the defining instruction in the same way, and a `v_or_b32` of two padded SDWA results into `UNUSED_PRESERVE`. So every accepted shape is, by that pass, equal to an extract, the e32 operation and a placement, each of which the translator already lowers. This is an argument from the compiler, not a measurement on the console, and it is not checked against the ISA document here. Refused, each with a diagnostic: `UNUSED_SEXT` (the pass never produces it), `UNUSED_PRESERVE` onto a destination without a tracked numeric value, ` clamp`, an inline constant or literal source (LLVM 15 assembles one, but no equivalence above covers it), float modifiers, a spelling that does not print every field, and the SDWA spelling of any other integer operation. Four programs draw every form once through the `R32G32B32A32_UINT` readback target, bit for bit, with A, B, C and D taken from constants s44 to s47: the bitwise group selects a sign-extended `BYTE_1`, `WORD_1`, two bytes placed into `BYTE_3`, and a sign-extended `WORD_0`; the shift group takes its counts from `BYTE_1`, `BYTE_0` and `BYTE_2` of A (0x24, 0x25 and 0x23 in the first row, so a count not reduced modulo 32 differs), shifts a sign-extended `WORD_1` into `WORD_1`, and writes `~BYTE_2` into `WORD_0` of a register under `UNUSED_PRESERVE`; the arithmetic group adds `BYTE_3`, subtracts a sign-extended `BYTE_0`, reverse-subtracts `WORD_0`, and adds two bytes into `BYTE_1` under `UNUSED_PRESERVE`, where the carry out of the byte must be dropped; the min/max group compares sign-extended and zero-extended words and bytes and one whole-dword pair. Each group runs on two rows. Every input and every expected result is a normal binary32 or zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Forty wrong references, each replacing one channel, must report `pixel_mismatch`: a dropped or added sign extension on either source, the neighbouring byte or word, an ignored selection, swapped selections, a result placed in the wrong field, padding for `UNUSED_PRESERVE`, a carry leaving its byte, a shift count from the wrong byte or not reduced, arithmetic for logical shift, swapped subtraction order, signed for unsigned comparison, and a neighbouring operation for each form. That is 48 draws from 4 compiles and 4 renderer launches. A static control asserts that with whole-dword selections each form emits the statement of its e32 form. They run as their own tests, `pixel_aot_sdwa_integer_hardware` and `pixel_aot_sdwa_integer_warp` (`tests/pixel_aot_check.py draw-sdwa-integer`), with their own coverage record, and the LLVM 15 round trip covers the four programs; LLVM 15 prints all fifteen instructions as written for gfx1013 and gfx1030. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated words on desktop GPUs, not that the console does.

To supply a reference for another arithmetic program, run `offscreen_check.exe hardware --check-pixel-case shader.cso reference.case`. The case contains 256 bytes of constants followed by 64 expected RGBA colors in row-major little-endian binary32, one for each texel of the test texture. Generate the expected colors after round-toward-zero half export and widening back to float.

Every draw fixture compiles its translated shader under an explicit binary32 denormal mode: DXC runs with `-denorm preserve` at `ps_6_2`, the minimum shader model that accepts `-denorm`, matching CPU references built from Python floats, which keep binary32 subnormals. The Scarlett pipeline's `FLOAT_MODE` is unmeasured, so preserve is the harness's recorded assumption rather than a claim about the console. A dedicated control draws one program with `v_min_f32` and `v_max_f32` against 1.0 and -1.0, and a `v_mul_f32` by 1.0, under both `-denorm preserve` and `-denorm ftz` with mode-matched references: the least subnormal `0x00000001` survives min and max under preserve and exports zero under ftz, while the multiply by 1.0 folds to a move and must export identical raw bits in both modes; the preserve-compiled shader has to mismatch the ftz reference. Binary16 and binary64 denormal behavior is established by separate fixtures, not by this control. The curve fixture records its mode as `any`: an RTX 5060 Ti (driver 32.0.16.2124) refuses PSO creation for that one program under `-denorm preserve` (`E_OUTOFMEMORY`), while WARP and the RX 9060 XT accept it; the curve rows contain no binary32 subnormals, so the mode cannot change their exported bits, and the fixture asserts that flushing leaves every expected word unchanged.

The binary16 denormal mode is recorded as `preserve` and holds by construction rather than by a compile flag. DXIL has no binary16 denormal attribute: `-denorm` only sets `fp32-denorm-mode`. The packed f16 lowering widens each half from its encoding, computes in binary32 and narrows in integer arithmetic, and every binary16 subnormal, and every product or sum of two, is a binary32 normal, so a binary32 flush cannot reach it. The packed result is held as binary32 bits; packed f16 consumers and the export only reinterpret it, which the draw below checks with a word that is a binary32 subnormal. A CPU-side control checks that the lowered `v_pk_*_f16` body uses neither driver conversions (`f32tof16`, `f16tof32`) nor native half types. A draw runs `v_pk_add_f16` on two normal inputs whose sum is the subnormal `0x8001` (the required row: DXIL's `fadd` FTZ text requires such a result to flush), and as supplementary rows `v_pk_max_f16` on the least and largest binary16 subnormals, `v_pk_mul_f16` that makes the subnormal `0x0200` from normal inputs, and `v_pk_fma_f16` whose halves round to nearest even. It reads the packed words back through the integer target, because one of them (`0x80000200`) is a binary32 subnormal as a word. The same preserve reference must pass under both `-denorm preserve` and `-denorm ftz`, and a reference that flushes binary16 subnormals must fail. This shows that the harness's f16 results do not depend on the f32 flag. It says nothing about the Scarlett `FLOAT_MODE` f16 bits, which remain unmeasured, and the compressed-export path, which narrows through the driver's `f32tof16`, is not covered by this control.

A case file may carry a trailer after the 64 expected colors: one flags byte, whose bit 0 selects a `R32G32B32A32_UINT` readback target, followed by 64 per-texel mask bytes whose bit ch accepts a NaN of any sign or payload on that channel. A masked channel passes when the expected word and the exported word are both NaN, and the renderer logs a `nan_class` line with both raw words even on pass; masking a channel whose expected word is not NaN is a case-format error. Unmasked channels stay strict bitwise, comparison-predicate results stay strict, and integer moves, bit operations and bitcasts round-trip bit-for-bit through the integer readback. Any normalization observed on the float render target is specific to that path. The fixtures never claim a payload, sign or quieting rule for NaN results, only the class.

`v_fmaak_f32` computes S0 × S1 + K and `v_fmamk_f32` computes S0 × K + S1, where K is the 32-bit literal that LLVM prints in that position. Both use the `v_fma_f32` lowering; the translator refuses a register where the literal belongs. The draw computes each instruction twice with inputs 1.5, 2.5, -3.0 and 0.75 and the literals 0.25, 3.0, -0.5 and 0.5. Every product and sum is exact in binary32, so the reference does not depend on fusion. A reference that swaps the literal with S1 must report a mismatch in each channel.

`v_madak_f32` computes S0 × S1 + K and `v_madmk_f32` computes S0 × K + S1 with the same literal rules as the fma forms: K is the 32-bit literal LLVM prints in that position, a register there is refused, and so are the e64, DPP and SDWA spellings (LLVM 15 itself rejects e64 for these opcodes). They are not fused: both use the `v_mad_f32` helper, which rounds the product to binary32 before the add and flushes binary32 denormals. The draw checks 4097 × 4097 − 16785408 through both instructions, which is 0 unfused and 1.0 fused (exact rational arithmetic gives 1.0), and 2^-127 × 2^126 + 0.5 through both, which is 0.5 with the denormal input flushed and 1.0 without. A reference that expects the fused or the denormal-preserving result in any one channel must report a mismatch, and a dev-time mutation (not part of CI) that lowered the two instructions through `fma` made the draw report `pixel_mismatch`. The draw matched on an RTX 3080 (driver 32.0.16.2124) and on WARP under Windows 11 (10.0.26200.9974). Whether Scarlett flushes a denormal product or sum in these forms, as the helper does, is not measured.

Several VOP3 (e64) encodings were accepted by the translator but drawn by no fixture. One draw now covers `v_floor_f32_e64` of `-|x|`, `v_fract_f32_e64` of `-x`, `v_max_f32_e64` and `v_min_f32_e64` with negation and absolute value on SGPR operands, and another covers `v_fmac_f32_e64` with modified sources. All 16 `v_cmp_*_f32_e64` predicates are also drawn, writing an SGPR mask from negated operands. Negating both operands swaps less and greater and keeps equality and unordered, so the five predicate inputs (including NaN on either side) give a different expected value wherever a dropped negation would change the result. Each such reference must report a mismatch, as must a wrong result in each arithmetic channel. The approximate `v_rcp`, `v_rsq` and `v_sqrt` e64 forms are not drawn by this test, and `v_exp` and `v_log` are drawn by the one described in the next paragraph. These draws run as their own tests, `pixel_aot_e64_hardware` and `pixel_aot_e64_warp` (`tests/pixel_aot_check.py draw-e64`), apart from `pixel_aot_hardware` and `pixel_aot_warp`, so each stays within the 60 s test budget; the controls run once, in the draw and binary modes, and the hardware test reports a skip when the renderer returns 77.

`v_exp_f32_e64`, `v_log_f32_e64` and `v_mac_f32_e64` were also accepted with no fixture drawing them; the translator is unchanged and one program now draws all three. D3D allows `exp2` and `log2` a last-place error, so a raw result has no single binary32 reference. Each input therefore makes the true result a small integer, and the program adds 0.5 and takes the floor, which returns that integer for any result within 0.5 of it: `v_exp_f32_e64` of `-A` and `v_log_f32_e64` of `|B|` are drawn for (A, B) = (-3, -32) and (-4, -8), giving 8 and 5, then 16 and 3. This checks the operation and its source modifiers, which are what e64 adds over e32, and not last-place accuracy. `v_mac_f32_e64` is exact and is compared directly: one channel accumulates `-A * |B|` onto C (96.25 and 32.5), and one repeats the 4097 * 4097 case of the `v_mad_f32` fixture with both sources negated, which is 0 through the unfused helper and 1.0 if fused. Eight wrong references on the first row, each replacing one channel, must report `pixel_mismatch`: a dropped negation on exp, the natural exponential, log for exp, the natural logarithm, exp for log, a dropped negation on mac, a dropped accumulator and a fused multiply-add. That is 10 draws from one compile and one renderer launch. They run as their own tests, `pixel_aot_e64_approx_hardware` and `pixel_aot_e64_approx_warp` (`tests/pixel_aot_check.py draw-e64-approx`), with their own coverage record, and the LLVM 15 round trip covers the program for gfx1013; `v_mac_f32` is not in the gfx1030 list. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. Not shown: last-place accuracy of exp and log on any adapter, inputs outside these rows (zero, negative arguments to log, infinities, NaN), and what the console computes.

Three VOP3 (e64) spellings were unrecognised: `v_interp_p1_f32_e64`, `v_interp_p2_f32_e64` and `v_nop_e64`. LLVM defines each as a second encoding of an instruction the translator already lowers, and LLVM 15 assembles `v_interp_p1_f32_e64 v8, v0, attr0.x` to the two-word VOP3 form (`D6000008 00020000`) and prints it back with the suffix. The translator now accepts the interpolation pair in that spelling only in the operand shape of the e32 form, a plain VGPR source and `attr0`, under the same pairing and mask rules, and emits the same shader as for e32; the two encodings may be mixed within a pair. What the VOP3 encoding adds is not lowered: a negated or absolute source, `clamp`, `mul:2` and `div:2`, an SGPR source and another attribute are each refused, because nothing here establishes how they apply to an interpolation. `v_nop_e64` is accepted wherever `v_nop` is (entry mask, before the export, no operand). LLVM 15 prints both encodings of the no-op as `v_nop`, so a decoded binary already reached that branch; the new spelling is what an assembly fixture writes to select the VOP3 encoding (`D5800000 00000000`), and the round trip assembles it and lowers the decoded text to the same shader. Three programs are drawn, each with one `v_nop_e64`: the sampled fixture with both whole-quad pairs in e64, the channel-swapped one with e32 for p1 and e64 for p2, and the entry-mask attribute fixture with all eight interpolations in e64. The sampled ones are compared with the renderer's texture reference in their own channel order and must report `pixel_mismatch` against the other order; the attribute one is compared per pixel with the CPU reference (1 - v, 0.25 + 0.5u, u, v) over 32 by 32 pixels, and a zeroed channel at one interior pixel must report a mismatch at that pixel, once for each of the four channels. That is 3 matching draws and 6 mismatching ones from three compiles. They run as their own tests, `pixel_aot_e64_spellings_hardware` and `pixel_aot_e64_spellings_warp` (`tests/pixel_aot_check.py draw-e64-spellings`), with their own coverage record. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. That the VOP3 encoding of the interpolation pair computes what the VINTRP encoding does is taken from LLVM's instruction definitions, not measured on a console; the draws show only that the translated shader is the e32 one.

The e64 draws use `offscreen_check <backend> --check-pixel-batch <shader.cso> <case> [<case> ...]`. Each process creates one D3D12 device for a shader and checks the case files in argument order. Each draw retains its own resources and readback. The runner prints a zero-based case index and one of `pass`, `mismatch` or `error`. It exits with 1 if any case fails, including an intentional mismatch, and 77 if hardware is unavailable. The Python fixture checks both the ordered statuses and the process exit code. Each arithmetic batch ends with a matching reference after its negative controls.

`v_fract_f32` returns x - floor(x) clamped to `0x3f7fffff`, so its result stays below 1.0. The lowering uses an explicit clamp, not a bare D3D `frac`, which has no clamp and differs between adapters. For an input of -2^-30 the unclamped difference rounds to 1.0. An RTX 4070 Ti (driver 32.0.16.1088) exported 1.0 for that input, while WARP 10.0.26100.9818 already exported the clamped value. The e64 lanes take the absolute value of -0.25 and the negated absolute value of 2.75 before computing the fraction. Each exports 0.25. Ignoring either source modifier would give 0.75 in that lane. An e32 lane checks -2^-23. A reference that expects 1.0 for the first input must report a mismatch. NaN still propagates: it fails the clamp comparison.

`v_mad_f32` and `v_mac_f32` round the product to binary32 before the add; they are not fused. LLVM 15's AMDGPU backend selects them for an unfused multiply-add only when binary32 denormals are flushed: `SITargetLowering::isFMADLegal` notes that they do not support denormals. Both go through a helper that flushes each operand, the product and the sum to signed zero and keeps the two roundings with `precise`; `v_mac_f32` accumulates into its destination. The draw checks 4097 × 4097 − 16785408 for both instructions, which is 0 unfused and 1.0 fused, and 2^-127 × 2^126 + 0.5, which is 0.5 with the denormal input flushed and 1.0 without. References that expect the fused or the denormal-preserving result must report a mismatch. At commit d311159 the draw matched on an RTX 4070 Ti (driver 32.0.16.1088) and on WARP 10.0.26100.9818, and all three wrong references were detected. Whether Scarlett also flushes a denormal product or sum, as the helper does, is not measured.

`v_frexp_mant_f32` (in e32 and e64 encodings) and `v_ldexp_f32` lower through explicit IEEE-754 binary32 bit helpers (`frexp_mant32` and `ldexp32`) so subnormals, signed zero, infinity, NaN, and large exponent shifts stay independent of desktop driver flush-to-zero modes. `v_ldexp_f32` accepts float source modifiers on `src0` and `clamp`, while decoding `src1` as a signed 32-bit integer and rejecting float modifiers on the exponent. Binary and draw fixtures check normal values, signed zero, source negation and absolute value, runtime scalar exponents, negative exponents, and output clamping; replacing `v_frexp_mant_f32` with `v_mov_b32` or inverting a `v_ldexp_f32` exponent sign must report a pixel mismatch.

Quad-local `DPP16` (`quad_perm` with `row_mask:0xf bank_mask:0xf`) and `DPP8` (`dpp8` repeating the same 2×2 quad permutation on lanes 0–3 and 4–7) lower across 23 32-bit integer, 24-bit multiply, bitwise, shift and bit-scan `VOP1`/`VOP2` forms (`add_nc_u32`, `sub_nc_u32`, `subrev_nc_u32`, `min`/`max` `i32`/`u32`, `mul`/`mul_hi` `i24`/`u24`, `and`/`or`/`xor`/`xnor`/`not_b32`, `lshlrev`/`lshrrev_b32`, `ashrrev_i32`, `bfrev_b32`, `ffbl_b32` and `ffbh_u32`/`ffbh_i32`) via SM 6.0 quad intrinsics, emitting 12-bit-split `dpp_mul_hi_{u,i}24` and MSB-indexed `dpp_ffbh_{u,i}32` helpers only when used. The `dpp_int32` fixture derives integer quad lane ID `{0, 1, 2, 3}` from `uv.x`/`uv.y`, tests all 23 forms across three constant sets (including negative/extreme `i24` products, zero/all-ones bit scans and shift counts `>= 32`), exports all four bytes of the 32-bit accumulator as exact `[0, 255]` integers, and checks five mutations (`AcrossX` vs `AcrossY`, cyclic `dpp8` rotation, `ashrrev` vs `lshrrev`, signed vs unsigned `mul_hi` and `ffbh`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Quad-local `DPP16` (`quad_perm` with `row_mask:0xf bank_mask:0xf`) and `DPP8` (`dpp8` where lanes 0–3 and 4–7 apply the same 2×2 quad permutation) lower across the 20 supported `VOP1`/`VOP2` pixel forms (`v_mov_b32_dpp`, `v_cvt_f32_f16_dpp`, `v_cndmask_b32_dpp`, the 9 unary `f32` ops and 8 binary/accumulate `f32` ops) by swizzling `src0` as `uint` bits into an unconditional local variable via SM 6.0 `QuadReadAcrossX`, `QuadReadAcrossY`, `QuadReadAcrossDiagonal` or uniform `QuadReadLaneAt` (selected by `WaveGetLaneIndex() & 3u`), so subnormal/f16 bit patterns stay intact, quad intrinsics never execute inside conditional expressions (`v_mul_legacy_f32_dpp`/`v_cndmask_b32_dpp`), and results work across wave sizes 4 through 64; cross-quad `DPP8`, `row_*`/`wave_*` controls, partial masks, non-VGPR operands and source modifiers on `DPP8`/move/select are rejected. The `dpp` fixture derives the per-quad lane ID `{0, 1, 2, 3}` from interpolated `uv.x`/`uv.y` (`v8`/`v9`) and exercises all 20 forms across two constant sets, with eleven mutations (`AcrossX` vs `AcrossY`, `AcrossY` vs `AcrossX`, cyclic `dpp8` rotation, lane broadcast, `DPP16` negation of each source, `floor` vs `ceil`, lane 3 of a generic `quad_perm`, `abs` on `v_max_f32_dpp`, `abs` on a negative half in `v_cvt_f32_f16_dpp`, and an integer instead of a fractional `v_exp_f32_dpp` input) that must each report a mismatch; helper-lane behavior at primitive edges and cross-quad DPP remain unverified. Like `curve`, the `dpp` fixture records its binary32 denormal mode as `any` because NVIDIA driver 32.0.16.2101 (RTX 5080) refuses PSO creation under `-denorm preserve` (`E_OUTOFMEMORY`) while WARP passes; its expected words contain no binary32 subnormals and the fixture asserts `flush_ftz(word) == word` for every expected word. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The plain `VOP1` spellings of `v_bfrev_b32`, `v_ffbl_b32`, `v_ffbh_u32` and `v_ffbh_i32` (`_e32` and `_e64`) lower with the semantics of the merged DPP path above: `reversebits`, `firstbitlow` (0xffffffff for zero) and the `dpp_ffbh_{u,i}32` helpers (emitted once, only when used). The fixtures also use `v_xnor_b32`, which lowers through the integer table as described above. The sources are `s44`..`s47`, so no input passes through a float VGPR; VGPR sources lower as well. Source modifiers, `clamp`, wrong operand counts and `v256` are rejected with a `ValueError`; SDWA forms, 64-bit and 16-bit variants stay out of scope. The `draw-plain-bitscan` fixtures compare two shaders (plain spellings, and mixed `_e32`/`_e64` with a literal) over nine cases that rotate the words `0`, `0xffffffff`, `0x80000000`, `1`, `0x7fffffff`, `0xffff`, `0xaaaaaaaa`, `0x12345678` and `0xfffffffe` through every source register against a Python bit-loop reference, and four mutations (`ffbh_i32` to `ffbh_u32`, `ffbl_b32` to `ffbh_u32`, `xnor_b32` to `xor_b32`, `bfrev_b32` to `not_b32`) must each report a mismatch. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_plain_bitscan_" -V`, or `python -B tests/pixel_aot_check.py draw-plain-bitscan <dxc> <offscreen_check> <hardware|warp>`; `tests/form_coverage_check.py` asserts these forms, and the `v_xnor_b32` forms the fixtures use, count as covered. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

24-bit vector integer multiplies (`v_mul_i32_i24`, `v_mul_u32_u24`, `v_mul_hi_i32_i24` and `v_mul_hi_u32_u24` in e32 and e64 encodings) lower through lazy 32-bit HLSL helpers (`mul24_u32`, `mul24_i32`, `mul24_hi_u32` and `mul24_hi_i32`) that mask or sign-extend bits `[23:0]` and compute the 48-bit high product shifted right by 32 without requiring 64-bit integer shader ops. Float source modifiers, `clamp`, SDWA forms, out-of-range registers and writes to pending sample destinations are rejected. The binary and batched draw fixture covers all 8 forms across five constant sets with nonzero upper bytes (`bits [31:24]`), exact `-2^23 * 512 = -2^32` low-zero negative carry, `-2^23 * -2^23`, `0xffffff * 0xffffff`, zero with upper-byte garbage and mixed bit patterns, folding all eight 32-bit results into a 32-bit checksum unpacked through `v_cvt_f32_ubyte0`..`3`, and checks both wrong-mode references (unmasked 32-bit operands, signed/unsigned swaps and missing low-zero carry) and a mutated shader replacing `v_mul_hi_i32_i24_e32` with `v_mul_hi_u32_u24_e32`.

Packed binary16 `VOP3P` arithmetic (`v_pk_add_f16`, `v_pk_mul_f16`, `v_pk_min_f16`, `v_pk_max_f16` and `v_pk_fma_f16`) widens each selected 16-bit lane with `op_sel`, `op_sel_hi`, `neg_lo` and `neg_hi`, applies IEEE `minNum`/`maxNum` or arithmetic in binary32, optionally clamps to `[0, 1]`, and repacks both lanes with an explicit round-to-nearest-even helper (`pack16_rne`) so results do not depend on `f32tof16` rounding differences across desktop adapters. Inline binary16 float constants occupy bits `[15:0]` with bits `[31:16]` zeroed, matching `gfx1013` hardware, while 32-bit hex literals and integer inline constants supply both halves. The `vop3p` fixture draws three constant sets covering lane selection, negation, inline high-half zero extension, `minNum` against a quiet NaN literal (`0x40007e00`), `[0, 1]` clamp and exact binary16 halfway ties, with five mutations (`neg_lo`, `neg_hi`, `min` vs `max`, inline high-half zero extension and `clamp`) that must each report a mismatch; packed integer `VOP3P`, `v_fma_mix_*` and dot-product forms stay rejected. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Mixed-precision `VOP3P` FMA (`v_fma_mix_f32`, `v_fma_mixlo_f16` and `v_fma_mixhi_f16`) interprets each source as binary32 (`op_sel == 0`) or binary16 (`op_sel == 1`, low or high half via `op_sel_hi`, widened through `widen16`), applies `abs` and `neg` after widening, evaluates the multiply-add with optional `[0, 1]` clamp, and for `v_fma_mixlo_f16`/`v_fma_mixhi_f16` packs the destination half with `pack16_rne` while preserving the untouched 16 bits of `vdst` (rejecting an uninitialized destination). The `fma_mix` fixture draws three constant sets covering mixed f16/f32 inputs, `|.|`/`-`, `op_sel_hi`, preserved high/low destination halves, `[0, 1]` clamp, and binary16 halfway RNE ties, with five mutations (`abs`, `neg`, `op_sel_hi`, `mixhi` preservation and `clamp`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`VOP3P` dot products (`v_dot2_f32_f16`, `v_dot2_i32_i16`, `v_dot2_u32_u16`, `v_dot4_i32_i8`, `v_dot4_u32_u8`, `v_dot8_i32_i4` and `v_dot8_u32_u4`) compute the packed lane products plus the 32-bit accumulator. `v_dot2_f32_f16` widens its `f16` halves through `widen16` with `op_sel`, `op_sel_hi`, `neg_lo`, `neg_hi` and optional `[0, 1]` `clamp`, while the six integer forms accumulate in emulated 64-bit unsigned pairs (`uint2`) so wrapping and saturating `clamp` (`INT32_MIN`/`INT32_MAX`/`UINT32_MAX`) stay exact on SM 6.0 without requiring 64-bit integer shader support. The `dot` fixture assembles on `gfx1030` (since LLVM's `gfx1013` subtarget omits dot-product instructions) and draws four constant sets covering mixed signs, `INT32_MAX`/`INT32_MIN`/`UINT32_MAX` saturation and wrapping, with five mutations (`neg_lo`, signed vs unsigned `dot2`/`dot8`, and signed/unsigned `clamp`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Packed 16-bit integer `VOP3P` arithmetic and shifts (`v_pk_add_i16`, `v_pk_add_u16`, `v_pk_sub_i16`, `v_pk_sub_u16`, `v_pk_min_i16`, `v_pk_min_u16`, `v_pk_max_i16`, `v_pk_max_u16`, `v_pk_mul_lo_u16`, `v_pk_mad_i16`, `v_pk_mad_u16`, `v_pk_lshlrev_b16`, `v_pk_lshrrev_b16` and `v_pk_ashrrev_i16`) select each half-word lane through `op_sel` and `op_sel_hi`, mask shift counts to 4 bits (`& 15`), and saturate `clamp` on the six clamping add/sub/mad forms to `[-32768, 32767]` or `[0, 65535]` (rejecting `clamp` on min/max/mul/shift and rejecting `neg_lo`/`neg_hi` or float inline constants). The `pk_int16` fixture draws three constant sets covering all 14 forms, shift counts `>= 16`, inline `-1`, signed/unsigned clamping boundaries and cross-lane swaps, exporting all four bytes of the packed accumulator as exact `[0, 255]` integers with five mutations (`ashrrev` sign fill, signed vs unsigned min, `sub_u16` clamp, `op_sel` swap and `mad_i16` clamp) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`s_buffer_load_dword` reads one word from the tracked four-SGPR constant descriptor. Supported offsets are dword-aligned immediates within the 256-byte desktop binding, with `null` denoting offset zero. The lowerer requires the descriptor to be ready and tracks the destination until `s_waitcnt lgkmcnt(0)`. The synthetic binary and draw checks cover offsets 0, 8 and 252. Two sets of constant words matched all four channels of 4,096 pixels per draw on WARP and RTX 5080. Reading the neighboring word changes the first pixel on both adapters. [AMD RDNA 2 scalar memory rules](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The gfx10 SOPK wait forms `s_waitcnt_vmcnt null, K`, `s_waitcnt_lgkmcnt null, K` and `s_waitcnt_vscnt null, K` are accepted with `null` as the only SGPR operand, because `null` reads as 0 and the count is then the immediate `K` (decimal or hex, 0 to 63, the gfx10 six-bit field). `s_waitcnt_vmcnt null, K` and `s_waitcnt_lgkmcnt null, K` act exactly like the `vmcnt(K)` and `lgkmcnt(K)` tokens of `s_waitcnt`. `s_waitcnt_vscnt null, K` is a no-op, because the translator lowers no vector memory stores and nothing is outstanding on the store counter. An SGPR operand (its runtime value would add to or replace the count and is not modelled), a count above 63 and a missing operand are refused with a `ValueError`. Like the other wait forms they are accepted only before the color export, and `tools/scan_unsupported_shaders.py` marks the `null` forms with a count of 0 to 63 as supported. The controls compare each form with its `s_waitcnt` token equivalent, including a partial `vmcnt` with two outstanding samples, and `tests/pixel_aot_check.py binary` round-trips one sampled fixture that contains all three forms through LLVM 15 (gfx1013) and the disassembler. No GPU draw covers them; the identical-HLSL controls are the evidence. The semantics follow the LLVM 15 gfx1013 encodings (`s_waitcnt_vmcnt null, 0x0` is `00 00 7d bc`, `s_waitcnt_lgkmcnt null, 0x0` is `00 00 7d bd`, `s_waitcnt_vscnt null, 0x0` is `00 00 fd bb`); they were not measured on hardware.

`s_buffer_load_dwordx2` reads two consecutive words under the same descriptor, offset and wait rules. The destination is an even-aligned SGPR pair, the form LLVM prints, and both words must lie inside the 256-byte binding, so the last accepted offset is 248. The offset needs dword alignment only: the synthetic binary and draw checks read the pairs at offsets 12, 20 and 248, and the first pair spans two `float4` rows of the desktop buffer. Each of the six words feeds its own exact binary32 step, and two sets of constant words matched all four channels of 4,096 pixels per draw on WARP 10.0.19041, WARP 1.0.22 and RTX 3060 (driver 32.0.15.9636). Changing the second word of any pair in the bound buffer, or reading the middle pair one word early, changes the first pixel on each adapter. Register offsets and the eight-word and sixteen-word buffer loads remain rejected.

A pixel program may read more than one constant buffer. `--extra-constant SGPR OFFSET`, the `extra_constants` argument of `lower`, names one further 16-byte descriptor by the entry register that holds the low word of its table address and by its byte offset in that table. Repeated options map in order to `b1`, `b2` and onward, each a 256-byte `float4 paramsN[16]` buffer under the same read rules as `b0`. The register may be the texture table or another entry register. A table register is accepted only as the low address word of an `s_load_dwordx4` whose high word is a tracked zero. A descriptor the contract does not name, a nonzero or unknown high word, arithmetic on a table register, a texture descriptor load from a constant table and a four-register span that mixes words of two descriptors are rejected, and an entry register outside the contract stays unbound. These are explicit desktop bindings supplied by the caller. The translator does not infer which descriptors a title uses as constant buffers, how large they are, or which desktop register a native slot had in the original source.

The `three-buffers` fixture loads the `b0` descriptor and a second descriptor from the texture table and a third from the table at entry s9, reads word 2 of each buffer and exports one buffer per channel. Every other word of the three buffers holds a sentinel. On Windows 10 build 19045 with the D3D12 debug layer enabled, two constant sets matched all four channels of 4,096 pixels per draw on WARP 10.0.19041.5794 and RTX 3060 (driver 32.0.15.9636). A reference with `b1` and `b2` exchanged, a reference that reads `b0` in place of `b1`, and a translation with the two contract entries exchanged each report a mismatch on both adapters. The pixel-case trailer carries the extra buffers under bit 3 of the flags byte: 512 bytes follow the flags byte and any viewport, 256 for `b1` and then 256 for `b2`, and a shorter file is a case-format error. The renderer's root signature gains root CBVs `b1` and `b2`; they are set only for a case that supplies them.

Before a single sample, whole-quad addition, FMA and legacy multiplication can compute affine coordinates from paired interpolation and ready uniform constants. Each coordinate used for implicit LOD must have a value defined on helper lanes. An entry-only write results in a value without that proof, and multiplication of two varying operands, absolute value and saturation fall outside the affine subset. The source-built draw checks a partial triangle on an 8×8 target with two solid-color mips. WARP and RTX 5080 select mip 1 at every covered pixel and preserve clear color outside the triangle. Replacing helper coordinates with the quad's top-left coordinates changes the first mismatched pixel at (6, 0) to mip 0 on both adapters.

`v_cmp_class_f32` is true when the class of S0 is in the 10-bit mask S1, with the bits of LLVM's `SIInstrFlags::ClassFlags`: signaling NaN, quiet NaN, -inf, -normal, -denormal, -0, +0, +denormal, +normal and +inf. The lowering classifies the raw binary32 encoding, so desktop denormal handling does not change the class. The mask may be an inline constant, a literal or a register. The draw classifies +inf against the +inf bit, a quiet NaN against both NaN bits, -0 against the +0 bit and the smallest positive denormal against its bit, and selects 1.0 or 0 for each. A reference with any single result flipped must report a mismatch. Signaling NaN inputs are not drawn, and whether Scarlett classifies a denormal input without flushing it first is not measured.

```powershell
ctest --preset dev -R '^pixel_aot_' -V
```

For a compatible local record, substitute the archive, exact record name, two register numbers and four descriptor words below. Run the translator from the repository root in WSL, then compile and draw from a Windows development shell. The JSON printed by the translator identifies each input and output by SHA-256 for local reproduction. The renderer's `rgba` expectation checks a copy and `bgra` checks a red/blue swap.

```sh
python3 -B tools/pixel_aot.py "$archive" "$record" --llvm libLLVM-15.so.1 \
    --texture-sgpr "$texture_reg" --interp-sgpr "$interp_reg" \
    --sampler-words "$word0" "$word1" "$word2" "$word3" --output out/pixel-aot/translated.hlsl
```

When several records share the exact name, add `--container-sha256` with the SHA-256 of the full decoded DXBC container. Run the [shader extractor](#optional-compressed-record-inventory) with `--length-prefixed-names --format dxbc`, then hash the desired `.dxbc` file using `sha256sum` or `Get-FileHash -Algorithm SHA256`. Exactly one record must match both the name and the digest. Two containers can carry identical native instructions and different metadata, so their native program hashes can be identical.

```powershell
.\tools\dev.ps1
dxc -T ps_6_2 -denorm preserve -E main -Fo out/pixel-aot/translated.cso out/pixel-aot/translated.hlsl
.\out\build\dev\offscreen_check.exe hardware --check-pixel out/pixel-aot/translated.cso rgba
.\out\build\dev\offscreen_check.exe warp --check-pixel out/pixel-aot/translated.cso rgba
```

## Package integrity

For a local build with a multi-file v1 torrent and `layout*.xml`, run the offline verification with Python 3.9 or newer. Substitute your local directory name.

```powershell
python tools/verify_build.py "game-files/<build-directory>"
```

The script verifies SHA-1 pieces across file boundaries in torrent order and compares layout destinations separately with disk and torrent entries. The report is `out/integrity/<build-directory>.json`. Exit code 0 means the payload passed piece verification. `layout.absent_from_disk` lists missing layout entries separately.

## PE dependency baseline

The scanner uses x64 Windows, Python 3.11 or newer and pefile. Set up an isolated environment once.

```powershell
python -m venv out/pe-env
.\out\pe-env\Scripts\python.exe -m pip install pefile==2024.8.26
```

Capture a DLL-only baseline. When an EXE becomes available, write a separate report and compare it with the saved baseline.

```powershell
$pePython = '.\out\pe-env\Scripts\python.exe'
$buildDir = 'game-files/<build-directory>'
& $pePython tools/pe_inventory.py $buildDir --output out/dependencies/pre-exe.json
# Run the next command after adding the main executable.
& $pePython tools/pe_inventory.py $buildDir --baseline out/dependencies/pre-exe.json --output out/dependencies/with-exe.json
```

`summary` gives counts. `unresolved_edges` lists requested exports missing from both the package and the host. Each `imports` entry identifies the caller, import mechanism, provider and symbol. `#N` denotes an ordinal. The scanner reads import descriptors and lookup tables itself. pefile skips names outside letters, digits and `._?@$()<>` and drops a descriptor left empty, so those entries would otherwise vanish from the graph. Ordinal imports stay `#N` even where pefile's built-in table supplies a name. Each provider is listed under the file name the loader looks for: a descriptor name without an extension gets `.dll` appended, while a trailing dot suppresses the append and is dropped, as with `LoadLibrary`. Without this, an import of `intake_forward` would miss the package's `intake_forward.dll` and report `provider_not_found`; `bundled_export_forwarders` rewrites the caller's descriptor to both forms. [LoadLibraryW extension rule](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryw) · [Wine DLL name lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c) `images` includes architecture, SHA-256, export names, ordinals and forwarders. `diff` distinguishes new caller relationships from dependencies seen for the first time in the build.

The scanner reads System32 export tables and applies the host API-set v6 map at each hop, using the forwarding DLL as the importer at the next hop. Each import's `desktop_resolution.chain` records the requested module, selected provider, symbol and any advertised forwarder. Its final status identifies a terminal export, missing provider or export, malformed forwarder, or cycle. `desktop_export` retains the first-hop result for older report consumers. The comparison covers System32 DLLs on the measured machine. Package selection at runtime, parameter layouts and behavior require separate evidence. Export names are read as stored, because pefile stops at the first name outside its character set and would list every later named export, for the package and System32 alike, by ordinal only. An export directory, address, name or name-ordinal table, name or forwarder that does not lie in a section's file data, or a name or forwarder with no terminating NUL there, stops the scan with an error naming its RVA. pefile instead drops the name, forwarder or whole table with a warning. `static_exe_intake` covers each case on a patched copy of `intake_provider.dll`. [PE export address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#export-address-table) · [Wine API-set lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c)

Package DLLs can also forward exports, to another package DLL, to themselves, by ordinal or to System32. An edge whose provider is in the package gets a `package_resolution` chain. It uses the same statuses, adds `ambiguous_package_basename`, and marks each hop's `package` origin. Plain module names select a uniquely named package image before System32. API-set contracts always select System32 hosts. So do the names in the host's `\KnownDlls` object directory, plus `ntdll.dll`: the loader maps a known DLL from System32 before it searches the application directory, at the first hop and at every forwarder hop. The report lists these names under `known_dlls` and marks such a hop `known_dll`. `dependency_paths` does not treat a package file with a known DLL's name as loaded and reports the edge under its System32 status. An edge counts as resolved through the package only if its chain reaches a terminal export. `dependency_paths` adds each packaged forwarder target after its provider with the reason `forwarder` and reports a broken chain under the chain's terminal status. Earlier tools treated any matching export name as resolved. They omitted the target module and accepted forwarders to missing exports, missing modules or cycles. The `bundled_export_forwarders` test builds `intake_forward.dll` with seven placeholder exports, because linkers differ in how a .def file declares forwarders. It moves the export table of a temporary copy into a new section and turns the seven exports into forwarders: by name, by ordinal, a two-hop chain through itself, a missing target export, a self-cycle, an absent module and a by-name forwarder whose target module is written in uppercase. The uppercase target must resolve to the same provider hop as the lowercase ones. It then checks the inventory and path report for a caller that imports all seven. A second scan adds a copy of `intake_provider.dll` named `kernel32.dll` to the package. The caller's KERNEL32 imports must still follow the System32 chain, and the copy must stay unlinked in the path report. `conditional_import_paths` checks the same precedence with synthetic export tables, including a packaged forwarder whose next hop is a known DLL. [Dynamic-link library search order](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-search-order)

On Windows build 26200, the pre-executable inventory covered 16 package DLLs and 1,337 import edges. Of these, 171 edges (78 distinct requested module/symbol pairs) went through a single export forwarder. All 171 reached a terminal export. In total 1,329 edges resolved on the desktop. The eight remaining edges requested six distinct interfaces across `xmem.dll` and `d3d12_xs.dll`. The 16 package DLLs had zero forwarded exports of their own. The historical baseline was preserved for comparison.

## Heap and process memory

`src/era_heap.cpp` is a desktop handler. It splits the attribute word using SlimEra's Xbox One ERA layout: object type in bits 0–13, page size in bits 14–15, allocator ID in bits 16–23, log2 alignment in bits 24–28, memory type in bits 29–32 and reserved bits 33–63. Object type and allocator ID are carried as tags and do not affect placement, as in SlimEra's default heap.

The desktop handling is:

- **Heap (memory type 0):** served from the CRT heap at the requested alignment, minimum 16 bytes, and zero-filled. The page size does not affect these requests. SlimEra passes every heap request to `_aligned_malloc`, which commits the alignment padding as heap memory, so a 257-byte request with exponent 31 would commit more than 2 GiB. The desktop handler instead gives alignments above the page size their own reservation and commits only the pages that hold the payload; `VirtualAlloc` returns them zeroed.
- **Graphics (memory types 1–6):** committed as a separate `VirtualAlloc` region. `release_with_attributes` frees it with `VirtualFree`. The region starts on the desktop allocation granularity, which covers 4 KB and 64 KB pages. This is ordinary CPU-visible memory; no GPU mapping or caching behavior is modeled.
- **Rejected:** physical memory types 7–9, undefined types 10–15, the undefined page-size value 3, reserved bits, and graphics requests whose page size or alignment exceeds the allocation granularity all return a null pointer.

`release` frees heap allocations. It frees a page-backed block with `VirtualFree` when `VirtualQuery` reports it as the base of its own allocation; a CRT block never is, because `_aligned_malloc` stores its base pointer below the returned address. `release_with_attributes` takes the allocation's attribute word, as `XMemFree` callers do.

What is measured is the desktop behavior above. Under the ERA layout, the two recorded words give alignments that match their callers' own arithmetic: 32 bytes for Bink and 256 bytes for AMD. The following remain unconfirmed on Scarlett:

- that Scarlett uses the ERA layout at all;
- what allocator ID `0xe5` means;
- the GPU visibility and caching of memory type 5 and the other graphics types;
- the console's error conventions.

[SlimEra attributes](https://github.com/XWine1/SlimEra/blob/main/era.h#L105-L137) · [SlimEra default heap](https://github.com/XWine1/SlimEra/blob/main/kernelx.cpp#L600-L657) · [Issue #126](https://github.com/R0Y00/Xbox2Windows/issues/126)

| CTest case | Inputs and checks |
|---|---|
| `era_heap_allocation` | Allocate 257 bytes using six alignment exponents. Check alignment of at least 16 bytes and the requested alignment, initial zeroing, writes and release. Debug builds also compare CRT heap snapshots. |
| `era_heap_failure` | Each reserved bit, combined with a heap alignment and with the AMD graphics word, returns a null pointer. So do memory types 7–15, page-size value 3, graphics memory with 4 MB pages or a 128 KiB alignment, zero size and excessive size. |
| `era_heap_reuse` | For three alignment exponents and three sizes, fill a block with `0xa5` and release it. Then hold up to 32 new blocks of the same shape until the released address returns. Every new block must read as zero. The output reports `recycled_address`, the number of the nine cases in which the CRT returned the dirty address. glibc returned all nine in a Linux build with a `posix_memalign` stand-in. Windows 11 build 26200.9974 with MSVC 14.44.35207 returned the dirty address in all nine cases in both Debug and Release. Low-fragmentation heap randomization can lower it. |
| `era_heap_observed_words` | Desktop handling of the two recorded words. Under the ERA layout, the Bink word `0x05e50000` splits into heap memory, 4 KB pages, allocator field `0xe5` and 32-byte alignment. The AMD word `0xa8004000` splits into memory type 5, 64 KB pages, allocator field 0 and 256-byte alignment. Bink-style 97- and 193-byte blocks are 32-byte aligned and zeroed, and each wrapper recovers its base. AMD-rounded requests of 256 to 65,792 bytes start a committed read-write region on a 64 KiB boundary and are zeroed and writable. After release, `VirtualQuery` reports the region as free. Nonzero object-type and allocator tags leave heap placement unchanged. |
| `era_heap_large_alignment` | Exponents 13, 16, 17, 20 and 31 with sizes of 1 and 257 bytes, one page, one page plus one byte and three pages minus one byte. Each block must be aligned and zeroed. `VirtualQuery` must report the block as the base of its own private read/write allocation, with a committed region equal to the payload rounded up to whole pages. After release the address must be `MEM_FREE`. The output reports the 25 cases, the committed bytes and the summed alignments. With the previous `_aligned_malloc` path, the first case already fails because the block lies inside a CRT heap allocation. |
| `era_heap_graphics_types` | Each graphics memory type 1–6 with 4 KB and 64 KB pages and alignment exponents 0, 4, 8 and 16, at 1 byte and one allocation granularity plus one byte. Exponent 16 is the 64 KiB granularity boundary itself. Each block must start on the granularity, be zeroed and writable, and be reported by `VirtualQuery` as the base of its own committed private read/write allocation. After release with the same attributes the address must be `MEM_FREE`. For every type, 4 MB pages, exponent 17 and page-size value 3 return a null pointer. The output reports 96 served and 18 rejected cases at granularity 65,536 on Windows 11 build 26200. Changing the granularity comparison from `>` to `>=` fails exponent 16 with 4 KB pages, and narrowing the graphics range to types 1–5 fails type 6; `era_heap_observed_words` catches only the first change. These are desktop policy checks; the console semantics of the six graphics types remain unconfirmed. |
| `era_heap_concurrency` | Four threads run 100 rounds each. They hold simultaneous allocations, write distinct values, compare every byte and release the memory. |
| `era_heap_caller_layout` | Owned desktop storage covers all 32 base residues at eight payload sizes. Each Bink-style wrapper recovers its original base after a payload write and preserves the surrounding guard bytes. Ten AMD-style requests check 256-byte size rounding and the unused tail. These cases establish caller-side arithmetic. The Bink-style storage uses the recorded Bink word. |

`desktop_memory_query` supplies Windows observations for `XMemVirtualQuery` and `XMemGetWorkingSetStatistics`. The test reserves memory, commits and writes 1,024 pages, checks the read-only/writable boundary, then decommits. It verifies residency of a locked page and records working set, peak working set and private commit at three points. Values vary between runs. Scarlett structure layout, units and accounting scope remain unconfirmed. [Windows working sets](https://learn.microsoft.com/en-us/windows/win32/memory/working-set)

The same test creates writable and copy-on-write views of a two-page, pagefile-backed mapping. After a write through the copy view, `VirtualQueryEx` reports `MEM_MAPPED` for both pages. With both pages locked and resident, `QueryWorkingSetEx` clears `Shared` only for the written copy page. Writes through the other view remain visible on the untouched page. [Windows copy-on-write queries](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex#remarks)

`src/era_query.cpp` is a desktop handler for `XMemVirtualQuery`, shaped by the four BVH call sites. It writes the Win32 `MEMORY_BASIC_INFORMATION` to bytes `0x00`–`0x2F`; the callers read State at `+0x20` and Protect at `+0x24`, matching that layout. The byte at `+0x30` has no public definition: the Microsoft reference for `XMemVirtualQuery` and its result structure is restricted to the Xbox developer program.

The handler therefore fills that byte by a desktop policy, not by a confirmed Scarlett field:

- it writes `0x01` when the queried address lies in a region registered with `register_gpu_region`, and `0x00` otherwise;
- later bytes are left untouched;
- the policy applies only to queries of the current process;
- a buffer shorter than 48 bytes fails with `ERROR_BAD_LENGTH`, as `VirtualQueryEx` does.

With registration, the callers' predicates accept GPU-intended memory and reject everything else. What the console's tail byte means, and what sets it, remain open. The selector `0x20000001` and the output units of `XMemGetWorkingSetStatistics` have no public reference either; the desktop `xmem.dll` below reports that call as unsupported.

| CTest case | Inputs and checks |
|---|---|
| `era_query_fields` | Reserved, committed read/write, read-only and no-access pages, each queried with and without registration. Bytes `0x00`–`0x2F` match a plain `VirtualQueryEx` result byte for byte, and bytes after `+0x30` keep their sentinel. |
| `era_query_tail` | Negative control: before registration a committed read/write page fails the tail check. After registering two pages, the tail byte is set at the first, middle and last byte and clear on the next committed page and on a reserved page. The callers' predicates accept read/write and read-only, reject the source check on no-access, and reject both after decommit. A real handle to the process counts as the current process, and unregistering clears the flag. |
| `era_query_registry` | Null, empty and wrapping ranges are refused. Exact, front, back, containing and contained overlaps are refused, adjacent ranges are accepted, and unregistering needs the exact base. |
| `era_query_short` | A 47-byte buffer returns 0 with `ERROR_BAD_LENGTH` and is left untouched. A 48-byte buffer succeeds without writing the tail byte. A null output returns `ERROR_BAD_LENGTH` at 47 bytes and `ERROR_NOACCESS` at 48 or 64 bytes, matching desktop `VirtualQueryEx`. |

### Desktop xmem.dll

`src/xmem_shim.cpp` builds `xmem.dll`, which exports the four functions the package DLLs import by name. The call shapes come from the observed call sites, not from a published header:

| Export | Observed call shape | Desktop handling |
|---|---|---|
| `XMemAlloc` | Size and attribute word (Bink and AMD callers). | `era_heap::allocate`. Graphics memory types are also tracked by `era_title_gpu`, which registers them with `era_query` so they count as GPU-intended, and gives them desktop GPU addresses once a D3D12 device is attached (see below). |
| `XMemFree` | Allocation and the same attribute word. | Untracks graphics allocations, dropping any alias and the `era_query` registration, then `era_heap::release_with_attributes`. |
| `XMemVirtualQuery` | The four BVH callers pass the process in `rcx`, the address in `rdx` and a 64-byte buffer in `r8`; `r9` is not set, so there is no length argument. They test the 32-bit result in `eax`. | `era_query::query` on the 64-byte buffer; returns `TRUE` when it succeeds. |
| `XMemGetWorkingSetStatistics` | The seven AMD callers pass `0x20000001` in `ecx` and an output address in `rdx` and test the 32-bit result. On failure each uses zero instead of the 64-bit value at `+8`. | Returns `FALSE` with `ERROR_NOT_SUPPORTED` and leaves the output untouched. |

The registration of graphics memory and the unsupported working-set call are desktop policies, not confirmed Scarlett behavior.

A local scan of the inspected package's 16 DLLs together with this `xmem.dll` (`tools/pe_inventory.py`) resolved all six xmem import edges to it. The unresolved edges dropped from eight to two, both `d3d12_xs.dll` root-signature functions. The scan reads the images and runs no package code.

| CTest case | Inputs and checks |
|---|---|
| `xmem_shim_exports` | The four names resolve through `GetProcAddress`, and the test's named imports bind to the same module. |
| `xmem_shim_allocation` | The Bink word returns 32-byte-aligned, zeroed heap memory. The AMD word returns committed read/write memory on a 64 KiB boundary that `XMemFree` releases (`MEM_FREE`). Zero size, a reserved bit and a physical memory type return null, and freeing null is a no-op for both memory types. |
| `xmem_shim_query` | Graphics memory from `XMemAlloc` passes both BVH predicates and keeps the sentinel after `+0x30`, through the pseudo-handle and a real handle. After `XMemFree` the address reads as `MEM_FREE` with a clear tail. Heap memory fails the tail check, and an address outside user space returns `FALSE` without touching the buffer. |
| `xmem_shim_working_set` | Returns `FALSE` with `ERROR_NOT_SUPPORTED` and leaves the output untouched, so the callers' value is zero. |

### Desktop d3d12_xs.dll

`src/d3d12_xs_shim.cpp` builds `d3d12_xs.dll`, exporting the two root-signature functions that remained unresolved across the 16 package DLLs after `xmem.dll`:

| Export | Observed call shape | Desktop handling |
|---|---|---|
| `D3D12SerializeVersionedRootSignature` | `XboxBvhBuilder.dll` (`0xc2d0`/`0xc32f`) copies a 48-byte descriptor, sets console flag bits `0x03000000` at `+0x28`, and passes a null error blob. | Clears only the observed `0x03000000` bits in a local copy for v1.0/v1.1 descriptors before calling desktop `d3d12.dll`. Unobserved invalid flag bits stay untouched so desktop `d3d12.dll` still returns `E_INVALIDARG`. |
| `D3D12CreateRootSignatureDeserializer` | `amd_fidelityfx_upscaler_xbox.dll` (`0x4f88`) passes a pointer and length from an embedded container plus an interface IID and output address. | Forwards directly to desktop `d3d12.dll`. |

This covers only the two root-signature imports observed in the pre-executable DLL inventory. Any `d3d12_xs.dll` APIs imported by a future main executable remain separate work.

| CTest case | Inputs and checks |
|---|---|
| `d3d12_xs_shim_exports` | Both names resolve through `GetProcAddress` inside `d3d12_xs.dll`, and the test's named import accepts `0x03000000` while desktop `d3d12.dll` rejects it with `E_INVALIDARG`. |
| `d3d12_xs_shim_serialize` | Baseline and console-tagged (`0x01000000`, `0x02000000`, `0x03000000`) v1.1 and v1.0 descriptors serialize identically to desktop `d3d12.dll` without mutating caller input while preserving desktop flags. Unobserved flag bits (`0x04000000`, `0x07000000`, `0x08000000`) return `E_INVALIDARG`, and a null descriptor or unknown version fails with the same HRESULT as desktop `d3d12.dll`. |
| `d3d12_xs_shim_deserialize` | Round-trips a sanitized blob from `d3d12_xs.dll` and reads the embedded `RTS0` part of `pso_compute_embedded`; rejects `pso_compute_stripped` and empty/null buffers with `E_INVALIDARG`. |
| `d3d12_xs_shim_warp_root` | Serializes a v1.1 `SRV(t0),UAV(u0),RootConstants(16,b0)` descriptor tagged with `0x03000000` through `d3d12_xs.dll` (verified rejected by desktop `d3d12.dll`), creates a WARP `ID3D12RootSignature`, and builds a compute PSO with `pso_compute_stripped`. |

### Attribute words at xmem call sites

`tools/xmem_attribute_scan.py` reads a PE image without loading or running it. It reports the attribute word at each call or tail jump into `xmem.dll`'s `XMemAlloc` or `XMemFree`, from a normal or delay-load import. It finds sites reached through the IAT slot, through an import thunk, or through a register loaded from the slot.

Code is decoded one `.pdata` function at a time, and linearly between functions, so memory stays bounded for a large image.

From each site the scanner walks backwards over the function's control flow to every definition of RDX:
- **Jumps:** jumps inside the function are followed. A join reports each distinct definition, and a loop that does not change RDX adds nothing.
- **Calls:** a call on the path may change RDX under the Microsoft x64 convention, so it ends that path as unknown. Non-volatile registers are followed across calls.
- **Immediates:** an immediate, including `xor edx, edx`, is a constant. A 32-bit register copy keeps only the low 32 bits.
- **Data loads:** a load from RIP-relative data is a constant when the data is in a read-only section. In a writable section it is reported as data and is never a constant, because a write through a pointer can change the word before the call and such writes are not tracked. The report keeps the initial value and the RIP-relative writes found (`written_at`, possibly empty) as evidence only.
- **Function entry:** reaching the entry means the caller supplies the register still being tracked, including the register of a followed copy such as `mov rdx, r8`. Direct calls and jumps to the entry are resolved one level deep from that register; a 32-bit copy on the way truncates the caller's value. An exported function can also be called from outside the image, so its sites never count as complete.
- **Everything else:** partial-register writes, arithmetic, pointer loads, blocks reached only through jump tables, and chained unwind fragments are reported as computed or unknown, never as constants.

Constant words are decoded with the `era_heap` layout and classified with `era_heap::allocate`'s desktop rules for a non-zero size.

The report lists each site with its route and region, and the import thunks found. Its summary gives:
- every distinct word with its sites;
- the words for which `era_heap` returns null;
- the sites with a path that does not end in a constant.

`--fail-on-null` and `--fail-on-unresolved` exit with 1 in those cases.

| CTest case | Inputs and checks |
|---|---|
| `xmem_attribute_scan` | Two source-built MASM fixtures are linked against the desktop `xmem.dll` import library. `tests/cpu/xmem_attributes.asm` has 25 sites, one per form: IAT call with unwind data, linker thunk, read-only constant data, writable data with no RIP-relative writer, writable data written through an address held in a register before the RIP-relative load into EDX, written data, register copy, call through a register, exported and internal pass-throughs with a caller, caller-copy pass-throughs whose entry register is R8 (64-bit, and 32-bit with the caller's value truncated), 64-bit immediate with a reserved bit, `xor`, a computed word, direct and indirect jumps, if/else joins with equal and different words, a loop, a call that clobbers RDX, a non-volatile register across a call, a 32-bit copy of a 64-bit constant, and a partial-register write. Each site's route, source, word and classification must match, as must the summary and the exit codes of both flags. Both writable-data sites must be unresolved, so `--fail-on-unresolved` fails on them. `tests/cpu/xmem_delay.asm` is linked with `/DELAYLOAD:xmem.dll` and has one constant site through the delay-load IAT. Both flags must exit with 0 on it. The 13 decoding cases also run in the Linux Python job. |

Sixteen mutants of the scanner each fail this test. They cover call clobbering, join following, merging, read-only data, writers, truncation, export completeness, thunks, delay imports, indirect blocks, register following, `xor`, partial writes, caller resolution, register calls, and the null flag. After the writable-data change, two more mutants also fail it: treating writable data with no RIP-relative writer as a constant (the earlier behaviour, which reported the pointer-written site as constant `0xa8004000`), and dropping `written_at`. After the entry-register change, two more also fail it: resolving callers with the default RDX instead of the recorded entry register, and dropping the 32-bit truncation of the entry register.

A local read-only scan of the inspected package's 16 DLLs found `xmem` call sites in two of them. All five are constant on every path and resolve to the two words already pinned in `era_heap_observed_words`:
- the Bink DLL: two sites, both through linker import thunks, word `0x05e50000` (heap, 32-byte alignment);
- the AMD upscaler: three sites through the IAT slot, word `0xa8004000` (memory type 5, an allocation-granularity graphics allocation), loaded from a read-only section.

One full decoding pass over the largest DLL, 0.7 MB of code with 2,452 `.pdata` functions, read 342,768 instructions in 0.07 s.

```powershell
.\out\pe-env\Scripts\python.exe -B tools/xmem_attribute_scan.py "game-files/<build-directory>/<image>" --output out/xmem-scan.json --fail-on-null
```

### Desktop GPU addresses for title allocations

On the console a graphics allocation's CPU pointer is also its GPU virtual address. Titles pass those addresses to D3D12 calls, and the BVH builder's shaders read 64-bit addresses from their input records. A desktop D3D12 resource gets a GPU virtual address chosen by the driver.

- `era_gpu_alias::open` (`src/era_gpu_alias.cpp`) opens an existing `VirtualAlloc` allocation, such as an `era_heap` graphics allocation, with `ID3D12Device3::OpenExistingHeapFromAddress`. It then places one cross-adapter buffer with unordered access over it.
- The title pointer and the buffer's GPU virtual address then name the same system-memory bytes, without copies.
- `era_title_va::Table` (`src/era_title_va.cpp`) maps title address ranges to those desktop GPU virtual addresses.

What is covered:
- Translating title addresses that D3D12 calls receive. Root views at translated addresses are exercised below.
- Shader loads through 64-bit title addresses read from memory, via `src/title_va_lookup.hlsli`.
- Stores, integer add and sub atomics, and float min and max (a compare-exchange loop) through those addresses, on the same aliased system-memory heaps.

Not covered:
- Performance. GPU access to this heap goes over the bus.

How the shader lookup works:
- `era_title_va::shader_ranges` packs the table's sorted ranges: low and high words of begin and end, and a descriptor slot.
- `TitleLookup` binary-searches them with 64-bit comparisons on `uint2`. It takes the access size and succeeds only for a 4-byte-aligned address whose whole access lies inside one range. It then returns the slot of the buffer that aliases the range, and the byte offset, which is exact because a range is shorter than 4 GiB. `shader_ranges` also requires each range to start on a 4-byte boundary.
- The shader then loads through that slot's raw UAV.

| CTest case | Inputs and checks |
|---|---|
| `era_title_va_translate` | Offsets inside two ranges, the last word of a range, and a zero byte count translate to the matching desktop address. |
| `era_title_va_bounds` | Bytes just before and after a range, spans crossing its end, a span longer than the range and a wrapping span return 0. |
| `era_title_va_registry` | Empty, wrapping, zero-GPU and overlapping ranges are rejected; adjacent ranges are accepted and listed in order; a removed range no longer translates. |
| `gpu_alias_warp`, `gpu_alias_hardware` | A 256 KiB memory-type-1 allocation from `era_heap` is opened as a heap. A compute shader reads 4,096 words through a root UAV at the translated title address `+0x1000` and writes transformed words through a root UAV at `+0x20000`. The CPU checks every result and the unchanged source through the title pointer, with no readback buffer. 64 guard words after the written range must keep their sentinel. Opening a pointer `0x1000` into the allocation must return `E_INVALIDARG` from `era_gpu_alias::open` without reaching D3D12; passing it to `OpenExistingHeapFromAddress` makes the debug layer report message 1088. Returns 77 when the adapter does not report `D3D12_FEATURE_EXISTING_HEAPS`. |
| `era_title_va_shader` | Packing splits begin and end into low and high words, including a range across a 4 GiB line; slots follow table order; unsorted, overlapping, empty, 4 GiB and unaligned-start ranges are rejected, and a length that is not a multiple of 4 is accepted. |
| `gpu_alias_lookup_warp`, `gpu_alias_lookup_hardware` | Three memory-type-1 allocations and one placed across a 4 GiB line are opened as heaps, packed, and bound as raw UAVs in a descriptor table. The placed range ends two bytes short of its allocation, so its last aligned word crosses the range end. A compute shader loads one DWORD through each of 150 title addresses read from an input buffer: starts, middles, each range's last valid word and the aligned word after it, misaligned starts at both ends, the words just outside each allocation, the null page, an address above user space, and the same low words 4 GiB above and below the placed allocation. Values and slots must match the CPU reading through the title pointers; misaligned, tail-crossing and out-of-range addresses must return the miss marker. The hit and miss split varies with where the allocations land. |
| `gpu_alias_lookup_writes_warp`, `gpu_alias_lookup_writes_hardware` | The same four allocations. A compute shader runs 1,047 records through the lookup: 14 stores (range starts, middles, each last valid word, and the words on either side of the 4 GiB line, all distinct); 256 contending threads each for add, sub, float min and float max on one word per operation; and nine rejected records (stores to unmapped addresses, a store and a float min on the tail-crossing word, misaligned store, add and sub inside ranges), which must report 0. Every word of all four allocations is then compared with the CPU expectation through the title pointers, so rejected writes, including the in-range bytes of the tail-crossing word, must leave the backing words unchanged. A compare-exchange loop that does not retry after losing a race passes on WARP, which showed no conflicting update. It fails on hardware. Before the fixture, a probe on four plain zeroed buffers in the same kind of UAV table has 256 threads each add 1 through a per-thread array index; each buffer must read 64, and the expected and actual counts are always printed. The fixture then runs in full and reports two groups, each with a mismatch count and its first mismatch: the shared add and sub words, and everything else (the per-record results and every other word). Wrong probe counts or any mismatch fail the test. It returns 77 only when the adapter does not support existing heaps. |

Measured at commit `55cea41` on Windows 11 build 26300, with the debug layer unavailable (Graphics Tools not installed):
- `gpu_alias_hardware` passed on an NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088).
- `gpu_alias_warp` passed on WARP 10.0.26100.9818.
- On both, the interior open returned `E_INVALIDARG` (`0x80070057`).
- At commit `985f0ab`, `gpu_alias_lookup_hardware` and `gpu_alias_lookup_warp` passed on the same two adapters with 4 ranges and 122 records, mismatches 0 (one run: 104 hits and 18 misses).
- At commit `8bf7666`, `gpu_alias_lookup_writes_warp` passed on WARP 10.0.26100.9818: 1,041 records, sum 128416, min -1250, max 1241.62, three misses, 147,456 words checked, mismatches 0. At the next commit, `gpu_alias_lookup_writes_hardware` passed on the NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088) with the same values, 10 of 10 repeated runs. A compare-exchange loop without retry is caught there (the max word came out low).
- At commit `1c19cb6` (whole-DWORD validation), both lookup tests passed on WARP 10.0.26100.9818: lookup 150 records with 48 misses; writes 1,047 records with 9 rejected, 147,456 words checked; mismatches 0. Removing the span check, the alignment check, or both (the earlier first-byte check) fails both tests. The hardware variants were not re-run for this commit (the GPU was in use).
- On Windows 10 22H2 (build 19045) the in-box WARP 10.0.19041.5794 gives a wrong result, so `gpu_alias_lookup_writes_warp` fails there: in the atomics probe each buffer reads 256, not 64. In the fixture this made the add word `0x7d680` (four times the sum `0x1f5a0`) and the sub word `0x76bc0` (1,000,000 minus four times the sum); stores, compare-exchange and the per-record results were correct. The factor is 4 for arrays of 2, 3, 8 and 16 UAVs, for raw, structured and typed views, with or without `NonUniformResourceIndex`, and `InterlockedXor` is also applied an even number of times. A single UAV and an index read from a constant buffer are exact; a literal index of 2 updates element 0. DXC 1.7.2308, 1.8.2502 and 1.9.2609 give the same result there. App-local WARP 1.0.9 through 1.0.22 and an NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) are exact for the array of 8 and for the literal index, and pass the fixture. On the failing WARP the fixture reports the add and sub words as the only two mismatches: the other group, 1,047 per-record results and 147,454 words, has none. At commit `246990b` the Windows 11 in-box WARP 10.0.26100.9818 read 64 in all four probe buffers with mismatches 0 (measured by the fixture's author).
- At commit `9bdbb20` two RDNA 4 adapters give the same probe result, so `gpu_alias_lookup_writes_hardware` fails on both by design: each of the four buffers reads 256, not 64, on all three runs of each adapter. The adapters are an AMD Radeon RX 9070 XT (device `7550`, driver 32.0.32015.7029, debug layer enabled; [report](https://github.com/R0Y00/Xbox2Windows/issues/369)) and an AMD Radeon RX 9060 XT (device `7590`, driver 32.0.32029.2010, debug layer unavailable; [report](https://github.com/R0Y00/Xbox2Windows/issues/369#issuecomment-7071098620)). On both, the title-address fixture itself records zero mismatches in both groups: the add and sub words, and the 1,047 per-record results and 147,454 other words. In the same runs an NVIDIA GeForce RTX 5060 Ti (driver 32.0.16.2124) and the in-box WARP 10.0.26100.9711 and 10.0.26100.9818 read 64 in all four buffers. Local probe variants on the RX 9070 XT, not committed, isolated the condition. A resource index that varies within a wave gives 256 in every buffer, both without the returned value and when lane pairs share an index. An index that is uniform across each wave, a literal index of 0 and plain stores through `NonUniformResourceIndex` are exact. An explicit waterfall that selects the index with `WaveReadLaneFirst` and runs the atomic once per distinct index reads 64 in all four buffers. The cause is not established.
- At commit `65492448` on Windows 10 22H2 (build 19045.6456, MSVC 19.44.35230.0, Windows SDK 10.0.26100.0 DXC, debug layer enabled on both backends), a GTX 1060 6 GB (driver 32.0.15.6094) reads 64 in all four probe buffers in 3/3 runs, and the in-box WARP 10.0.19041.5794 on the same host reproduces the Windows 10 behavior above: each buffer reads 256 in 3/3 runs, with the fixture reporting the shared add and sub words as the only two mismatches. Both backends matched all 1,047 per-record results and the other 147,454 words, with zero debug errors or warnings ([report](https://github.com/R0Y00/Xbox2Windows/issues/369#issuecomment-7071770464)).
- At commit `185b9d6c` on Windows 11 build 26200 with the debug layer enabled, an RDNA 3 adapter reproduces the same probe failure: an AMD Radeon RX 7900 XTX (device `744c`, driver 32.0.32029.2010) reads 256 in each of the four buffers, with the title-address fixture recording zero mismatches across all 1,047 records and 147,454 checked words and zero D3D12 debug errors or warnings ([report](https://github.com/R0Y00/Xbox2Windows/issues/369)).

## Offscreen rendering and readback

`offscreen_hardware` selects a high-performance adapter. `offscreen_warp` selects Microsoft's software rasterizer. Both draw the same DXC-compiled triangle to a 64×64 RGBA8 texture. After GPU completion the test reads the texture using its row pitch and compares colors and coverage pixel by pixel. Outputs are `triangle_hardware.bmp` and `triangle_warp.bmp` in the build directory. `ctest --preset dev -V` reports the adapter, driver, debug layer and pixel differences. Error or corruption messages from an available debug layer fail the test.

The original test machine had Windows Graphics Tools installed. A second build in `out/build/agility` used Microsoft Agility SDK 1.620.2. Set `-DOFFSCREEN_AGILITY_620_DIR=<directory containing the x64 SDK DLLs>` to reproduce it. This configuration verifies debug interface activation. [Agility SDK package](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2)

### Desktop GPU addresses for xmem graphics allocations

`era_title_gpu` (`src/era_title_gpu.cpp`) is a process-wide registry that connects `xmem.dll` to the GPU address pieces above.

How it works:
- `xmem.dll` tracks every graphics allocation through it. Without a device this only registers the region with `era_query`, as before.
- `attach(device)` opens every tracked allocation as an `era_gpu_alias` heap and adds its range to an `era_title_va::Table`. Allocations tracked later are aliased at once.
- `translate` gives the desktop GPU address of a title span, and `locate` gives the buffer and offset behind it, for descriptors.
- `shader_view` returns the packed ranges and the buffers in slot order for `TitleLookup`.

Failures and lifecycle:
- An allocation that cannot be aliased stays tracked without a translation. `attach` reports the first such failure after trying every allocation.
- `detach` drops every alias and keeps tracking. Attaching a different device re-aliases everything.
- The registry does not wait for GPU work before an alias is released: releasing memory the GPU still uses is a title error on the console as well.
- The first `track` or `attach` creates the registry, and it is never destroyed, so unloading the module releases no D3D12 object. The other functions never create it; without it they behave as for an empty registry.
- These functions are `noexcept`, and `track` runs while `XMemAlloc` is being serviced. An allocation failure, including while creating the registry, returns `false` from `track` (`E_OUTOFMEMORY` from `attach`) instead of ending the process. It registers nothing, and the call can be retried.

The registry is private to the module that links it. `xmem.dll` keeps its four exports and its imports, and loading it does not load D3D12. The direct-linked tests cover attach and lookup within that module. A D3D12 client in another DLL cannot reach the registry: a shared registry, with a fixture that loads both DLLs and shows the client sees the real provider's allocations, is left for the first such client.

| CTest case | Inputs and checks |
|---|---|
| `gpu_alias_registry_warp`, `gpu_alias_registry_hardware` | Two memory-type-1 allocations and an interior block of a region placed below the first one (not an allocation base, so it cannot be aliased) are tracked before attach. `era_query` must report them GPU-intended, with nothing translated. `attach` must report the interior block's `E_INVALIDARG` and still alias both allocations. `locate` and `translate` must agree. A span past the end and the interior block must not be translated or located. A third allocation tracked after attach is aliased at once, and an unaliasable block tracked after attach stays tracked without a translation. Re-attaching the same device keeps existing aliases. The registry's shader view then drives `TitleLookup` on the GPU: 15 addresses, 12 hits, matching CPU reads through the title pointers. `untrack`, `detach` and a full re-attach are checked against the same translations. Duplicate, overlapping, null and empty input are rejected. |
| `xmem_shim_exports` | Now also requires that loading `xmem.dll` has not loaded `d3d12.dll`. |
| `era_title_gpu_alloc` | No GPU. The program replaces the global `operator new` so that one chosen allocation fails. Before the first `track`, `counts`, `translate`, `locate`, `shader_view`, `untrack` and `detach` must make no allocation. Then every allocation of a first `track` (registry creation included) and of a later one is failed in turn. Each failure must return `false`, leave nothing tracked, registered with `era_query` or translated, and the call is retried. The first `track` must have more allocations that can fail than the later one. An `abort()` is reported on stderr instead of the debug CRT's dialog, and the test has a 30 s timeout. |

Eight mutants of the registry each fail the test:
- never aliasing on `track`;
- stopping `attach` at the first failure;
- keeping the `era_query` registration after `untrack`;
- keeping aliases on `detach`;
- no bounds in `locate`;
- untracking on an alias failure;
- reversing the shader view's buffers;
- skipping the `era_query` registration.

Measured at commit `f847956`, Windows 11 build 26300, debug layer enabled:
- `gpu_alias_registry_warp` passed on WARP 10.0.26100.9818, with attach reporting `0x80070057` for the interior block, mismatches 0 and `debug_errors=0`. It passed 100 of 100 repeated runs.
- `gpu_alias_registry_hardware` passed on an NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088), with the same results. It passed 200 of 200 repeated runs.
- The full suite passed: 148 tests, with 3 capability skips (`bvh_wave64_hardware`, `bvh_wave64_warp`, `wave_shuffle_warp`).
- The stop-at-first-failure mutant failed 20 of 20 runs on each adapter ("two of three aliased"). All eight mutants were first measured at `d52905d`, on the RTX 3070 with the debug layer unavailable.

Before `f847956`, the interior block's search tried only the 256 MB directly below the first allocation. That search found no free block in about 1 of 40 hardware runs, and the test failed in setup ("region below the first allocation").

Measured at commit `21a65e5`, Windows 11 build 26300, debug layer enabled:
- The full suite passed: 149 tests, with the same 3 capability skips.
- `era_title_gpu_alloc` handled 6 failing allocations in a first `track` and 2 in a later one. It passed 50 of 50 repeated runs.
- `gpu_alias_registry_hardware` passed 100 of 100 repeated runs on the RTX 3070, and `gpu_alias_registry_warp` passed 50 of 50.
- Four mutants fail `era_title_gpu_alloc`:
  - creating the registry with a throwing `new`: `abort()`, exit 3;
  - building `shader_view`'s empty result by construction, which allocates under the debug STL: `abort()`, exit 3;
  - a `counts` that creates the registry: exit 1;
  - a `track` that keeps the `era_query` registration after a failed insertion: exit 1.

### BVH buffer operations on in-box WARP

The five source-built buffer shaders compile at `cs_6_0`. The fixture compares
copy, fill, numbering, span copy and bounds reset against a CPU reference,
including sentinel words outside the written ranges. Adapters below SM 6.0
return the capability skip code 77. A mismatch in a dispatched fixture fails
the test.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -V -R '^bvh_buffer_ops_(hardware|warp)$'
```

On Windows 10 22H2 build 19045.6456, the GTX 1060 6 GB (32.0.15.6094) and
in-box WARP (10.0.19041.5794) each matched all 384 words in each of 26 cases.
The D3D12 debug layer reported zero errors and warnings.

### Windows 10 with the app-local Agility SDK and WARP

The Windows 10 22H2 in-box runtime caps an RTX 3060 at SM 6.5 and the in-box WARP (10.0.19041) at SM 6.2 with no DXR, so the `cs_6_5` BVH fixtures, the DXR checks and forced wave64 skip there. Without Graphics Tools, the debug layer reports `DXGI_ERROR_SDK_COMPONENT_MISSING` even when the SDK layers are app-local. The Agility build therefore continues with `debug_layer=unavailable` for that one result, and other debug-layer failures still stop the test. Capabilities reported on Windows 10 19045.6466:

| Runtime | RTX 3060 (32.0.15.9636) | WARP |
|---|---|---|
| In-box | SM 6.5, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 | SM 6.8, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 and WARP 1.0.22 | SM 6.8, DXR tier 1.2, 32 lanes | 1.0.22: SM 6.8, DXR tier 1.2, 4 to 128 lanes |

Extract the [Agility SDK](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2) and [WARP](https://www.nuget.org/packages/Microsoft.Direct3D.WARP/1.0.22) packages, then configure with both directories. The Agility directory holds `D3D12Core.dll`, `d3d12SDKLayers.dll` and the package renamed to `sdk.nupkg`. The WARP directory is the package's `build/native/bin/x64`:

```powershell
cmake --preset dev -B out/build/w10 -DOFFSCREEN_AGILITY_620_DIR=<agility dir> -DOFFSCREEN_WARP_DIR=<warp dir>
cmake --build out/build/w10
ctest --test-dir out/build/w10 --no-tests=error
```

`OFFSCREEN_WARP_DIR` requires `OFFSCREEN_AGILITY_620_DIR`. On the in-box runtime, WARP 1.0.22 reports DXR tier 1.2, but `dxr_query_warp`, `dxr_update_warp` and `bvh_differential_warp` fail with every ray reported as a miss. With the Agility runtime the same WARP passes them. Configuring the same build directory again without `OFFSCREEN_WARP_DIR` deletes the copied `d3d10warp.dll`, so `offscreen_check` goes back to the in-box WARP. `app_local_warp_toggle` checks this with a stand-in DLL.

In this configuration all `bvh_*_warp`, `dxr_*_warp` and `bvh_differential_warp` tests pass, and the hardware wave64 check skips at its 32-lane range with SM 6.6 available. `bvh_wave64_warp` now runs and fails at field 25, `WaveReadLaneAt(v, (lane*7+3) % 64)`. In two groups, all 128 lanes received the value of lane m where `7m+3 ≡ lane (mod 64)`, which is the inverse of the requested shuffle. Field 24's shuffle, the mirror read and the native-width run pass. At WARP's native width of 8, `7n+3 ≡ 3-n (mod 8)` is self-inverse, so the default `bvh_waves_warp` cannot show the difference. `wave_shuffle_warp` narrows the cause. WARP 1.0.22 runs a forced wave as blocks of its native 8 lanes, and it picks the source block using the request of the wrong lane: the lane of the reader's block that sits at the requested position. For this shuffle that equals the inverse permutation. A rotation by one lane gets 16 of 64 reads wrong at widths 16, 32 and 64, and none of them match its inverse. Reads where every lane of a block requests the same source block are unaffected. [Open question](https://github.com/R0Y00/Xbox2Windows/issues/145)

## Mapped DLL draw with a Windows reference

`cpu_native_frame_hardware` and `cpu_native_frame_warp` load the same source-built DLL with `LoadLibraryW` and with `pe_map`. Both paths record TLS before the DLL entry on process attach and detach. The DLL's `guest_draw` export selects the shared two-texture shader fixture and calls the graphics provider through its bound import. The provider calls the host test, which runs the existing offscreen renderer in a child process. The renderer checks every RGBA channel against the CPU reference, and the test compares the two 64×64 captures byte for byte. Withholding the graphics provider must fail mapping before startup or drawing.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_frame_(hardware|warp)$' -V
```

On Windows 11 build 26200, the RTX 3060 Ti (driver 621.14) and WARP each matched all 4,096 pixels per load path with zero D3D12 debug warnings or errors.

## Static executable intake

By default, the intake tool locates the Scarlett entry in the sole build's `MicrosoftGame.Config`. It accepts the layout path or a separate candidate file and opens the game files for reading only.

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py
# Inspect a separately supplied candidate.
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py "E:\incoming\game_scarlett_master.exe"
```

Each run writes a new JSON report in `out/intake/`. Exit codes are 3 for a missing layout executable, 2 for an input or inspection failure and 0 for completed inspection. The report links the previous verification time, torrent SHA-256, install chunks and baseline summary. The inspected torrent omitted the EXE. The 13,345 matching pieces and 223,880,932,870 bytes verified in the historical report cover that original payload. A later EXE needs separate provenance. Intake rescans the package PEs and lists changed DLLs.

For each candidate the tool records SHA-256, architecture, entry RVA and section, TLS directory and callback order, then compares imports, modules and unresolved items with the baseline. Source-built PE fixtures and a miniature torrent cover named, ordinal and delayed imports, import names that pefile rejects, missing providers, TLS callbacks, invalid callback addresses, truncation, an absent entry and path conflicts. These checks establish static properties. Authenticity and startup remain unverified.

`pe.startup.locations` lists TLS callbacks in table order followed by the executable entry. Each location includes its RVA, section and file offset. When the x64 exception directory supplies a covering function interval, the report includes its inclusive start, exclusive end and unwind data RVA. A location can legitimately lack a covering record. An absent or unusable table leaves the independently observed startup locations available. In the local debug fixture the table had 110 records, one covering its TLS callback. The entry had no covering record. CTest cross-checks against pefile and exercises both absent and incomplete directories. [Microsoft x64 unwind data](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64)

### Frame plane requests in BinkAllocateFrameBuffers

One iteration of the exported frame-buffer allocator issues up to five requests to the [group descriptor append helper](#grouped-allocation-descriptor-builders). Slots 0, 3 and 4 use the first pitch and height. Slots 1 and 2 use the second pair. With pitches 96 and 32 and heights 7 and 3, enabling all five produced sizes 672, 96, 96, 672 and 672 in that order. Each call received the address of the selected plane's destination field, and its pitch field was written after the call.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_frame_planes$'`. Setting `BINK_DESCRIPTOR_IMAGE` also enables the `cpu_bink_package_frame_planes` test after building `cpu_bink_package_descriptors`. That comparison translates the loop body from RVA `0x2257` up to `0x2325` in the fingerprinted image. It supplies a frame index, the matching cursor and two preselected pitches. A desktop observer records the arguments at the five append sites. Pitch selection, the outer loop and backing storage allocation are outside this check.

### Frame buffers through the shared Windows heap

The integrated package replay enters the allocator with a context, frame table and requested alignment. It translates the function body, including format-based pitch selection, frame count adjustment, the five plane sites, the group allocation and the owner and accounting writes. The Windows compiler provides the stack prologue and epilogue. The group and heap functions are the same translations used in their individual tests. The resulting DLL imports `XMemAlloc` and `XMemFree` by name from the shared desktop `xmem.dll`.

The public control combines a source-written outer function with the translated MASM plane loop, descriptor helpers and grouped heap caller. Both builds run 36 cases, including all plane-enable masks on one frame, empty requests, two-frame allocation and the three-to-two count adjustment. Each enabled plane is filled independently and the entire backing block is compared, including padding. A second link of the same objects records the import arguments and forwards to the shared DLL, or injects one null allocation. On failure the caller returned zero, kept the owner null and destination slots unchanged for requested planes, and retained the pitch and accounting updates. Retrying the same context succeeded. The normal release returned the original heap base once with the allocation attributes.

Build the dev preset and run `ctest --preset dev --no-tests=error -R 'frame_to_heap$' -V`. For the local package case, configure the same fingerprinted DLL for `BINK_DESCRIPTOR_IMAGE` and `BINK_HEAP_IMAGE` and the grouped and heap entries documented below. The scenarios supply context and frame metadata; video stream decoding is outside this test.

### Grouped allocation descriptor builders

In the inspected Bink image, the function interval beginning at RVA `0x67c0` contains direct calls to the group initializer (`0x25b80`), append helper (`0x25b20`) and grouped allocator (`0x25a40`). The initializer places two adjacent arrays after a 64-byte header. Each successful append records a destination and a padded stride. With five available slots, requests of 0, 33, 1, 1025 and 32 bytes produced strides of 32, 64, 128, 1280 and 512 bytes in order. The stride in 32-byte units is congruent to the previous total in units plus one, modulo 32. All five stored strides were even. These helpers do not establish who produces odd tagged entries.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_descriptors$'`. The test compares a source-written MASM implementation and its translation against explicit expected arrays after every append. Set `BINK_DESCRIPTOR_IMAGE` to the local Bink DLL to enable the fingerprint-checked `cpu_bink_package_descriptors` target and test. In the translated interface a capacity breakpoint returns status zero, allowing the test to check that both arrays and their guard word survive an extra append. The native control runs only within capacity.

### Bink allocation and release on the Windows heap

The exported binding now serves requests from `era_heap`, so the Bink shim and the heap experiments share one attribute decoder, alignment rule and release path. `bink_heap_check.py` checks on the binding directly that a 97- or 129-byte request with `0x05e50000` returns a 32-byte-aligned, zeroed block and that the matching release frees it.

The local test lowers the allocation and release callers and the exported `BinkSetMemory` setter from x64 machine code to C++, links the result to a Windows process-heap adapter, and runs the new DLL. The translator follows the setter's export jump and binds its two pointer stores to the same slots the allocation path reads. It also resolves the named PE imports, including tail transfers through linker thunks. The console-specific meaning of the heap attributes remains open.

On Windows, the public native fixture, its translation and the locally translated callers passed the same checks:

- Lengths 0 and `0xffffffff` returned null before reaching the allocator. Three forced allocation failures returned null after one request each. The largest input, `0x100000001`, verified 64-bit size handling without a large allocation.
- Each accepted length requested 64 extra bytes with attributes `0x05e50000`. Returning heap-backed storage at each of the 32 possible base residues produced a 32-byte-aligned user pointer. The check wrote the entire payload and compared both header bytes and the surrounding guards.
- With 32 allocations live, releasing them in reverse order passed each original base to the adapter once with the allocation attributes. All 32 backing blocks were freed by `HeapFree`. Releasing null made no allocator call.
- Callback pairs A and B each received one allocation request (97 and 193 bytes for 33- and 129-byte payloads). Both blocks contained the corresponding release function address, and the payload and guards matched. After installing B, releasing A's block called A's release function once with A's original base. After clearing the callbacks, releasing B's block likewise called B once with B's base. Neither operation called the XMem adapter.
- A null custom return for a 65-byte payload led to exactly one 129-byte `XMemAlloc` request. After switching callbacks, that block went back to `XMemFree` once with the original base and attributes. When both allocation attempts failed the caller returned null. A separate all-ones custom return stopped the request before the XMem adapter.

For the public control, build `cpu_bink_heap_lifted` in the dev preset, then run `ctest --preset dev -R '^cpu_aot_bink_heap$' --no-tests=error -V`. For a local package, set the CMake cache entries `BINK_HEAP_IMAGE` to the DLL path and `BINK_HEAP_ENTRIES` to the semicolon-separated allocation and release function **entry** RVAs for that image. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_heap`. Generated translations stay in the build directory.

The grouped caller at RVA `0x25a40` in the inspected Bink DLL tail-calls the allocator at `0x25bf0` when its first argument is null. A 33-byte prefix becomes 64 bytes before the allocator adds its own 64 bytes. With a group and 48 extra bytes, the same input produces a 176-byte request at the heap provider. On success, an odd length word selects a pointer slot relative to the returned block and an even word selects an external slot. Clearing the low bit gives the payload stride. On allocation failure both the extra-byte total and the entry count are cleared and the external slot retains its value. The [descriptor builders](#grouped-allocation-descriptor-builders) produce even strides, and no code in the inspected image produces an odd one. The append helper at `0x25b20` is the only writer of the length array. It stores the request rounded up to 32 bytes plus a multiple of 32, so bits 0–4 of every stored word are clear. Of the ten call sites of `0x25a40`, three pass no group: `BinkUtilMalloc`, the asynchronous thread start and the subtitle loader through `0x8460`. The other seven pass a stack group that the same function filled only through `0x25b20`: one each in `BinkAllocateFrameBuffers` and `BinkOpenTrack`, and five in the open routine at `0x67c0`. None of these functions writes the group header or its arrays directly. The initializer, append helper and grouped allocator are not exported and have no data references outside `.pdata`. In this image the relative pointer-slot branch is therefore unreachable, and the odd length in the group controls below is synthetic. This was established by static analysis, without execution.

Build `cpu_bink_heap_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_aot_bink_group$'` for the source-built assembly control and its translation. For the inspected local DLL (SHA-256 `9eaf045d004cd7d6a8382aaff0c6ea23a29fec4ef8bfacd726b54f5bc26fafd0`), set `BINK_HEAP_ENTRIES` to `0x25bf0;0x25bc0` and `BINK_GROUP_ENTRY` to `0x25a40` alongside `BINK_HEAP_IMAGE`. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_group`. The test supplies both descriptor arrays and runs the translated caller with the Windows test heap.

## Upscaler working-set counter updates

Immediately after the statistics calls at RVAs `0x74d2` and `0x75b1`, both paths add the new sample minus the previous sample to a 64-bit counter. A zero status substitutes zero for the new sample even if the supplied output word is nonzero. With starting counters 11 and 101, a previous sample of 23, a new sample of 70, successful status and flag 256, creation produces 58 and 148. Release produces 58 and 101 because it reads only the low flag byte. Both counters wrap modulo 2^64.

Build `cpu_working_set_delta_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_working_set_delta$'` for the source-built assembly control and its translation. With `FFX_UPSCALER_IMAGE` set to the fingerprinted local upscaler, build `ffx_package_working_set_delta` and run its namesake test for the two translated instruction ranges. The test supplies the status, samples and flags at the point where the query returns. The Xbox statistics provider, measurement units and real sample values remain unverified.

## Six API call sites

After loading the toolchain, inspect three local DLLs. `out/calls/six-apis.json` records image SHA-256, import slots, callers of thunks, embedded container sections and code references. Addresses below are RVAs in the relevant DLL. Indirect calls need execution evidence to identify the selected path.

```powershell
.\tools\dev.ps1
.\out\pe-env\Scripts\python.exe -B tools/call_evidence.py "game-files/<build-directory>"
```

| Import | Observed inputs, return handling and desktop check |
|---|---|
| `XMemAlloc` | At `0x25c35`, Bink's fallback requests the length plus 64 with attributes `0x05e50000` and checks for null. It advances the base by `64 - (base & 31)` bytes (33–64), giving a 32-byte-aligned user pointer. The two preceding bytes hold the offset at `[-1]` and a tag at `[-2]`. At `0x565d`, AMD rounds the nonzero request up to a multiple of 256, reads attributes at `0x6eee0` (on-disk initial value `0xa8004000`) and checks for null. Under the ERA layout the alignment fields give 32 and 256 bytes, matching this caller arithmetic. `era_heap_observed_words` checks the desktop handling of both words; allocator-ID and GPU-visibility semantics remain unconfirmed. |
| `XMemFree` | At `0x25be4`, Bink uses the saved offset to recover the allocation base and passes the same attributes. Custom allocations use a matching callback. AMD cleanup at `0x5712` and release at `0x5921` both use the allocation's attribute slot. The latter also clears the stored pointer. No observed caller consumes the return value. |
| `XMemVirtualQuery` | Four BVH callers pass the current process, target address and a zeroed 64-byte buffer. Each checks a nonzero return, bit `0x1000` of the word at `+0x20` and either low bit of the byte at `+0x30`. Source acceleration structure validation at `0x19c69` also requires the byte at `+0x24` to intersect mask `6`. Destination acceleration structure (`0x19ccb`), index buffer (`0x19d9d`) and vertex buffer (`0x19df9`) use the common checks. Diagnostics describe the whole check. The individual tail bits have no confirmed meaning. |
| `XMemGetWorkingSetStatistics` | Seven AMD calls pass selector `0x20000001` and a local output address. At `0x26b3` and elsewhere a zero return contributes zero to a calculation. Success uses the 64-bit value at `+8`. Code near `0x74d2/75b1` uses a before/after difference to update a counter. The units, scope and selector meaning remain open. |
| `D3D12SerializeVersionedRootSignature` | At `0xc2d0` the BVH code copies a caller's 48-byte descriptor and adds flags `0x03000000` at `+0x28`. At `0xc32f` it passes the descriptor, a blob output pointer and a null error output. Negative HRESULT enters diagnostics. Success uses the blob length and data pointer. A synthetic v1.1 descriptor returned `S_OK` with baseline flags and `E_INVALIDARG` with the two added bits. |
| `D3D12CreateRootSignatureDeserializer` | At `0x4f88`, AMD reads a byte pointer and a 32-bit length from record offsets `+0/+8`, then supplies an interface IID and object output address. A negative HRESULT or null object triggers partial cleanup. Success retrieves the descriptor via the object. Desktop code read the root signature from a complete Microsoft sample shader and returned `E_INVALIDARG` for a synthetic shader without one. |

### Graphics input paths

- The BVH serializer helper has 16 direct calls. At `0xd967–0xd998` the caller builds a version, six parameters and the parameter-array address on the stack. At `0x2151d` another path references container `0x25700` and passes it downstream with length `0x1134`. The DLL contains 52 code-referenced DXBC containers with structurally valid DXIL programs and `RTS0` sections. All 52 root signature headers carry `0x03000000`. The desktop failure case reproduces these flag bits. Their meaning remains unconfirmed.
- AMD registers the deserialization function in a callback table at `0xcea3–0xceaa`. A producer at `0xe9b8/0xe9c3` selects container `0x714f0` of length `0x1910`, copies its descriptor record and calls through the table at `0xebad`. The DLL has 29 structurally complete containers, each with a matching pointer-table entry and `RTS0` and `ACCC` sections. None contains DXIL. These observations support an embedded source. Runtime initialization determines the indirect path.
- Counts cover the inspected DLLs. Scene shaders, archive read order and the graphics path used at startup have no execution record here. An entry executable would allow its imports and initialization calls to be examined.

### Microsoft sample on desktop

The test uses Microsoft's SimpleTriangle at `Xbox-GDK-Samples` commit `b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The project includes a `Gaming.Xbox.Scarlett.x64` configuration and a published reference image. Desktop builds compile the two HLSL files using the sample's three vertices, colors, sRGB render target and clear color. The test checks SHA-256 for both shaders and the image. Set `SIMPLE_TRIANGLE_DIR` to that revision's sample directory and run `ctest --preset dev -R reference_triangle -V`. A sparse checkout into the default location needs no cache entry: `git clone --filter=blob:none --no-checkout https://github.com/microsoft/Xbox-GDK-Samples out/reference/b1e77b6ac10f95b07296025fda171ca2ddc42dbe`, then in that directory `git sparse-checkout set --no-cone /Samples/IntroGraphics/SimpleTriangle/` and `git checkout b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The sample repository's `.gitattributes` writes `*.hlsl` with CRLF. The test hashes the shaders with LF line endings, as they are stored in the repository, so a raw download and a checkout give the same result. [Microsoft sample overview](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/simpletriangle/) · [Pinned source and reference image](https://github.com/microsoft/Xbox-GDK-Samples/tree/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/IntroGraphics/SimpleTriangle)

RTX 2060 and WARP rendered at 64×64. Excluding triangle edges by normalized coordinates left 10,812 color channels per adapter. Differences from the source-derived colors were at most 2 on an 8-bit scale. Against the published image the mean absolute difference was 6.88 and the maximum was 17. The two desktop images differed at 94 pixels, by at most 1 per channel. The published image came without a device model, build hash or lossless GPU readback. Source-derived colors supply the acceptance values. Microsoft distributes console GDKX separately. [Microsoft developer resources](https://developer.microsoft.com/en-us/games/resources/)

## Compute and ray tracing before executable intake

The inspection reads bytes from two local DLLs. Desktop parsers inspect serialized descriptors and DXIL. GPU tests execute HLSL built from this repository. After building, run:

```powershell
$pkg = (Get-ChildItem game-files -Directory)[0].FullName
.\out\pe-env\Scripts\python.exe -B tools/probe_roots.py $pkg --controls out/build/dev
ctest --preset dev -R 'compute_|dxr_(query|pipeline)|shader_metadata|bvh_buffer_ops' -V
```

`out/calls/desktop-roots.json` records per-container return codes, root signature summaries, matching desktop fixtures, static call counts and validator diagnostics. It also hashes the input inventory and probe executable. Each container was checked independently.

| Windows check | XboxBvhBuilder | AMD upscaler |
|---|---|---|
| Both D3D12 deserializers read the embedded signature from the full container | 52/52 accepted, 16 distinct signatures | 29/29 accepted, 3 distinct signatures |
| Reserialize unmodified v1.1 descriptor | 52/52 returned `E_INVALIDARG` | 29/29 accepted |
| Clear only `0x03000000` in a memory copy and reserialize | 52/52 accepted. Console flag semantics are unknown. | Original flags were zero. |
| Validate the entire container and the internal bitcode separately using the Windows SDK | Both checks rejected all 52 Shader Model 6.5 compute programs. | `ACCC` present, no DXIL input. |

Both source-built control shaders passed container and module validation. For the BVH modules the validator reported an unrecognized `dxil-ms-xb2` target and `xbox.compiler.options` metadata. DXC disassembly showed 43 modules directly calling 33 distinct `xbox.` functions, including cache-controlled reads, cross-lane operations and floating-point atomics. The other nine had no such direct calls. Diagnostics retain up to 2 KiB per message. Counts use the complete disassembly. [DXC validator interface](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/dxcapi.h)

The first synthetic compute layout uses an SRV, a UAV and 16 root constants. Its `RTS0` payload matches 11 BVH containers byte for byte after clearing the two flags. The second uses 13 UAVs, 21 SRVs, four root CBVs and a static sampler. It matches 27 unmodified AMD containers. Both tests access the last registers in the descriptor tables. RTX 2060 and WARP wrote 257 correct integer words per layout and preserved 63 sentinel words.

The native DXR fixture builds a bottom-level structure (BLAS) from one triangle and a top-level structure (TLAS) from two instances. Five rays cover hits, a geometric miss, distance clipping, translation and instance masks. The existing `dxr_query` test uses inline RayQuery on DXR Tier 1.1 with Shader Model 6.5. The `dxr_pipeline` test uses a source-built `lib_6_3` ray generation, miss and closest-hit library with TraceRay and DispatchRays on DXR Tier 1.0 or later with Shader Model 6.3 or later. The standalone GPU check includes `dxr_pipeline`. The original RTX 2060 and WARP inline runs hit instances 7 and 11 at distance 1 and missed the other three rays. The GTX 1060 pipeline and both app-local WARP paths reported the same instance IDs, primitive index 0 and hit distance 1. The three miss rows were all zero. Integration of the game's acceleration structure layout and consumers with desktop DXR remains unverified. [Microsoft DXR specification](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#d3d12_raytracing_tier)

Four priority BVH containers use the first layout, are referenced by the function starting at `0x20270` and use 32×1×1 thread groups. Source-built HLSL reproduces the data operations inferred from static instructions. Independent CPU results supply the expected values.

| Container RVA | Observed input and write behavior |
|---|---|
| `0xb3ae0` | Copy the specified count of 16-byte input blocks to matching output offsets. |
| `0xb82e0` | Write the same constant to all four 32-bit words of each valid block. |
| `0xb4bd0` | Write consecutive unsigned 32-bit integers starting at a seed. |
| `0xb5be0` | Read the length parameter from the second input word and copy `64 × (parameter + 1)` bytes in steps. When reset is enabled, write the positive and negative maximum finite float bit patterns to six words at output offsets 16–39. |

Tests use disjoint buffers and cover zero length, thread-group boundaries, integer wraparound and untouched memory. Each desktop backend runs 26 cases and checks 384 words per case. The fourth operation schedules its optional reset in a second dispatch with a UAV barrier. Source cross-group synchronization, aliasing and actual game call parameters remain open.

### Module-only validation by DXC release

`shader_metadata_probe` validates each control shader twice through `root_probe`: the whole container with default flags, then the bare bitcode with `DxcValidatorFlags_ModuleOnly`. With the DXC 1.7.2308 in the original Windows SDK 10.0.26100 release, the second call returns `E_INVALIDARG` without a validation status. [Issue #98](https://github.com/R0Y00/Xbox2Windows/issues/98) The test still runs the root signature, container and disassembly checks. It reports a skip only when both module calls return that code and `dxil.dll` has a 101.x file version. `shader_metadata_controls` checks that rule without a validator and prints each case. A refusal from a 1.x validator, one refused and one validated shader in either order, another call failure and a failed module status are all rejected. The Linux Python job runs the same controls.

The table repeats `root_probe`'s two calls with the `dxcompiler.dll` and `dxil.dll` of each official x64 release. The inputs were `compute_root`, `compute_tables` and `triangle_vs` compiled by DXC 1.7.2308. Every release accepted the three containers. The calls ran under Wine 11.0. The 1.7.2308 and 1.9.2609 rows match the native Windows 10 results in #98. `IDxcVersionInfo` reports 1.8 on both sides of the boundary, so the test reads the file version instead.

| DXC release | `dxil.dll` file version | `IDxcVersionInfo` | Module-only call |
|---|---|---|---|
| 1.7.2308 | 101.7.2308.12 | 1.7 | `E_INVALIDARG` |
| 1.8.2403, 1.8.2405, 1.8.2407 | 101.8.2403.18, 101.8.2405.17, 101.8.2407.12 | 1.8 | `E_INVALIDARG` |
| 1.8.2502 | 1.8.2502.8 | 1.8 | Status 0 |
| 1.8.2505, 1.8.2505.1, 1.9.2602, 1.9.2602.24, 1.9.2607, 1.9.2609 | 1.x | 1.9 | Status 0 |

The SDK's own validator depends on its servicing level. On native Windows 11 build 26200.9974, Windows SDK 10.0.26100.8249 installs `dxcompiler.dll` and `dxil.dll` 1.8.2502.11 under `bin/10.0.26100.0/x64`. CMake selected that `dxc.exe` by default, and `shader_metadata_probe` passed with `module_only=validated`. Which servicing update first shipped 1.8.2502 is untested.

To check another release on Windows, configure a separate build directory with its compiler, then run the test:

```powershell
cmake --preset dev -B out/build/dxc-check -DDXC_EXECUTABLE=<path>\bin\x64\dxc.exe
cmake --build out/build/dxc-check
ctest --test-dir out/build/dxc-check --no-tests=error -R '^shader_metadata_probe$' -V
```

### Retargeting dxil-ms-xb2 modules to desktop DXIL

`tools/dxil_retarget.py` parses LLVM 3.7 bitcode blocks and records without modifying opaque subblocks such as function bodies. It rewrites `MODULE_CODE_TRIPLE` from `dxil-ms-xb2` to `dxil-ms-dx` and removes the `METADATA_NAME`/`METADATA_NAMED_NODE` pair for `xbox.compiler.options` in `METADATA_BLOCK_ID`, preserving numbered metadata indices. For a DXBC container it also canonicalizes the `DXIL` program header to `BitcodeOffset=16`, retargets any DXIL `STAT` reflection part, clears the `0x03000000` root signature flag pair in `RTS0` (unless `--keep-root-flags` is set) and zeroes the stale 16-byte DXBC digest. Desktop DXC rejects `dxil-ms` as an unknown triple; `dxil-ms-dx` is the accepted desktop DXIL triple.

On the local `XboxBvhBuilder.dll` (SHA-256 `99f803ac22c01ef329dddb1d40be67238d2a812d01e8d040b3cb5135bc23707e`; 52 SM 6.5 compute containers), 9 modules make no direct `xbox.*` calls (`0x25700`, `0x35720`, `0x3ab00`, `0x46880`, `0x92e30`, `0xb3ae0`, `0xb4bd0`, `0xb5be0`, `0xb82e0`). With `dxil.dll` 1.9.2602.17 through `root_probe`:

- Before retargeting, all 9 failed both container and `ModuleOnly` validation with `0x80aa0009` (`Unknown target triple 'dxil-ms-xb2'` and `Named metadata 'xbox.compiler.options' is unknown`).
- With `--keep-root-flags`, all 9 passed `ModuleOnly` (`0x00000000`, zero diagnostics) and all 9 failed container validation with `0x80aa0013` (`Unsupported bit-flag set (root signature flags 0x3000000)`).
- With default container retargeting (clearing `0x03000000`), all 9 passed both `ModuleOnly` and container validation (`0x00000000`, zero diagnostics).

The other 43 BVH modules still fail validation after retargeting because of direct `xbox.*` calls and aggregate `insertvalue`/`extractvalue` instructions used with those intrinsics. The 29 AMD upscaler containers carry `RTS0` and `ACCC` without a `DXIL` part.

Passing `--sign` to `tools/dxil_retarget.py` (with `--probe root_probe.exe`) re-signs retargeted DXBC containers in place through `dxil.dll` (`IDxcValidator::Validate` with `DxcValidatorFlags_InPlaceEdit`). Adding `--pso-probe root_device_probe.exe` (and optional `--warp-only`) tests `CreateRootSignature` and `CreateComputePipelineState` on desktop D3D12 against the re-signed container.

Each container entry of the `--pe` report also carries `xbox_intrinsics`, a read-only inventory of the `xbox.*` names in the module as it was submitted. `calls` counts the symbols referenced by call instructions, `declarations` lists the function-table entries, `references` counts the symbols the module holds without calling them, with their form (alias, store, bitcast, global initializer or call operand; `debug-only` when the only use is a debug intrinsic's metadata), and `metadata_only` lists names carried by metadata string values (`!"xbox.…"`) without being declared, called or otherwise referenced; a named-metadata key such as `!xbox.compiler.options` names the node and is not counted. An unused declaration, or a name that only metadata mentions, is therefore never reported as a call. `--only-without-xbox-calls` selects the modules where none of those forms reach an `xbox.*` symbol, and the report counts them as `skipped_xbox_intrinsics`. The inventory itself changes no retargeting or submission behaviour; the submission gate described below does. Passing `--probe root_probe.exe` in the container mode of the same tool prints the inventory inside `validation.ir.xbox_intrinsics`.

`tools/xbox_intrinsic_gate.py` gates the submission path. The module it reads is the one that would be submitted, after retargeting, and it is refused while it still reaches an `xbox.*` builtin, either by calling it or by any other form that keeps it reachable — an alias, a stored pointer, a bitcast, a global initializer or a call operand — and the refusal names that form. A call and every other executable reference are listed separately, so a diagnostic says whether the module calls the builtin or only holds it. A symbol that appears only as metadata inside a debug intrinsic (`@llvm.dbg.value`, `llvm.dbg.declare`, `llvm.dbg.assign`) is not executable use: it does not refuse the module and is reported as debug-only, and only the callee decides, so a line that merely mentions a debug intrinsic still counts as an executable call. The scanner reads the LLVM 3.x text that `dxc -dumpbin` prints; the newer `#dbg_value` record form would be over-blocked rather than missed, which keeps the gate closed. The archive filter counts anything reachable, debug-only uses included, while the gate counts executable uses, so `--only-without-xbox-calls` also skips a module whose only reference is debug metadata — the conservative direction. A lowering counts only when the transformed module no longer calls or holds that symbol, because the gate reads instructions and not adapter claims. An adapter status table is advisory — it annotates each blocking entry with what the adapter reports and never clears a call or a reference the module still holds. A refused module names those calls and the probe process is never started, so it produces zero submissions, and the gate does not fall back to a creation failure such as `E_FAIL`. The inventory is mandatory: without a disassembly, which `--probe` produces, nothing is submitted and the result carries `gate.verdict = "NOT_CHECKED"` with `reason = "module-not-disassembled"`, so an unexamined module cannot look like a cleared one. A declaration, a name that only metadata mentions, and a module whose calls were removed all pass. Each `--pe` entry carries the decision as `gate`, the report counts `gate_rejected` and, with a probe, `pso_refused`, and `python tools/xbox_intrinsic_gate.py check-text <module.ll>` prints the same decision without a probe. `dxil_retarget_controls` covers the refusal of all six reachable forms — a call, an alias, a store, a bitcast, a global initializer and a call operand — the three admitting shapes, an advisory LOWERED status that still refuses, a missing inventory that starts no probe process, the archive filter treating every form as reaching an intrinsic, a metadata string that only spells a call and the `--pso-probe` without `--probe` path.

Public CI tests pure-Python bitcode/container/signing controls (`dxil_retarget_controls`) and source-built `compute_root.cso` (SM 6.0) and `CopyBlocksSM65.cso` (SM 6.5) shaders synthetically injected with `dxil-ms-xb2`, `xbox.compiler.options`, `BitcodeOffset=112` and `RTS0` flags `0x03000000` (`dxil_retarget_probe`). Negative controls verify that unmodified `xb2`, `--target-triple dxil-ms` and `--keep-root-flags` fail both validation and `--sign`, and that unsigned (zero-digest) and tampered-digest containers fail `CreateComputePipelineState` on WARP. Re-signed retargeted containers pass `CreateComputePipelineState` on WARP (for `compute_root.cso`, and for `CopyBlocksSM65.cso` when WARP reports SM 6.5 or newer), and the re-signed `compute_root.cso` executes on WARP with CPU-checked readback through `offscreen_check warp --compute-root-file`. `dxil_retarget_probe_hardware` runs the same signing, `CreateComputePipelineState`, and `offscreen_check hardware --compute-root-file` checks against a physical D3D12 adapter and skips with exit code `77` when no hardware adapter is visible.

```powershell
ctest --preset dev --no-tests=error -R '^dxil_retarget_' -V
# Local validation, re-signing and compute PSO check of the 9 modules without xbox.* calls:
.\out\pe-env\Scripts\python.exe -B tools/dxil_retarget.py --pe "game-files/<build-directory>/XboxBvhBuilder.dll" --only-without-xbox-calls --sign --probe out/build/dev/root_probe.exe --pso-probe out/build/dev/root_device_probe.exe --output-dir out/calls/dxil-signed --report out/calls/dxil-retarget.json
```

### BVH reads and cross-lane operations

In `out/calls/desktop-roots.json`, `static_operands` from `tools/probe_roots.py` groups functions by module count, call site and operand form. The counts below describe static instructions across the 52 BVH programs. Runtime frequency is unknown.

| Read form | Modules | Static calls |
|---|---:|---:|
| Global, one word | 17 | 227 |
| Global, two words | 3 | 129 |
| Global, three words | 12 | 184 |
| Global, four words | 10 | 44 |
| Scalar, one word | 16 | 32 |

Across 27 distinct modules, all 616 reads use 64-bit addresses and end with two LLVM `undef` arguments. They provide no definite cache policy. At RVA `0xa4780` a count read from base plus 12 limits active threads. A loop in `0xc6260` reads a word at base plus four times the index and writes it to the destination. The fixture exercises both data behaviors and the scalar offset `(index << 8) | 44`.

Two successive writes to the same GPU buffer are each ordered by a UAV-to-SRV transition before reads of all four widths. Each round submits 128 synthetic requests covering low-word carry, shared and scattered addresses, a final valid vector, overrun, misalignment and different high words. The CPU uses independent 64-bit range arithmetic and compares each returned word. Out-of-range sentinels are a fixture rule. Console cache hit rate, address ownership, invalid access behavior and visibility without explicit ordering remain unknown.

Eight modules contain 75 calls to `LaneSwizzle.i32`, with immediates 30, 60, 120, 240 and 480 occurring 15 times each. The `0x95e20` sequence conditionally accumulates exchanged values and reads lane 31. This motivated a candidate five-stage prefix sum within each 32-lane subgroup. Tests exercise native waves and group-shared memory separately. The logical 64-lane path also checks both halves of the ballot mask, prefix counts, dynamic indices and lane 31. The native path checks the active minimum, broadcast, divergent votes and dynamic reads. The CPU reconstructs expectations from GPU-reported native lane IDs.

Each backend runs 38 groups with empty, full and sparse ballots and 32 single-point inputs covering every position in each half. RTX 2060 reported native width 32–32 and WARP 4–4. Both passed the group-shared 64-logical-lane test and skipped forced native wave64 on capability grounds. A capable adapter will run that case. Single-point inputs, CPU prefix sums and final ray hits provide the PC acceptance checks. [WaveReadLaneAt semantics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/wavereadlaneat)

```powershell
ctest --preset dev -R 'bvh_reads|bvh_waves|bvh_wave64|bvh_hits' -V
```

### From buffer reads to ray hits

`bvh_hits_hardware` and `bvh_hits_warp` connect bounded address reads and half-group prefix sums to ray intersections. Box and triangle tests follow AMD GPURT's software approach. A synthetic root holds 64 child bounds with logical 64-bit triangle addresses. The 64 logical lanes test bounds and compact candidates using both half-group sums. Receiving lanes fetch vertices and reduce nearest hits within and across native waves. The fixture breaks distance ties by the smallest primitive ID. [AMD GPURT intersections](https://github.com/GPUOpen-Drivers/gpurt/blob/dev/src/shaders/IntersectCommon.hlsl)

Three successive scenes occupy the same GPU buffer. Each is written as a UAV and then read as an SRV. A CPU brute-force, double-precision plane intersection independently checks each ray's box count, triangle hit count, closest distance and primitive ID. Each round uses the same 132 rays, covering each leaf in isolation, forward, reverse and oblique rays, edges, vertices, zero distance and box-only intersections. Two rays lie in the first row's plane with a zero depth component. They enter the flat boxes through the zero-direction slab branch, one ending exactly on a box face, and never hit a triangle.

| Scene | Hits | Misses |
|---|---:|---:|
| Sparse layout, isolate all 64 leaves | 112 | 20 |
| Permuted addresses, overlapping geometry and equal-distance hits | 14 | 118 |
| Permuted again, with invalid address high bits, an out-of-range tail and a degenerate triangle | 108 | 24 |

RTX 2060 and WARP agreed with the CPU on every field. Closed intervals, tie breaking and invalid-leaf handling are fixture rules. The tests establish synchronized desktop reads and hit results. Scarlett node layouts and cache hints remain hypotheses to check against producer writes and consumer reads.

### Independent BVH checks on PC

Each scene sends the same 136 rays through a double-precision brute-force CPU test, a custom shader reading packed nodes, and standard DXR. DXR builds its own BLAS and TLAS from the original accessible vertices. The differential test prefers inline queries on Tier 1.1 and uses DispatchRays on Tier 1.0. Its custom BVH shader still requires Shader Model 6.5. All paths check hit status and closest distance. The CPU also checks that the specific DXR-selected primitive intersects at that distance, allowing valid ties. Comparisons use triangle interiors and distance margins. Edge and endpoint cases use the preceding fixture's fixed conventions. [DXR ray extents](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-extents)

RTX 2060 and WARP each completed 408 comparisons. In fault controls only the custom shader's input changes. Swapping child 0 and 32 addresses changed four results, replaying the previous scene changed 102 and aliasing invalid address high bits changed one. Each fault must alter its designated witness ray. The GTX 1060 with driver 32.0.15.6094 on Windows 10 build 19045.6456 matched all 408 comparisons through DispatchRays. App-local WARP 1.0.22 with Agility SDK 1.620.2 matched all 408 through each DXR path. All three fault witnesses fired in each run. The five-ray and differential checks reported zero D3D12 debug warnings and errors. In-box WARP 10.0.19041.5794 reports no DXR and skips these checks.

```powershell
$env:XBOX2WINDOWS_ADAPTER = 'GTX 1060'
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^(dxr_(pipeline|query)|bvh_differential)_hardware$' -V
# After configuring out/build/w10 with the Windows 10 app-local instructions
cmake --build out/build/w10 --target offscreen_check
ctest --test-dir out/build/w10 --no-tests=error -R '^(dxr_(pipeline|query)|bvh_differential(_pipeline)?)_warp$' -V
```

Project acceptance uses reproducible PC behavior. The next game-specific check is to identify the builder selected by the main executable, its node-buffer inputs and outputs, and the downstream queries, then add the observed constraints to PC tests.

### Building bounds under contention

Static disassembly identified 51 direct calls each to `xbox.global_atomic_fmin` and `xbox.global_atomic_fmax` across seven modules each. Each call takes a 64-bit address and a float after the opcode. Integer atomic add and subtract each occurred at four sites with a 64-bit address and a 32-bit operand. Regenerate `out/calls/desktop-roots.json` with `probe_roots.py` to inspect per-module counts and operand forms.

`bvh_atomics` builds seven sets of axis-aligned bounds in GPU memory. Contending threads use 32-bit compare-and-exchange to extend six raw float fields. Two counters cross the unsigned wrap boundary. The CPU checks every field and verifies that each accepted point received a unique prior value from each counter. Inputs cover positive-only and negative-only bounds, both signed zeros, subnormals, finite extremes, invalid points and an empty bucket. The fixture skips a point with any non-finite coordinate and orders negative zero before positive zero. These are explicit desktop conventions.

The query dispatch reads the accumulated bounds after a UAV barrier and tests 54 axis-aligned rays. The CPU computes each expected bucket mask by projecting the original points onto the three axes. RTX 2060 and WARP each passed 270 ray masks over five input batches, including the empty input and a reversed 1,025-point submission. Each full submission accepted 897 points. This exercises a source-built producer and consumer with shared bounds. The observed atomic calls alone do not identify the game's node layout or special-value rules.

### Packed leaves and initialization stores

The per-module `validation.ir.packing` report follows the immediate definition of the byte selector at all 106 `V_ALIGNBYTE_B32` sites. Each selector is masked with 3. The three modules contribute 44, 44 and 18 sites. Across seven other modules, 47 `BFI` sites include variable masks, zero and contiguous low-bit masks through 1023. The desktop test uses these constraints and AMD's bit selection and byte alignment operations to build a source-owned leaf format. Its field offsets and flag assignments are fixture conventions. [AMD RDNA 2 ISA reference](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`bvh_packed` uploads 64 packed leaf records per scene. A compute shader reconstructs their fields at four byte offsets, selects the ID and flags through a mask, then writes bounds, triangle vertices and logical addresses. The next dispatch traces the existing 132 rays directly against that buffer. A separate readback compares all 1,280 scene words plus four guard words per scene against the CPU layout. Three scenes cover address carry, an invalid high word, a truncated vertex span, degeneracy and ties. Flipping one selected ID bit in the first scene changed exactly four returned IDs. Distances and counts matched the independent geometry reference in both runs on RTX 2060 and WARP.

The static inventory records one scalar, six three-word and nine four-word cached stores. All have two trailing undefined operands. At module RVA `0xb92d0`, two pairs write the largest finite float three times, then its negative three times at the same base plus 12 bytes. `bvh_atomics` now initializes the six bounds on the GPU from dirty storage with this pair of stores. A UAV barrier orders initialization before accumulation. Empty buckets expose incomplete resets, and independent checks cover the two adjacent counters. Cache policy and the game's allocation layout remain unknown.

### Numeric operations on bounds

All 180 observed `Med3_F32` calls had two constant operands. In 156 calls they were the negative and positive largest finite floats. Six used the positive lower bound with float bits `0x0f000000` and the same upper bound. The remaining 18 used zero and `0x3f7ff972`. For ordered values these medians clamp the variable input to the two constants. The operand inventory now includes all 33 distinct direct Xbox intrinsic names.

`bvh_numeric` checks those three clamps at their boundaries, including neighboring float encodings and infinite inputs. It also tests three-input float extrema, three-input unsigned min/median/max and two independent packed binary16 lanes. For half precision the CPU decodes the exponent and fraction in double precision. Inputs cover every finite nonzero binary16 encoding and positive zero. The fixture maps signed zeros to positive zero and excludes non-finite half inputs. Each desktop backend agreed with the CPU on all 655,360 words from 65,536 input rows. NaN propagation and native Scarlett behavior remain open.

### Updating a live DXR scene

`dxr_update` moves a triangle from depth 1 to 3, changes two instance transforms and changes one visibility mask. It updates BLAS and TLAS in place using separate build and update scratch-size requirements. Seven rays cover old and new positions, visibility masks and distance clipping. Four change hit status. Both desktop backends matched the expected hit, instance, primitive and distance fields before and after the change. Each in-place result matched a fresh full build of the changed scene. [DirectX acceleration structure updates](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#acceleration-structure-update-constraints)

```powershell
ctest --preset dev -R 'bvh_packed|bvh_atomics|bvh_numeric|dxr_update|era_heap_caller_layout|static_exe_intake' -V
```

### Face culling under mirrored instances

`dxr_facing` sends 96 inline queries at eight instances of one two-triangle BLAS. Each instance receives twelve rays: both triangles, both directions along world z, and ray flags `RAY_FLAG_NONE`, `RAY_FLAG_CULL_BACK_FACING_TRIANGLES` and `RAY_FLAG_CULL_FRONT_FACING_TRIANGLES`. The instances combine `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_FRONT_COUNTERCLOCKWISE`, `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_CULL_DISABLE`, non-uniform scale and mirroring on x, y or z. The DXR specification defines a triangle as front facing when its vertices appear clockwise from the ray origin in a left-handed frame, evaluates that winding in object space so a negative-determinant instance transform leaves facing unchanged, and makes both cull flags inert on a cull-disabled instance. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [Instance flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#d3d12_raytracing_instance_flags)

The CPU reference in `tests/dxr_facing_reference.h` moves each ray into object space, intersects every triangle in double precision and applies those three rules. Before any GPU work the test compares it with a hand-derived table of front and back letters per instance. The reference expects 72 hits, 36 of them front facing, and 24 culled rays. A world-space winding test would disagree on 48 of the 96 rays, which all target the four mirrored instances. For each ray the shader reports hit status, `CommittedTriangleFrontFace`, instance ID, primitive index and distance. Status, facing, instance and primitive must match exactly. A hit distance of 2 or 3 may differ by 1e-5 relative, and a miss must leave all four words zero. Those 96 rays cross triangle interiors inside the distance interval. A separate CPU-only ray meets the z-scaled instance at the open upper endpoint of 2. Single-precision division in the inverse direction transform gave 1.9999999403953568 and incorrectly admitted the hit. The reference performs that division in double precision. A second fixture sends 12 rays through one identity instance of a square split into two consistently wound triangles in one BLAS geometry. Six rays cross its internal diagonal from opposite directions at three points. Each must commit a hit at distance 1 with the expected facing, and either incident primitive ID is valid. Four interior rays require a specific primitive and two rays outside the square must miss. The shader reports only the committed hit. Candidate multiplicity, silhouette edges, vertices and GPU distance endpoints remain unmeasured. [DXR shared-edge rules](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#fixed-function-ray-triangle-intersection-specification) The custom BVH shader has no facing rule and takes no part. The fixture skips below DXR tier 1.1 or Shader Model 6.5.

```powershell
ctest --preset dev --no-tests=error -R '^dxr_facing_' -V
```

### Opacity and candidate handling in inline queries

The other DXR fixtures build one opaque geometry and trace with a `RAY_FLAG_FORCE_OPAQUE` query, so no non-opaque candidate ever reaches a shader. `dxr_opacity` builds a BLAS of four single-triangle geometries stacked along z at depths 1 to 4. Geometries 1 and 3 carry `D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE`. Three instances use no flags, `D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_OPAQUE` and `D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_NON_OPAQUE`. Each instance receives 42 rays: both directions along z, seven ray flag sets (`RAY_FLAG_NONE`, `FORCE_OPAQUE`, `FORCE_NON_OPAQUE`, `CULL_OPAQUE`, `CULL_NON_OPAQUE`, `ACCEPT_FIRST_HIT_AND_END_SEARCH` alone and with `FORCE_NON_OPAQUE`) and three accept masks. `tests/ray_candidates.hlsl` declares `RayQuery<RAY_FLAG_NONE>` and commits a non-opaque candidate when its geometry index is set in the accept mask, a fixture convention carried in bits 16 to 19 of the flags word. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow)

The CPU reference in `tests/dxr_opacity_reference.h` intersects every triangle in double precision and applies the specification's order: ray flags override instance flags, which override geometry flags, and the cull flags then remove primitives by the resulting opacity. Before any GPU work the test compares it with a hand-derived table of opacity letters per instance and flag set. Swapping the ray and instance precedence in the reference breaks that table at ray 48. The reference expects 94 hits, 42 of them committed by the shader, and 32 misses. Ignoring instance flags would change the closest hit on 26 rays.

Without the first-hit flag, status, instance, geometry and primitive must match the closest committable hit exactly. With it, the query may end on any committable hit, but must present no candidate after a commit. Every presented candidate must be a non-opaque triangle inside the ray extents and not behind the current committed hit. A candidate at exactly the committed distance is allowed, because an implementation may present the committed triangle or an equal-distance one again. Each non-opaque triangle in front of the closest hit, and each committed one, must have been presented. A miss under the first-hit flag requires every non-opaque triangle. Hit distances of 1 to 4 may differ by 1e-5 relative. Rays cross triangle interiors well inside the distance interval, so edge and endpoint rules stay out of scope. Candidate counts and visit order are implementation-defined and are not compared. The fixture skips below DXR tier 1.1 or Shader Model 6.5.

The shader separates a candidate presented after its own commit from one presented after an automatic opaque commit only. The test prints up to 16 failing rays with their categories and a count per category. It also records `CandidateTriangleRayT` at each of its own commits.

An RTX 5080 (driver 32.0.16.1692, DXR tier 1.2) matched all 126 rays in both runs. WARP 10.0.26100.9818 (DXR tier 1.1) produced the expected committed hit on every ray, but does not end a first-hit search at a committed hit, automatic or shader. The specification ends traversal after the first committed hit, whether it comes from fixed-function intersection or from the shader. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow) The first WARP run reported 11 mismatching rays and printed only ray 15: with `RAY_FLAG_ACCEPT_FIRST_HIT_AND_END_SEARCH` and accept mask 0, WARP committed opaque geometry 1 at distance 2, then presented geometry 0 at distance 1. The second run separated the categories over 11 rays. Rays 19, 20, 61, 62, 100, 101, 103 and 104 had candidates presented after a shader commit, and rays 16 and 17 after both kinds of commit. One more ray had candidates presented after an automatic opaque commit only. On WARP alone, the test counts a first-hit ray with candidates after either kind of commit as `warp_after_commit` and does not fail on it, provided the final hit is committable and every other check on the ray passes. A committable hit is opaque or accepted by the shader. Every other category still fails on WARP, and hardware stays fully strict.

On an AMD RDNA 4 GPU, a run reported by denn0x in the review of #216 failed `dxr_opacity_hardware`. Every mismatch was a shader-committed hit with the correct instance and geometry, but its `CommittedRayT()` equalled the true distance plus TMin. The specification makes a committed non-opaque triangle the closest hit at the candidate's distance, and `CandidateTriangleRayT` and `CommittedRayT` both measure the distance from the ray origin, not from TMin. [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow) · [CommittedRayT](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#rayquery-committedrayt) · [CandidateTriangleRayT](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#rayquery-candidatetrianglerayt) All 126 rays use TMin 0.25, so the offset is visible at every hit distance. On the RX 9070 XT with driver 32.0.32015.7029 the reporter observed 43 affected rays. Each such ray increments both `committed_t_plus_tmin` and the committed-hit mismatch count, and the device test fails. The diagnostic identifies shader-committed hits with the expected identity and candidate distance whose committed distance differs by TMin. The same tolerance applies on every adapter. A synthetic control adds 0.25 to a known hit distance and requires the committed-hit check to reject it.

```powershell
ctest --preset dev --no-tests=error -R '^dxr_opacity_' -V
```

### Desktop checks for the query tail and root flags

On x64 Windows, `desktop_memory_query` observed a 48-byte write from `VirtualQueryEx`. State and protection occupy offsets `0x20` and `0x24`. All 16 trailing sentinel bytes survived. With the caller's zero initialization, even a committed read/write page fails the tail check. The test supplies seven tail bytes after the query and compares the four callers' two predicate shapes on reserved, committed read/write, read-only and no-access pages. It also checks the zero-return path.

`bvh_root_roundtrip` uses a source-built compute shader with a v1.1 root signature. The desktop serializer rejected the added `0x03000000` bits with `E_INVALIDARG`. Clearing just those bits and serializing, decoding and serializing again produced the same bytes as the original control. RTX 2060 and WARP both created the root signature and pipeline from the control shader and recovered signature. Each dispatch matched 257 CPU reference words and preserved 63 guard words. This establishes the desktop behavior for that shader. The two flags' Scarlett semantics remain open.

`desktop_bvh_root_flags` asserts the same three cases with a synthetic v1.1 descriptor and no local inputs: baseline flags serialize, while `0x01000000`, `0x02000000` and the pair each return `E_INVALIDARG` with no blob produced. The suite therefore fails if a future SDK or driver starts accepting either console bit on its own.

### Creating root signatures on a second workstation

The second maintainer workstation ran Windows 26200.9974 with SDK 10.0.26100.0, an RTX 4060 Laptop GPU (driver 32.0.16.1656) and WARP (10.0.26100.9818). At commit `127fa86`, both backends passed 408 differential ray queries with the debug layer enabled. Forced native wave64 skipped on both. The hardware reported 32 native lanes and WARP reported 4. [Hardware report](https://github.com/R0Y00/Xbox2Windows/issues/1#issuecomment-7058907095)

The device probe groups containers in each DLL by the SHA-256 of their serialized root signature (`RTS0`). For one representative per group it deserializes the v1.1 descriptor, changes the flags in a memory copy, and records the serializer HRESULT. For each successful serialization it calls `CreateRootSignature` on each selected device and records that HRESULT separately.

| Local input | Containers / distinct layouts | Serializer and device results |
|---|---:|---|
| BVH builder | 52 / 16 | All 16 returned `E_INVALIDARG` in each of three cases: original flags, clear only `0x01000000`, or clear only `0x02000000`. Clearing both returned `S_OK` for serialization and device creation on both backends for every layout. |
| AMD upscaler | 29 / 3 | All three original layouts returned `S_OK` for serialization and device creation on both backends. |

The observed boundary is root signature creation. The desktop DXIL validator still rejected all 52 BVH programs, and the 29 upscaler containers contained no DXIL. An open binding test is the BVH layout shared by seven containers, including the one at RVA `0x99d60`. A source-built dispatch with CPU-checked readback would extend coverage beyond device creation for this layout.

After the first build with the Python dependencies, run the two source-built controls on WARP:

```powershell
ctest --preset dev -R '^root_device_controls_warp$' -V
```

With local inputs, generate the call inventory first. The runner verifies the DLL hashes and writes per-layout HRESULTs to `out/calls/root-devices.json`. Use `--warp-only` for a software-only report in `out/calls/root-devices-warp.json`.

```powershell
.\tools\dev.ps1
$pkg = 'game-files/<build-directory>'
$python = '.\out\pe-env\Scripts\python.exe'
& $python -B tools/call_evidence.py $pkg
& $python -B tools/probe_root_devices.py $pkg
```

### Dynamic dependencies at startup

Five `GetProcAddress` calls were inspected in the local AMD loader. The `0x10000` descriptor selects the packaged upscaler, whose five exports match. `0x20000` and `0x30000` select `amd_fidelityfx_framegeneration_xbox.dll`, absent from the inspected directory and torrent manifest. The loader checks for an already-loaded module before calling `LoadLibraryA`. Whether the main executable requests frame generation remains unknown. [AMD FidelityFX API](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

The executable intake report's `dependency_paths` gives an explanatory path from the entry to each reachable dependency, preferring ordinary imports and marking delay imports and confirmed AMD branches. The AMD branch data applies only to a SHA-256-matched loader. New inventories include System32 forwarder chains. Older inventories provide first-hop evidence only. Dynamic lookups and execution order remain open. With DLLs alone, trace from each one:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/dependency_paths.py out/dependencies/pre-exe.json |
    Set-Content out/dependencies/pre-exe-paths.json -Encoding utf8
```

The conditional FidelityFX routes are pinned to one loader hash. When a reachable image has the loader's basename but a different SHA-256, the report adds an `unverified_loader_hash` finding that names both hashes, instead of returning no conditional edge and exiting successfully. `dependency_paths_controls` checks the pinned hash and a changed one with a synthetic inventory.

```powershell
.\out\pe-env\Scripts\python.exe -B tests/dependency_paths_check.py
```

To rehearse intake with the locally built candidate, keep its report in a separate directory:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py out/build/dev/intake_candidate.exe --reports out/intake-rehearsal
```

The tool reads the candidate as a file and uses the manifest slot as its graph name. The local rehearsal matched the candidate's SHA-256 to the fixture and preserved all 16 baseline DLL hashes. The fixture supplied ordinary imports by name and ordinal from the missing `intake_provider.dll`, a delay import from the missing `intake_delay.dll`, and an ordinary import of `ffxQuery` from the packaged AMD loader. From that loader, descriptor `0x10000` reached the packaged upscaler. Descriptors `0x20000` and `0x30000` reached the absent frame-generation module conditionally. The independent CTest uses a temporary package without that loader and checks that the report omits the frame-generation branch. The game's startup dependencies still require its own entry image.

## Resource chunks and shader formats

### Relocated resource root

Run `python -B tools/resource_root.py <archive> --offset <byte-offset> --stored <compressed-bytes> --decoded <decoded-bytes>` for one known zlib member. Both sizes must be exact and at most 2 MiB. The output contains the selected root's numeric type hash and byte size. The source-built control runs with `ctest --preset dev --no-tests=error -R '^resource_relocated_root$' -V`.

The measured little-endian PRD0 v0x7a header stores a one-based root block index at byte 32, schema and instance directory pointers at bytes 40 and 56, and their counts at bytes 72 and 76. The reader resolves pointers relative to virtual address `0x50000000` within the decoded member. Each 16-byte instance entry gives a type, length and pointer. Exactly one 32-byte schema entry must have the root type, and its declared structure size must equal the root length. The root and both directories must fit in disjoint spans, and the block-map pointer must not fall within any of these extents. This is a profile of the observed relocated records. The section-based script walker below uses separate LSCH and PSIN tables. [Related metadata row layouts and root selection](https://github.com/dexyfex/CodeWalker/blob/485d56bec00262ed7fa472261cce7bbc6202b96e/CodeWalker.Core/GameFiles/MetaTypes/Meta.cs) · [Observed-version header reference](https://github.com/rpatty-dev/PercyStar/blob/1b710c5349ef1911d94092f19173e9813dfb300c/src/anim.rs#L180-L219)

Three 8 MiB windows per archive (start, middle, end) yielded 556 verified roots of 352 bytes in an animation archive, 189 of 304 bytes in a navigation archive and 43 of 192 bytes in a pack archive. Each archive had a distinct root type across these samples. A further 102 marked members lacked a required in-range extent. The marker alone cannot identify an animation class or establish clip counts or playback behavior.

### State-linked settings in a script archive

`python -B tools/script_state_probe.py path/to/script.rpf --state <label>` checks aligned zlib pages, section lengths through CHKS, schema and array extents, and parent-to-child attribute ranges, and rejects a state node with more than one label-kind `id` attribute or with an `id` attribute of any other kind, as the replay and the native path host do. It emits aggregate counts and at most one matching example. The supported profile has five root arrays. Other layouts and entries outside the recognized zlib framing require separate inspection.

In the local base script archive, 532 children of state nodes carried orbit distance, heading and pitch bounds, blend duration and priority together. Reformatting the stored integers, floats and vectors as decimal strings reproduced all five saved value hashes for each child. This establishes authored camera settings associated with states. Activation conditions, time units, interpolation and native calls still need execution evidence. The source-built regression changes a scalar while preserving its saved hash and separately introduces a parent cycle. Further synthetic trees cover a non-state parent, a missing camera attribute, a root with a parent, a state label that selects nothing, and integer and boolean `id` attributes whose hash equals the label. Such an `id` makes the record malformed, matching `resolve_script_path` (-3) and the `script_dispatch_pairs` regression. [CodeWalker field-name reference](https://github.com/dexyfex/CodeWalker/blob/master/CodeWalker.Core/GameFiles/MetaTypes/MetaNames.cs)

The Windows path replay locates a record by an anchor state ID. It consumes that record's node and attribute rows in memory. After building with the dev preset, run this command with the local base archive:

```powershell
python -B tools/script_path_replay.py path/to/script.rpf --host out/build/dev/script_path_host.dll --record-with bumper_f motorbike ./bumper_f ../bumper_r ../bumper_f car/bumper_f
```

Bare paths begin at the record's `states` container. A leading `./` starts at the active node and `../` moves to its parent. Each input is supplied by the experiment. The native host walks the stored first-child and sibling links, checks reverse links and selects a unique matching child ID. It reports a state whose `id` attribute is not label-kind as malformed; the replay rejects such a record before calling the host. A second lookup uses parent indices to verify the resulting node. A missing or ambiguous input leaves the test host at its last selection. The local motorbike record yields distinct front and rear camera bounds and restores the original bounds on returning to the front. The runtime event that supplies the state ID remains unresolved.

### One synthetic BCPack mode-6 block

The [fixture supplied in #442](https://github.com/R0Y00/Xbox2Windows/issues/442) pins 104 packed bytes and the expected 16-byte BC7 block in `tests/bcpack_mode6_vector.py`. The optional [Leonida reference decoder](https://github.com/timelery/Leonida/tree/03d1e118a676d73aba66b1793cf033f6ad23b2b2/tools) reproduces all 16 bytes in strict mode with an empty provisional set. To repeat the packed-byte comparison, point `BCPACK_REFERENCE_TOOLS` at that revision's local tools directory and run `python -B tests/bc7_blocks_check.py -v`. The normal suite requires no external decoder.

The CPU block test and the D3D12 BC7 draw tests check all 16 RGBA texels against fixed expected bytes. Changing the last selector from 15 to 0 selects the opposite endpoint only for the final texel. This covers a single zero-history synthetic vector. The local BCPack probe does not decode arbitrary BC7 streams.

### Two synthetic BCPack mode-6 blocks

`tests/bcpack_mode6_multiblock_vector.py` pins 132 packed bytes containing two distinguishable mode-6 blocks and the expected 32-byte BC7 sequence (8×4 pixels). The optional [Leonida reference decoder](https://github.com/timelery/Leonida/tree/03d1e118a676d73aba66b1793cf033f6ad23b2b2/tools) reproduces both blocks in strict mode with an empty provisional set, and truncated streams fail before upload.

The CPU block check and the D3D12 BC7 draw tests compare all 32 RGBA texels against fixed expected bytes on WARP and hardware. Swapping the two decoded blocks fails GPU readback (`pixel_mismatch x=0 y=0`), and mutating the final selector in block 1 produces an isolated mismatch at pixel `(7, 3)`.

### Exporting two TTPF entries to DDS

These measurements use Leonida's Python exporter on Windows with Python 3.14.2. Both indices are zero-based and refer to the same local `ttdb_ui.ttpf` pool, 845,115,648 bytes, SHA-256 `dc983ed7f6b78d1693fa5757f17c03bafb790e851594fdf4d412579b61d867f6`. The SHA-256 values in the table cover each complete DDS, including its header. Original pixel colours and coverage across the full set remain unverified. [Measurement request](https://github.com/R0Y00/Xbox2Windows/issues/458#issuecomment-7074603775)

| Pool index | BCPack codec / DXGI format | Dimensions / mips | Export mode | Observed result |
|---:|---|---|---|---|
| 0 | 1 / BC1_UNORM (71) | 512 × 512 / 1 | Strict | Exit 0, 131,220 bytes, SHA-256 `cd39c88921eb395ad633b1bd9ee05a160f693bea8b1e85ad8543bc9cd272022a` |
| 8253 | 7 / BC7_UNORM (98) | 512 × 256 / 1 | Strict | Exit 2, `BC7 signature rank 149 is inferred-only (unconfirmed)`, no DDS written |
| 8253 | 7 / BC7_UNORM (98) | 512 × 256 / 1 | Fitted, history revision `3c997c411666b31372b0d2b82afd5ea669b4d774` | Exit 0, 131,220 bytes, SHA-256 `84e7abf023a568056178f6353d8e86e5d848e536e2aa92f1a028266231b1e15f` |

Run the following PowerShell commands from the Xbox2Windows repository root with a fresh output directory. Set `$pool` to the matching local pool. The layout commands report the dimensions, mip count and the codecs used by each mip. [Pinned exporter](https://github.com/ItsNotRexed/Leonida/blob/b0befb4b88d6640354940117448ea19d52507584/tools/ttpf_mips.py) · [Pinned decoder](https://github.com/ItsNotRexed/Leonida/blob/b0befb4b88d6640354940117448ea19d52507584/tools/bcpack.py)

```powershell
$pool = (Resolve-Path 'game-files/ttdb_ui.ttpf').Path
(Get-Item -LiteralPath $pool).Length
Get-FileHash -LiteralPath $pool -Algorithm SHA256
$out = New-Item -ItemType Directory -Path out/ttpf-dds-measurements
git init --quiet "$out/Leonida"
git -C "$out/Leonida" fetch --depth 1 https://github.com/ItsNotRexed/Leonida.git b0befb4b88d6640354940117448ea19d52507584
git -C "$out/Leonida" checkout --detach b0befb4b88d6640354940117448ea19d52507584
$exporter = "$out/Leonida/tools/ttpf_mips.py"
python -I $exporter layout $pool 0
python -I $exporter export $pool 0 "$out/strict-0.dds"
Get-FileHash "$out/strict-0.dds" -Algorithm SHA256
python -I $exporter layout $pool 8253
python -I $exporter export $pool 8253 "$out/strict-8253.dds"
$LASTEXITCODE
Test-Path "$out/strict-8253.dds"
```

For the fitted run, use the same PowerShell session. The pinned calibration module exposes the BC7 endpoint and index history, including which bits were confirmed and which endpoint slots used a last-resort estimate. The commands serialize these fields to the JSON format accepted by the exporter. [Calibration source](https://github.com/timelery/Leonida/blob/3c997c411666b31372b0d2b82afd5ea669b4d774/tools/bcpack_tables_bptc.py)

```powershell
Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/timelery/Leonida/3c997c411666b31372b0d2b82afd5ea669b4d774/tools/bcpack_tables_bptc.py' -OutFile "$out/history_snapshot.py"
@'
import json
import runpy
import sys
from pathlib import Path

out = Path(sys.argv[1])
table = runpy.run_path(str(out / "history_snapshot.py"))["load_bc7"]()
bc7 = {
    "historyEndpoints": table["history_endpoints"],
    "historyEndpointBits": table["history_endpoint_bits"],
    "historyEndpointConfirmedBits": table["history_endpoint_confirmed_bits"],
    "historyIndices": table["history_indices"],
    "historyIndexBits": table["history_index_bits"],
    "historyIndexConfirmedBits": table["history_index_confirmed_bits"],
    "historyEndpointLastResort": sorted(table["last_resort"]),
}
(out / "history.json").write_text(json.dumps({"bc7": bc7}), encoding="utf-8")
'@ | python -I - "$out"
python -I $exporter export $pool 8253 "$out/fitted-8253.dds" --allow-provisional --history "$out/history.json"
Get-FileHash "$out/fitted-8253.dds" -Algorithm SHA256
```

### Drawing BC7 textures with a recompiled pixel shader

`linearize_4k_s_tail(tile, width, height, tail_level)` reads a single BC7 level from a shared GFX10 4 KiB mip-tail tile. Supply the decoded tile, the requested pixel dimensions and its zero-based position within the tail. Whole-tile storage and per-slot bounds are checked before reading. [AMD mip-tail address calculation](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10addrlib.cpp)

To test the published fixed-symbol rANS hypothesis against an isolated BCPack prefix, run `python -B tools/bcpack_probe.py packet.bin --blocks N` with an independently established block count for that stream. A chunk can contain several streams. A header match checks the predicted byte budget and initial state. A lane that reaches the expected terminal state without refills reports `verified_constant_lane`. When the supplied read masks assign every required refill byte, the probe decodes all symbols using those bytes. A result of `verified_constant_lane_with_supplied_refills` confirms that the lane reaches its terminal state under the proposed assignment. The zero-based `refill_symbol_indices` identify the symbols that consumed the bytes. An empty result leaves the lane models unresolved. This probe produces diagnostics only. [Microsoft BCPack modes](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/dstorage/enums/dstorage_bcpack_mode)

`tools/bc7_tiles.py` converts one padded GFX10 `SW_4K_S` surface of 16-byte BC7 blocks to desktop block rows. The caller supplies the pixel dimensions and the bytes of one mip level. Each 4 KiB tile contains 16 × 16 BC7 blocks. The address equation applies to the standard non-XOR mode. [AMD GFX10 swizzle tables](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10SwizzlePattern.h)

The desktop check uploads those rows as `DXGI_FORMAT_BC7_UNORM`, runs the translated copy shader, and checks its floating-point readback against CPU-decoded RGBA values after half-precision export. Its case file starts with `BC7C` and two little-endian 32-bit pixel dimensions, followed by tightly packed BC7 block rows and one reference RGBA8 value per texel. The base texture dimensions must be divisible by four. Each resident mip uses its logical dimensions, up to 4096 on either axis and 4 Mi pixels total, with one reference value per texel. Even a 1×1 mip occupies a complete 16-byte BC7 block.

```powershell
ctest --preset dev -R '^bc7_draw_' -V
.\out\build\dev\offscreen_check.exe hardware --check-bc7 copy.cso base.case capture.rgba
.\out\build\dev\offscreen_check.exe hardware --check-bc7-mip1 copy.cso base.case mip1.case
.\out\build\dev\offscreen_check.exe hardware --check-bc7-tail copy.cso 3 mip3.case mip4.case mip5.case mip6.case mip7.case
```

The tail check uses one logical texture per draw. For a tail whose resident width or height has already reached one texel, specify the original logical dimensions with `--base-size WIDTH HEIGHT` immediately after the first resident mip index. Without this option the command shifts both first-resident dimensions left by that index. For a magnified fallback pixel exactly on a source texel boundary, the readback must match one complete neighboring RGBA texel. The same is accepted, per axis, for the next texel when the pixel center lies up to 1/512 texel below a boundary, tie included. The exact texel is compared first, the last texel has no such neighbor under clamp addressing, and a non-magnified draw accepts no neighbor. Observed: on the Windows 10 in-box WARP 10.0.19041.5794, drawing the 2×1 mip of a 4096×4 texture returned the second texel at 16 pixels of the 4096×4 target (x = 2044 to 2047 in each row), 4 of 2048×2 (x = 1022 and 1023), 1 of 1024×1 (x = 511) and 1 of 512×1 (x = 255), and at none of the smaller targets. The 4×4096 texture gave the same counts along y, as did the same texels uploaded as `R8G8B8A8_UNORM`. WARP 1.0.9 through 1.0.22 and an RTX 3060 on driver 596.36 returned the first texel at all of these pixels. Inferred: these positions are exactly those where rounding the scaled coordinate to the nearest multiple of 1/256 texel, ties upward, reaches the boundary. `D3D12_SUBTEXEL_FRACTIONAL_BIT_COUNT` in `d3d12.h` is 8, which fits this model but does not show how that WARP is implemented. Controls replace single pixels with a texel fetched at its center, so they give the same readback on every adapter: the next texel passes at pixel 2044 of 4096 and at the tie, pixel 255 of 512, and fails at pixels 2043 and 254. A mix of channels from both texels at pixel 2044, the previous texel at pixel 2048 and the first texel at the clamped edge pixel 4095 also fail. It uploads all supplied consecutive levels at their original subresource indices and sets the SRV's `MostDetailedMip` to the first supplied level. The command above takes a 64×64 first resident level and tests viewports from 512×512 down to 4×4. Point sampling at the three largest sizes must reproduce magnified pixels from the 64×64 reference. Each smaller viewport must match its own reference. The test uses committed resources and measures sampling correctness.

To check a proposed first read round in the BCPack probe, append `--prefetch-mask MASK`. Set bit *i* for each of the first eight lanes expected to read a byte in that round. The probe uses the proposed lane order to locate the byte and compares it with the last byte emitted by the reverse encoder. A matching first byte tests a single refill prediction. `--next-mask MASK` supplies the immediately following round in the same way. Each constant lane takes its next refill byte in every round in which its bit is set, so a lane that skips the first round takes its first refill byte from the second. A read beyond the lane's header budget is listed under `unbudgeted_read_offsets` instead of being ignored. Refill positions after these two rounds and the assignment of BC7 block fields require separate evidence.

The default header layout is interleaved at an eight-byte stride. Pass `--lane-stride 6` to read eight contiguous six-byte records, as proposed for BC7 BCPack streams in [#15](https://github.com/R0Y00/Xbox2Windows/issues/15). `--scan-lanes` prints each lane's mode, budget and state bytes without claiming a decode. The option changes only how the 48-byte header is read; the constant-lane model, the variable BC7 lane models and an independently known BC7 block remain separate questions.

`tools/bc7_blocks.py` supplies independently known BC7 blocks for [issue #15](https://github.com/R0Y00/Xbox2Windows/issues/15). `layout(mode, partition)` lists the name, first bit and width of every field in a 16-byte block, with bit 0 as the low bit of the first byte. `unpack(block)` and `pack(fields)` convert between a block and those named values, and `decode(block)` returns sixteen RGBA8 texels. Every bit of the eight valid modes belongs to one field, so `pack(unpack(block))` reproduces any such block. `fixture()` builds a 64×128 surface of 512 blocks. Each field value comes from a SHA-256 digest of the mode, case number and field name. It cycles through the modes and covers every partition, rotation and index selection value. The module contains no BCPack logic. It gives a candidate lane-to-field mapping the complete block rows and field values to compare against. [Microsoft BC7 format](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format)

```powershell
ctest --preset dev --no-tests=error -R '^bc7_block_reference$' -V
python -B tools/bc7_blocks.py out/bc7_fields.case --fields out/bc7_fields.json
```

The test compares the first bit of each field group with offsets transcribed from the published mode layouts, checks that every anchor texel lies in its subset, and decodes three blocks whose texels are worked out by hand in the test comments. It also decodes the mode-6 blocks of the draw fixture, which builds its own reference texels, and pins SHA-256 digests of the generated block rows and texels. When the optional `texture2ddecoder` package is installed, the test additionally compares 16,897 decoded blocks with that decoder. This comparison is skipped otherwise. The command above writes the fixture in the `BC7C` case layout. The `bc7_draw_warp` and `bc7_draw_hardware` checks also draw the 512-block fixture. WARP 10.0.26100.9818 and an RTX 5080 on driver 32.0.16.1692 each matched the CPU reference across 8,192 texels and 32,768 channels. Flipping the rotation bit of a mode-4 block while retaining the original reference produced the first mismatch at (16, 0).

The optional capture contains verified, top-down RGBA8 pixels converted from the GPU readback. The mip test binds both levels in one texture and renders to a half-size target so implicit derivatives select the lower mip. An original mode-6 fixture varies endpoints by block and indices by texel. The negative control shifts only the lower level's compressed blocks and requires a pixel mismatch. [Microsoft BC7 mode reference](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format-mode-reference)

### Decoding MDU rANS symbol streams

`tools/bcpack_symbols.py` decodes an MDU entropy stream into per-lane integer
symbols, consumed byte count, clock count and per-lane fragment/final-state
diagnostics. Supply independently established symbol widths and a symbol count
per lane. The implementation uses the public cycle-level entropy reference from
[bcpack-rs by Ahmet Dedeler](https://github.com/Ahmet-Dedeler/bcpack-rs/tree/5bf5cc56ed56f1ababf1f9592300bd182ff90bd8),
with Microsoft's MIT-licensed model tables and per-lane decoder semantics.
[Source revision, extraction and licenses](../tools/_bcpack_rans/README.md).

```powershell
python -B tools/bcpack_symbols.py stream.bin --alphabet-bits 8 8 5 6 5 5 6 5 8 8 8 8 --symbols 64
ctest --preset dev --no-tests=error -R '^bcpack_rans_symbols$' -V
```

The widths above describe the reference's twelve BC1 lanes; they are actual bit
widths, not zero-based hardware alphabet indices. The wrapper accepts 1–16 lanes,
widths of 1–9 bits, at most 1,048,576 total symbols and a nonempty input of at most
2 MiB. It reads a bounded stream, handles changing model headers, alphabet
narrowing, fragment flushes (restoring at most two omitted low zero bytes), long
fragment lengths and shared-stream refills with the reference's eight-byte feed window and lockstep symbol outputs. All lanes
must terminate legally and produce exactly the requested count. Zero padding
may follow a completed stream; non-zero trailing bytes fail. Truncated streams,
undefined alphabet selections and illegal terminal states also fail.

Run `python -B tests/bcpack_symbols_check.py` on Linux or Windows without extra
packages. The controls compare all 9,168 symbols of two unchanged, Rust-generated
synthetic streams (12 × 64 and 12 × 700), including multiple fragments and more
lanes than the feed window. A separate reverse-equation encoder checks all 144
alphabet/model combinations, long headers, narrowed alphabets and flushed
fragments. Table checks pin the upstream canonical digest and test inverse
interval boundaries. Rejection controls cover truncation, trailing bytes, wrong
counts, invalid headers, terminal states and argument limits. The existing
mode-6 packet from [#442](https://github.com/R0Y00/Xbox2Windows/issues/442) also verifies all 32 symbols across 16 lanes and consumes its full 104 bytes.
Those expected symbols were cross-checked with
[Leonida revision 6a6704d](https://github.com/timelery/Leonida/tree/6a6704df1592e1dc4a3215ddf1423a78adb52045),
which reconstructs the known BC7
block exactly without provisional entries. Our test checks the entropy symbols
only. That case requires the bounded flush-zero extension to the vendored
reference; it fails without it. The CLI has a successful JSON output case and
a nonzero-exit diagnostic case.

Observed on Linux with Python 3.14.4: all eight entropy test methods passed.
On Windows 11 build 26300.9974 with Python 3.12.8, the CTest selection
`^bcpack_(rans_symbols|prefix_and_mip_tail)$` passed 2/2 tests after configuring
with `INTAKE_PYTHON` pointing to that interpreter. These checks use no GPU.

This adds entropy decoding. BC1 endpoint/codebook reconstruction, BC7 lane-to-field
mapping, archive stream discovery and rendered pixels remain separate work.
The existing prefix probe keeps its diagnostic behavior. No local archive or
texture payload is used by these checks.

### Reading logical ranges and probing structured resources

`tools/paged_resource.py` reads a logical byte range from an open archive and a supplied segment map. Each segment gives a physical offset, stored byte count excluding padding, decoded byte count, and codec. Raw and zlib segments may be mixed. The reader verifies the complete compressed stream and decoded size before returning bytes from it. Reads that touch an unknown codec fail. Segments after it remain independently addressable.

Run `python -B tools/texture_descriptors.py <archive.ttpf>` to group descriptors in the first raw ETPF table by width, height, DXGI format number and mip-marker byte. The reader checks stored and logical bounds and reads up to 1 MiB of descriptors in batches of at most 4 KiB. It leaves texture payloads unread. The marker alone does not identify a resident mip. [Published descriptor layout](https://github.com/rpatty-dev/PercyStar/blob/1b710c5349ef1911d94092f19173e9813dfb300c/src/tex.rs#L748-L785)

Add `--raw-page-map INDEX` to report the logical and file offsets of a zero-based descriptor's first 4 KiB page. The interval must be raw. Equal stored and logical lengths require no padding check. For unequal lengths, the excess stored bytes must match the 12-bit trailing-byte count from the starting boundary. The command reads this suffix (up to 4,095 bytes) and verifies that each byte is zero. The command checks that boundary positions increase strictly, reads the boundary table in batches of at most 4 KiB, and accepts tables up to 1 MiB. It reads no texture payload.

Use `--zlib-segment INDEX` for a descriptor in a codec-8 interval. This mode reads and decompresses the selected interval, limiting both stored and decoded sizes to 1 MiB. It verifies the zlib checksum and end of stream, checks the decoded length against the declared extent, and requires any trailing bytes to be zero. The report contains the fields for a `Segment`, its logical start, and the descriptor page's offset within it. Decoded bytes are not included in the report.

Run `python -B tools/texture_descriptors.py --lookup-key 0x0123456789abcdef first.ttpf second.ttpf` to search the parallel little-endian 64-bit key tables in ETPF version 0x27a. Each match lists the archive path and zero-based descriptor index, in input order and then index order. Repeated keys produce separate matches. An absent key produces an empty list. The reader scans at most 512 KiB of keys per archive in batches of up to 4 KiB and leaves descriptor bodies and texture payloads unread. [Key table layout](https://github.com/holdmysocks/SixNG/blob/2f672555819f96993adeed3273653ad53eeb1d81/crates/sixng-core/src/ttpf.rs#L112-L123)

Use `python -B tools/texture_descriptors.py --descriptor INDEX archive.ttpf` to inspect an individual entry, for example after `--lookup-key`. The JSON gives the zero-based index, width, height, numeric DXGI format, mip-marker byte and logical offset of the first 4 KiB page. The command reads the bounded headers and the selected 16-byte record. It leaves other descriptors, key tables and texture payloads unread.

`tools/section_probe.py` samples up to 8 MiB at the start, middle and end of each input. It looks for 16-byte-aligned zlib streams, with stored and decoded sizes capped at 256 KiB. A section record must start and end with the requested tags, and its big-endian section sizes must exactly cover the decoded buffer. Streams cut off by a window or size cap are omitted. Counts establish record boundaries within sampled regions. Interpreting payload fields or validating a checksum inside a section requires its format.

```powershell
python -B tools/section_probe.py --first PSIN --last CHKS "game-files/<build-directory>/levels.rpf"
ctest --preset dev -R resource_range_controls -V
```

### AWC sound-effect format metadata

Run `python -B tools/awc_metadata.py <archive> --offset <byte-offset> --size <byte-count>` with a known ADAT bank range. The offset and size accept decimal or `0x` hexadecimal integers. The tool reads the stream table and each stream's tag descriptors, checks their extents against the supplied range, and reads the first 32 bytes of each format tag. Its JSON groups streams by numeric codec ID and sample rate. For sound-effect banks, the sample count in each group is the sum of the counts in its format tags. Container discovery and playback require separate evidence.

The reader supports little-endian version 1 sound-effect banks with flag words `0xff000001` or `0xff020001`, at most 4,096 streams, and nonoverlapping tags. Each stream must have one nonempty data tag (`0x55`) and one format tag (`0x76`). The stream's high three bits give its tag count. Each 64-bit tag descriptor encodes its type, byte count and bank-relative offset. [Public AWC format reader](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c)

Three locally inspected banks contained 563, 603 and 176 streams. All 1,342 format tags recorded 48,000 Hz and codec ID 11. The source-built fixture uses two rates and two codec IDs, verifies grouping when format and audio tags are out of physical order, and fails on any read outside the metadata. Run `ctest --preset dev --no-tests=error -R '^audio_awc_metadata$' -V`.

The same command also reads streamed (music) banks. The public reader treats a bank as streamed when the first stream ID is 0 ([`awc.c` line 379](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L379)). That stream's music header tag (`0x48`) gives the block count, a `block_chunk` value and one 16-byte record per channel with its ID, sample count, sample rate and codec ID ([lines 434–486](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L434-L486)). The JSON adds `"layout": "music"` and a `music` object with `channels`, `block_count` and `block_chunk`. Here `stream_count` counts stream-table entries, including the ID-0 stream. Each format group counts channels with the same rate and codec, and its `samples` field is the largest channel sample count in that group. The public reader keeps one maximum over all channels instead ([lines 480–481](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L480-L481)). The public reader assigns `block_chunk` to its `full_block_size` (lines [92](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L92) and [195](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L195)). Its meaning was not measured here, and the tool does not compare it with the data tag. The tool reads the header, stream table, the ID-0 stream's tag descriptors and the music header. It does not bound or read the other streams' tags. The public reader takes the music metadata only from the ID-0 stream's tags ([lines 414–422](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L414-L422)). For Vorbis music banks (codec ID 8) it also reads the other streams' `0x7F` setup tags to decode audio ([lines 564–595](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L564-L595)), which an inventory does not need. It notes that some banks carry a useless sound-effect header or a separate cloned data chunk on the other streams ([lines 349–377](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L349-L377)).

Music banks must use the flag words `0xff000001` or `0xff020001` and have at most 32 stream-table entries, the limit the public reader applies to streamed banks ([lines 384–387](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L384-L387), [`AWC_MAX_MUSIC_CHANNELS`](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc_streamfile.h#L6)). The ID-0 stream needs exactly one music header and one nonempty data tag, its tags, including empty ones, must lie between the declared header size and the end of the supplied range, and they must not overlap. The bank must declare 1 to 32 channels, a music header of at least 12 bytes plus 16 bytes per channel, a nonzero block count and a nonzero `block_chunk`, and all channels must share one codec ID. These rules are stricter than the public reader in the following ways: its header parser ignores the declared header size and does not check for a tag that extends past the supplied range, more than one music header or data tag (it keeps the last one), a repeated ID 0, an empty data tag (it rejects only a missing data offset, [lines 597–600](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L597-L600)), overlapping tags, a channel count of 0, a music header shorter than 12 bytes or than its channel records, a zero block count or a zero `block_chunk`, and skips the codec comparison for a codec ID of 0 ([line 475](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L475)). The public reader also accepts the flag bits `0x00010000`, `0x00040000` and `0x00080000` ([lines 328–346](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L328-L346)), which this tool rejects. In one respect the tool is more lenient: the public reader rejects `0x76` and `0x7F` tags on the ID-0 stream of a streamed bank ([lines 508–534](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L508-L534)), while this tool only bounds them and does not read them. Banks outside these rules fail with a specific message. No local music bank was inspected for this change. The source-built fixture has four channels at two rates, more channels than stream-table entries and ignored tags on other streams. The tests accept the 32-stream and 32-channel limits and tags at the range boundaries, and include a negative control for each rule and for each rejected flag bit. Run `ctest --preset dev --no-tests=error -R '^audio_awc_metadata$' -V`.

### Source-built binding, wave and texture checks

`dual_uav` dispatches through two root UAVs with a count supplied as a root constant. The CPU checks the input, active output and unwritten tail over seven counts. Decreasing the count from 129 to 128 leaves word 128 untouched. The wave tests compare a full-group mirror with a half-local mirror at lanes 31 and 32 and check an inclusive sum across both halves. With native wave64 support they also check a native mirror using the lane assignment returned by the GPU.

`texture_mips` writes an original RGBA8 checker pattern in padded 4×4 Morton tiles, decodes it into the device's copy footprints and point-samples every texel through a texture SRV. It checks all 148 texels of a 13×9 texture and its three smaller mips, plus a guard word after each level. Morton order is the synthetic input convention. Establishing a particular title's texture format and addressing mode requires separate evidence.

```powershell
ctest --preset dev -R 'dual_uav|texture_mips|bvh_waves|bvh_wave64' -V
```

### Optional compressed-record inventory

Run the following after building the native validator. The optional pass scans named records in the shader archive, decodes one raw-DEFLATE stream at a time and writes only aggregate metadata to the resource report. A candidate must fit the file, have matching stored sizes, reach the stream end, match the declared output length and contain a bounded DXBC section table with a DXIL program header. The pass caps stored and decoded spans at 8 MiB and 16 MiB. It records shader stages, section counts, LLVM bitcode framing, unused bytes at stream end and rejection reasons. The native validator samples the first accepted container per stage. These are content-based counts and depend on the recognized record format. [External record-format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/shader_carve.py)

Use `tools/extract_shaders.py --length-prefixed-names` with archives whose record names begin with a one-byte length that includes the NUL terminator. A printable length byte can also be a valid first character of a prefixless name. The extractor keeps the full printable run by default.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --scene-records --scene-validator out/build/dev/root_probe.exe
ctest --preset dev -R 'compressed_scene_metadata|static_exe_intake|desktop_memory_query|era_heap_failure' -V
```

The synthetic intake test compares the candidate's direct unresolved imports against a DLL baseline and against a second baseline with a peer caller. Each `entry_import_gaps` item records ordinary or delayed binding and whether the same module/symbol pair was already unresolved. Transitive and conditional routes remain in `dependency_paths`. The memory tests check a real one-byte Windows query failure and unsupported heap attribute bits in combination with a supported alignment.

### Raw archive scan

After building, use the source-built pixel shader as a known DXIL control.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --shader-control out/build/dev/triangle_ps.cso
```

`out/resources/<build-directory>.json` records install chunks, flags, byte counts and missing layout destinations. It records file-header signatures for the build and scans all raw bytes in `appdata0.rpf`, `common.rpf` and `shaders.rpf` for format markers. Candidate DXBC containers are checked for length, section boundaries and DXIL program headers. The synthetic `dxcontainer_part_offsets` regression verifies that part-table entries can be enumerated independently of the physical layout, while rejecting overlapping, out-of-bounds, or duplicate-FourCC part ranges. Both the part map and DXIL scanner reject repeated FourCC tags even when those parts are physically separate; the regression includes a valid framed DXIL control to distinguish duplicate tags from missing shader payloads. It checks structural framing, not shader execution. `sample_streams` covers at most eight qualifying zlib streams per file within the first 4 MiB. Plaintext stays in memory. The sample cannot characterize the rest of the archive. Installation order and runtime I/O need independent checks. [DXIL container format](https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst) · [RPF9 observation source](https://gtamodx.com/columns/eXk_fiBUaYta)

### Ballot bit extraction in the named SPIR-V bridge

The HLSL bridge lowers a subgroup ballot read to a dynamic DWORD selection and a bit
test in that word. The source-built fixture supplies four distinguishable words and
reads all 128 positions in the resulting logical vector. This checks the mask layout
independently of the desktop device's wave size. On RTX 4060 Laptop and WARP, both
readbacks matched 128 output words and 192 guards. Exchanging expected positions
31 and 32 produced exactly two mismatches on each adapter.

With SPIRV-Tools and SPIRV-Cross available at configure time, run
`ctest --preset dev --no-tests=error -R spirv_ballot_extract -V`.

### Ordinary and comparison sampler views in the SPIR-V bridge

The named profile can use one indexed sampler table for ordinary samples and
depth-reference operations. In mixed modules SPIRV-Cross infers one comparison
type for that table. The bridge keeps ordinary accesses at set 0, binding 1.
For each depth-reference use it loads the same descriptor index from set 3,
binding 1. The host must populate both views from the guest sampler descriptors,
using ordinary filters in the first and comparison filters and the guest
comparison function in the second. Both operations retain their texture and
coordinates. Set 3 must be free. The supported path has a direct access chain,
sampler load and sampled image. Decorated sampler paths require separate support.

With SPIRV-Tools and SPIRV-Cross installed, run:

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_sampler_views_' -V
```

The fixture compiles both shaders at Shader Model 6.0 and dispatches the compute
shader for readback. The compiled pixel shader uses implicit LOD and the
compute shader uses explicit LOD. Both compare against a depth reference.

On Windows 10 build 19045.6456, the GTX 1060 (32.0.15.6094) and in-box WARP
(10.0.19041.5794) matched all four readback words in each of the three sampler
orders. Sampling returned 0.25 and 0.75, and comparison against 0.5 returned 0
and 1. Reversing either pair of wrap/clamp descriptors exchanged only that
pair's results. App-local WARP 1.0.22 produced identical results with the
SM 6.0 and SM 6.6 compute shaders. All these runs reported zero D3D12 warnings
and errors. The original SM 6.6 readback on RTX 4060 Laptop and WARP produced
the same values.

### Standalone comparison-sampler split in the SPIR-V bridge

When one standalone SPIR-V sampler variable feeds both depth-comparison and ordinary
sampling, SPIRV-Cross declares a `SamplerComparisonState` and passes it to
`SampleLevel`, which DXC rejects with
`no matching member function for call to 'SampleLevel'`. The indexed sampler table
above is handled by the sampler views. For a standalone sampler, the opt-in
`tools/spirv_sampler_split.py` keeps the comparison sampler at its binding and moves
only the ordinary uses to a new sampler at a binding the host supplies with
`--ordinary-binding`. That binding must be free in the same descriptor set. The pass
prints the `descriptor_set`, `comparison_binding` and `ordinary_binding` mapping for
the host.

The pass first looks for a directly loaded standalone sampler that feeds both kinds of
sampling. Modules without one are returned byte-for-byte unchanged. That includes the
indexed table above and unrelated samples on other samplers. For the sampler it splits,
the profile covers direct, non-array float32 `Texture2D` sampling in one compute entry
point at a constant LOD of zero. Other uses of that sampler, an occupied binding, a
nonzero LOD and more than one mixed sampler are rejected with a specific diagnostic.

The [source-built fixture](../tests/spirv_window/shared_sampler.spvasm) compares
references 0.25 and 0.75 against depth 0.5 with point-clamp `LESS_EQUAL`, and
samples an ordinary texel of 0.375. The test first checks that DXC rejects the
unsplit module. It then splits the module to ordinary binding 5, validates it for
Vulkan 1.2, cross-compiles it and compiles it as `cs_6_6`. On RX 9070 XT and WARP,
both readbacks returned 1.0, 0.0 and 0.375 and left all 13 guard words unchanged.
A reference claiming that the second comparison passed produced exactly one
mismatch, at word 1, on each adapter. Two descriptor controls change one static
sampler at a time. Switching s2 to `GREATER` exchanged only the two comparison
results. Clamping the ordinary sampler to `MinLOD` 1 changed only the ordinary
sample, to 0.625 from the value texture's second mip. The occupied-binding,
nonzero-LOD, already-separated and passthrough controls run without a GPU.

```powershell
ctest --preset dev --no-tests=error -R '^spirv_compare_sampler_' -V
```

## Independent bit and byte alignment readback

The `pixel_aot_align_warp` and `pixel_aot_align_hardware` fixtures run dedicated
`v_alignbit_b32` and `v_alignbyte_b32` programs. Both are included in the
source-built binary round trip. Each exports four observable UINT channels; the
variable-count instruction aliases its second source and destination.

Each operation has 24 input/reference cases from three asymmetric word pairs
and eight counts, including zero, bit counts 31 and 32, byte counts 3 and 4,
and wrapped counts. References use Python's 64-bit concatenation and shift.
Input and output words avoid NaN and subnormal encodings so float register
transport cannot obscure the instruction comparison. A wrong expected word is
surrounded by correct cases; a separately compiled shader with reversed inputs
must fail every reference.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_align_(warp|hardware)$' -V
```

Python-only controls check explicit reference words and translator acceptance.
Those controls do not establish GPU execution; the command above must pass on
the named backend before recording readback evidence.

## Independent VOP3 ternary integer ALU readback

The `pixel_aot_vop3_ternary_int_warp` and `pixel_aot_vop3_ternary_int_hardware` fixtures run
dedicated 3-operand integer ALU programs covering 8 instructions:
- `v_add3_u32`: `(src0 + src1 + src2)`
- `v_or3_b32`: `(src0 | src1 | src2)`
- `v_xor3_b32`: `(src0 ^ src1 ^ src2)`
- `v_and_or_b32`: `((src0 & src1) | src2)`
- `v_xad_u32`: `((src0 ^ src1) + src2)`
- `v_lshl_or_b32`: `((src0 << (src1 & 31)) | src2)`
- `v_lshl_add_u32`: `((src0 << (src1 & 31)) + src2)`
- `v_add_lshl_u32`: `((src0 + src1) << (src2 & 31))`

All 8 instructions are included in the source-built binary round trip. Each exports four
observable UINT channels; the fourth instruction in each program aliases its second source
and destination register.

Each group has 30 input/reference cases from three asymmetric word pairs and ten shift/add
counts (0, 1, 2, 3, 4, 8, 16, 24, 31, 32). References use 32-bit integer arithmetic.
Input and output words avoid NaN and subnormal encodings so float register transport
cannot obscure the instruction comparison. A wrong expected word is surrounded by correct
cases; a separately compiled shader with mutated operations must fail every reference.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_vop3_ternary_int_(warp|hardware)$' -V
```

Python-only controls check explicit reference words and translator acceptance.
Those controls do not establish GPU execution; the command above must pass on
the named backend before recording readback evidence.
