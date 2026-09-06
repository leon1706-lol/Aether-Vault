"""Enterprise (cloud) login (v1.3.3). Drives the same OIDC device-code flow `av login`
uses, and persists to the same `~/.aether-vault/session.json` -- so `av init --mode
enterprise` and a bare `av login` end up in the exact same authenticated state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import session_store, ui


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


def _resolve_url(repo_root: Path | None) -> str:
    if repo_root is not None:
        from .core import load_config

        cfg = load_config(repo_root)
        return cfg.get("remote_url", "http://localhost:8000")
    return "http://localhost:8000"


class DeviceCodeEnterpriseAuthProvider:
    """Drives `/api/auth/device/code` + `/api/auth/device/token` -- shared by `av login`
    and this Protocol-conforming seam so both use one real implementation, not two
    copies of the same polling loop."""

    def __init__(self, repo_root: Path | None = None, provider_id: str | None = None):
        self._repo_root = repo_root
        self._provider_id = provider_id

    def login(self) -> EnterpriseSession | None:
        import requests

        url = _resolve_url(self._repo_root)
        provider_id = self._provider_id

        if not provider_id:
            try:
                resp = requests.get(f"{url}/api/sso-providers", timeout=10)
            except Exception:
                ui.print_step(f"Could not reach {url} to list SSO providers.", status="error")
                return None
            providers = [p for p in resp.json().get("providers", [])
                        if p.get("kind") == "oidc"] if resp.status_code == 200 else []
            if len(providers) == 1:
                provider_id = providers[0]["id"]
            else:
                ui.print_step(
                    "Enterprise login needs exactly one OIDC SSO provider configured on "
                    f"{url} (found {len(providers)}). An admin must run `av idp add` "
                    "first, or run `av login --provider <id>` directly once one exists.",
                    status="warn",
                )
                return None

        try:
            resp = requests.post(f"{url}/api/auth/device/code",
                                 json={"provider_id": provider_id}, timeout=10)
        except Exception:
            ui.print_step(f"Could not reach {url} to start enterprise login.", status="error")
            return None
        if resp.status_code != 200:
            ui.print_step(f"Could not start device login (HTTP {resp.status_code}).", status="error")
            return None

        device = resp.json()
        ui.print_step(
            f"Open this URL to finish enterprise login: {device['verification_uri_complete']} "
            f"(code: {device['user_code']})",
            status="info",
        )
        try:
            import webbrowser

            webbrowser.open(device["verification_uri_complete"])
        except Exception:
            pass

        interval = device.get("interval", 5)
        deadline = time.time() + device.get("expires_in", 600)
        session_token = None
        while time.time() < deadline:
            time.sleep(interval)
            try:
                poll_resp = requests.post(f"{url}/api/auth/device/token",
                                          json={"device_code": device["device_code"]}, timeout=10)
            except Exception:
                continue
            if poll_resp.status_code == 200:
                session_token = poll_resp.json()["access_token"]
                break
            try:
                detail = poll_resp.json().get("detail", {})
            except Exception:
                detail = {}
            if isinstance(detail, dict) and detail.get("error") == "expired_token":
                break

        if session_token is None:
            ui.print_step("Enterprise login was not completed in time.", status="warn")
            return None

        try:
            who_resp = requests.get(f"{url}/api/auth/whoami",
                                    headers={"Authorization": f"Bearer {session_token}"}, timeout=10)
            who = who_resp.json() if who_resp.status_code == 200 else {}
        except Exception:
            who = {}

        expires_at = time.time() + 8 * 3600
        session_store.save_session(
            token=session_token, url=url, username=who.get("username"),
            provider_id=provider_id, tenant_id=who.get("tenant_id"), expires_at=expires_at,
        )
        ui.print_step(f"Logged in as {who.get('username', '(unknown)')} on {url}.", status="ok")
        return EnterpriseSession(token=session_token, org_id=who.get("tenant_id") or "", expires_at=expires_at)

    def logout(self) -> None:
        session_store.clear_session()

    def current_session(self) -> EnterpriseSession | None:
        session = session_store.load_session()
        if session is None:
            return None
        return EnterpriseSession(
            token=session["token"], org_id=session.get("tenant_id") or "",
            expires_at=session.get("expires_at") or 0,
        )

    def refresh(self) -> EnterpriseSession | None:
        # No refresh-token flow exists yet -- an expired session needs a real `av login`
        # re-run. Returns the still-valid current session unchanged rather than raising.
        return self.current_session()


def run_enterprise_login_flow(repo_root: Path | None = None) -> bool:
    """Attempt enterprise login. Returns True once a real, usable session exists --
    either one already on disk (no re-prompt needed) or a freshly established one."""
    provider = DeviceCodeEnterpriseAuthProvider(repo_root=repo_root)
    existing = provider.current_session()
    if existing is not None:
        return True
    session = provider.login()
    return session is not None
