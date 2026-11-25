// Desktop xapu.dll trap stubs.
// Every export traps; see trap.h. Scaffolding, not an ABI claim.
#define X2W_TRAP_MODULE "xapu.dll"
#define X2W_TRAP_MODULE_ID 3
#include "startup_stubs/trap.h"
#include "startup_stubs/local_log.h"
#include <timeapi.h>
#include <cstring>
#include <new>

// Experimental (docs/startup-runtime.md). With X2W_AUDIO_PERMISSIVE=1 the exports
// stand in for the audio command queue; otherwise they trap. Call shapes observed:
//   XApuConnect(input*, output*, handle*) -> HRESULT (the handle is later passed to the others)
//   XApuGetResultCount(handle, 0) -> count
//   XApuDequeueResult(handle, result*) -> HRESULT; zeroes 0x58 bytes. With the experimental
//     X2W_XAPU_TYPED_COMPLETIONS=1, also echoes the command type at +0 and slot at +4.
//   XApuEnqueueCommand(handle, command*, ...) -> HRESULT; reads 32-bit type at +0 and slot at +4.
//   XApuDisconnect(handle)
namespace {
using u64 = std::uint64_t;
constexpr long hr_ok = 0;
constexpr long hr_pointer = static_cast<long>(0x80004003);  // E_POINTER
constexpr long hr_handle = static_cast<long>(0x80070006);   // E_HANDLE
constexpr long hr_out_of_memory = static_cast<long>(0x8007000E); // E_OUTOFMEMORY
constexpr long hr_pending_results = static_cast<long>(0x8AC81001); // XAPU_E_PENDING_RESULTS
constexpr std::size_t command_header_size = 8;
constexpr std::size_t result_size = 0x58;
constexpr std::size_t max_queue_entries = 16384;  // 128 KiB cap per connection for undrained commands

bool permissive() noexcept { return x2w_local::env_on("X2W_AUDIO_PERMISSIVE"); }

struct QueueEntry {
    std::uint32_t type = 0;
    std::uint32_t slot = 0;
};

struct Connection {
    Connection* next = nullptr;
    u64 handle = 0;
    QueueEntry* entries = nullptr;
    std::size_t head = 0;
    std::size_t count = 0;
    std::size_t capacity = 0;
};

SRWLOCK connection_lock = SRWLOCK_INIT;
Connection* connections = nullptr;
u64 next_handle = 0x5841500000000001ULL;
bool timer_active = false;

bool push_result(Connection& conn, QueueEntry entry) noexcept
{
    if (conn.count >= max_queue_entries) return false;
    if (conn.count == conn.capacity) {
        const std::size_t doubled = conn.capacity ? conn.capacity * 2 : 64;
        const std::size_t next_cap = doubled > max_queue_entries ? max_queue_entries : doubled;
        auto* next = new (std::nothrow) QueueEntry[next_cap];
        if (!next) return false;
        for (std::size_t i = 0; i < conn.count; ++i) {
            next[i] = conn.entries[(conn.head + i) % conn.capacity];
        }
        delete[] conn.entries;
        conn.entries = next;
        conn.head = 0;
        conn.capacity = next_cap;
    }
    conn.entries[(conn.head + conn.count) % conn.capacity] = entry;
    ++conn.count;
    return true;
}

QueueEntry pop_result(Connection& conn) noexcept
{
    const QueueEntry entry = conn.entries[conn.head];
    --conn.count;
    if (conn.count == 0) {
        conn.head = 0;
    } else {
        conn.head = (conn.head + 1) % conn.capacity;
    }
    return entry;
}

Connection* find_connection(u64 handle) noexcept
{
    for (auto* conn = connections; conn; conn = conn->next) {
        if (conn->handle == handle) return conn;
    }
    return nullptr;
}

void hex(const void* data, std::size_t bytes, char* out, std::size_t size)
{
    out[0] = 0;
    if (!data || !x2w_local::readable(data, bytes)) return;
    const auto* p = static_cast<const unsigned char*>(data);
    std::size_t used = 0;
    for (std::size_t i = 0; i < bytes && used + 4 < size; ++i) used += std::snprintf(out + used, size - used, "%02x ", p[i]);
}
}  // namespace

