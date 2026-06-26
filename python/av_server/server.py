import hashlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session, init_db
from .models import DBCommit, DBObject, DBRef, DBTree, utcnow_naive
from .redis_cache import cache
from .storage import CASStorage

logger = logging.getLogger("av_server")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replaces the deprecated @app.on_event("startup") hook.
    await init_db()
    await cache.init_filter()
    yield


app = FastAPI(title="Aether-Vault Server", version="1.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "HEAD"],
    allow_headers=["*"],
)

DATA_DIR = Path(os.environ.get("AV_DATA_DIR", "/data"))
storage = CASStorage(DATA_DIR)

# --- Request size guards for push_commit (reject hostile/oversized payloads early) ---
MAX_TREE_ENTRIES = 100_000
MAX_METRICS = 1_000
MAX_TAGS = 200
MAX_MESSAGE_LEN = 20_000
MAX_TAG_LEN = 200


class RefUpdate(BaseModel):
    commit_hash: str


# Ref names end up as filesystem paths in the legacy CASStorage fallback
# (refs_dir / ref_name). Because the route uses {ref_name:path}, a raw value like
# "../../etc/passwd" would otherwise escape the data directory (path traversal / LFI).
# Allow only safe, relative, slash-delimited names.
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def validate_ref_name(ref_name: str) -> str:
    if (
        not ref_name
        or not _REF_NAME_RE.match(ref_name)
        or ref_name.startswith("/")
        or "\\" in ref_name
        or ".." in ref_name.split("/")
    ):
        raise HTTPException(status_code=400, detail="Invalid ref name")
    return ref_name


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
    except IntegrityError:
        # A concurrent upload of the same hash inserted the row first. CAS is idempotent
        # (identical content), so treat the duplicate as success rather than a 500.
        await db.rollback()
        await cache.add_hash(hash)
        return Response(status_code=409, content="Object already exists")
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

    # Reject oversized/abusive payloads before doing any DB work (the endpoint is otherwise
    # unauthenticated; without bounds a single request could store an unbounded tree/metrics
    # blob). These limits are generous for real ML repos but cap pathological input.
    raw_tree = commit_data.get("tree", {})
    if not isinstance(raw_tree, dict) or len(raw_tree) > MAX_TREE_ENTRIES:
        raise HTTPException(status_code=422, detail="Commit tree too large or malformed")
    metrics = commit_data.get("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS:
        raise HTTPException(status_code=422, detail="Too many metrics or malformed")
    tags = commit_data.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or any(
        not isinstance(t, str) or len(t) > MAX_TAG_LEN for t in tags
    ):
        raise HTTPException(status_code=422, detail="Too many/oversized tags or malformed")
    if len(commit_data.get("message", "") or "") > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=422, detail="Commit message too long")

    # Per-project separation: every repo gets a project_id at `av init` (backfilled for repos
    # initialized before this was added — see python/av_cli/main.py's load_config). Fall back
    # to a single "legacy" bucket rather than rejecting the push outright, so an older client
    # that hasn't picked up the backfill yet still syncs instead of erroring.
    project_id = commit_data.get("project_id") or "legacy"
    project_name = commit_data.get("project_name") or "Legacy / Unknown"
    if not isinstance(project_id, str) or len(project_id) > 128 or not isinstance(project_name, str) or len(project_name) > 200:
        raise HTTPException(status_code=422, detail="Invalid project_id/project_name")

    # Support both new flat-tree and legacy {code:{}, artifacts:{}} formats
    if "code" in raw_tree or "artifacts" in raw_tree:
        # Flatten legacy format into unified dict
        flat_tree: Dict[str, Any] = {}
        for path, h in raw_tree.get("code", {}).items():
            flat_tree[path] = {"hash": h, "size": 0, "type": "code"}
        for path, info in raw_tree.get("artifacts", {}).items():
            flat_tree[path] = info
        raw_tree = flat_tree

    # Persist the author-supplied commit time rather than the insert time, otherwise
    # commits flushed late from the client's pending-push queue would sort as "newest"
    # in the dashboard despite being authored earlier. Fall back to now() if absent/invalid.
    commit_ts = None
    raw_ts = commit_data.get("timestamp")
    if raw_ts:
        try:
            parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            # Store naive UTC to stay consistent with the model's utcnow() default column.
            commit_ts = parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except (ValueError, TypeError):
            commit_ts = None

    try:
        root_tree_hash = await build_merkle_tree(db, raw_tree)
        parents: List[str] = commit_data.get("parents", [])
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data.get("message", ""),
            author=commit_data.get("author", "anonymous"),
            parent_hash=parents[0] if parents else None,
            root_tree_hash=root_tree_hash,
            tags=tags,
            metrics=metrics,
            project_id=project_id,
            project_name=project_name,
        )
        if commit_ts is not None:
            new_commit.timestamp = commit_ts
        db.add(new_commit)
        await db.commit()
        return Response(status_code=201)
    except IntegrityError as exc:
        await db.rollback()
        # An IntegrityError here is NOT necessarily "this commit hash already exists" — it can
        # equally be a FK violation on DBTree.object_hash (a tree entry references an object
        # that the client hasn't uploaded yet) or on DBRef in a later request. Blindly mapping
        # every IntegrityError to 409 previously caused commits referencing not-yet-uploaded
        # objects to be silently dropped: the client (which treats 409 as idempotent success,
        # by design, for genuine duplicate-hash races) believed the push succeeded while the
        # commit/tree never actually made it into the database. Re-check what actually
        # happened before deciding the response.
        recheck = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
        if recheck.scalar_one_or_none():
            return Response(status_code=409, content="Commit already exists")
        # Anything other than 201/409 is treated as a failed push by the client (it retries /
        # keeps the commit queued) — unlike the bug above, this must NOT be 409.
        raise HTTPException(
            status_code=500,
            detail=(
                "Commit references an object or tree that violates a database constraint "
                f"(commit not stored — are all objects uploaded first?): {exc}"
            ),
        )
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
    # Rebuild the tree from the DB Merkle Tree.
    # Level-order traversal with one batched query per depth level (was one query per node,
    # i.e. N+1). Because identical subtrees are deduplicated, the same tree_hash can appear
    # under several paths, so we carry a list of path prefixes per hash for each level.
    async def resolve_tree(root_hash: str) -> dict:
        tree_data: dict = {}
        frontier: list[tuple[str, str]] = [(root_hash, "")]  # (tree_hash, path_prefix)
        while frontier:
            prefixes_by_hash: Dict[str, List[str]] = {}
            for th, prefix in frontier:
                prefixes_by_hash.setdefault(th, []).append(prefix)

            rows = (
                await db.execute(
                    select(DBTree).where(DBTree.tree_hash.in_(list(prefixes_by_hash.keys())))
                )
            ).scalars().all()
            rows_by_hash: Dict[str, list] = {}
            for r in rows:
                rows_by_hash.setdefault(r.tree_hash, []).append(r)

            next_frontier: list[tuple[str, str]] = []
            for th, prefixes in prefixes_by_hash.items():
                for prefix in prefixes:
                    for entry in rows_by_hash.get(th, []):
                        full_path = f"{prefix}/{entry.path_name}" if prefix else entry.path_name
                        if entry.child_tree_hash:
                            next_frontier.append((entry.child_tree_hash, full_path))
                        else:
                            tree_data[full_path] = {
                                "hash": entry.object_hash,
                                "size": entry.size,
                                "type": entry.type,
                                "layers": entry.layers or [],
                            }
            frontier = next_frontier
        return tree_data

    tree_data = await resolve_tree(commit.root_tree_hash) if commit.root_tree_hash else {}

    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "timestamp": commit.timestamp.isoformat() if commit.timestamp else None,
        "parent_hash": commit.parent_hash,
        "root_tree_hash": commit.root_tree_hash,
        "tags": commit.tags or [],
        "metrics": commit.metrics or {},
        "tree": tree_data,
        "project_id": commit.project_id,
        "project_name": commit.project_name,
    }


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

