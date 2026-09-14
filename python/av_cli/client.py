import hashlib
import os
import time
import uuid
import click
import requests
from pathlib import Path


def _default_timeout() -> tuple[float, float]:
    """(connect, read) timeout in seconds for every request except server_available()
    (which keeps its own short, fixed 2s probe timeout). V1.6.0 (Probleme.md): no request
    in this module had a timeout at all before except that one probe -- a stalled upload or
    download blocked its worker thread forever, with no way to recover short of killing the
    process. AV_HTTP_TIMEOUT overrides the read half only; connect stays a fixed 5s -- a
    hung DNS/TCP handshake should fail fast regardless of how long the caller expects the
    transfer itself to take."""
    read = 120.0
    override = os.environ.get("AV_HTTP_TIMEOUT", "").strip()
    if override:
        try:
            read = float(override)
        except ValueError:
            pass
    return (5.0, read)


class AuthenticationError(Exception):
    """Raised when the server rejects a request with 401 — either no token was sent at all,
    or the one sent doesn't match the server's AV_API_TOKEN ("Protected" mode). Callers (CLI
    commands) catch this to prompt for the current token interactively rather than letting it
    look like a generic network/not-found failure."""


class RefRaceError(Exception):
    """Raised when update_ref(expected_hash=...) loses a compare-and-swap race. `.current`/
    `.expected` carry both hashes so the caller can attribute the race instead of just
    reporting a generic push failure."""

    def __init__(self, ref_name: str, current: str | None, expected: str):
        self.ref_name = ref_name
        self.current = current
        self.expected = expected
        super().__init__(
            f"ref '{ref_name}' race: expected {expected[:7] if expected else expected}, "
            f"server has {current[:7] if current else '(none)'}"
        )


class ObjectIntegrityError(Exception):
    """Raised internally by download_object() when the downloaded bytes' SHA-256 doesn't
    match the hash they were fetched by -- never propagated to a caller (download_object
    catches it and returns False, same contract as any other download failure), just gives
    the log line a precise reason instead of a bare False."""


