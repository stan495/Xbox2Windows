// Experimental (docs/startup-runtime.md): which failed Win32 lock requests mean "held elsewhere, try
// again later" (busy) and which are real errors, how the save lock waits and reports a failure, and
// that a reused, released or destroyed lock object frees its lock. The mapping is plain integer
// logic, so it runs on every platform with the codes a Windows node reports; the other cases use real
// lock files in a temporary folder. Synthetic data only.
#include <startup_stubs/file_lock.h>

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <string>
#include <system_error>
#include <thread>

namespace {
int failures;
void expect(bool ok, const char* what)
{
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++failures;
    }
}

bool is_busy_code(std::uint32_t code)
{
#ifdef _WIN32
    return x2w_save::win_lock_error_is_busy(code);
#else
    return code == static_cast<std::uint32_t>(EWOULDBLOCK);
#endif
}

void check_mapping()
{
    using namespace x2w_save;
    expect(win_lock_error_is_busy(33), "ERROR_LOCK_VIOLATION means held elsewhere");
    expect(win_lock_error_is_busy(32), "ERROR_SHARING_VIOLATION means held elsewhere");
    expect(win_lock_error_is_busy(997), "ERROR_IO_PENDING (not granted at once) means held elsewhere");
    expect(!win_lock_error_is_busy(0), "no error is not busy");
    expect(!win_lock_error_is_busy(5), "ERROR_ACCESS_DENIED is a real error");
    expect(!win_lock_error_is_busy(6), "ERROR_INVALID_HANDLE is a real error");
    expect(!win_lock_error_is_busy(87), "ERROR_INVALID_PARAMETER is a real error");
}

// A long wait must not overflow the deadline: the helper frees the lock after about 150 ms, and the
// waiter, given the largest timeout, has to wait for it instead of giving up at once. A watchdog ends
// the process if the wait never returns, so a regression cannot hang the run.
void check_long_wait(const std::filesystem::path& lock_file)
{
    using namespace x2w_save;
    using Clock = std::chrono::steady_clock;
    FileLock holder;
    expect(holder.acquire(lock_file, true, std::chrono::milliseconds(0)) == LockResult::locked, "holder takes a free lock");

    const auto start = Clock::now();
    std::thread releaser([&holder] {
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
        holder.release();
    });
    std::mutex gate;
    std::condition_variable done_cv;
    bool done = false;
    std::thread watchdog([&] {
        std::unique_lock<std::mutex> lock(gate);
        if (!done_cv.wait_for(lock, std::chrono::seconds(10), [&] { return done; })) {
            std::fputs("FAIL: a long wait never returned\n", stderr);
            std::_Exit(2);
        }
    });

    FileLock waiter;
    const LockResult result = waiter.acquire(lock_file, true, (std::chrono::milliseconds::max)());
    const auto waited = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start);
    {
        std::lock_guard<std::mutex> lock(gate);
        done = true;
    }
    done_cv.notify_all();
    watchdog.join();
    releaser.join();

    expect(result == LockResult::locked, "a wait for ever ends locked, not timeout, once the holder lets go");
    expect(waited >= std::chrono::milliseconds(100), "a wait for ever really waits for the holder");
    expect(waited < std::chrono::seconds(5), "a wait for ever ends soon after the holder lets go");
    waiter.release();

    // A timeout at the other end of the range is one attempt: busy at once while held, locked when free.
    FileLock held;
    expect(held.acquire(lock_file, true, std::chrono::milliseconds(0)) == LockResult::locked, "second holder takes the free lock");
    FileLock probe;
    const auto probe_start = Clock::now();
    expect(probe.acquire(lock_file, true, (std::chrono::milliseconds::min)()) == LockResult::timeout,
           "the most negative timeout is one attempt: timeout while held");
    expect(Clock::now() - probe_start < std::chrono::seconds(2), "the most negative timeout does not wait");
    held.release();
    expect(probe.acquire(lock_file, true, (std::chrono::milliseconds::min)()) == LockResult::locked,
           "the most negative timeout still locks a free file");
}