@app.put("/api/refs/{ref_name:path}")
async def update_ref(
    ref_name: str, payload: RefUpdate, db: AsyncSession = Depends(get_session)
) -> dict:
    ref_name = validate_ref_name(ref_name)
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
    ref_name = validate_ref_name(ref_name)
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
async def list_refs(project_id: Optional[str] = None, db: AsyncSession = Depends(get_session)) -> dict:
    # Refs are namespaced "<project_id>/<branch>" by the client (see av_cli/main.py's
    # `commit` command) rather than via a DB column, since the ref-name path parameter
    # already supports slashes and is already validated — no schema change needed here.
    query = select(DBRef)
    if project_id:
        query = query.where(DBRef.name.like(f"{project_id}/%"))
    result = await db.execute(query)
    refs = result.scalars().all()
    if refs:
        return {r.name: r.commit_hash for r in refs}
    if project_id:
        return {}
    # Fallback to legacy storage
    return storage.list_refs()


# ---------------------------------------------------------------------------
# Stats (legacy endpoint preserved)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_session)) -> dict:
    # Previously this walked the entire CAS objects directory and stat()ed every shard on
    # every call — and the Web UI polls it every ~15s. Use indexed DB aggregates instead;
    # fall back to the filesystem only when the DB has no objects yet (legacy/empty state).
    total_objects = (await db.execute(select(func.count(DBObject.hash)))).scalar_one()
    if total_objects == 0:
        return storage.get_storage_stats()

    total_size = (await db.execute(select(func.sum(DBObject.size)))).scalar_one() or 0
    total_commits = (await db.execute(select(func.count(DBCommit.hash)))).scalar_one()
    total_refs = (await db.execute(select(func.count(DBRef.name)))).scalar_one()
    return {
        "total_objects": total_objects,
        "total_commits": total_commits,
        "total_refs": total_refs,
        "total_size_bytes": total_size,
    }


