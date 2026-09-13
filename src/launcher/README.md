# `src/launcher/` — the native `av` launcher

## What this is

A small C++ executable (`av_launcher.cpp`) that speaks the daemon's real wire protocol
(`daemon_protocol.py`, `PROTOCOL_VERSION == 1`) directly — connect, send one request frame,
read one response frame, print the bytes, exit with the daemon's exit code — without ever
starting a Python interpreter. For an allowlisted command with a warm daemon already running,
this is the only way to actually beat Git LFS's no-op `git add`/`git status` on this project's
own subprocess-launch methodology: no CPython startup, however lazily `av_cli` itself imports,
can compete with a process that never starts one at all.

When the fast path doesn't apply — `--help`, a command outside the daemon's allowlist, no
`.av` repo above the current directory, no daemon reachable, a stale/mismatched discovery
file, a forged or rejected token — the exe **transparently falls back** to the real Python
CLI (`av-py`, or `python -m av_cli.launcher` next to it, or `AV_PYTHON`, or a bare `python` on
`PATH`, in that order). Every fallback reason has a name (see `AV_LAUNCHER_TRACE` below);
every fallback re-execs a real Python process with the *original, unmodified* argv, so
correctness never depends on this binary at all — worst case, it costs nothing over not
having it.

## Platform support: Windows and POSIX (Linux/macOS) — verified two different ways

`av_launcher.cpp` is a single implementation shared by both platform families: argv
classification, repo-root discovery, the discovery-file protocol, HMAC verification, and the
`run()` function that sequences all of it exist ONCE (in an unconditional, `#ifdef`-free
block), so the two platforms can never silently diverge on the actual protocol logic — only
on the OS boundary calls each needs (transport: named pipe vs. `AF_UNIX` socket; process
spawn: `CreateProcessW` vs. `execvp`; own-exe-path: `GetModuleFileNameW` vs. `/proc/self/exe`
vs. `_NSGetExecutablePath`; and so on), each isolated behind its own `#ifdef _WIN32`.

**How each side was actually verified is genuinely different, and that's worth being exact
about rather than blurring together**: this project's own dev box is Windows-only, with no
Linux/macOS toolchain to compile *or execute* anything for either platform. Every Windows
code path is built AND exercised end-to-end, on this box, against a live `av daemon`
(`tests/test_launcher_native.py`, run directly here every time this file changes). The POSIX
branches were written to the identical protocol/design and reviewed with the same care, but
have never been compiled or run on this box at all — `.github/workflows/tests.yml`'s
`launcher-native-posix` job (a matrix over `ubuntu-latest`/`macos-latest`) is the ONLY place
they are ever actually built and exercised, running the exact same
`tests/test_launcher_native.py` for real on real Linux/macOS runners. That CI job existing
and passing is the actual proof for that half of this file, not local development here —
matches this project's own established discipline elsewhere for exactly this situation (see
`src/README.md`'s hashing-backend invariant: the x86 SHA-NI kernel is real code, reviewed
carefully, but explicitly marked "not execution-verified" until run on hardware that has it;
the honest difference here is that CI *can* actually execute the POSIX launcher, so that gap
gets closed automatically the first time this lands in a PR/push, not left open indefinitely).

**Two real, previously-latent bugs found building and testing the Windows side** (both
pre-date and are independent of the POSIX rewrite):

1. **`Conn`'s missing move constructor** (the transport connection wrapper, `PipeConn` at the
   time this was found). A class with a user-declared destructor gets no *implicit* move
   constructor (C++11 rule) — the connect helper's by-value return silently fell back to the
   implicit *copy* constructor, a plain memberwise copy of the raw `HANDLE`. The temporary's
   destructor then closed that same handle out from under the caller, so every direct
   (non-fallback) round trip failed with `ERROR_INVALID_HANDLE` on the very next `WriteFile`.
   Fixed with an explicit move-only `Conn` (both the Windows and POSIX variants now share
   this same move-only discipline, written correctly from the start on the POSIX side).
