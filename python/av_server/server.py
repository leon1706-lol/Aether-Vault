import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session, init_db
from .models import DBCommit, DBObject, DBRef, DBTree
from .redis_cache import cache
from .storage import CASStorage

logger = logging.getLogger("av_server")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Aether-Vault Server", version="1.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "HEAD"],
    allow_headers=["*"],
)

DATA_DIR = Path(os.environ.get("AV_DATA_DIR", "/data"))
storage = CASStorage(DATA_DIR)


class RefUpdate(BaseModel):
    commit_hash: str


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    await init_db()
    await cache.init_filter()


# ---------------------------------------------------------------------------
# Merkle Tree builder
# ---------------------------------------------------------------------------

async def build_merkle_tree(db: AsyncSession, tree_data: Dict[str, Any]) -> str:
    """
    Recursively converts the flat path→info dict (from a commit) into a
    content-addressed Merkle Tree stored in DBTree rows.
    Returns the root tree hash.
    """
    nodes: Dict[str, Any] = {}
    for path, info in tree_data.items():
        parts = path.split("/", 1)
        name = parts[0]
        if len(parts) == 1:
            nodes[name] = {"is_dir": False, "info": info}
        else:
            if name not in nodes:
                nodes[name] = {"is_dir": True, "children": {}}
            nodes[name]["children"][parts[1]] = info

    entries = []
    for name, node in sorted(nodes.items()):
        if node["is_dir"]:
            child_hash = await build_merkle_tree(db, node["children"])
            entries.append(
                {"name": name, "child_hash": child_hash, "obj_hash": None, "type": "tree", "size": 0}
            )
        else:
            info = node["info"]
            # Support both flat-tree format {"hash":..., "size":..., "type":...}
            # and legacy code/artifacts split format (just a plain hash string).
            if isinstance(info, str):
                info = {"hash": info, "size": 0, "type": "code"}
            entries.append(
                {
                    "name": name,
                    "child_hash": None,
                    "obj_hash": info.get("hash"),
                    "type": info.get("type", "file"),
                    "size": info.get("size", 0),
                    "layers": info.get("layers", []),
                }
            )

    tree_content = json.dumps(entries, sort_keys=True)
    tree_hash = hashlib.sha256(tree_content.encode()).hexdigest()

    result = await db.execute(
        select(DBTree).where(DBTree.tree_hash == tree_hash).limit(1)
    )
    if not result.first():
        for entry in entries:
            db.add(
                DBTree(
                    tree_hash=tree_hash,
                    path_name=entry["name"],
                    child_tree_hash=entry["child_hash"],
                    object_hash=entry["obj_hash"],
                    type=entry["type"],
                    size=entry["size"],
                    layers=entry.get("layers", []),
                )
            )
        await db.flush()

    return tree_hash


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health_check() -> dict:
    return {"status": "ok", "version": "1.4.0"}


# ---------------------------------------------------------------------------
# Objects (CAS blobs)
# ---------------------------------------------------------------------------