# ---------------------------------------------------------------------------
# Garbage Collection
# ---------------------------------------------------------------------------

# Objects newer than this many seconds are never collected, even if no commit references
# them yet. A client uploads object shards first and pushes the commit afterwards, so a GC
# running inside that window would otherwise delete a live object whose commit is still
# in-flight. This grace period closes that race without needing a global GC/upload lock.
GC_GRACE_SECONDS = 3600

# Delete in batches to stay well under driver bind-parameter limits (asyncpg ~32k).
_GC_DELETE_BATCH = 500


def _collect_alive_in_memory(
    root_hash: Optional[str], tree_map: Dict[str, list], visited: set, alive: set
) -> None:
    """Iteratively mark every object/layer hash reachable from a root tree as alive.

    Operates over a pre-loaded {tree_hash: [entries]} map, so the whole GC mark phase costs
    a single DBTree query instead of one query per tree node (was N+1 and recursive).
    """
    stack = [root_hash]
    while stack:
        th = stack.pop()
        if not th or th in visited:
            continue
        visited.add(th)
        for entry in tree_map.get(th, []):
            if entry.child_tree_hash:
                stack.append(entry.child_tree_hash)
            if entry.object_hash:
                alive.add(entry.object_hash)
            if entry.layers:
                for layer in entry.layers:
                    if isinstance(layer, dict) and "hash" in layer:
                        alive.add(layer["hash"])


