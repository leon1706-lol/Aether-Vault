import os
import uuid
import requests
from pathlib import Path

class VaultClient:
    def __init__(self, server_url: str = 'http://localhost:8000'):
        self.server_url = server_url.rstrip('/')
        self.session = requests.Session()

    def upload_object(self, file_path: Path, sha256_hash: str) -> bool:
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        try:
            head_resp = self.session.head(url)
            if head_resp.status_code == 200:
                return True # Already exists
            
            with open(file_path, 'rb') as f:
                resp = self.session.post(url, data=f)
            return resp.status_code == 201
        except requests.exceptions.RequestException as e:
            print(f"Error uploading object: {e}")
            return False

    def download_object(self, sha256_hash: str, dest_path: Path) -> bool:
        url = f"{self.server_url}/api/objects/{sha256_hash}"
        tmp_path = None
        try:
            with self.session.get(url, stream=True) as resp:
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
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def push_commit(self, commit_data: dict) -> bool:
        url = f"{self.server_url}/api/commits"
        try:
            resp = self.session.post(url, json=commit_data)
            return resp.status_code in (201, 409)  # 409 = commit already exists, idempotent success
        except requests.exceptions.RequestException as e:
            print(f"Error pushing commit: {e}")
            return False

    def get_commit(self, commit_hash: str) -> dict | None:
        url = f"{self.server_url}/api/commits/{commit_hash}"
        try:
            resp = self.session.get(url)
            if resp.status_code == 200:
                return resp.json()
            return None
        except requests.exceptions.RequestException:
            return None

    def update_ref(self, ref_name: str, commit_hash: str) -> bool:
        url = f"{self.server_url}/api/refs/{ref_name}"
        try:
            resp = self.session.put(url, json={"commit_hash": commit_hash})
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def get_ref(self, ref_name: str) -> str | None:
        url = f"{self.server_url}/api/refs/{ref_name}"
        try:
            resp = self.session.get(url)
            if resp.status_code == 200:
                return resp.json().get("commit_hash")
            return None
        except requests.exceptions.RequestException:
            return None

    def server_available(self) -> bool:
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
