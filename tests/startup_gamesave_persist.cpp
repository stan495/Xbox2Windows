// Experimental (docs/startup-runtime.md): the on-disk store behind the game-save stand-in
// (src/startup_stubs/save_store.h, called by the XGameSave slots of runtime_proxy.cpp) keeps
// containers and blobs between processes. The test writes in one process and starts itself again
// (argument "read") to read the bytes back in a second process. Synthetic data only.
#include "../src/startup_stubs/save_store.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <ctime>
#include <string>
#include <thread>
#ifdef _WIN32
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace fs = std::filesystem;
using x2w_save::Status;

// Unique per process and per call even when several tests start in the same
// second (std::rand() without srand() is constant on some runtimes).
static std::string unique_suffix() {
#ifdef _WIN32
    const unsigned long pid = static_cast<unsigned long>(GetCurrentProcessId());
#else
    const unsigned long pid = static_cast<unsigned long>(getpid());
#endif
    return std::to_string(pid) + "-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
}

namespace {
constexpr int exit_missing = 10, exit_mismatch = 11;

std::vector<unsigned char> payload(unsigned seed, std::size_t size)
{
    std::vector<unsigned char> bytes(size);
    for (std::size_t i = 0; i < size; ++i) bytes[i] = static_cast<unsigned char>(seed * 31 + i * 7 + 3);
    return bytes;
}

// Second process: exit 0 when the blob holds payload(seed, size), else a code that says why.
// "exists <root> <blob>" answers whether a blob of slot-a is there at all.
int exists_reader(const char* root, const char* blob)
{
    std::vector<unsigned char> bytes;
    return x2w_save::Store(root).read_blob("slot-a", blob, bytes) == Status::ok ? 0 : exit_missing;
}

int reader(const char* root, const char* seed_text)
{
    const unsigned seed = static_cast<unsigned>(std::strtoul(seed_text, nullptr, 10));
    std::vector<unsigned char> bytes;
    const Status status = x2w_save::Store(root).read_blob("slot-a", "profile.dat", bytes);
    if (status == Status::not_found) return exit_missing;
    return status == Status::ok && bytes == payload(seed, 300) ? 0 : exit_mismatch;
}

int run_child(const std::string& self, const std::string& arguments)
{
    std::string command = "\"" + self + "\" " + arguments;
#ifdef _WIN32
    command = "\"" + command + "\"";  // cmd.exe drops the outer quotes
#endif
    const int code = std::system(command.c_str());
#ifdef _WIN32
    return code;
#else
    return code == -1 ? -1 : (code >> 8) & 0xff;
#endif
}

int run_reader(const std::string& self, const fs::path& root, unsigned seed)
{
    return run_child(self, "read \"" + root.string() + "\" " + std::to_string(seed));
}
// 0: the blob is there, exit_missing: it is not.
int run_exists(const std::string& self, const fs::path& root, const std::string& blob)
{
    return run_child(self, "exists \"" + root.string() + "\" " + blob);
}

// A directory that redirects to another one: a symbolic link, or a junction on Windows.
bool make_redirect(const fs::path& link, const fs::path& target)
{
    std::error_code error;
#ifdef _WIN32
    const std::string command = "\"mklink /J \"" + link.string() + "\" \"" + target.string() + "\" >nul 2>&1\"";
    std::system(command.c_str());
    return fs::is_directory(link, error);
#else
    fs::create_directory_symlink(target, link, error);
    return !error;
#endif
}

int failures;
void expect(bool ok, const char* what)
{
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++failures;
    }
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc == 4 && std::strcmp(argv[1], "read") == 0) return reader(argv[2], argv[3]);
    if (argc == 4 && std::strcmp(argv[1], "exists") == 0) return exists_reader(argv[2], argv[3]);
    if (argc != 1) return 2;

    std::error_code ec;
    const fs::path root = fs::temp_directory_path(ec) / ("x2w-gamesave-persist-" + unique_suffix());
    fs::remove_all(root, ec);
    const x2w_save::Store store(root / "store");
    const std::string self = argv[0];
    const std::vector<unsigned char> first = payload(1, 300), second = payload(2, 300);

    // Negative control: before anything is written, the second process must see nothing. If this
    // returned 0 the reader would be passing for the wrong reason.
    expect(run_reader(self, root / "store", 1) == exit_missing, "reader finds a blob before any write");

    // Write in this process, read in another.
    expect(store.create_container("slot-a") == Status::ok, "create container");
    expect(store.create_container("slot-a") == Status::ok, "an existing container directory counts as success");
    expect(fs::is_directory(root / "store" / "slot-a"), "one directory per container");
    expect(store.write_blob("slot-a", "profile.dat", first.data(), first.size()) == Status::ok, "write blob");
    expect(fs::is_regular_file(root / "store" / "slot-a" / "profile.dat"), "one file per blob");
    expect(run_reader(self, root / "store", 1) == 0, "second process reads the bytes back");
    // Negative control: the reader's comparison can fail.
    expect(run_reader(self, root / "store", 2) == exit_mismatch, "reader rejects different bytes");

    // Overwrite replaces the bytes and leaves no side file.
    expect(store.write_blob("slot-a", "profile.dat", second.data(), second.size()) == Status::ok, "overwrite blob");
    expect(run_reader(self, root / "store", 2) == 0, "second process reads the overwritten bytes");
    expect(run_reader(self, root / "store", 1) == exit_mismatch, "old bytes are gone");
    std::size_t entries = 0;
    for (const auto& item : fs::directory_iterator(root / "store" / "slot-a", ec)) { (void)item; ++entries; }
    expect(entries == 1, "no side file is left behind");

    // Empty blob, missing blob, missing container.
    std::vector<unsigned char> bytes;
    expect(store.write_blob("slot-a", "empty", nullptr, 0) == Status::ok && store.read_blob("slot-a", "empty", bytes) == Status::ok && bytes.empty(), "empty blob round trip");
    expect(store.read_blob("slot-a", "absent", bytes) == Status::not_found, "missing blob is not_found");
    expect(store.read_blob("slot-none", "profile.dat", bytes) == Status::not_found, "missing container is not_found");

    // Names that could leave the store are refused and nothing is created outside it.
    const char* bad[] = {"", "..", ".", "../x", "a/b", "a\\b", "/abs", "\\abs", "C:x", "C:\\x", "a..b", "x.", "NUL", "com1.txt", "a\nb", "n*", "a.x2w-part", " x", "x ", "  double-lead", "CON .txt", "nul .dat", "com9.txt", "lpt9"};
    for (const char* name : bad) {
        expect(!x2w_save::valid_name(name), name);
        expect(store.create_container(name) == Status::bad_name, "bad container name refused");
        expect(store.write_blob("slot-a", name, first.data(), first.size()) == Status::bad_name, "bad blob name refused");
        expect(store.write_blob(name, "profile.dat", first.data(), first.size()) == Status::bad_name, "write below bad container refused");
        expect(store.read_blob("slot-a", name, bytes) == Status::bad_name, "bad blob read refused");
        expect(store.delete_blob("slot-a", name) == Status::bad_name, "bad blob delete refused");
        expect(store.delete_container(name) == Status::bad_name, "bad container delete refused");
    }
    expect(!x2w_save::valid_name(std::string(x2w_save::max_name_bytes + 1, 'a')), "name length is capped");
    // The accepted side of the same cap: a name of exactly max_name_bytes is legal.
    expect(x2w_save::valid_name(std::string(x2w_save::max_name_bytes, 'a')), "a name at the cap is accepted");
    // Names that only look like devices stay legal: the refusal is COM0-9 and LPT0-9 as the
    // whole stem, not every name that starts with those letters.
    for (const char* name : {"COMX", "CONSOLE", "com10", "LPT", "aux2.dat"})
        expect(x2w_save::valid_name(name), name);
    expect(store.write_blob("slot-a", "big", first.data(), x2w_save::max_blob_bytes + 1) == Status::too_large, "size is capped");
    expect(!fs::exists(root / "x") && !fs::exists(root / "abs"), "nothing outside the store root");

    // Delete a blob, then the container.
    expect(store.delete_blob("slot-a", "profile.dat") == Status::ok, "delete blob");
    expect(store.delete_blob("slot-a", "profile.dat") == Status::not_found, "second delete finds nothing");
    expect(run_reader(self, root / "store", 2) == exit_missing, "second process no longer finds the deleted blob");
    expect(store.delete_container("slot-a") == Status::ok && !fs::exists(root / "store" / "slot-a"), "delete container removes its directory");
    expect(fs::exists(root / "store"), "the store root itself stays");

    // A link left at the old guessable staging name is never opened: the outside file stays intact
    // and the blob ends up a regular file.
    {
        const fs::path outside = root / "outside.bin";
        std::ofstream(outside, std::ios::binary) << "untouched";
        expect(store.create_container("slot-l") == Status::ok, "create link container");
        std::error_code link_error;
        fs::create_symlink(outside, root / "store" / "slot-l" / "profile.dat.x2w-part", link_error);
        if (link_error) std::puts("note: symlink creation not permitted here, link control skipped");
        else {
            expect(store.write_blob("slot-l", "profile.dat", first.data(), first.size()) == Status::ok, "write beside a planted link");
            std::ifstream check(outside, std::ios::binary);
            std::string seen((std::istreambuf_iterator<char>(check)), std::istreambuf_iterator<char>());
            expect(seen == "untouched", "a link at the old staging name is not followed");
            expect(fs::is_regular_file(fs::symlink_status(root / "store" / "slot-l" / "profile.dat")), "the blob is a regular file");
        }
    }

    // Two writers on the same blob never share a staging file: both succeed and the blob holds one
    // whole payload.
    {
        const std::vector<unsigned char> one = payload(5, 1 << 20), two = payload(6, 1 << 20);
        bool fail_one = false, fail_two = false;
        for (int round = 0; round < 20; ++round) {
            std::thread a([&] { fail_one |= store.write_blob("slot-c", "shared.dat", one.data(), one.size()) != Status::ok; });
            std::thread b([&] { fail_two |= store.write_blob("slot-c", "shared.dat", two.data(), two.size()) != Status::ok; });
            a.join();
            b.join();
        }
        expect(!fail_one && !fail_two, "concurrent writes to one blob both succeed");
        expect(store.read_blob("slot-c", "shared.dat", bytes) == Status::ok && (bytes == one || bytes == two), "the blob holds one whole payload");
        std::size_t left = 0;
        for (const auto& item : fs::directory_iterator(root / "store" / "slot-c", ec)) { (void)item; ++left; }
        expect(left == 1, "no staging file is left after concurrent writes");
    }

    // The staging suffix is reserved whatever the case (the same file on a case-insensitive disk).
    expect(!x2w_save::valid_name("profile.X2W-PART") && !x2w_save::valid_name("a.x2W-Part"), "staging suffix is reserved case-insensitively");
    expect(x2w_save::valid_name("profile.x2w-par"), "a near miss of the suffix is a valid name");

    // Closed containers give their table entry back: far more than 64 distinct names, one at a time.
    {
        x2w_save::ContainerTable table;
        bool all = true;
        for (int i = 0; i < 300; ++i) {
            const long c = table.open("rot-" + std::to_string(i));
            const long u = c < 0 ? -1 : table.begin_update(static_cast<std::size_t>(c));
            all &= c >= 0 && u >= 0 && table.update_container(static_cast<std::size_t>(u)) == "rot-" + std::to_string(i);
            if (u >= 0) table.end_update(static_cast<std::size_t>(u));
            if (c >= 0) table.close(static_cast<std::size_t>(c));
        }
        expect(all && table.in_use() == 0, "300 open/close rounds of distinct containers never exhaust the table");
        // Negative control: containers kept open do exhaust it, and an open update keeps its entry.
        long last = 0;
        for (std::size_t i = 0; i < x2w_save::ContainerTable::capacity; ++i) last = table.open("held-" + std::to_string(i));
        expect(last >= 0 && table.open("one-too-many") == -1, "a full table of open containers refuses another");
        const long u = table.begin_update(0);
        table.close(0);
        expect(u >= 0 && table.update_container(static_cast<std::size_t>(u)) == "held-0", "an open update keeps its container entry after close");
        const long after_close = table.begin_update(0);
        expect(after_close == -1, "a closed container cannot start an update while another update keeps its entry");
        if (after_close >= 0) table.end_update(static_cast<std::size_t>(after_close));
        table.end_update(static_cast<std::size_t>(u));
        expect(table.open("now-free") >= 0, "the entry is reclaimed once the update ends");
    }

    // An update is staged until it is submitted: nothing reaches the disk before, an update closed
    // without a submit changes nothing, and a second process only ever sees committed state.
    {
        const fs::path shared = root / "store";
        expect(store.create_container("slot-a") == Status::ok && store.write_blob("slot-a", "profile.dat", first.data(), first.size()) == Status::ok, "update fixture: first state");
        x2w_save::ContainerTable table;
        const long container = table.open("slot-a");
        const long update = table.begin_update(static_cast<std::size_t>(container));
        x2w_save::Action write{false, "profile.dat", second};
        x2w_save::Action extra{false, "extra.dat", second};
        expect(table.stage(static_cast<std::size_t>(update), write) && table.stage(static_cast<std::size_t>(update), extra), "actions are staged");
        expect(run_reader(self, shared, 1) == 0 && run_exists(self, shared, "extra.dat") == exit_missing, "second process sees nothing of a staged update");
        // (a) closed without a submit: still the first state, and nothing is left staged.
        table.end_update(static_cast<std::size_t>(update));
        const long again = table.begin_update(static_cast<std::size_t>(container));
        expect(table.staged_count(static_cast<std::size_t>(again)) == 0, "a closed update leaves nothing staged");
        expect(run_reader(self, shared, 1) == 0 && run_exists(self, shared, "extra.dat") == exit_missing, "close without submit leaves the disk unchanged");
        table.end_update(static_cast<std::size_t>(again));

        // (b) the second action fails: the first is not published either.
        std::vector<x2w_save::Action> failing{write, x2w_save::Action{true, "absent.dat", {}}};
        expect(store.commit("slot-a", failing) == Status::not_found, "a failing second action fails the commit");
        expect(run_reader(self, shared, 1) == 0, "the first action was not published");
        failing[1] = x2w_save::Action{false, "a/b", {}};  // a bad name is refused before anything is touched
        expect(store.commit("slot-a", failing) == Status::bad_name && run_reader(self, shared, 1) == 0, "a bad second action publishes nothing");
        fs::create_directory(shared / "slot-a" / "blocked.dat");  // a directory where a blob should go
        failing[1] = x2w_save::Action{false, "blocked.dat", second};
        expect(store.commit("slot-a", failing) != Status::ok && run_reader(self, shared, 1) == 0, "a blob that cannot be replaced publishes nothing");
        fs::remove(shared / "slot-a" / "blocked.dat", ec);
        std::size_t leftovers = 0;
        for (const auto& item : fs::directory_iterator(shared / "slot-a", ec)) { (void)item; ++leftovers; }
        expect(leftovers == 1, "failed commits leave no staging or backup file");

        // (c) the whole update at once; a later action on a blob wins, a delete in the same update works.
        std::vector<x2w_save::Action> whole{write, extra, x2w_save::Action{false, "profile.dat", first}, x2w_save::Action{false, "profile.dat", second}};
        expect(store.commit("slot-a", whole) == Status::ok, "a whole update commits");
        expect(run_reader(self, shared, 2) == 0 && run_exists(self, shared, "extra.dat") == 0, "second process sees the committed update");
        expect(store.commit("slot-a", {x2w_save::Action{true, "extra.dat", {}}, x2w_save::Action{false, "more.dat", first}}) == Status::ok &&
                   run_exists(self, shared, "extra.dat") == exit_missing && run_exists(self, shared, "more.dat") == 0, "delete and write commit together");
        std::size_t after = 0;
        for (const auto& item : fs::directory_iterator(shared / "slot-a", ec)) { (void)item; ++after; }
        expect(after == 2, "a commit leaves no staging or backup file");
        expect(!x2w_save::valid_name("a.X2W-OLD"), "the backup suffix is reserved too");
    }

    // A redirected container directory is refused by every operation, and nothing happens behind it.
    {
        const fs::path outside = root / "outside-dir";
        fs::create_directories(outside, ec);
        std::ofstream(outside / "profile.dat", std::ios::binary) << "behind the link";
        if (!make_redirect(root / "store" / "slot-r", outside)) std::puts("note: a directory link cannot be made here, redirect control skipped");
        else {
            expect(store.read_blob("slot-r", "profile.dat", bytes) == Status::bad_name, "read through a redirected container is refused");
            expect(store.delete_blob("slot-r", "profile.dat") == Status::bad_name && fs::exists(outside / "profile.dat"), "delete through a redirected container is refused");
            expect(store.write_blob("slot-r", "new.dat", first.data(), first.size()) != Status::ok && !fs::exists(outside / "new.dat"), "write through a redirected container is refused");
            expect(store.commit("slot-r", {x2w_save::Action{true, "profile.dat", {}}}) != Status::ok && fs::exists(outside / "profile.dat"), "commit through a redirected container is refused");
            expect(store.create_container("slot-r") == Status::bad_name, "a redirected container cannot be created over");
            expect(store.delete_container("slot-r") == Status::bad_name && fs::exists(outside / "profile.dat"), "a redirected container is not deleted through");
        }
    }

    fs::remove_all(root, ec);
    if (failures) return 1;
    std::puts("startup_gamesave_persist: ok");
    return 0;
}