@app.post("/api/admin/gc")
async def run_garbage_collection(db: AsyncSession = Depends(get_session)) -> dict:
    """
    Mark-and-sweep GC:
    1. Walk every commit's Merkle Tree to collect live hashes.
    2. Delete orphaned DBObject rows and physical shard files (respecting a grace period
       so concurrently-uploaded-but-not-yet-committed objects are not reaped).
    3. Delete DBTree rows for trees no longer referenced.
    4. Rebuild the Redis Bloom Filter from surviving hashes.
    """
    import asyncio
    from datetime import timedelta

    try:
        gc_cutoff = utcnow_naive() - timedelta(seconds=GC_GRACE_SECONDS)

        # --- Mark phase: load all trees once, traverse in memory (no N+1) ---
        all_trees = (await db.execute(select(DBTree))).scalars().all()
        tree_map: Dict[str, list] = {}
        for entry in all_trees:
            tree_map.setdefault(entry.tree_hash, []).append(entry)

        alive_hashes: set = set()
        visited_trees: set = set()
        for commit in (await db.execute(select(DBCommit))).scalars().all():
            _collect_alive_in_memory(commit.root_tree_hash, tree_map, visited_trees, alive_hashes)

        # --- Sweep DB objects (protect recently-created rows via grace period) ---
        obj_rows = (await db.execute(select(DBObject.hash, DBObject.created_at))).all()
        dead_hashes = {
            h for (h, created_at) in obj_rows
            if h not in alive_hashes and (created_at is None or created_at < gc_cutoff)
        }

        dead_list = list(dead_hashes)
        for i in range(0, len(dead_list), _GC_DELETE_BATCH):
            batch = dead_list[i : i + _GC_DELETE_BATCH]
            await db.execute(delete(DBObject).where(DBObject.hash.in_(batch)))

        if visited_trees:
            dead_trees = [th for th in tree_map if th not in visited_trees]
            for i in range(0, len(dead_trees), _GC_DELETE_BATCH):
                batch = dead_trees[i : i + _GC_DELETE_BATCH]
                await db.execute(delete(DBTree).where(DBTree.tree_hash.in_(batch)))

        # --- Sweep physical shard files (skip alive + recently-written, off the event loop) ---
        loop = asyncio.get_running_loop()
        # gc_cutoff is a *naive* datetime that represents UTC (see utcnow_naive()'s docstring).
        # Calling .timestamp() directly on a naive datetime makes Python treat it as *local*
        # time, silently shifting the resulting epoch by the host's UTC offset — on a host
        # ahead of UTC this makes grace_ts artificially too early, so st_mtime (a real,
        # correctly-UTC-based epoch) almost never looks "old enough" and physical shards are
        # never actually swept; on a host behind UTC it would do the opposite and delete
        # objects *before* their real grace window expires. Attaching tzinfo=utc first makes
        # .timestamp() compute the correct epoch regardless of the host's local timezone.
        grace_ts = gc_cutoff.replace(tzinfo=timezone.utc).timestamp()

        def purge_orphans():
            count = 0
            for obj_path in storage.objects_dir.glob("*/*"):
                if obj_path.is_file():
                    h = obj_path.parent.name + obj_path.name
                    if h in alive_hashes:
                        continue
                    # Never delete a shard written during/after the grace window — its
                    # commit may still be on its way from the client.
                    if obj_path.stat().st_mtime >= grace_ts:
                        continue
                    obj_path.unlink()
                    count += 1
            return count

        deleted_count = await loop.run_in_executor(None, purge_orphans)

        # Rebuild Bloom Filter from the surviving set
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
async def sync_refs(limit: int = 1000, offset: int = 0, db: AsyncSession = Depends(get_session)):
    """Endpoint for remote teams to pull branch references with pagination."""
    result = await db.execute(select(DBRef).limit(limit).offset(offset))
    refs = result.scalars().all()
    return {
        "timestamp": utcnow_naive().isoformat(),
        "refs": {r.name: r.commit_hash for r in refs},
        "next_offset": offset + limit if len(refs) == limit else None
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


# ---------------------------------------------------------------------------
# Web UI Dashboard Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/commits")
async def list_commits(
    limit: int = 50,
    offset: int = 0,
    project_id: Optional[str] = None,
    db: AsyncSession = Depends(get_session)
) -> dict:
    """Paginated commit list for the Web UI dashboard, newest first.

    Optionally scoped to a single project via ?project_id= — without it, commits from every
    project on this shared registry are returned (matches the dashboard's pre-existing
    behavior so it doesn't break for callers that don't know about projects yet).
    """
    query = select(DBCommit)
    count_query = select(func.count(DBCommit.hash))
    if project_id:
        query = query.where(DBCommit.project_id == project_id)
        count_query = count_query.where(DBCommit.project_id == project_id)

    result = await db.execute(query.order_by(DBCommit.timestamp.desc()).limit(limit).offset(offset))
    commits = result.scalars().all()
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    return {
        "commits": [
            {
                "hash": c.hash,
                "message": c.message,
                "author": c.author,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                "parent_hash": c.parent_hash,
                "root_tree_hash": c.root_tree_hash,
                "tags": c.tags or [],
                "metrics": c.metrics or {},
                "project_id": c.project_id,
                "project_name": c.project_name,
            }
            for c in commits
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
    }


@app.get("/api/projects")
async def list_projects(db: AsyncSession = Depends(get_session)) -> dict:
    """Every project that has ever pushed a commit to this registry, for the Web UI's
    Projects tab (lets a user discover and switch between local repos sharing this server)."""
    result = await db.execute(
        select(
            DBCommit.project_id,
            DBCommit.project_name,
            func.count(DBCommit.hash).label("commit_count"),
            func.max(DBCommit.timestamp).label("last_push"),
        ).group_by(DBCommit.project_id, DBCommit.project_name)
        .order_by(func.max(DBCommit.timestamp).desc())
    )
    return {
        "projects": [
            {
                "project_id": row.project_id,
                "project_name": row.project_name,
                "commit_count": row.commit_count,
                "last_push": row.last_push.isoformat() if row.last_push else None,
            }
            for row in result.all()
        ]
    }


@app.get("/api/dashboard/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_session)) -> dict:
    """Unified summary endpoint for the Web UI dashboard home page."""
    # Commits
    commit_result = await db.execute(
        select(DBCommit).order_by(DBCommit.timestamp.desc()).limit(50)
    )
    commits = commit_result.scalars().all()

    total_commits_result = await db.execute(select(func.count(DBCommit.hash)))
    total_commits = total_commits_result.scalar_one()

    # Refs
    ref_result = await db.execute(select(DBRef))
    refs = ref_result.scalars().all()

    # Objects count and size
    total_objects_result = await db.execute(select(func.count(DBObject.hash)))
    total_objects = total_objects_result.scalar_one()
    
    total_size_result = await db.execute(select(func.sum(DBObject.size)))
    total_size = total_size_result.scalar_one() or 0

    return {
        "server_version": "1.4.0",
        "total_commits": total_commits,
        "total_branches": len(refs),
        "total_objects": total_objects,
        "total_size_bytes": total_size,
        "refs": {r.name: r.commit_hash for r in refs},
        "recent_commits": [
            {
                "hash": c.hash,
                "message": c.message,
                "author": c.author,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                "parent_hash": c.parent_hash,
                "root_tree_hash": c.root_tree_hash,
                "tags": c.tags or [],
                "metrics": c.metrics or {},
            }
            for c in commits[:20]
        ],
    }
