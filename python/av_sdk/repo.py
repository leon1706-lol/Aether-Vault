"""av_sdk.Repo — the agent-facing handle for one Aether-Vault repository.

Every method returns plain dicts (identical shapes to `av --output json` data payloads)
and raises SDKError on failure. Commits go through core.commit_staged — THE single
writer shared with the CLI — so offline queueing, run tagging, and atomicity are never
duplicated or diverged.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

from av_cli.exceptions import AetherVaultException

from .exceptions import SDKError


def _parse_envelope(raw: str) -> dict | None:
    """Finds the last JSON object line in captured CLI output (None when absent)."""
    for line in reversed((raw or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                doc = json.loads(line)
                if isinstance(doc, dict) and "ok" in doc and "meta" in doc:
                    return doc
            except json.JSONDecodeError:
                continue
    return None


class Repo:
    """Context-managed handle: `with Repo(path) as repo: repo.commit("...")`.

    The repository directory is pinned at construction; operations chdir into it for
    the duration of each internal invocation (commands resolve via cwd), restoring
    afterwards even on failure.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not (self.path / ".av").is_dir():
            raise SDKError("not_a_repo",
                           f"{self.path} is not an Aether-Vault repository (no .av/).")

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    # -- internals ------------------------------------------------------------

    def _invoke(self, args: list[str]) -> dict | None:
        """Runs `av --output json <args>` in-process; returns parsed envelope data."""
        from av_cli.main import cli

        previous_cwd = Path.cwd()
        captured = io.StringIO()
        try:
            os.chdir(self.path)
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
                cli.main(args=["--output", "json", *args], prog_name="av-sdk",
                         standalone_mode=False)
        except SystemExit as exc:  # click raises Exit(code) for our fail() path
            envelope = _parse_envelope(captured.getvalue())
            if isinstance(exc.code, int) and exc.code not in (0, None):
                code = (envelope or {}).get("error", {}) or {}
                raise SDKError(code.get("code", "validation"),
                               code.get("message") or f"exit {exc.code}") from None
        except AetherVaultException as exc:
            raise SDKError("validation", str(exc)) from None
        finally:
            os.chdir(previous_cwd)
        envelope = _parse_envelope(captured.getvalue())
        return (envelope or {}).get("data")

    # -- public surface -------------------------------------------------------

    def status(self) -> dict:
        return self._invoke(["status"]) or {}

    def add(self, *paths: str | Path) -> dict:
        data = self._invoke(["add", *[str(p) for p in paths]]) or {}
        return data

    def commit(
        self,
        message: str,
        *,
        tags: list[str] | tuple = (),
        metrics: dict | None = None,
        no_upload: bool = False,
    ) -> dict:
        args = ["commit", "-m", message]
        for tag in tags:
            args += ["--tag", str(tag)]
        for key, value in (metrics or {}).items():
            args += ["--metric", f"{key}={value}"]
        if no_upload:
            args.append("--no-upload")
        data = self._invoke(args)
        if not data or not data.get("committed"):
            raise SDKError("nothing_to_commit",
                           (data or {}).get("reason") or "Nothing staged to commit.")
        return data

    def push(self) -> dict:
        return self._invoke(["push"]) or {"drained": 0, "still_queued": 0, "reachable": None}

    def log(self, limit: int = 30) -> str:
        res = self._invoke(["log", "--limit", str(limit)])
        return res if isinstance(res, str) else ""

    def diff_semantic(self, target: str | None = None) -> dict:
        args = ["diff"] + ([target] if target else [])
        return self._invoke(args) or {}

    def run_start(self, name: str | None = None, parent_run_id: str | None = None) -> dict:
        args = ["run", "start"] + ([name] if name else [])
        if parent_run_id:
            args += ["--parent", parent_run_id]
        return self._invoke(args) or {}

    def run_finish(self, *, failed: bool = False, metrics: dict | None = None) -> dict:
        args = ["run", "finish"] + (["--fail"] if failed else [])
        for k, v in (metrics or {}).items():
            args += ["--metric", f"{k}={v}"]
        return self._invoke(args) or {}

    def handoff_dict(self) -> dict:
        """The live .avh v2 document (built fresh, same as `av handoff --update`)."""
        from av_cli.handoff import build_handoff_dict

        return build_handoff_dict(self.path, None)

    def context_note(self, note: str, agent: str | None = None) -> dict:
        args = ["context", "note", note] + (["--agent", agent] if agent else [])
        return self._invoke(args) or {}
