// V1.6.0: a minimal, PRECOMPILED (checked into git, never built at install time) fallback
// `av` for the one narrow case the real native launcher (av_launcher.cpp) can't cover on
// Windows: the local C++ toolchain that would normally compile it is itself broken or
// missing. That's also exactly why this can't be "just compile a smaller stub instead" --
// if there's no working compiler, nothing gets compiled, full stop. This file is compiled
// ONCE, by hand, with a working toolchain (see src/launcher/README.md for the exact command
// and when it needs re-doing), and the resulting binary is committed as
// src/launcher/precompiled/av_shim_win32.exe. setup.py's `_record_shim_artifact()` injects
// THIS binary (not a `.py` text shim) whenever the real native build fails, giving `av` a
// real, double-clickable, PATH-invokable `.exe` even in that scenario -- the text-shim
// fallback stayed the answer on POSIX (a shebang script needs no such wrapper) and is what
// ships if this precompiled binary is ever itself missing/stale for some reason.
//
// Does exactly one thing: find a Python interpreter and exec `<interp> -m av_cli.launcher`
// with this process's own argv forwarded unchanged. No daemon routing, no argv
// classification, no protocol code at all -- if the REAL launcher couldn't be built, the
// only thing this needs to guarantee is that `av` still runs Python's own av_cli correctly,
// exactly like `av-py` already does.
#ifndef _WIN32
#error "av_launcher_shim.cpp is Windows-only -- POSIX never needs a compiled wrapper for a shebang script"
#endif

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <cstdio>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {

std::wstring utf8_to_wide(const std::string &s) {
    if (s.empty()) return {};
    int size = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), size);
    return out;
}

std::optional<std::wstring> get_env_w(const wchar_t *name) {
    DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0) return std::nullopt;
    std::wstring buf(needed, L'\0');
    DWORD written = GetEnvironmentVariableW(name, buf.data(), needed);
    if (written == 0) return std::nullopt;
    buf.resize(written);
    return buf;
}

fs::path own_exe_path() {
    std::vector<wchar_t> buf(MAX_PATH);
    for (;;) {
        DWORD n = GetModuleFileNameW(nullptr, buf.data(), (DWORD)buf.size());
        if (n == 0) return {};
        if (n < buf.size() - 1) {
            std::error_code ec;
            return fs::weakly_canonical(fs::path(buf.data(), buf.data() + n), ec);
        }
        buf.resize(buf.size() * 2);
    }
}

}  // namespace

int wmain(int argc, wchar_t **wargv) {
    fs::path exe_path = own_exe_path();
    fs::path exe_dir = exe_path.has_parent_path() ? exe_path.parent_path() : fs::current_path();
    fs::path venv_root = exe_dir.has_parent_path() ? exe_dir.parent_path() : exe_dir;

    std::vector<std::pair<fs::path, bool>> candidates;
    if (auto av_python = get_env_w(L"AV_PYTHON")) candidates.emplace_back(fs::path(*av_python), false);
    candidates.emplace_back(exe_dir / "av-py.exe", false);
    candidates.emplace_back(venv_root / "python.exe", true);

    fs::path target;
    bool dash_m = false;
    for (const auto &[c, is_m] : candidates) {
        std::error_code ec;
        if (fs::exists(c, ec)) {
            target = c;
            dash_m = is_m;
            break;
        }
    }
    if (target.empty()) {
        target = "python.exe";
        dash_m = true;
    }

    std::wstring cmdline = L"\"" + target.wstring() + L"\"";
    if (dash_m) cmdline += L" -m av_cli.launcher";
    for (int i = 1; i < argc; ++i) {
        std::wstring a = wargv[i];
        bool needs_quote = a.empty() || a.find(L' ') != std::wstring::npos;
        cmdline += L" ";
        if (needs_quote) cmdline += L"\"" + a + L"\"";
        else cmdline += a;
    }

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::vector<wchar_t> mutable_cmdline(cmdline.begin(), cmdline.end());
    mutable_cmdline.push_back(L'\0');
    BOOL ok = CreateProcessW(nullptr, mutable_cmdline.data(), nullptr, nullptr, TRUE, 0,
                              nullptr, nullptr, &si, &pi);
    if (!ok) {
        std::fwprintf(stderr, L"av: could not find a Python interpreter to run av_cli "
                               L"(tried AV_PYTHON, av-py.exe, python.exe next to this "
                               L"launcher, and PATH). Set AV_PYTHON or ensure av-py is "
                               L"installed.\n");
        return 1;
    }
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    return (int)code;
}
