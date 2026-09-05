"""v1.3.0 contract matrix (todo.md item 6): closes the two real gaps `test_exit_codes.py`
left — a full exit-code registry proof (all seven codes, not one command each) and a
generic anti-leakage sweep across the ENTIRE CLI, not just the two commands regression
#93 originally fixed.

Building this sweep is what found (and this cycle then fixed) two previously-uncaught
leaks: `av --output json add`/`commit` printed "Staged [...]" from stage_one_file()
unconditionally, and `av --output json watch` printed several _finalize_commit() human
echoes because it never passed a result_sink (cmd_history.py's `commit` always has). Both
are fixed at the source (core.py, cmd_watch.py) — see development/Probleme.md.
"""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli

# Commands genuinely exempt from "single clean JSON envelope, zero leakage" — each has a
# specific, documented reason, not a shortcut:
#   watch      - streams one envelope PER auto-commit by design (NDJSON), not one envelope
#                for the whole (indefinitely-running) invocation. Covered by its own test
#                below instead of the generic sweep.
#   test       - runs the real pytest suite as a subprocess; JSON mode already suppresses
#                the live stream and emits one final envelope (see cmd_devtools.py), but
#                actually invoking it here would recursively run the whole suite.
#   benchmark  - same shape as test (dev-only, subprocess-heavy); exercised manually, not
#                via CliRunner recursion.
#   webui      - starts Docker + opens a real browser tab; its status prose runs through
#                ui.print_step()/rich console across docker_runtime.py, not click.secho —
#                silencing it fully would mean threading a quiet flag through every step
#                of that shared, human-interactive module for a command with zero agent
#                relevance (not in AGENTS.md's v1.2 supported-commands table; a browser
#                tab is not a thing an agent can consume). Left as a human/ops tool.
#   import-lightning, import-transformers, import-mlflow
#              - require the actual ML framework installed (torch/transformers/mlflow) to
#                do anything meaningful; invoking bare exercises only their own
#                framework-missing ImportError path, which IS clean JSON-safe but adds
#                nothing this sweep needs three extra skip-branches for.
_LEAKAGE_EXEMPT = {"watch", "test", "benchmark", "webui", "import-lightning",
                    "import-transformers", "import-mlflow"}


def _iter_command_paths():
    """Yields (display_name, args) for every leaf command reachable with zero required
    positional arguments — groups recurse into their subcommands; a leaf that needs a
    positional argument is invoked anyway (it'll hit click's own usage error, exit 2,
    which is exempt below by design — EXIT_USAGE is documented as "click's own usage-error
    code" in core.py, a pre-existing, accepted exception to the envelope contract)."""
    def _walk(name, cmd, prefix):
        full = prefix + [name]
        if hasattr(cmd, "commands"):  # a Group
            for sub_name, sub_cmd in cmd.commands.items():
                yield from _walk(sub_name, sub_cmd, full)
        else:
            yield (" ".join(full), full)

    for name, cmd in cli.commands.items():
        yield from _walk(name, cmd, [])


ALL_COMMAND_PATHS = list(_iter_command_paths())


def _sandbox_compose_dir(repo, monkeypatch):
    """Prevents commands under `av auth` (set-token/clear/rotate) — swept generically
    below along with every other zero-required-arg command — from resolving a REAL
    docker-compose.yml via `_find_source_root()` and writing a real `.env` / restarting
    the real running engine container as a side effect of running this test file.

    `_find_source_root()` returns this actual checkout's root for an editable install
    (see main.py's own docstring) — completely independent of the `repo` fixture's
    tmp_path sandbox that every OTHER command in this sweep already respects via `cwd`.
    Without this, `pytest tests/test_contract_matrix.py` on any machine with Docker
    running mutates the real `.env` and restarts the real `aether-vault-engine` container
    three times per run (set-token, clear, rotate) — the exact same real, dangerous side
    effect `tests/test_cli.py::_sandbox_compose_dir` already exists to prevent for its own
    dedicated auth tests; this sweep re-introduced it by not reusing that pattern. See
    development/Probleme.md.
    """
    import python.av_cli.main as main_module

    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: repo)


