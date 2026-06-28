import os
import uuid
import requests
from pathlib import Path


class AuthenticationError(Exception):
    """Raised when the server rejects a request with 401 — either no token was sent at all,
    or the one sent doesn't match the server's AV_API_TOKEN ("Protected" mode). Callers (CLI
    commands) catch this to prompt for the current token interactively rather than letting it
    look like a generic network/not-found failure."""


class VaultClient:
    def __init__(self, server_url: str = 'http://localhost:8000', api_token: str | None = None):
        self.server_url = server_url.rstrip('/')
        self.session = requests.Session()
        if api_token:
            self.session.headers["Authorization"] = f"Bearer {api_token}"

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
        """Upload `file_path` as object `sha256_hash`.

        Pass known_missing=True when the caller already confirmed (e.g. via
        batch_check_objects) that the server doesn't have this hash, to skip the HEAD
        existence check and go straight to the POST.
        """
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        try:
            if not known_missing:
                head_resp = self.session.head(url)
                self._raise_for_auth(head_resp)
                if head_resp.status_code == 200:
                    return True # Already exists

            with open(file_path, 'rb') as f:
                resp = self.session.post(url, data=f)
            self._raise_for_auth(resp)
            return resp.status_code == 201
        except requests.exceptions.RequestException as e:
            print(f"Error uploading object: {e}")
            return False

    def download_object(self, sha256_hash: str, dest_path: Path) -> bool:
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        tmp_path = None
        try:
            with self.session.get(url, stream=True) as resp:
                self._raise_for_auth(resp)
                if resp.status_code == 200:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = dest_path.with_name(dest_path.name + f".tmp.{uuid.uuid4().hex}")
                    with open(tmp_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                            f.write(chunk)
                    tmp_path.replace(dest_path)
                    return True
                return False
        except requests.exceptions.RequestException as e:
            print(f"Error downloading object: {e}")
            return False
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    def object_exists(self, sha256_hash: str) -> bool:
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        try:
            resp = self.session.head(url)
            self._raise_for_auth(resp)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def batch_check_objects(self, sha256_hashes: list[str]) -> set[str]:
        """Return the subset of `sha256_hashes` that already exist on the server.

        Single round trip via `/api/sync/batch-objects` instead of one HEAD request per
        hash — callers uploading many objects (e.g. a commit) should check existence here
        before falling back to per-object HEAD checks.
        """
        if not sha256_hashes:
            return set()
        url = f"{self.server_url}/api/sync/batch-objects"
        try:
            resp = self.session.post(url, json=sha256_hashes)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return set(resp.json().get("found", []))
        except requests.exceptions.RequestException as e:
            print(f"Error checking object batch: {e}")
        return set()

    def push_commit(self, commit_data: dict) -> bool:
        url = f"{self.server_url}/api/commits"
        try:
            resp = self.session.post(url, json=commit_data)
            self._raise_for_auth(resp)
            return resp.status_code in (201, 409)  # 409 = commit already exists, idempotent success
        except requests.exceptions.RequestException as e:
            print(f"Error pushing commit: {e}")
            return False

    def get_commit(self, commit_hash: str) -> dict | None:
        url = f"{self.server_url}/api/commits/{commit_hash}"
        try:
            resp = self.session.get(url)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException:
            return None

    def update_ref(self, ref_name: str, commit_hash: str) -> bool:
        url = f"{self.server_url}/api/refs/{ref_name}"
        try:
            resp = self.session.put(url, json={"commit_hash": commit_hash})
            self._raise_for_auth(resp)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def get_ref(self, ref_name: str) -> str | None:
        url = f"{self.server_url}/api/refs/{ref_name}"
        try:
            resp = self.session.get(url)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json().get("commit_hash")
            return None
        except requests.exceptions.RequestException:
            return None

    def server_available(self) -> bool:
        # /api/health is always exempt from auth (server.py's require_token middleware), even
        # in Protected mode — never raises AuthenticationError, deliberately: this is the one
        # probe that must keep working with zero credentials so "is the server up at all" stays
        # answerable before anyone has a token configured (and so restart_service's own
        # readiness wait, which calls this, never deadlocks against a freshly-protected server).
        url = f"{self.server_url}/api/health"
        try:
            resp = self.session.get(url, timeout=2)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def run_gc(self) -> dict | None:
        """Trigger garbage collection on the remote server."""
        url = f"{self.server_url}/api/admin/gc"
        try:
            resp = self.session.post(url)
            self._raise_for_auth(resp)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException as e:
            print(f"Error running GC: {e}")
            return None

    def fetch_all_refs(self) -> dict:
        url = f"{self.server_url}/api/sync/refs"
        refs = {}
        offset = 0
        limit = 1000
        while True:
            try:
                resp = self.session.get(url, params={"limit": limit, "offset": offset})
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
                print(f"Error syncing refs: {e}")
                break
        return refs