extern "C" __declspec(noinline) long XApuConnect(u64 rcx, u64 rdx, u64 r8, u64 r9)
{
    if (!permissive())
        x2w_trap::fire(X2W_TRAP_MODULE, X2W_TRAP_MODULE_ID, "XApuConnect", 0, rcx, rdx, r8, r9, _ReturnAddress());
    char input[3 * 0x18 + 1], output[3 * 0x10 + 1];
    hex(reinterpret_cast<const void*>(rcx), 0x18, input, sizeof(input));
    hex(reinterpret_cast<const void*>(rdx), 0x10, output, sizeof(output));
    if (!r8 || !x2w_local::writable(reinterpret_cast<const void*>(r8), sizeof(void*))) {
        x2w_local::logf("x2w-audio XApuConnect input=[%s] output(before)=[%s] -> E_POINTER", input, output);
        return hr_pointer;
    }
    auto* conn = new (std::nothrow) Connection;
    if (!conn) return hr_out_of_memory;
    AcquireSRWLockExclusive(&connection_lock);
    const u64 handle = next_handle++;
    if (next_handle == 0) next_handle = 0x5841500000000001ULL;
    conn->handle = handle;
    conn->next = connections;
    connections = conn;
    if (!conn->next) {
        // The mix worker sleeps for 2 ms between refills. A coarse Windows tick can
        // stretch that wait beyond the time represented by the queued samples.
        timer_active = timeBeginPeriod(1) == TIMERR_NOERROR;
        if (!timer_active) x2w_local::logf("x2w-audio 1 ms mixer timer request unavailable");
    }
    ReleaseSRWLockExclusive(&connection_lock);
    *reinterpret_cast<void**>(r8) = reinterpret_cast<void*>(handle);
    x2w_local::logf("x2w-audio XApuConnect input=[%s] output(before)=[%s] -> handle %p, S_OK", input, output,
                    reinterpret_cast<void*>(handle));
    return hr_ok;
}

extern "C" __declspec(noinline) std::uint32_t XApuGetResultCount(u64 rcx, u64 rdx, u64 r8, u64 r9)
{
    if (!permissive())
        x2w_trap::fire(X2W_TRAP_MODULE, X2W_TRAP_MODULE_ID, "XApuGetResultCount", 4, rcx, rdx, r8, r9, _ReturnAddress());
    AcquireSRWLockShared(&connection_lock);
    const Connection* conn = find_connection(rcx);
    const std::uint32_t count = conn
        ? static_cast<std::uint32_t>(conn->count > 0xFFFFFFFFULL ? 0xFFFFFFFFULL : conn->count)
        : 0u;
    ReleaseSRWLockShared(&connection_lock);
    static LONG calls;
    if (x2w_local::first_calls(calls, 2))
        x2w_local::logf("x2w-audio XApuGetResultCount handle=0x%llx rdx=0x%llx -> %u", rcx, rdx, count);
    return count;
}

extern "C" __declspec(noinline) long XApuDequeueResult(u64 rcx, u64 rdx, u64 r8, u64 r9)
{
    if (!permissive())
        x2w_trap::fire(X2W_TRAP_MODULE, X2W_TRAP_MODULE_ID, "XApuDequeueResult", 1, rcx, rdx, r8, r9, _ReturnAddress());
    if (!rdx || !x2w_local::writable(reinterpret_cast<const void*>(rdx), result_size)) {
        static LONG bad_out_calls;
        if (x2w_local::first_calls(bad_out_calls, 2))
            x2w_local::logf("x2w-audio XApuDequeueResult handle=0x%llx -> E_POINTER", rcx);
        return hr_pointer;
    }
    QueueEntry entry{};
    AcquireSRWLockExclusive(&connection_lock);
    Connection* conn = find_connection(rcx);
    long hr = hr_handle;
    if (conn) {
        if (conn->count > 0) {
            entry = pop_result(*conn);
            hr = hr_ok;
        } else {
            hr = hr_pending_results;
        }
    }
    ReleaseSRWLockExclusive(&connection_lock);
    if (hr == hr_ok) {
        auto* out = reinterpret_cast<unsigned char*>(rdx);
        std::memset(out, 0, result_size);
        if (x2w_local::env_on("X2W_XAPU_TYPED_COMPLETIONS")) {
            std::memcpy(out, &entry.type, sizeof(entry.type));
            std::memcpy(out + 4, &entry.slot, sizeof(entry.slot));
        }
    }
    static LONG calls;
    if (x2w_local::first_calls(calls, 2))
        x2w_local::logf("x2w-audio XApuDequeueResult handle=0x%llx -> 0x%08lx", rcx, static_cast<unsigned long>(hr));
    return hr;
}

