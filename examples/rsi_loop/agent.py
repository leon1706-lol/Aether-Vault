"""A deterministic, scripted reference agent driving the full v1.3.1 RSI loop through
`av_sdk.Repo` alone (todo.md item 46 / plan WP-40) — no LLM key, no network call this
script doesn't make explicitly, no hidden state beyond what's printed as it runs.

Narrative (each step is a real call against a real, reachable aether-vault registry —
this is not a mock): register a baseline improver -> propose a self-edit -> get it
approved -> apply it in a real sandbox -> run a capability canary against it -> arm the
improver-promotion gate to require a reviewer's sign-off -> attempt to promote (DENIED:
review_required) -> get it reviewed by a second identity -> promote again (ALLOWED) ->
record a lesson from what was learned -> set a tiny compute budget and run it to
exhaustion (BUDGET_EXHAUSTED). Along the way it also touches the blackboard, causal
lineage, strategy memory, and cross-run search surfaces, so the script's own log is a
working tour of every RSI R1-R5 surface `av_sdk.Repo` exposes (see
`development/architecture.md`'s "RSI SDK Surface Contract" for what's deliberately NOT
mirrored on the SDK and why).

Requires a real, reachable aether-vault registry (`av init` already run against it) —
every RSI surface here is server-authoritative by design (no offline queue for improver
versions, change sets, canary results, etc. — see each surface's own architecture.md
section). `tests/test_rsi_loop.py` runs this SAME function stack-free against an
in-memory fake registry, proving the narrative's logic without Docker; running it here
for real against a live stack is part of the project's WP-44 live-verification pass.

Usage (against an already-`av init`-ed repo, pointed at a reachable registry):
    python examples/rsi_loop/agent.py /path/to/repo
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from av_sdk import Repo, SDKError


def _log(steps: list[dict], step: str, **detail) -> None:
    steps.append({"step": step, **detail})


def run_rsi_loop(repo_path: Path, *, print_fn: Callable[[str], None] | None = None) -> list[dict]:
    """Runs the full narrative against `repo_path` (an already-`av init`-ed repo) and
    returns the step log `_log()` built along the way — the return value is what
    `tests/test_rsi_loop.py` asserts against; `print_fn` (default: no printing, so the
    test run stays quiet) is what the `__main__` block below wires to `print` for a human
    running this for real."""
    say = print_fn or (lambda _msg: None)
    steps: list[dict] = []

    with Repo(repo_path) as repo:
        # 0. A real training checkpoint to give the canary (step 6) and the improver's
        #    own lineage something concrete to reason about — the RSI surfaces below
        #    version the IMPROVER, but the improver still exists to produce model runs.
        say("Committing an initial training checkpoint (val_loss=0.5)...")
        (repo_path / "checkpoint.txt").write_text("stub model weights", encoding="utf-8")
        repo.add("checkpoint.txt")
        commit = repo.commit("initial checkpoint", metrics={"val_loss": 0.5})
        _log(steps, "commit", hash=commit["hash"])

        # 1. Baseline improver — the root of this lineage.
        say("Registering baseline improver version...")
        base = repo.improver_register(sign=False)
        _log(steps, "improver_register", id=base["id"])
        say(f"  -> {base['id']}")

        # 2. Propose a self-edit: a structured diff + rationale + predicted risk.
        say("Proposing a self-edit (risk: low)...")
        change_set = repo.improver_propose(
            diff_text="--- a/agent/train_loop.py\n+++ b/agent/train_loop.py\n"
                     "-lr = 3e-4\n+lr = 1e-4\n",
            rationale="Lower the learning rate — val_loss plateaued at the old rate "
                     "across the last three runs (see strategy memory below).",
            risk="low",
        )
        _log(steps, "improver_propose", id=change_set["id"])
        say(f"  -> change set {change_set['id']}")

        # 3. A (distinct, in a real deployment) reviewer approves the CHANGE SET itself —
        #    the prerequisite for applying it; distinct from the reviewer-GATE approval
        #    step 8 below, which gates PROMOTING the resulting improver version.
        say("Approving the change set...")
        repo.improver_review(change_set["id"], "approved")
        _log(steps, "improver_review", decision="approved")

        # 4. Apply it: mint the next improver version, record `last_good` for rollback.
        say("Applying the change set (mints the next improver version)...")
        applied = repo.improver_apply(change_set["id"])
        candidate = applied["new_improver_id"]
        _log(steps, "improver_apply", new_improver_id=candidate,
             previous_improver_id=applied["previous_improver_id"])
        say(f"  -> {candidate} (previous: {applied['previous_improver_id']})")

        # 5. Actually execute the change in an isolated sandbox — the mechanical
        #    self-edit application, not just the bookkeeping step above. `local` is the
        #    safety default for a reference script; a real deployment would pass
        #    `driver="docker"` for genuine process isolation.
        say("Executing the change in a local sandbox...")
        job = repo.sandbox_run(["python", "-c", "print('training loop patched and re-run')"],
                               driver="local", improver_id=candidate)
        _log(steps, "sandbox_run", state=job["state"], exit_code=job["exit_code"])
        say(f"  -> job {job['job_id']}: {job['state']} (exit {job['exit_code']})")

        # 6. Run a capability canary against the candidate — must not regress.
        say("Running the core-capability canary...")
        canary = repo.canary_run("core-capability", improver_id=candidate)
        _log(steps, "canary_run", passed=canary["passed"])
        say(f"  -> {'PASS' if canary['passed'] else 'FAIL'}")

        # 7. Arm the improver-promotion gate to require a reviewer's sign-off. This is
        #    local-only config (no network call, no SDK method by design — see
        #    architecture.md) so it's set directly via the same file cmd_improver.py
        #    reads, matching what `av improver policy set main --require-review` does.
        from av_cli.cmd_improver import load_improver_policies, save_improver_policies

        say("Arming the improver-promotion gate (require_review) for 'main'...")
        policies = load_improver_policies(repo_path)
        policies["main"] = {**policies.get("main", {}), "require_review": True}
        save_improver_policies(repo_path, policies)
        _log(steps, "policy_armed", rule="require_review")

        # 8. First promotion attempt: DENIED — nobody has reviewed this candidate yet.
        say("Requesting promotion (expecting denial: no review on file yet)...")
        try:
            repo.improver_promote(candidate)
            _log(steps, "improver_promote_denied", unexpectedly_allowed=True)
            say("  -> UNEXPECTEDLY ALLOWED (this is a bug in the reference loop or the gate)")
        except SDKError as exc:
            _log(steps, "improver_promote_denied", code=exc.code, exit_code=exc.exit_code)
            say(f"  -> DENIED ({exc.code}, exit {exc.exit_code}) — as expected")

        # 9. Get it reviewed. In a real deployment this call is made by a SECOND
        #    identity/token (a self-review is rejected server-side, 422) — the reference
        #    loop runs single-identity for simplicity, so this call demonstrates the
        #    SUBMISSION shape, not the self-review rejection itself (that's proven
        #    directly in tests/test_review.py against the live server).
        say("Submitting a reviewer approval for the candidate...")
        review = repo.review_submit(candidate, "approve", target_type="improver")
        _log(steps, "review_submit", decision=review.get("decision", "approve"))

        # 10. Second promotion attempt: ALLOWED.
        say("Requesting promotion again (expecting success)...")
        promoted = repo.improver_promote(candidate)
        _log(steps, "improver_promote_allowed", candidate=promoted["candidate"])
        say(f"  -> PROMOTED: {promoted['candidate']} -> '{promoted['into']}'")

        # 11. A durable claim on the shared blackboard, evidenced by this change set.
        say("Posting a claim to the shared blackboard...")
        claim = repo.blackboard_post(
            "Lowering lr to 1e-4 unblocks the val_loss plateau",
            evidence=[{"type": "change_set", "ref": change_set["id"]}],
        )
        _log(steps, "blackboard_post", id=claim["id"])

        # 12. Record the causal claim explicitly (agent-authored, not yet independently
        #     verified) and add a searchable strategy-memory entry for future lineages.
        say("Recording a causal link and a strategy-memory entry...")
        repo.lineage_link("change_set", change_set["id"], "val_loss", effect_delta=-0.15)
        repo.strategy_add("lower_lr_on_plateau", "worked",
                          hyperparameters={"lr": 1e-4}, run_ids=[])
        _log(steps, "lineage_and_strategy_recorded")

        # 13. Distill what was learned into the "what we believe now" document.
        say("Recording a lesson...")
        lesson = repo.lessons_update({
            "beliefs": ["lr 1e-4 unblocks the val_loss plateau seen at 3e-4"],
            "source_change_set": change_set["id"],
        })
        _log(steps, "lessons_update", object_id=lesson.get("object_id"))

        # 14. Set a tiny compute budget for the next run and drive it to exhaustion —
        #     proving the loop stops on its own rather than needing an external kill.
        say("Setting a tiny budget and running it to exhaustion...")
        budget = repo.budget_set("rsi-loop-demo", compute_seconds_limit=10.0)
        repo.budget_consume(budget["id"], compute_seconds=6.0)
        try:
            repo.budget_consume(budget["id"], compute_seconds=6.0)
            _log(steps, "budget_exhausted", unexpectedly_not_exhausted=True)
            say("  -> UNEXPECTEDLY NOT EXHAUSTED (this is a bug in the reference loop)")
        except SDKError as exc:
            _log(steps, "budget_exhausted", code=exc.code, exit_code=exc.exit_code)
            say(f"  -> STOPPED ({exc.code}, exit {exc.exit_code}) — as expected")

        # 15. Confirm the causal graph and strategy memory are queryable across the
        #     lineage, not just written — the whole point of these surfaces existing.
        matches = repo.search_runs("val_loss", direction="down")
        _log(steps, "search_runs", match_count=len(matches))
        say(f"Cross-run search found {len(matches)} matching run(s).")

    say("RSI loop complete.")
    return steps


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python examples/rsi_loop/agent.py /path/to/repo", file=sys.stderr)
        raise SystemExit(2)
    run_rsi_loop(Path(sys.argv[1]), print_fn=print)