2. **`daemon.py::_serve_connection` only caught `ProtocolError`, not `OSError`.**
   `read_status()` — used by both `av daemon status` and every `maybe_auto_spawn()` call —
   deliberately connects and disconnects without sending anything, to check reachability.
   That makes the daemon's blocking read raise `BrokenPipeError` (an `OSError`, not a
   `ProtocolError`), which was uncaught and killed the **entire** daemon thread, not just
   that one connection. No existing test caught this because every `maybe_auto_spawn` test
   mocks `read_status` rather than driving it against a real live daemon. See
   `development/Probleme.md` for the full writeup; both are fixed and covered by
   `tests/test_daemon.py::test_zero_byte_disconnect_does_not_kill_the_daemon_thread` and
   `tests/test_launcher_native.py`'s multi-call round-trip tests.

**A third real bug, found testing the packaging side (see below), not the protocol itself**:
`setup.py`'s fallback-shim path never copied its artifact into the interpreter's own Scripts
directory, only the successful-build path did — so a build that fell back to the shim left
whatever `av.exe` a PRIOR successful build had left there untouched, silently masking that a
genuinely fresh install with a broken toolchain would have had no working `av` at all. Found
by deliberately breaking the build (renaming `av_launcher.cpp` away) to test the fallback
for real rather than reasoning about it in the abstract — the exact same discipline that
found the two protocol bugs above. Fixed by extracting one shared copy-to-scripts-dir helper
both paths call.

## Packaging: `av` IS this binary now (on Windows, when it builds)

