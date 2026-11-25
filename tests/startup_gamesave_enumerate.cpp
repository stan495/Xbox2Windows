// Experimental (docs/startup-runtime.md): enumeration in the on-disk store behind the game-save
// stand-in (src/startup_stubs/save_store.h, Store::list_containers and Store::list_blobs). Plain
// standard C++: containers are directories below one root, blobs are files in them. The listing of
// a container is taken under the same shared publication lock as read_blobs, so a second process
// that publishes between reads is seen whole or not at all, and a locked container reports busy.
// The test starts itself again ("hold", "write") for the cross-process checks. Synthetic data only.
#include "../src/startup_stubs/save_store.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <set>
#include <string>
#include <thread>
#ifdef _WIN32
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace fs = std::filesystem;
namespace x2w_save {
// Only the test compares entries; found by argument-dependent lookup inside std::equal.
inline bool operator==(const ContainerEntry& a, const ContainerEntry& b) { return a.name == b.name; }
inline bool operator==(const BlobEntry& a, const BlobEntry& b) { return a.name == b.name && a.bytes == b.bytes; }
}  // namespace x2w_save
using x2w_save::BlobEntry;
using x2w_save::ContainerEntry;
using x2w_save::Status;

namespace {
int failures;
void expect(bool ok, const char* what)
{
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++failures;
    }
}

// Unique per process and per call even when several tests start in the same second.
std::string unique_suffix()
{
#ifdef _WIN32
    const unsigned long pid = static_cast<unsigned long>(GetCurrentProcessId());
#else
    const unsigned long pid = static_cast<unsigned long>(getpid());
#endif
    return std::to_string(pid) + "-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
}

std::vector<unsigned char> bytes_of(std::size_t size, unsigned char seed)
{
    return std::vector<unsigned char>(size, seed);
}

void touch(const fs::path& file)
{
    std::ofstream(file, std::ios::binary).put('x');
}

bool wait_for(const fs::path& file, int timeout_ms = 15000)
{
    for (int waited = 0; waited < timeout_ms; waited += 2) {
        std::error_code ec;
        if (fs::exists(file, ec)) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    return false;
}

std::string quoted(const fs::path& path) { return "\"" + path.string() + "\""; }

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

// A directory or file that redirects to another one: a symbolic link, or a junction on Windows.
bool make_redirect(const fs::path& link, const fs::path& target, bool directory)
{
    std::error_code error;
#ifdef _WIN32
    if (!directory) {
        fs::create_symlink(target, link, error);  // needs a privilege that is not always held
        return !error;
    }
    const std::string command = "\"mklink /J \"" + link.string() + "\" \"" + target.string() + "\" >nul 2>&1\"";
    std::system(command.c_str());
    return fs::is_directory(link, error);
#else
    if (directory) fs::create_directory_symlink(target, link, error);
    else fs::create_symlink(target, link, error);
    return !error;
#endif
}

// Generation g of the container the lister watches: two blobs of the same size, 100 + g bytes, so a
// listing that mixes two generations shows two sizes.
constexpr std::size_t generation_base = 100;
std::vector<x2w_save::Action> generation(unsigned number)
{
    const std::size_t size = generation_base + number;
    const unsigned char seed = static_cast<unsigned char>(number);
    return {x2w_save::Action{false, "a.dat", bytes_of(size, seed)}, x2w_save::Action{false, "b.dat", bytes_of(size, seed)}};
}

// Second process: takes the exclusive publication lock of slot-a and keeps it until told to go.
int holder(const char* root, const char* dir)
{
    x2w_save::FileLock lock;
    const fs::path file = fs::path(root) / ("slot-a" + std::string(x2w_save::lock_suffix));
    if (lock.acquire(file, true, std::chrono::seconds(10)) != x2w_save::LockResult::locked) return 1;
    touch(fs::path(dir) / "held");
    return wait_for(fs::path(dir) / "release") ? 0 : 2;
}

// Second process: holds slot-gone's exclusive lock, deletes its directory after a short pause, and releases.
int deleter(const char* root, const char* dir)
{
    x2w_save::FileLock lock;
    const fs::path container = fs::path(root) / "slot-gone";
    const fs::path file = fs::path(root) / ("slot-gone" + std::string(x2w_save::lock_suffix));
    if (lock.acquire(file, true, std::chrono::seconds(10)) != x2w_save::LockResult::locked) return 1;
    touch(fs::path(dir) / "held");
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    std::error_code ec;
    fs::remove_all(container, ec);
    return ec ? 2 : 0;
}

// Second process: publishes generations 1..rounds of slot-g, two blobs each, one commit per generation.
int writer(const char* root, const char* rounds_text)
{
    const unsigned rounds = static_cast<unsigned>(std::strtoul(rounds_text, nullptr, 10));
    const x2w_save::Store store(root);
    for (unsigned number = 1; number <= rounds; ++number)
        if (store.commit("slot-g", generation(number)) != Status::ok) return 1;
    return 0;
}

bool names_are(const std::vector<BlobEntry>& blobs, const std::vector<std::pair<std::string, std::uint64_t>>& want)
{
    if (blobs.size() != want.size()) return false;
    for (std::size_t i = 0; i < want.size(); ++i)
        if (blobs[i].name != want[i].first || blobs[i].bytes != want[i].second) return false;
    return true;
}

bool containers_are(const std::vector<ContainerEntry>& found, const std::vector<std::string>& want)
{
    if (found.size() != want.size()) return false;
    for (std::size_t i = 0; i < want.size(); ++i)
        if (found[i].name != want[i]) return false;
    return true;
}
}  // namespace

