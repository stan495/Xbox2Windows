// Checks src/startup_stubs/trap.h without a GPU device: exit_code bit-masking,
// listed() comma/wildcard/2048-char boundary behavior, X2W_TRAP_CONTINUE and X2W_TRAP_FAIL
// logging and return values, continue-over-fail precedence, and child-process default trap
// termination with X2W_TRAP_LOG and stderr output.
#define X2W_TRAP_MODULE "testmod"
#define X2W_TRAP_MODULE_ID 0x42u
#include "startup_stubs/trap.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

X2W_TRAP_EXPORT(0x07u, trap_sample_alpha)
X2W_TRAP_EXPORT(0x08u, trap_sample_beta)

namespace {

int failures = 0;
int cases = 0;

void check(bool cond, const char* name)
{
    ++cases;
    if (!cond) {
        std::printf("FAIL %s (GetLastError=%lu)\n", name, static_cast<unsigned long>(GetLastError()));
        std::fflush(stdout);
        ++failures;
    }
}

std::string read_file_bytes(const wchar_t* path)
{
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return {};
    std::string out;
    char buf[512];
    DWORD got = 0;
    while (ReadFile(file, buf, sizeof(buf), &got, nullptr) && got > 0) {
        out.append(buf, buf + got);
    }
    CloseHandle(file);
    return out;
}

std::string executable_basename()
{
    char path[MAX_PATH]{};
    const DWORD n = GetModuleFileNameA(nullptr, path, MAX_PATH);
    if (!n || n >= MAX_PATH) return {};
    const char* base = path;
    for (const char* p = path; *p; ++p) {
        if (*p == '\\' || *p == '/') base = p + 1;
    }
    return base;
}

void test_exit_code()
{
    check(x2w_trap::exit_code(0u, 0u) == 0xE2570000u, "exit_code zero module and index");
    check(x2w_trap::exit_code(0x12u, 0x34u) == 0xE2571234u, "exit_code encodes module and index");
    check(x2w_trap::exit_code(0xFFu, 0xFFu) == 0xE257FFFFu, "exit_code max 8-bit fields");
    check(x2w_trap::exit_code(0x100u, 0u) == 0xE2570000u, "exit_code masks module overflow bits");
    check(x2w_trap::exit_code(0x800u, 0u) == 0xE2570000u, "exit_code masks module overflow bit 19");
    check(x2w_trap::exit_code(0u, 0x100u) == 0xE2570000u, "exit_code masks index overflow bits");
    check(x2w_trap::exit_code(0x1ABu, 0x2CDu) == 0xE257ABCDu, "exit_code masks module and index to 8 bits");
}

void test_listed(bool prefix_mutate)
{
    const char* const var_name = "X2W_TEST_TRAP_LIST";

    SetEnvironmentVariableA(var_name, nullptr);
    check(!x2w_trap::listed(var_name, "trap_sample_alpha"), "listed unset returns false");

    SetEnvironmentVariableA(var_name, "");
    check(!x2w_trap::listed(var_name, "trap_sample_alpha"), "listed empty returns false");

    SetEnvironmentVariableA(var_name, "*");
    check(x2w_trap::listed(var_name, "trap_sample_alpha") &&
              x2w_trap::listed(var_name, "anything_else"),
          "listed wildcard '*' matches any export");

    SetEnvironmentVariableA(var_name, "*alpha");
    check(!x2w_trap::listed(var_name, "trap_sample_alpha") &&
              x2w_trap::listed(var_name, "*alpha"),
          "listed '*alpha' is literal rather than wildcard");

    SetEnvironmentVariableA(var_name, "Alpha,Beta,Gamma");
    check(x2w_trap::listed(var_name, "Alpha"), "listed matches first comma item");
    check(x2w_trap::listed(var_name, "Beta"), "listed matches middle comma item");
    check(x2w_trap::listed(var_name, "Gamma"), "listed matches last comma item");
    check(!x2w_trap::listed(var_name, "alpha"), "listed is case-sensitive");
    check(!x2w_trap::listed(var_name, "Alpha2"), "listed rejects superstring");
    if (prefix_mutate) {
        check(x2w_trap::listed(var_name, "Al"), "prefix-match control expects prefix to match");
    } else {
        check(!x2w_trap::listed(var_name, "Al"), "listed rejects prefix match");
    }

    SetEnvironmentVariableA(var_name, ",Alpha,,Beta,");
    check(x2w_trap::listed(var_name, "Alpha") && x2w_trap::listed(var_name, "Beta") &&
              !x2w_trap::listed(var_name, "Gamma"),
          "listed skips empty comma entries");

    // 2048-byte buffer boundary: 2047 chars (+ NUL = 2048) fits; 2048 chars (+ NUL = 2049) fails.
    const std::string target = "TargetExport";
    const std::string fit_pad(2047u - 1u - target.size(), 'x');
    const std::string fit_list = fit_pad + "," + target;
    check(fit_list.size() == 2047u, "fit_list has exact 2047-byte length");
    SetEnvironmentVariableA(var_name, fit_list.c_str());
    check(x2w_trap::listed(var_name, target.c_str()), "listed accepts 2047-byte env list");

    const std::string over_pad(2048u - 1u - target.size(), 'x');
    const std::string over_list = over_pad + "," + target;
    check(over_list.size() == 2048u, "over_list has exact 2048-byte length");
    SetEnvironmentVariableA(var_name, over_list.c_str());
    check(!x2w_trap::listed(var_name, target.c_str()), "listed rejects >=2048-byte env list");

    SetEnvironmentVariableA(var_name, nullptr);
}

void test_export_modes(bool fail_first_mutate)
{
    // Null and invalid handles are safe no-ops in write_all.
    x2w_trap::write_all(nullptr, "ignored", 7);
    x2w_trap::write_all(INVALID_HANDLE_VALUE, "ignored", 7);

    wchar_t temp_dir[MAX_PATH]{};
    const DWORD dir_len = GetTempPathW(MAX_PATH, temp_dir);
    check(dir_len > 0 && dir_len < MAX_PATH, "GetTempPathW succeeds");
    if (!dir_len || dir_len >= MAX_PATH) return;

    const auto folder = std::wstring(temp_dir) + L"x2w_trap_hdr_" + std::to_wstring(GetCurrentProcessId()) +
                        L"_" + std::to_wstring(GetTickCount64());
    const auto log_path = folder + L"\\trap.log";
    const auto err_path = folder + L"\\stderr.txt";
    const auto child_err_path = folder + L"\\child_stderr.txt";
    if (child_err_path.size() >= MAX_PATH) {
        check(false, "temporary folder has space for trap log names");
        return;
    }
    if (!CreateDirectoryW(folder.c_str(), nullptr)) {
        check(false, "create temporary trap folder");
        return;
    }

    const HANDLE saved_stderr = GetStdHandle(STD_ERROR_HANDLE);
    HANDLE stderr_file = CreateFileW(err_path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                                     CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    const bool captured = stderr_file != INVALID_HANDLE_VALUE &&
                          SetStdHandle(STD_ERROR_HANDLE, stderr_file) != FALSE;
    check(captured, "capture stderr handle");

    SetEnvironmentVariableW(L"X2W_TRAP_LOG", log_path.c_str());
    const std::string exe_name = executable_basename();
    check(!exe_name.empty(), "executable basename is non-empty");

    // 1) X2W_TRAP_CONTINUE returns 0 and records x2w-continue.
    SetEnvironmentVariableA("X2W_TRAP_CONTINUE", "trap_sample_alpha");
    SetEnvironmentVariableA("X2W_TRAP_FAIL", nullptr);
    check(x2w_trap::continues("trap_sample_alpha") && !x2w_trap::fails("trap_sample_alpha"),
          "continues/fails switches reflect X2W_TRAP_CONTINUE");
    const std::uint64_t cont_ret = trap_sample_alpha(0x11u, 0x22u, 0x33u, 0x44u);
    check(cont_ret == 0u, "X2W_TRAP_CONTINUE export returns 0");

    // 2) X2W_TRAP_FAIL returns E_FAIL (0x80004005) and records x2w-fail.
    SetEnvironmentVariableA("X2W_TRAP_CONTINUE", nullptr);
    SetEnvironmentVariableA("X2W_TRAP_FAIL", "trap_sample_beta");
    check(!x2w_trap::continues("trap_sample_beta") && x2w_trap::fails("trap_sample_beta"),
          "continues/fails switches reflect X2W_TRAP_FAIL");
    const std::uint64_t fail_ret = trap_sample_beta(0x55u, 0x66u, 0x77u, 0x88u);
    check(fail_ret == 0x80004005u, "X2W_TRAP_FAIL export returns E_FAIL");

    // 3) Continue-over-fail precedence when export is in both lists.
    SetEnvironmentVariableA("X2W_TRAP_CONTINUE", "trap_sample_alpha");
    SetEnvironmentVariableA("X2W_TRAP_FAIL", "trap_sample_alpha");
    const std::uint64_t prec_ret = trap_sample_alpha(0xAAu, 0xBBu, 0xCCu, 0xDDu);
    if (fail_first_mutate) {
        check(prec_ret == 0x80004005u, "fail-first control expects E_FAIL precedence");
    } else {
        check(prec_ret == 0u, "X2W_TRAP_CONTINUE takes precedence over X2W_TRAP_FAIL");
    }

    if (captured) SetStdHandle(STD_ERROR_HANDLE, saved_stderr);
    if (stderr_file != INVALID_HANDLE_VALUE) CloseHandle(stderr_file);

    const std::string log_text = read_file_bytes(log_path.c_str());
    const std::string err_text = read_file_bytes(err_path.c_str());
    check(log_text == err_text, "X2W_TRAP_LOG matches stderr output");

    const std::string expect_cont =
        "x2w-continue testmod!trap_sample_alpha rcx=0x11 rdx=0x22 r8=0x33 r9=0x44 ret=" + exe_name + "+0x";
    const std::string expect_fail =
        "x2w-fail testmod!trap_sample_beta rcx=0x55 rdx=0x66 r8=0x77 r9=0x88 ret=" + exe_name + "+0x";
    const std::string expect_prec = std::string(fail_first_mutate ? "x2w-fail" : "x2w-continue") +
        " testmod!trap_sample_alpha rcx=0xaa rdx=0xbb r8=0xcc r9=0xdd ret=" + exe_name + "+0x";
    check(log_text.find(expect_cont) != std::string::npos &&
              log_text.find("exit=0xe2574207\r\n") != std::string::npos,
          "log records x2w-continue line and exit code");
    check(log_text.find(expect_fail) != std::string::npos &&
              log_text.find("exit=0xe2574208\r\n") != std::string::npos,
          "log records x2w-fail line and exit code");
    check(log_text.find(expect_prec) != std::string::npos,
          "log records continue-over-fail precedence line");

    // 4) Child process default trap path (TerminateProcess + exit_code + X2W_TRAP_LOG + stderr).
    SetEnvironmentVariableA("X2W_TRAP_CONTINUE", nullptr);
    SetEnvironmentVariableA("X2W_TRAP_FAIL", nullptr);
    DeleteFileW(log_path.c_str());

    char exe_path[MAX_PATH]{};
    const DWORD exe_len = GetModuleFileNameA(nullptr, exe_path, MAX_PATH);
    check(exe_len > 0 && exe_len < MAX_PATH, "GetModuleFileNameA succeeds for child spawn");
    if (exe_len && exe_len < MAX_PATH) {
        SECURITY_ATTRIBUTES sa{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        HANDLE child_err = CreateFileW(child_err_path.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                       &sa, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        check(child_err != INVALID_HANDLE_VALUE, "create child stderr file");
        if (child_err != INVALID_HANDLE_VALUE) {
            STARTUPINFOA si{};
            si.cb = sizeof(si);
            si.dwFlags = STARTF_USESTDHANDLES;
            si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
            si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
            si.hStdError = child_err;
            PROCESS_INFORMATION pi{};
            std::string cmd = std::string("\"") + exe_path + "\" --child-fire";
            const BOOL ok = CreateProcessA(nullptr, &cmd[0], nullptr, nullptr, TRUE, 0, nullptr, nullptr, &si, &pi);
            CloseHandle(child_err);
            check(ok != FALSE, "CreateProcessA --child-fire succeeds");
            if (ok) {
                const DWORD wait = WaitForSingleObject(pi.hProcess, 10000);
                DWORD child_exit = 0;
                const BOOL got_code = GetExitCodeProcess(pi.hProcess, &child_exit);
                if (wait != WAIT_OBJECT_0) {
                    TerminateProcess(pi.hProcess, 1);
                }
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                check(wait == WAIT_OBJECT_0, "child trap terminates promptly");
                check(got_code && child_exit == 0xE2574207u,
                      "child trap exits with encoded module/index exit code");
            }
        }
        const std::string child_log = read_file_bytes(log_path.c_str());
        const std::string child_stderr = read_file_bytes(child_err_path.c_str());
        const std::string expect_trap =
            "x2w-trap testmod!trap_sample_alpha rcx=0x1234 rdx=0x5678 r8=0x9abc r9=0xdef0 ret=" + exe_name + "+0x";
        check(child_log == child_stderr &&
                  child_log.find(expect_trap) != std::string::npos &&
                  child_log.find("exit=0xe2574207\r\n") != std::string::npos,
              "child trap writes matching x2w-trap line to stderr and X2W_TRAP_LOG");
    }

    SetEnvironmentVariableW(L"X2W_TRAP_LOG", nullptr);
    for (const auto* path : {&err_path, &child_err_path, &log_path}) DeleteFileW(path->c_str());
    check(RemoveDirectoryW(folder.c_str()) != FALSE, "clean up temporary trap folder");
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc == 2 && std::strcmp(argv[1], "--child-fire") == 0) {
        trap_sample_alpha(0x1234u, 0x5678u, 0x9abcu, 0xdef0u);
        return 1;
    }

    const bool prefix_mutate =
        argc > 2 && std::strcmp(argv[1], "--mutate") == 0 && std::strcmp(argv[2], "prefix-match") == 0;
    const bool fail_first_mutate =
        argc > 2 && std::strcmp(argv[1], "--mutate") == 0 && std::strcmp(argv[2], "fail-first") == 0;
    if (argc > 1 && !prefix_mutate && !fail_first_mutate) {
        std::printf("unknown option\n");
        return 2;
    }

    test_exit_code();
    test_listed(prefix_mutate);
    test_export_modes(fail_first_mutate);

    if (failures) {
        std::printf("startup_trap_header: %d of %d FAILED\n", failures, cases);
        return 1;
    }
    std::printf("startup_trap_header: PASS (%d cases)\n", cases);
    return 0;
}