// A failed open names its cause and does not keep the value of an earlier call.
void check_error_reporting(const std::filesystem::path& dir, const std::filesystem::path& lock_file)
{
    using namespace x2w_save;
    FileLock holder;
    expect(holder.acquire(lock_file, true, std::chrono::milliseconds(0)) == LockResult::locked, "holder takes a free lock (errors)");

    FileLock probe;
    expect(probe.acquire(lock_file, true, std::chrono::milliseconds(0)) == LockResult::timeout, "a held lock gives timeout");
    const std::uint32_t busy = probe.last_error();
    expect(is_busy_code(busy), "a busy attempt records the busy code");

    const std::filesystem::path missing = dir / "no_such_folder" / "x.lock";
    expect(probe.acquire(missing, true, std::chrono::milliseconds(0)) == LockResult::error, "a missing folder gives error");
    expect(probe.last_error() != 0, "a failed open records a system error");
    expect(probe.last_error() != busy, "a failed open does not keep the earlier busy code");
    holder.release();

    FileLock free_lock;
    expect(free_lock.acquire(lock_file, false, std::chrono::milliseconds(0)) == LockResult::locked, "a free lock locks");
    expect(free_lock.last_error() == 0, "a free lock leaves no error");
    expect(probe.acquire(lock_file, false, std::chrono::milliseconds(0)) == LockResult::locked, "a shared lock joins a shared lock");
    expect(probe.last_error() == 0, "a lock after an error leaves no error");
}

// The store keeps one FileLock per operation and relies on its destructor; a lock object can also be
// reused. Each case is checked from a second object, which can only take the lock once the first
// one's handle is closed: an acquire must drop what the object held before, the destructor must drop
// the lock, and a second release must not touch a handle the object no longer owns.
void check_release_paths(const std::filesystem::path& dir)
{
    using namespace x2w_save;
    const std::filesystem::path first = dir / "reuse_a.lock", second = dir / "reuse_b.lock";
    const auto at_once = std::chrono::milliseconds(0);

    FileLock reused;
    expect(reused.acquire(first, true, at_once) == LockResult::locked, "reuse: the object takes a free lock");
    expect(reused.acquire(first, true, at_once) == LockResult::locked,
           "re-acquire: taking the same lock again on the same object succeeds (its earlier hold is dropped first)");
    expect(reused.acquire(second, true, at_once) == LockResult::locked, "reuse: the object moves to a second lock");
    FileLock other;
    expect(other.acquire(first, true, at_once) == LockResult::locked,
           "re-acquire: moving to another file frees the lock the object held before");
    expect(other.acquire(second, true, at_once) == LockResult::timeout, "reuse: the second lock is still held by the object");
    reused.release();
    other.release();

    {
        FileLock scoped;
        expect(scoped.acquire(first, true, at_once) == LockResult::locked, "scope: the object takes a free lock");
    }
    expect(other.acquire(first, true, at_once) == LockResult::locked, "destructor: a destroyed object frees its lock");
    other.release();

    // The released object's old handle value is free for the system to hand out again, often to the
    // very next open; a stale copy of it must not be closed on the second release.
    FileLock once;
    expect(once.acquire(first, true, at_once) == LockResult::locked, "double release: the object takes a free lock");
    once.release();
    FileLock holder;
    expect(holder.acquire(first, true, at_once) == LockResult::locked, "double release: a second object takes the freed lock");
    once.release();
    expect(other.acquire(first, true, at_once) == LockResult::timeout,
           "double release: releasing an object twice leaves the lock another object now holds");
}
}  // namespace

int main()
{
    check_mapping();

    std::error_code ec;
    const std::filesystem::path base = std::filesystem::temp_directory_path(ec);
    if (ec) {
        std::fprintf(stderr, "FAIL: no temporary folder\n");
        return 1;
    }
    const std::string unique = "x2w_lock_errors_" + std::to_string(static_cast<unsigned long long>(
        std::chrono::steady_clock::now().time_since_epoch().count()));
    const std::filesystem::path dir = base / unique;
    if (!std::filesystem::create_directories(dir, ec) || ec) {
        std::fprintf(stderr, "FAIL: cannot create %s\n", dir.string().c_str());
        return 1;
    }
    try {
        check_long_wait(dir / "wait.lock");
        check_error_reporting(dir, dir / "err.lock");
        check_release_paths(dir);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "FAIL: exception %s\n", e.what());
        ++failures;
    }
    std::filesystem::remove_all(dir, ec);

    if (failures) return 1;
    std::puts("startup_gamesave_lock_errors: ok");
    return 0;
}