int main(int argc, char** argv)
{
    if (argc == 4 && std::strcmp(argv[1], "hold") == 0) return holder(argv[2], argv[3]);
    if (argc == 4 && std::strcmp(argv[1], "delete") == 0) return deleter(argv[2], argv[3]);
    if (argc == 4 && std::strcmp(argv[1], "write") == 0) return writer(argv[2], argv[3]);
    if (argc != 1) return 2;

    std::error_code ec;
    const fs::path base = fs::temp_directory_path(ec) / ("x2w-gamesave-enumerate-" + unique_suffix());
    fs::remove_all(base, ec);
    const fs::path root = base / "store";
    const x2w_save::Store store(root);
    const std::string self = argv[0];
    const std::vector<BlobEntry> sentinel_blobs = {{"sentinel.dat", 7}};
    const std::vector<ContainerEntry> sentinel_containers = {{"sentinel"}};

    // A root that does not exist yet lists as empty, and the list it was given is replaced.
    std::vector<ContainerEntry> containers = sentinel_containers;
    expect(store.list_containers(containers) == Status::ok && containers.empty(), "a missing root lists as empty");
    fs::create_directories(root, ec);
    containers = sentinel_containers;
    expect(store.list_containers(containers) == Status::ok && containers.empty(), "an empty root lists as empty");

    // Two containers, three blobs, written in an order that is not the listing order.
    expect(store.write_blob("slot-b", "one.dat", bytes_of(5, 1).data(), 5) == Status::ok, "write slot-b/one.dat");
    expect(store.write_blob("slot-a", "zeta.dat", bytes_of(12, 2).data(), 12) == Status::ok, "write slot-a/zeta.dat");
    expect(store.write_blob("slot-a", "alpha.dat", bytes_of(300, 3).data(), 300) == Status::ok, "write slot-a/alpha.dat");

    // Created out of order, and enough of them that the order of the directory itself is unlikely to be the listing order.
    const std::vector<std::string> all_containers = {"aa-first", "mm-mid", "slot-a", "slot-b", "zz-last"};
    for (const char* name : {"zz-last", "mm-mid", "aa-first"})
        expect(store.create_container(name) == Status::ok, "create an extra container");
    expect(store.list_containers(containers) == Status::ok && containers_are(containers, all_containers),
           "containers listed by name");
    std::vector<BlobEntry> blobs = sentinel_blobs;
    expect(store.list_blobs("slot-a", blobs) == Status::ok && names_are(blobs, {{"alpha.dat", 300}, {"zeta.dat", 12}}),
           "blobs of slot-a: names, sizes and order");
    expect(store.list_blobs("slot-b", blobs) == Status::ok && names_are(blobs, {{"one.dat", 5}}), "blobs of slot-b");
    // Listing again gives the same answer: it reads, it does not consume.
    expect(store.list_blobs("slot-a", blobs) == Status::ok && names_are(blobs, {{"alpha.dat", 300}, {"zeta.dat", 12}}),
           "a second listing gives the same blobs");

    // What is not a committed blob or a container is not listed. The files below are the ones a
    // commit leaves while it runs (staging, replaced, lock) and entries the store never creates.
    const fs::path slot_a = root / "slot-a";
    touch(slot_a / "half.dat.x2w-part");
    touch(slot_a / "old.dat.x2w-old");
    touch(slot_a / "inner.x2w-lock");
    touch(slot_a / "Upper.dat.X2W-PART");  // the suffix is refused whatever its case
    touch(slot_a / "a..b.dat");             // ".." in a name
    fs::create_directory(slot_a / "subdir", ec);
    fs::create_directory(root / "junk.x2w-part", ec);
    fs::create_directory(root / "junk.x2w-old", ec);
    fs::create_directory(root / "junk.x2w-lock", ec);
    fs::create_directory(root / "dots..dir", ec);
    touch(root / "stray-file");  // a plain file in the root is not a container
    expect(store.list_blobs("slot-a", blobs) == Status::ok && names_are(blobs, {{"alpha.dat", 300}, {"zeta.dat", 12}}),
           "staging, old, lock, invalid-name and directory entries are not blobs");
    expect(store.list_containers(containers) == Status::ok && containers_are(containers, all_containers),
           "lock files, suffixed or invalid names, and plain files are not containers");

    // Links are not followed or listed: a blob that links to a real file, a container that links to a directory.
    const fs::path outside = base / "outside";
    fs::create_directories(outside / "real-container", ec);
    touch(outside / "real-blob.dat");
    const bool blob_link = make_redirect(slot_a / "linked.dat", outside / "real-blob.dat", false);
    const bool container_link = make_redirect(root / "linked-container", outside / "real-container", true);
    if (blob_link)
        expect(store.list_blobs("slot-a", blobs) == Status::ok && names_are(blobs, {{"alpha.dat", 300}, {"zeta.dat", 12}}),
               "a linked blob is not listed");
    else std::printf("note: no link could be created here; the blob link control was skipped\n");
    if (container_link) {
        expect(store.list_containers(containers) == Status::ok && containers_are(containers, all_containers),
               "a linked container is not listed");
        blobs = sentinel_blobs;
        expect(store.list_blobs("linked-container", blobs) == Status::bad_name && blobs == sentinel_blobs,
               "a linked container is refused and the output is untouched");
    } else std::printf("note: no directory link could be created here; the container link control was skipped\n");

    // Refusals leave the output as it was.
    blobs = sentinel_blobs;
    expect(store.list_blobs("slot-missing", blobs) == Status::not_found && blobs == sentinel_blobs,
           "a missing container is not_found and the output is untouched");
    expect(store.list_blobs("stray-file", blobs) == Status::io_error && blobs == sentinel_blobs,
           "a regular file in the root is io_error and the output is untouched");
    expect(store.list_blobs("bad:name", blobs) == Status::bad_name && blobs == sentinel_blobs,
           "an invalid container name is bad_name and the output is untouched");
    expect(store.list_blobs("", blobs) == Status::bad_name && blobs == sentinel_blobs, "an empty container name is bad_name");
    expect(store.list_blobs("slot-a.x2w-lock", blobs) == Status::bad_name && blobs == sentinel_blobs,
           "the lock suffix is not a container name");
    {
        const fs::path file_root = base / "file-root";
        touch(file_root);
        containers = sentinel_containers;
        expect(x2w_save::Store(file_root).list_containers(containers) == Status::io_error &&
                   containers == sentinel_containers,
               "a regular-file root is io_error and the output is untouched");
        const fs::path link_root = base / "link-root";
        if (make_redirect(link_root, root, true)) {
            containers = sentinel_containers;
            expect(x2w_save::Store(link_root).list_containers(containers) == Status::bad_name &&
                       containers == sentinel_containers,
                   "a redirected root is bad_name and the output is untouched");
        }
    }

    // A blob over the limit is reported, not listed.
    expect(store.create_container("slot-big") == Status::ok, "create slot-big");
    {
        const fs::path big = root / "slot-big" / "big.dat";
        touch(big);
        std::error_code resize_error;
        fs::resize_file(big, x2w_save::max_blob_bytes + 1, resize_error);
        expect(!resize_error, "resize the oversized blob");
        blobs = sentinel_blobs;
        expect(store.list_blobs("slot-big", blobs) == Status::too_large && blobs == sentinel_blobs,
               "a blob over max_blob_bytes is too_large and the output is untouched");
        fs::resize_file(big, x2w_save::max_blob_bytes, resize_error);
        expect(store.list_blobs("slot-big", blobs) == Status::ok && names_are(blobs, {{"big.dat", x2w_save::max_blob_bytes}}),
               "a blob of exactly max_blob_bytes is listed");
    }

    // An empty container lists as empty and replaces what the caller had.
    expect(store.create_container("slot-empty") == Status::ok, "create slot-empty");
    blobs = sentinel_blobs;
    expect(store.list_blobs("slot-empty", blobs) == Status::ok && blobs.empty(), "an empty container lists as empty");

    // Another process holds the publication lock: the listing waits for the timeout, reports busy
    // and leaves the output alone; when the lock goes, the same call succeeds.
    {
        const fs::path sync = base / "sync-hold";
        fs::create_directories(sync, ec);
        int holder_code = -1;
        std::thread hold([&] { holder_code = run_child(self, "hold " + quoted(root) + " " + quoted(sync)); });
        expect(wait_for(sync / "held"), "the holder process took the lock");
        const x2w_save::Store impatient(root, std::chrono::milliseconds(150));
        blobs = sentinel_blobs;
        const auto before = std::chrono::steady_clock::now();
        expect(impatient.list_blobs("slot-a", blobs) == Status::busy, "a locked container lists as busy");
        expect(blobs == sentinel_blobs, "busy leaves the output untouched");
        expect(std::chrono::steady_clock::now() - before >= std::chrono::milliseconds(100), "busy comes after the lock timeout");
        expect(impatient.list_blobs("slot-b", blobs) == Status::ok && names_are(blobs, {{"one.dat", 5}}),
               "another container is not held by that lock");
        touch(sync / "release");
        hold.join();
        expect(holder_code == 0, "the holder process ended cleanly");
        expect(impatient.list_blobs("slot-a", blobs) == Status::ok && names_are(blobs, {{"alpha.dat", 300}, {"zeta.dat", 12}}),
               "the listing succeeds once the lock is gone");
    }

    // A container deleted while list_blobs waits for the publication lock reports not_found (not io_error)
    // and leaves the output untouched.
    {
        expect(store.write_blob("slot-gone", "a.dat", bytes_of(4, 1).data(), 4) == Status::ok, "create slot-gone");
        const fs::path sync = base / "sync-delete";
        fs::create_directories(sync, ec);
        int deleter_code = -1;
        std::thread del([&] { deleter_code = run_child(self, "delete " + quoted(root) + " " + quoted(sync)); });
        expect(wait_for(sync / "held"), "the deleter process took the lock");
        blobs = sentinel_blobs;
        expect(store.list_blobs("slot-gone", blobs) == Status::not_found && blobs == sentinel_blobs,
               "a container deleted while waiting for the lock is not_found and the output is untouched");
        del.join();
        expect(deleter_code == 0, "the deleter process ended cleanly");
    }

    // A second process publishes two-blob generations while this one lists: every listing shows both
    // blobs of one generation (same size), never one of each, and the listing really overlapped the
    // writes (it saw several generations).
    {
        constexpr unsigned rounds = 300;
        expect(store.commit("slot-g", generation(0)) == Status::ok, "publish generation 0");
        std::atomic<bool> done{false};
        int writer_code = -1;
        std::thread write([&] {
            writer_code = run_child(self, "write " + quoted(root) + " " + std::to_string(rounds));
            done = true;
        });
        std::set<std::uint64_t> seen;
        unsigned listings = 0, torn = 0, failed = 0;
        auto look = [&] {
            std::vector<BlobEntry> now;
            ++listings;
            if (store.list_blobs("slot-g", now) != Status::ok) { ++failed; return; }
            if (now.size() != 2 || now[0].name != "a.dat" || now[1].name != "b.dat" || now[0].bytes != now[1].bytes) { ++torn; return; }
            seen.insert(now[0].bytes);
        };
        while (!done) look();
        write.join();
        look();  // the last generation, once the writer is gone
        expect(writer_code == 0, "the writer process published every generation");
        expect(failed == 0, "every listing during the commits succeeded");
        expect(torn == 0, "no listing mixed two generations or lost a blob");
        expect(seen.size() >= 2, "the listings overlapped the writes (saw more than one generation)");
        expect(seen.count(generation_base + rounds) == 1, "the final generation is listed");
    }

    fs::remove_all(base, ec);
    if (failures) {
        std::fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("game-save enumeration checks passed\n");
    return 0;
}