@app.post("/api/objects/{hash}")
async def upload_object(
    hash: str, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")

    # Fast path: Bloom Filter check before hitting DB
    might_exist = await cache.check_hash_exists(hash)
    if might_exist:
        result = await db.execute(select(DBObject).where(DBObject.hash == hash))
        if result.scalar_one_or_none():
            return Response(status_code=409, content="Object already exists")

    try:
        path = await storage.store_object(hash, request.stream())
        size = path.stat().st_size
        db.add(DBObject(hash=hash, size=size))
        await db.commit()
        await cache.add_hash(hash)
        return Response(status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/objects/{hash}")
def download_object(hash: str) -> StreamingResponse:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    obj_path = storage.get_object_path(hash)
    if not obj_path:
        raise HTTPException(status_code=404, detail="Object not found")

    def iterfile():
        with open(obj_path, mode="rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                yield chunk

    return StreamingResponse(iterfile(), media_type="application/octet-stream")


@app.head("/api/objects/{hash}")
async def head_object(
    hash: str, db: AsyncSession = Depends(get_session)
) -> Response:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")

    might_exist = await cache.check_hash_exists(hash)
    if might_exist:
        result = await db.execute(select(DBObject).where(DBObject.hash == hash))
        obj = result.scalar_one_or_none()
        if obj:
            return Response(status_code=200, headers={"Content-Length": str(obj.size)})
    else:
        # Bloom says no → quick fallback to filesystem for safety
        size = storage.get_object_size(hash)
        if size is not None:
            return Response(status_code=200, headers={"Content-Length": str(size)})

    return Response(status_code=404)


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------

@app.post("/api/commits")
async def push_commit(
    commit_data: Dict[str, Any], db: AsyncSession = Depends(get_session)
) -> Response:
    commit_hash = commit_data.get("hash", "")
    if not re.match(r"^[a-f0-9]{64}$", commit_hash):
        raise HTTPException(status_code=400, detail="Invalid commit hash format")

    result = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
    if result.scalar_one_or_none():
        return Response(status_code=409, content="Commit already exists")

    # Support both new flat-tree and legacy {code:{}, artifacts:{}} formats
    raw_tree = commit_data.get("tree", {})
    if "code" in raw_tree or "artifacts" in raw_tree:
        # Flatten legacy format into unified dict
        flat_tree: Dict[str, Any] = {}
        for path, h in raw_tree.get("code", {}).items():
            flat_tree[path] = {"hash": h, "size": 0, "type": "code"}
        for path, info in raw_tree.get("artifacts", {}).items():
            flat_tree[path] = info
        raw_tree = flat_tree

    try:
        root_tree_hash = await build_merkle_tree(db, raw_tree)
        parents: List[str] = commit_data.get("parents", [])
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data.get("message", ""),
            author=commit_data.get("author", "anonymous"),
            parent_hash=parents[0] if parents else None,
            root_tree_hash=root_tree_hash,
            tags=commit_data.get("tags", []),
            metrics=commit_data.get("metrics", {}),
        )
        db.add(new_commit)
        await db.commit()
        return Response(status_code=201)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/commits/{hash}")
async def get_commit(
    hash: str, db: AsyncSession = Depends(get_session)
) -> dict:
    if not re.match(r"^[a-f0-9]{64}$", hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    result = await db.execute(select(DBCommit).where(DBCommit.hash == hash))
    commit = result.scalar_one_or_none()
    if not commit:
        # Fallback: try local CAS file storage (backward compat)
        local = storage.get_commit(hash)
        if local:
            return local
        raise HTTPException(status_code=404, detail="Commit not found")
    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "timestamp": commit.timestamp.isoformat() if commit.timestamp else None,
        "parent_hash": commit.parent_hash,
        "root_tree_hash": commit.root_tree_hash,
        "tags": commit.tags or [],
        "metrics": commit.metrics or {},
    }


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

@app.put("/api/refs/{ref_name:path}")
async def update_ref(
    ref_name: str, payload: RefUpdate, db: AsyncSession = Depends(get_session)
) -> dict:
    stmt = select(DBRef).where(DBRef.name == ref_name).with_for_update()
    result = await db.execute(stmt)
    ref = result.scalar_one_or_none()
    if ref:
        ref.commit_hash = payload.commit_hash
    else:
        db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    await db.commit()
    return {"status": "updated"}


@app.get("/api/refs/{ref_name:path}")
async def get_ref(
    ref_name: str, db: AsyncSession = Depends(get_session)
) -> dict:
    result = await db.execute(select(DBRef).where(DBRef.name == ref_name))
    ref = result.scalar_one_or_none()
    if not ref:
        # Fallback to legacy file-based storage
        commit_hash = storage.get_ref(ref_name)
        if commit_hash:
            return {"ref": ref_name, "commit_hash": commit_hash}
        raise HTTPException(status_code=404, detail="Ref not found")
    return {"ref": ref.name, "commit_hash": ref.commit_hash}


@app.get("/api/refs")
async def list_refs(db: AsyncSession = Depends(get_session)) -> dict:
    result = await db.execute(select(DBRef))
    refs = result.scalars().all()
    if refs:
        return {r.name: r.commit_hash for r in refs}
    # Fallback to legacy storage
    return storage.list_refs()


# ---------------------------------------------------------------------------
# Stats (legacy endpoint preserved)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats() -> dict:
    return storage.get_storage_stats()


# ---------------------------------------------------------------------------
# Garbage Collection
# ---------------------------------------------------------------------------

async def _collect_alive_hashes(
    db: AsyncSession, tree_hash: str, visited: set, alive: set
) -> None:
    """Recursively mark all object hashes reachable from a tree node as alive."""
    if tree_hash in visited:
        return
    visited.add(tree_hash)
    result = await db.execute(select(DBTree).where(DBTree.tree_hash == tree_hash))
    for entry in result.scalars().all():
        if entry.child_tree_hash:
            await _collect_alive_hashes(db, entry.child_tree_hash, visited, alive)
        if entry.object_hash:
            alive.add(entry.object_hash)
        if entry.layers:
            for layer in entry.layers:
                if "hash" in layer:
                    alive.add(layer["hash"])


@app.post("/api/admin/gc")
async def run_garbage_collection(db: AsyncSession = Depends(get_session)) -> dict:
    """
    Mark-and-sweep GC:
    1. Walk every commit's Merkle Tree to collect live hashes.
    2. Delete orphaned DBObject rows and physical shard files.
    3. Delete DBTree rows for trees no longer referenced.
    4. Rebuild the Redis Bloom Filter from surviving hashes.
    """
    try:
        alive_hashes: set = set()
        visited_trees: set = set()

        result = await db.execute(select(DBCommit))
        for commit in result.scalars().all():
            await _collect_alive_hashes(db, commit.root_tree_hash, visited_trees, alive_hashes)

        result = await db.execute(select(DBObject.hash))
        all_db_hashes = set(result.scalars().all())
        dead_hashes = all_db_hashes - alive_hashes

        if dead_hashes:
            await db.execute(delete(DBObject).where(DBObject.hash.in_(list(dead_hashes))))
        if visited_trees:
            await db.execute(
                delete(DBTree).where(DBTree.tree_hash.notin_(list(visited_trees)))
            )

        # Delete orphaned physical shard files
        deleted_count = 0
        for obj_path in storage.data_dir.glob("objects/*/*"):
            if obj_path.is_file():
                h = obj_path.parent.name + obj_path.name
                if h not in alive_hashes:
                    obj_path.unlink()
                    deleted_count += 1

        # Rebuild Bloom Filter
        await cache.reset_filter()
        await cache.init_filter()
        for h in alive_hashes:
            await cache.add_hash(h)

        await db.commit()
        return {
            "status": "success",
            "alive_objects": len(alive_hashes),
            "deleted_objects": deleted_count,
            "reused_trees": len(visited_trees),
        }
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/sync/refs")
async def sync_refs(db: AsyncSession = Depends(get_session)):
    """Endpoint for remote teams to pull all current branch references."""
    result = await db.execute(select(DBRef))
    refs = result.scalars().all()
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "refs": {r.name: r.commit_hash for r in refs}
    }

@app.post("/api/sync/batch-objects")
async def check_objects_batch(hashes: List[str], db: AsyncSession = Depends(get_session)):
    """Check existence of multiple objects at once for faster synchronization."""
    found = []
    definitely_missing = []
    might_exist = []
    
    for h in hashes:
        if await cache.check_hash_exists(h):
            might_exist.append(h)
        else:
            definitely_missing.append(h)
            
    if might_exist:
        result = await db.execute(select(DBObject.hash).where(DBObject.hash.in_(might_exist)))
        db_found = list(result.scalars().all())
    else:
        db_found = []
        
    for h in db_found:
        found.append(h)
        
    db_found_set = set(db_found)
    actually_missing = definitely_missing + [h for h in might_exist if h not in db_found_set]
    
    return {
        "found": found,
        "missing": actually_missing
    }