`setup.py`'s `WheelWithNativeLauncher` (a `bdist_wheel` subclass) injects the compiled exe
directly into the already-built wheel's `.data/scripts/av.exe`, and `av` was removed from
`[project.scripts]` entirely (see `pyproject.toml`'s comment on `av-py`) — pip's own
installer then extracts it byte-for-byte as the real `av` command on install, no
entry-point wrapper involved at all. Verified end-to-end, not just built: a real
`python -m build --wheel` → fresh, empty venv → `pip install <wheel>` → `av --version`
answers from the genuine compiled exe (confirmed via `--av-launcher-info`, which only the
real binary understands), and the daemon-fallback/discovery-file flow behaves identically
to every other test in this file. `pip install -e .` (editable/dev installs, which never run
`bdist_wheel` at all) gets the same exe copied directly to the interpreter's scripts
directory by `BuildExtWithLauncher` itself, so `av` works identically there too.
`av-native.exe` remains a separate, stable-path copy for `tests/test_launcher_native.py`
(`AV_TEST_LAUNCHER`) independent of whatever `av` itself resolves to.

**Why `scripts=` doesn't work, and why this does instead** — worth recording so a future
change in this area doesn't repeat the failed attempt blind. `setuptools`' classic
`scripts=` argument is a **hard build break** on this toolchain: its vendored
`build_scripts` command calls `tokenize.open()` unconditionally on every `scripts=` entry to
sniff a `#!`-shebang line, which raises `SyntaxError: source code cannot contain null bytes`
on any raw binary executable — confirmed live via `python -m build --wheel` failing
outright. `WheelWithNativeLauncher` never touches that command at all: it lets the wheel
build completely normally, then re-writes the already-finished `.whl` (a zip file) via
`zipfile`, appending the exe under the correct `<namever>.data/scripts/av.exe` path
(`namever` read directly off the wheel's own `.dist-info/RECORD` path, never re-derived from
the wheel filename's stricter name normalization) and a matching RECORD row with a real
sha256/size. Confirmed safe on the INSTALL side too by reading pip's own installer
(`pip._internal.operations.install.wheel`) directly: a `.data/scripts/` file installs
byte-for-byte and its `fix_script()` step opens in binary mode, only acting on a literal
`#!python` first line — never crashing on other content — **except** when a file's name
(minus `.exe`) matches a registered `console_scripts`/`gui_scripts` entry point, in which
case pip silently drops it in favor of its own generated wrapper (`is_entrypoint_wrapper()`
in that same module). That's the one hard invariant this whole mechanism depends on: `av`
must never be a `[project.scripts]` entry, on any platform, ever.

**The one accepted trade-off**: if the native build fails (or on a non-Windows platform,
not attempted this revision), `BuildExtWithLauncher` records a plain `#!python` shim instead
of a real exe, and that shim gets injected the same way. On POSIX this works fine (a
shebang script needs no `.exe` wrapper). On Windows specifically, a bare extensionless text
file named `av` is not directly invokable from `PATH` — so a Windows machine with a broken
local C++ toolchain building from source (editable installs, or a from-scratch `pip install`
with no prebuilt wheel available) would need to use `av-py` explicitly in that one scenario.
A real end user installing the published wheel never hits this: that wheel is built once, on
CI, where the toolchain is guaranteed to work, and ships the real compiled exe already
embedded. Generating a real Windows launcher stub for the shim case too (matching what an
entry point would have produced, e.g. via `distlib`'s own launcher templates) is a bounded,
optional follow-up, not attempted here since it isn't reachable/testable on this box (the
native build has never once failed here to exercise it against).

Regression-tested by `tests/test_wheel_packaging.py` — drives a real `python -m build
--wheel` and inspects the resulting `.whl` (injected exe present, correctly sized, PE
magic bytes, executable bit set, RECORD entry with a real hash, `av` absent from
`entry_points.txt`).

## Files

- `av_launcher.cpp` — the whole thing (~600 lines): argv classification, repo-root discovery,
  discovery-file read, named-pipe transport, HMAC verification, output, fallback exec.
- `av_launcher_version.h` — generated by `setup.py` before every build (`#define
  AV_LAUNCHER_VERSION "<setuptools-scm version>"`); gitignored. The committed placeholder
  (`"0.0.0.dev0"`) only supports a bare manual `cl`/`clang++` invocation outside the build
  system — a real `pip install -e .` always overwrites it first.

## The discovery-file protocol

The exe cannot compute `daemon_common.endpoint_key()` itself — that folds in the Python
version and `daemon_common.__file__`'s own resolved path, neither of which a compiled binary
can observe. Instead:

1. The exe hashes the two strings it *does* know — its own resolved path and the repo root
   it found by walking up from `cwd` for a `.av` directory — into a 16-hex-char key, and
   looks for `<runtime_dir>/launcher-<key>.json`.
2. If that file is missing, unreadable, or names a different protocol/`cli_version`, the exe
   falls back to Python, first setting `AV_LAUNCHER_EXE`/`AV_LAUNCHER_REPO` env vars to the
   *exact same two strings* it just hashed.
3. The Python client (`daemon_client.call_daemon`), after any daemon round trip it completes
   successfully **and** verifies (MAC-checked), checks whether those two env vars are set. If
   so, it writes/updates `<runtime_dir>/launcher-<key>.json` — hashing those exact env-var
   strings via `daemon_common.launcher_discovery_file()`, never re-resolving them — with
   `{"protocol", "cli_version", "endpoint", "key_path", "pid"}`.
4. The *next* invocation via the exe (for the same exe path + repo root) finds this file,
   reads the token from `key_path`, and connects directly — no Python process at all.

Neither side ever re-normalizes what the other computed; they only need to agree on hashing
the same two strings, not on any path-canonicalization convention. A stale discovery file
(daemon since restarted, endpoint dead) simply fails to connect and falls back exactly like
having no file at all — self-healing within two invocations, never a hang or an error the
user sees.

## Diagnostics

`AV_LAUNCHER_TRACE=1` prints one line per decision point to stderr (`[av-native] ...`):
which fallback reason fired, or `connected to <pipe>` / `direct daemon round trip succeeded`.
Never enabled by default; costs nothing when unset. `--av-launcher-info` (hidden, not a real
subcommand) prints `{"version","protocol","allowlist","exe"}` as JSON — used only by
`tests/test_launcher_native.py` to introspect a build without guessing its version/paths.
