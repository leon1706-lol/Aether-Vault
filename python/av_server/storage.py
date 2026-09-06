import os
import json
import shutil
import hashlib
from pathlib import Path
from typing import AsyncIterator

class CASStorage:
    def __init__(self, base_path: Path = Path('/data')):
        self.base_path = base_path
        self.objects_dir = self.base_path / 'objects'
        self.commits_dir = self.base_path / 'commits'
        self.refs_dir = self.base_path / 'refs'
        
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.commits_dir.mkdir(parents=True, exist_ok=True)
        self.refs_dir.mkdir(parents=True, exist_ok=True)

    def _object_path(self, sha256_hash: str, tenant_id: str | None = None) -> Path:
        """`tenant_id=None` (default `shared` isolation mode) is the exact flat layout
        this method has always used. A real tenant_id (`AV_CAS_ISOLATION=isolated`)
        nests the object under its own tenant directory instead, so two tenants'
        identical bytes are physically stored twice rather than deduplicated."""
        if tenant_id is None:
            return self.objects_dir / sha256_hash[:2] / sha256_hash[2:]
        return self.objects_dir / tenant_id / sha256_hash[:2] / sha256_hash[2:]

    def _safe_ref_path(self, ref_name: str) -> Path:
        """Resolve a ref name under refs_dir, rejecting any path-traversal escape.

        Defence in depth: even though callers validate ref names, never let a crafted
        name resolve outside the refs directory (arbitrary file read/write).
        """
        p = (self.refs_dir / ref_name).resolve()
        refs_root = self.refs_dir.resolve()
        if refs_root != p and refs_root not in p.parents:
            raise ValueError(f"Ref name escapes refs directory: {ref_name}")
        return p

    async def store_object(self, sha256_hash: str, data_stream: AsyncIterator[bytes],
                           tenant_id: str | None = None) -> Path:
        target_path = self._object_path(sha256_hash, tenant_id)
        if target_path.exists():
            return target_path
            
        target_path.parent.mkdir(parents=True, exist_ok=True)
        import uuid
        temp_path = target_path.with_name(target_path.name + f".tmp.{uuid.uuid4().hex}")
        
        sha256 = hashlib.sha256()
        try:
            with open(temp_path, 'wb') as f:
                async for chunk in data_stream:
                    f.write(chunk)
                    sha256.update(chunk)
                    
            actual_hash = sha256.hexdigest()
            if actual_hash != sha256_hash:
                raise ValueError(f"Hash mismatch. Expected {sha256_hash}, got {actual_hash}")
                
            shutil.move(temp_path, target_path)
            return target_path
        except Exception:
            # Clean up temp file only on error — never after successful move
            if temp_path.exists():
                temp_path.unlink()
            raise

    def object_exists(self, sha256_hash: str, tenant_id: str | None = None) -> bool:
        return self.get_object_path(sha256_hash, tenant_id) is not None

    def get_object_path(self, sha256_hash: str, tenant_id: str | None = None) -> Path | None:
        """`tenant_id` given (isolated mode): checks the tenant-scoped path FIRST, then
        falls back to the flat legacy path, so an object stored before switching to
        isolated mode keeps being served with zero migration step."""
        if tenant_id is not None:
            scoped = self._object_path(sha256_hash, tenant_id)
            if scoped.exists():
                return scoped
        flat = self._object_path(sha256_hash, None)
        return flat if flat.exists() else None

    def get_object_size(self, sha256_hash: str, tenant_id: str | None = None) -> int | None:
        p = self.get_object_path(sha256_hash, tenant_id)
        return p.stat().st_size if p else None

    def store_commit(self, commit_hash: str, commit_data: dict) -> None:
        p = self.commits_dir / f"{commit_hash}.json"
        temp_p = p.with_suffix('.tmp')
        with open(temp_p, 'w') as f:
            json.dump(commit_data, f, indent=2)
        shutil.move(temp_p, p)

    def get_commit(self, commit_hash: str) -> dict | None:
        p = self.commits_dir / f"{commit_hash}.json"
        if not p.exists():
            return None
        with open(p, 'r') as f:
            return json.load(f)

    def update_ref(self, ref_name: str, commit_hash: str) -> None:
        p = self._safe_ref_path(ref_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        temp_p = p.with_name(p.name + ".tmp")
        with open(temp_p, 'w') as f:
            f.write(commit_hash)
        shutil.move(temp_p, p)

    def get_ref(self, ref_name: str) -> str | None:
        p = self._safe_ref_path(ref_name)
        if not p.exists():
            return None
        return p.read_text().strip()

    def list_refs(self) -> dict:
        refs = {}
        for root, _, files in os.walk(self.refs_dir):
            for file in files:
                rel_path = str(Path(root).relative_to(self.refs_dir) / file)
                rel_path = rel_path.replace('\\', '/').lstrip('./')
                refs[rel_path] = (Path(root) / file).read_text().strip()
        return refs

    def get_storage_stats(self) -> dict:
        total_objects = 0
        total_size = 0
        for root, _, files in os.walk(self.objects_dir):
            for file in files:
                total_objects += 1
                total_size += (Path(root) / file).stat().st_size
                
        total_commits = len(list(self.commits_dir.glob("*.json")))
        total_refs = sum(len(files) for _, _, files in os.walk(self.refs_dir))
        
        return {
            "total_objects": total_objects,
            "total_commits": total_commits,
            "total_refs": total_refs,
            "total_size_bytes": total_size
        }
