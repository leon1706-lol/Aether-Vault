"""Enterprise (cloud) login — interface seam for future auth, stubbed today.

`StubEnterpriseAuthProvider` is the only implementation that exists right now. When real
account-based auth is built, it implements `EnterpriseAuthProvider` and replaces the stub
below — `main.py`/`repl.py` only ever call `run_enterprise_login_flow()`, so nothing else
needs to change at the call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import ui


@dataclass
class EnterpriseSession:
    token: str
    org_id: str
    expires_at: float


class EnterpriseAuthProvider(Protocol):
    def login(self) -> EnterpriseSession | None: ...
    def logout(self) -> None: ...
    def current_session(self) -> EnterpriseSession | None: ...
    def refresh(self) -> EnterpriseSession | None: ...


class StubEnterpriseAuthProvider:
    """Only implementation today — enterprise auth isn't built yet."""

    def login(self) -> EnterpriseSession | None:
        ui.print_step(
            "Enterprise login is coming soon — falling back to Local mode for now.",
            status="warn",
        )
        return None

    def logout(self) -> None:
        return None

    def current_session(self) -> EnterpriseSession | None:
        return None

    def refresh(self) -> EnterpriseSession | None:
        return None


def run_enterprise_login_flow() -> bool:
    """Attempt enterprise login. Returns True if a real session was established.

    Always False today (stub) — callers should fall back to local-mode onboarding when this
    returns False.
    """
    provider = StubEnterpriseAuthProvider()
    session = provider.login()
    return session is not None