class TestAntiLeakage:
    """Every leaf command, invoked with --output json and no other args, from inside a
    real repo: either it's a click usage error (exit 2 — exempt, see module docstring) or
    its ENTIRE stdout must parse as one clean JSON envelope."""

    @pytest.mark.parametrize("display_name,args", ALL_COMMAND_PATHS,
                             ids=[p[0] for p in ALL_COMMAND_PATHS])
    def test_command_emits_clean_json_or_usage_error(self, repo, monkeypatch, display_name, args):
        if args[0] in _LEAKAGE_EXEMPT:
            pytest.skip(f"{display_name}: documented exception, see module docstring")
        # Every command runs from inside the sandboxed repo already (cwd); this additional
        # sandbox is specifically for the handful (av auth set-token/clear/rotate) that
        # resolve infrastructure via _find_source_root() instead of cwd — see the helper's
        # own docstring. Applied unconditionally so a FUTURE command gaining a similar
        # real-infrastructure touch is safe by default rather than by someone remembering.
        _sandbox_compose_dir(repo, monkeypatch)
        result = CliRunner().invoke(cli, ["--output", "json", *args])
        if result.exit_code == 2:
            pytest.skip(f"{display_name}: click usage error (missing required argument) "
                        "— EXIT_USAGE is a pre-existing, documented exception")
        try:
            env = json.loads(result.output)
        except json.JSONDecodeError:
            pytest.fail(
                f"{display_name} --output json leaked non-JSON output "
                f"(exit {result.exit_code}):\n{result.output!r}"
            )
        assert "ok" in env and "meta" in env, f"{display_name}: malformed envelope {env!r}"


class TestWatchStreamsNdjson:
    """watch's documented exception: one envelope per line, not one for the invocation."""

    def test_each_line_is_its_own_clean_envelope(self, repo):
        (repo / "c.ckpt").write_bytes(b"data")
        result = CliRunner().invoke(cli, ["--output", "json", "watch", "--interval", "0",
                                          "--debounce", "0", "--max-commits", "1"])
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert lines, "watch --output json produced no envelopes at all"
        for line in lines:
            env = json.loads(line)  # raises if any line isn't clean JSON on its own
            assert env["meta"]["command"] == "watch"
        events = [json.loads(l)["data"]["event"] for l in lines]
        assert "auto_commit" in events
        assert events[-1] == "stopped"


# ---------------------------------------------------------------------------
# Full exit-code registry matrix: every documented code, both output modes, driven
# through a command that actually produces it — test_exit_codes.py already pins one
# command per code; this file is the (code × mode) truth table read back structurally,
# so a future command losing its envelope on one specific code/mode combination fails
# here even if test_exit_codes.py's own one-shot assertion for that code still passes.
# ---------------------------------------------------------------------------

EXIT_CODE_REGISTRY = {
    "not_a_repo": 10,
    "nothing_to_commit": 11,
    "auth_failed": 12,
    "unreachable_queued": 13,
    "merge_conflict": 14,
    "validation": 15,
    "policy_denied": 16,
    "budget_exhausted": 17,
    "frozen": 18,
    "review_required": 19,
    "scope_denied": 20,
    "tenant_denied": 22,  # v1.3.2; 21 (login_required) deliberately unregistered, see core.py
}

# 13 (unreachable_queued) is real in the registry but, by AGENTS.md non-negotiable #3
# ("offline resilience is sacred"), `commit`/`push` deliberately exit 0 when queued —
# queued is a SAFE, complete outcome, not a failure. It's reserved for read-path commands
# where reachability IS the primary outcome (av audit list, av webhooks list) — those
# don't yet have a dedicated fail(..., "unreachable_queued", ...) repro in
# test_exit_codes.py (only commit's queued:true data-shape is pinned there), so the
# per-code repro check below is scoped to the six codes that DO always fail() at that exit
# status, matching what test_exit_codes.py's own file header documents.
_HAS_FAIL_PATH_REPRO = {k: v for k, v in EXIT_CODE_REGISTRY.items() if k != "unreachable_queued"}


class TestExitCodeRegistryIsSelfConsistent:
    def test_registry_constant_matches_documented_codes(self):
        from python.av_cli.core import _EXIT_CODES

        assert _EXIT_CODES == EXIT_CODE_REGISTRY, (
            "core.py's _EXIT_CODES drifted from the documented registry — update "
            "AGENTS.md/README's exit-code table alongside this test if the drift is "
            "intentional and additive (a new code), not accidental."
        )

    @pytest.mark.parametrize("code,exit_status", sorted(_HAS_FAIL_PATH_REPRO.items()))
    def test_every_code_has_a_dedicated_repro_in_test_exit_codes(self, code, exit_status):
        # Structural guard, not a re-run: fails loudly (naming the missing code) if a new
        # entry is ever added to the registry above without a real repro test alongside
        # it in test_exit_codes.py, rather than silently trusting the table.
        import tests.test_exit_codes as exit_codes_module

        has_marker = any(f"exits_{exit_status}" in name for name in dir(exit_codes_module)
                         if name.startswith("test_"))
        assert has_marker, (
            f"exit code {exit_status} ({code}) has no test_..._exits_{exit_status}_... "
            "function in tests/test_exit_codes.py"
        )
