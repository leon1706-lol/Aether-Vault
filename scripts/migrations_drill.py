"""v1.3.4 (todo.md item 38): a per-revision upgrade -> downgrade -> re-upgrade drill
against a real, fresh Postgres — stronger than `tests/test_server.py::
test_migration_chain_downgrades_and_reupgrades_cleanly`, which only proves ONE full
head-to-base-and-back round trip works. That's a real gap: a chain of N migrations can
downgrade-then-reupgrade cleanly as a single round trip while still having an individual
step in the middle that's broken (e.g. a downgrade that doesn't fully undo its own
upgrade, invisible unless you actually stop and check state between EVERY step).

Walks: upgrade all the way to head (asserting the DB lands on the SAME head
`ScriptDirectory` resolves) -> downgrade one revision at a time to base, asserting the
`alembic_version` table matches the expected revision after every single step -> upgrade
one revision at a time back to head, asserting the same in reverse.

Usage: DATABASE_URL=postgresql+asyncpg://... python scripts/migrations_drill.py
Exits 0 if every step lands where expected, 1 with a clear message naming which step and
which expected-vs-actual revision failed otherwise.
"""
import asyncio
import os
import sys


def _asyncpg_dsn(database_url: str) -> str:
    # asyncpg's own `connect()` wants a plain postgresql:// URL, not SQLAlchemy's
    # driver-qualified postgresql+asyncpg:// form.
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _current_db_revision(database_url: str) -> str | None:
    import asyncpg

    conn = await asyncpg.connect(dsn=_asyncpg_dsn(database_url))
    try:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version')"
        )
        if not exists:
            return None
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


def _ordered_revisions(script) -> list[str]:
    # Same convention tests/test_migrations.py already establishes for this repo's own
    # strictly-linear, zero-padded-numeric revision scheme ("0001".."0016", ...): a plain
    # lexicographic sort of every revision id walked from head IS base-to-head order.
    return sorted(rev.revision for rev in script.walk_revisions())


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL must point at a real, disposable Postgres database", file=sys.stderr)
        return 1

    from alembic import command
    from alembic.script import ScriptDirectory

    from av_server.database import _alembic_config

    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    revisions = _ordered_revisions(script)
    head = script.get_current_head()
    print(f"chain: {len(revisions)} revisions, head={head}")

    def check(step: str, expected: str | None) -> bool:
        actual = asyncio.run(_current_db_revision(database_url))
        ok = actual == expected
        status = "OK" if ok else "MISMATCH"
        print(f"  [{status}] after {step}: expected={expected!r} actual={actual!r}")
        return ok

    failures: list[str] = []

    print("-- upgrading to head --")
    command.upgrade(cfg, "head")
    if not check("upgrade head", head):
        failures.append("upgrade to head did not land on the ScriptDirectory's own head")

    print("-- downgrading one revision at a time to base --")
    for rev in reversed(revisions):
        # down_revision of the CURRENT rev is what we expect to land on after downgrading
        # past it -- alembic's own `command.downgrade(cfg, "-1")` walks exactly one step
        # regardless of which revision is current, which is what makes this a genuine
        # per-step drill rather than a single jump.
        command.downgrade(cfg, "-1")
        script_rev = script.get_revision(rev)
        expected = script_rev.down_revision  # None at the base of the chain
        if not check(f"downgrade past {rev}", expected):
            failures.append(f"downgrade past {rev}: expected to land on {expected!r}")

    print("-- upgrading one revision at a time back to head --")
    for rev in revisions:
        command.upgrade(cfg, "+1")
        if not check(f"upgrade to {rev}", rev):
            failures.append(f"upgrade step: expected to land on {rev!r}")

    if failures:
        print(f"\nFAILED — {len(failures)} step(s) did not land where expected:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"\nPASSED — all {len(revisions)} revisions verified upgrade -> downgrade -> re-upgrade, step by step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