class VaultClient:
    def __init__(self, server_url: str = 'http://localhost:8000', api_token: str | None = None):
        self.server_url = server_url.rstrip('/')
        self.session = requests.Session()
        if api_token:
            self.session.headers["Authorization"] = f"Bearer {api_token}"
        self._timeout = _default_timeout()

    def _raise_for_auth(self, resp: "requests.Response") -> None:
        if resp.status_code == 401:
            raise AuthenticationError(
                "Server rejected the request (401) — this registry is protected and needs a "
                "valid access token."
            )

    def close(self) -> None:
        """Release the pooled HTTP connections held by the underlying Session."""
        self.session.close()

    def __enter__(self) -> "VaultClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        # Defensive cleanup for callers that don't use the context manager. Guarded because
        # __del__ can run during interpreter shutdown when attributes may already be gone.
        try:
            self.session.close()
        except Exception:
            pass

    def upload_object(self, file_path: Path, sha256_hash: str, known_missing: bool = False) -> bool:
        """Upload `file_path` as object `sha256_hash`. Pass known_missing=True to skip the
        HEAD existence check when the caller already confirmed it's missing."""
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        try:
            if not known_missing:
                head_resp = self.session.head(url, timeout=self._timeout)
                self._raise_for_auth(head_resp)
                if head_resp.status_code == 200:
                    return True # Already exists

            with open(file_path, 'rb') as f:
                resp = self.session.post(url, data=f, timeout=self._timeout)
            self._raise_for_auth(resp)
            return resp.status_code == 201
        except requests.exceptions.RequestException as e:
            click.echo(f"Error uploading object: {e}", err=True)
            return False

    def download_object(self, sha256_hash: str, dest_path: Path) -> bool:
        """Downloads `sha256_hash` to `dest_path` via a temp-file + atomic rename, verifying
        the received bytes' own SHA-256 against `sha256_hash` before publishing (V1.6.0,
        Probleme.md: previously unverified -- a truncated transfer or a compromised/buggy
        server could hand back wrong bytes under a name the caller would then trust as
        content-addressed forever). Verification is streamed (hashlib updated per chunk
        alongside the write), so it costs no extra pass over the data."""
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        tmp_path = None
        try:
            with self.session.get(url, stream=True, timeout=self._timeout) as resp:
                self._raise_for_auth(resp)
                if resp.status_code == 200:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = dest_path.with_name(dest_path.name + f".tmp.{uuid.uuid4().hex}")
                    digest = hashlib.sha256()
                    with open(tmp_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                            f.write(chunk)
                            digest.update(chunk)
                    if digest.hexdigest() != sha256_hash:
                        raise ObjectIntegrityError(
                            f"downloaded content for {sha256_hash[:12]}… hashes to "
                            f"{digest.hexdigest()[:12]}… -- discarding, not publishing a "
                            "corrupt/mismatched object"
                        )
                    tmp_path.replace(dest_path)
                    return True
                return False
        except (requests.exceptions.RequestException, ObjectIntegrityError) as e:
            click.echo(f"Error downloading object: {e}", err=True)
            return False
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    def object_exists(self, sha256_hash: str) -> bool:
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        try:
            resp = self.session.head(url, timeout=self._timeout)
            self._raise_for_auth(resp)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def batch_check_objects(self, sha256_hashes: list[str]) -> set[str]:
        """Subset of `sha256_hashes` that already exist on the server, in a single round
        trip instead of one HEAD request per hash."""
        if not sha256_hashes:
            return set()
        url = f"{self.server_url}/api/sync/batch-objects"
        try:
            resp = self.session.post(url, json=sha256_hashes, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return set(resp.json().get("found", []))
        except requests.exceptions.RequestException as e:
            click.echo(f"Error checking object batch: {e}", err=True)
        return set()

    def push_commit(self, commit_data: dict) -> bool:
        url = f"{self.server_url}/api/commits"
        try:
            resp = self.session.post(url, json=commit_data, timeout=self._timeout)
            self._raise_for_auth(resp)
            return resp.status_code in (201, 409)  # 409 = commit already exists, idempotent success
        except requests.exceptions.RequestException as e:
            click.echo(f"Error pushing commit: {e}", err=True)
            return False

    def get_commit(self, commit_hash: str) -> dict | None:
        url = f"{self.server_url}/api/commits/{commit_hash}"
        try:
            resp = self.session.get(url, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException:
            return None

    def update_ref(self, ref_name: str, commit_hash: str, expected_hash: str | None = None) -> bool:
        """Advances `ref_name` to `commit_hash`. `expected_hash` (optional) requests
        compare-and-swap; a lost race (server returns 409) raises `RefRaceError` rather
        than a bare False, so it's distinguishable from an ordinary network failure."""
        url = f"{self.server_url}/api/refs/{ref_name}"
        payload: dict = {"commit_hash": commit_hash}
        if expected_hash is not None:
            payload["expected_hash"] = expected_hash
        try:
            resp = self.session.put(url, json=payload, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 409:
                detail = {}
                try:
                    detail = resp.json().get("detail") or {}
                except ValueError:
                    pass
                raise RefRaceError(ref_name, detail.get("current"), expected_hash or "")
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def get_ref(self, ref_name: str) -> str | None:
        url = f"{self.server_url}/api/refs/{ref_name}"
        try:
            resp = self.session.get(url, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json().get("commit_hash")
            return None
        except requests.exceptions.RequestException:
            return None

    def list_refs(self, project_id: str | None = None) -> dict:
        """{ref_name: commit_hash}, optionally scoped to one project's refs. Complete:
        the server pages this endpoint since V1.6.3 (default 1000, max 5000 per call),
        so a registry with more refs than one page is walked with `offset` until a short
        page comes back -- callers still get the whole mapping. An older server without
        paging ignores the params and returns everything in the first call."""
        url = f"{self.server_url}/api/refs"
        page_size = 5000
        merged: dict = {}
        offset = 0
        try:
            while True:
                params: dict = {"limit": page_size, "offset": offset}
                if project_id:
                    params["project_id"] = project_id
                resp = self.session.get(url, params=params, timeout=self._timeout)
                self._raise_for_auth(resp)
                if resp.status_code != 200:
                    return merged
                page = resp.json() or {}
                merged.update(page)
                if len(page) < page_size:
                    return merged
                offset += page_size
        except requests.exceptions.RequestException as e:
            click.echo(f"Error listing refs: {e}", err=True)
            return merged

    # V1.5.0: several real call sequences (a commit's own flush-then-check, and
    # materialize_file's per-shard reassembly loop) call server_available() several times
    # within a fraction of a second of each other -- each one a real 2s-timeout-capable HTTP
    # round trip. A 1s per-instance TTL cache turns those into one real probe; it's imper-
    # ceptible to every other caller (nearly all of which call this at most once per command).
    _SERVER_AVAILABLE_CACHE_TTL = 1.0

    def server_available(self) -> bool:
        now = time.monotonic()
        cached = getattr(self, "_server_available_cache", None)
        if cached is not None:
            value, checked_at = cached
            if now - checked_at < self._SERVER_AVAILABLE_CACHE_TTL:
                return value
        # /api/health is always exempt from auth, deliberately — this probe must keep
        # working with zero credentials so callers can ask "is the server up" before
        # anyone has a token configured. Timeout deliberately stays a short fixed 2s
        # (not self._timeout/AV_HTTP_TIMEOUT) -- this is a liveness probe, not a transfer.
        url = f"{self.server_url}/api/health"
        try:
            resp = self.session.get(url, timeout=2)
            value = resp.status_code == 200
        except requests.exceptions.RequestException:
            value = False
        self._server_available_cache = (value, now)
        return value

    def report_run_policy_outcome(self, run_id: str, decision: str, rule: str | None) -> bool:
        """Best-effort telemetry: records a promote()/enforce_policy() decision against the
        active run. Never raises — a False return is a no-op, never a block on the
        promotion/merge itself."""
        url = f"{self.server_url}/api/runs/{run_id}/policy-outcome"
        try:
            resp = self.session.post(url, json={"decision": decision, "rule": rule}, timeout=self._timeout)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def run_gc(self) -> dict | None:
        """Trigger garbage collection on the remote server. Known to run slow (a full
        mark-and-sweep over the whole registry) -- a longer read timeout than the module
        default, not affected by AV_HTTP_TIMEOUT."""
        url = f"{self.server_url}/api/admin/gc"
        try:
            resp = self.session.post(url, timeout=(self._timeout[0], 300.0))
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException as e:
            click.echo(f"Error running GC: {e}", err=True)
            return None

    def fetch_all_refs(self) -> dict:
        url = f"{self.server_url}/api/sync/refs"
        refs = {}
        offset = 0
        limit = 1000
        while True:
            try:
                resp = self.session.get(url, params={"limit": limit, "offset": offset}, timeout=self._timeout)
                self._raise_for_auth(resp)
                if resp.status_code == 200:
                    data = resp.json()
                    refs.update(data.get("refs", {}))
                    next_offset = data.get("next_offset")
                    if next_offset is None:
                        break
                    offset = next_offset
                else:
                    break
            except requests.exceptions.RequestException as e:
                click.echo(f"Error syncing refs: {e}", err=True)
                break
        return refs

    def list_projects(self) -> list[dict]:
        """Every project that has pushed to this registry: {project_id, project_name,
        commit_count, last_push} rows. Empty list on any failure."""
        url = f"{self.server_url}/api/projects"
        try:
            resp = self.session.get(url, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json().get("projects", [])
            return []
        except requests.exceptions.RequestException as e:
            click.echo(f"Error listing projects: {e}", err=True)
            return []

    def list_commits(self, project_id: str, limit: int = 500, offset: int = 0,
                     include_layers: bool = False) -> dict | None:
        """One page of a project's commits, newest first. `include_layers=True` attaches
        each commit's fully-resolved tree so clones are self-sufficient offline."""
        url = f"{self.server_url}/api/commits"
        try:
            resp = self.session.get(url, params={
                "project_id": project_id,
                "limit": limit,
                "offset": offset,
                "include_layers": "true" if include_layers else "false",
            }, timeout=self._timeout)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException as e:
            click.echo(f"Error listing commits: {e}", err=True)
            return None