extern "C" __declspec(noinline) long XApuEnqueueCommand(u64 rcx, u64 rdx, u64 r8, u64 r9)
{
    if (!permissive())
        x2w_trap::fire(X2W_TRAP_MODULE, X2W_TRAP_MODULE_ID, "XApuEnqueueCommand", 3, rcx, rdx, r8, r9, _ReturnAddress());
    if (!rdx || !x2w_local::readable(reinterpret_cast<const void*>(rdx), command_header_size)) {
        static LONG bad_cmd_calls;
        if (x2w_local::first_calls(bad_cmd_calls, 2))
            x2w_local::logf("x2w-audio XApuEnqueueCommand handle=0x%llx -> E_POINTER", rcx);
        return hr_pointer;
    }
    QueueEntry entry{};
    std::memcpy(&entry.type, reinterpret_cast<const unsigned char*>(rdx), sizeof(entry.type));
    std::memcpy(&entry.slot, reinterpret_cast<const unsigned char*>(rdx) + 4, sizeof(entry.slot));
    AcquireSRWLockExclusive(&connection_lock);
    Connection* conn = find_connection(rcx);
    long hr = hr_handle;
    if (conn) {
        hr = push_result(*conn, entry) ? hr_ok : hr_out_of_memory;
    }
    ReleaseSRWLockExclusive(&connection_lock);
    static LONG calls;
    if (x2w_local::first_calls(calls, 24)) {
        char command[3 * 0x60 + 1];
        hex(reinterpret_cast<const void*>(rdx), 0x60, command, sizeof(command));
        const auto offset = reinterpret_cast<u64>(_ReturnAddress()) - reinterpret_cast<u64>(GetModuleHandleW(nullptr));
        x2w_local::logf("x2w-audio XApuEnqueueCommand handle=0x%llx r8=0x%llx r9=0x%llx ret=+0x%llx command=[%s] -> 0x%08lx",
                        rcx, r8, r9, offset, command, static_cast<unsigned long>(hr));
    }
    return hr;
}

extern "C" __declspec(noinline) long XApuDisconnect(u64 rcx, u64 rdx, u64 r8, u64 r9)
{
    if (!permissive())
        x2w_trap::fire(X2W_TRAP_MODULE, X2W_TRAP_MODULE_ID, "XApuDisconnect", 2, rcx, rdx, r8, r9, _ReturnAddress());
    AcquireSRWLockExclusive(&connection_lock);
    Connection** slot = &connections;
    while (*slot && (*slot)->handle != rcx) slot = &(*slot)->next;
    Connection* conn = *slot;
    if (conn) *slot = conn->next;
    if (conn && !connections && timer_active) {
        timeEndPeriod(1);
        timer_active = false;
    }
    ReleaseSRWLockExclusive(&connection_lock);
    if (!conn) {
        x2w_local::logf("x2w-audio XApuDisconnect handle=0x%llx -> E_HANDLE", rcx);
        return hr_handle;
    }
    delete[] conn->entries;
    delete conn;
    x2w_local::logf("x2w-audio XApuDisconnect handle=0x%llx -> S_OK", rcx);
    return hr_ok;
}
