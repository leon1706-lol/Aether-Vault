import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from .database import async_session_factory, get_session, init_db
from . import rate_limit
from .models import (
    DBAuditLog,
    DBCommit,
    DBEvent,
    DBObject,
    DBRef,
    DBRun,
    DBRunCommit,
    DBTree,
    DBWebhook,
    DBWebhookDelivery,
    _new_uuid,
    utcnow_naive,
)
from .redis_cache import cache
from .storage import CASStorage

logger = logging.getLogger("av_server")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Webhook delivery retry worker (v1.2.2): failed deliveries persist with a next_retry_at
# and are re-driven on an interval until AV_WEBHOOK_MAX_ATTEMPTS is exhausted → dead-letter.
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("AV_WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_RETRY_INTERVAL_SECS = int(os.environ.get("AV_WEBHOOK_RETRY_INTERVAL_SECS", "30"))
# Terminal-status (delivered/dead) delivery rows are swept with the event retention window.
AUDIT_RETENTION_DAYS = int(os.environ.get("AV_AUDIT_RETENTION_DAYS", "90"))


async def _webhook_retry_worker(interval_secs: float = 30.0):
    """Background loop re-driving due webhook deliveries. Never raises outward — a tick
    that fails logs and retries on the next interval."""
    while True:
        await asyncio.sleep(interval_secs)
        try:
            await process_due_webhook_deliveries()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook retry worker tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replaces the deprecated @app.on_event("startup") hook.
    await init_db()
    await cache.init_filter()
    worker = asyncio.create_task(_webhook_retry_worker(WEBHOOK_RETRY_INTERVAL_SECS))
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title="Aether-Vault Server", version="1.4.0", lifespan=lifespan)

# --- Authentication ("Protected" mode) ------------------------------------------
# Two credential sources, both optional, both read once at process start (matching
# DATA_DIR below: `av auth ...` writes .env and restarts the service, so a fresh process
# always picks up changes — no per-request re-read needed):
#
#   AV_API_TOKEN   the owner's shared secret (legacy single-key mode, still fully valid)
#   AV_AUTH_USERS  JSON map {"username": "token", ...} — per-user access tokens
#                  (managed by `av auth add-user/list-users/remove-user`)
#
# A request is authenticated when its Bearer token matches EITHER source; the resolved
# username ("owner" for the shared secret) is stored on request.state.username and used
# by push_commit to attribute commits whose client sent author="anonymous". Both empty
# = Anonymous mode: every route behaves exactly as it always has — no auth at all.
AV_API_TOKEN = os.environ.get("AV_API_TOKEN", "").strip()

# Always reachable even in Protected mode:
# - /api/health: Docker healthchecks and VaultClient.server_available() depend on this being
#   checkable with no credentials — docker_runtime.restart_service()'s own readiness wait calls
#   server_available(), so gating health would make a freshly-protected server look perpetually
#   unreachable to the very code restarting it.
# - /docs, /openapi.json, /redoc: FastAPI's bundled Swagger/ReDoc UI has no way to attach our
#   custom Bearer header, so gating them would just break the webui's "API Docs" link with no
#   real security benefit — they expose the API's shape, not any actual data.
_AUTH_EXEMPT_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}


def _parse_auth_users(raw: str | None) -> dict[str, str]:
    """Parses the AV_AUTH_USERS JSON map. Invalid payloads fail startup loudly — a
    silently ignored auth map would look exactly like Anonymous mode."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"AV_AUTH_USERS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("AV_AUTH_USERS must be a JSON object of {username: token}.")
    users: dict[str, str] = {}
    for name, tok in parsed.items():
        name, tok = str(name).strip(), str(tok).strip()
        if not name or not tok:
            raise RuntimeError("AV_AUTH_USERS entries need non-empty username and token.")
        users[name] = tok
    return users


_AUTH_USERS = _parse_auth_users(os.environ.get("AV_AUTH_USERS"))


def _resolve_identity(supplied_token: str) -> str | None:
    """Bearer token → username ("owner" for the shared secret), or None when unknown.

    compare_digest on every candidate — timing-safe even though the map is small.
    """
    if AV_API_TOKEN and secrets.compare_digest(supplied_token, AV_API_TOKEN):
        return "owner"
    for name, tok in _AUTH_USERS.items():
        if secrets.compare_digest(supplied_token, tok):
            return name
    return None


async def require_token(request: Request, call_next):
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    if not AV_API_TOKEN and not _AUTH_USERS:
        return await call_next(request)  # Anonymous mode

    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    identity = _resolve_identity(supplied) if scheme.lower() == "bearer" and supplied else None
    if identity is None:
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API token"})
    request.state.username = identity
    return await call_next(request)


# MIDDLEWARE PIPELINE — Starlette runs the LAST-added middleware OUTERMOST, so these
# three registrations ARE the architecture; reorder them and you change what browsers
# and floods experience (Probleme.md #75):
#
#   registration order:  auth  →  CORS  →  rate limit
#   runtime order:       rate  →  CORS  →  auth  →  routes
#
# * CORS must sit OUTSIDE auth: browser preflights are credentialless by spec, and —
#   the subtle part — auth's own 401 JSONResponses need ACAO headers too, or the
#   browser can't even READ the 401 and TokenGate's entry prompt never fires (the webui
#   rendered empty dashboards instead). The original v1.1.x order had auth outside
#   CORS: Anonymous dashboards worked, Protected ones silently broke.
app.add_middleware(BaseHTTPMiddleware, dispatch=require_token)

_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("AV_CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
if not _CORS_ORIGINS:
    _CORS_ORIGINS = ["http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "HEAD"],
    allow_headers=["*"],
)


# --- Rate limiting (outermost middleware: floods are rejected before any other work) ---
# Defaults close the one destructive unauthenticated endpoint (GC) while leaving the data
# plane unlimited — legitimate clients burst (8-worker object uploads, thousand-file
# commits) and fixed global caps would false-positive on them. Operators opt the data
# plane in via AV_RATE_LIMIT_DEFAULT; see python/av_server/rate_limit.py.
_RATE_LIMITER = rate_limit.build_limiter_from_env()


@app.middleware("http")
async def limit_request_rate(request: Request, call_next):
    bucket = rate_limit.bucket_class_for(request.url.path)
    if bucket is None:
        return await call_next(request)
    client = request.client.host if request.client else "unknown"
    retry_after = _RATE_LIMITER.check(client, bucket)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded for '{bucket}' operations"},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


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
                    "chunks": info.get("chunks", []),
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
                    chunks=entry.get("chunks", []),
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
    request: Request, commit_data: Dict[str, Any], db: AsyncSession = Depends(get_session)
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
        # Merge commits carry parents[1:] in extra_parents (JSON string); parent_hash stays
        # parents[0] for backward compatibility with every existing consumer (webui graph,
        # older clients) that only understands a single parent.
        extra_parents = json.dumps(parents[1:]) if len(parents) > 1 else None
        # Per-user attribution: an authenticated user pushing with the default "anonymous"
        # author gets their username stamped; explicit client-set authors (AV_AUTHOR) are
        # respected — scripts own their attribution. Anonymous mode has no identity.
        author = commit_data.get("author", "anonymous")
        username = getattr(request.state, "username", None)
        if author == "anonymous" and username:
            author = username
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data.get("message", ""),
            author=author,
            parent_hash=parents[0] if parents else None,
            extra_parents=extra_parents,
            root_tree_hash=root_tree_hash,
            tags=tags,
            metrics=metrics,
            project_id=project_id,
            project_name=project_name,
        )
        # v1.2.2 signed commits: the client's signature blob rides along verbatim so
        # `av verify` keeps working on cloned/pulled copies, not just in the authoring repo.
        raw_signature = commit_data.get("signature")
        if isinstance(raw_signature, dict):
            new_commit.signature = json.dumps(raw_signature, sort_keys=True)
        # env_snapshot_id is part of the hashed/signed payload — persist it so cloned
        # payloads stay byte-equal to the authoring ones (signature validity + replay).
        env_id = commit_data.get("env_snapshot_id")
        if isinstance(env_id, str) and re.match(r"^[a-f0-9]{64}$", env_id):
            new_commit.env_snapshot_id = env_id
        if commit_ts is not None:
            new_commit.timestamp = commit_ts
        db.add(new_commit)
        await db.flush()

        # --- v1.2.0: run linkage + event + audit -------------------------------
        run_id = commit_data.get("run_id")
        if run_id:
            run_row = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
            if not run_row:
                # Lazy-create: multi-agent pushes must never fail on ordering — a run the
                # server hasn't seen yet (client registered offline) is created in
                # 'created' state and linked, exactly as if it had been registered first.
                run_row = DBRun(
                    id=run_id, project_id=project_id,
                    created_by=username or author,
                    metrics_summary=dict(metrics) if isinstance(metrics, dict) else {},
                    created_at=utcnow_naive(),
                )
                db.add(run_row)
            else:
                if isinstance(metrics, dict) and metrics:
                    merged = dict(run_row.metrics_summary or {})
                    merged.update(metrics)
                    run_row.metrics_summary = merged
                run_row.updated_at = utcnow_naive()
            # v1.2.2 env snapshot/replay: a commit carrying env_snapshot_id back-fills the
            # linked run's pointer when the run doesn't have one yet (first-link wins).
            env_snapshot_id = commit_data.get("env_snapshot_id")
            if env_snapshot_id and not run_row.env_snapshot_id:
                run_row.env_snapshot_id = str(env_snapshot_id)
            db.add(DBRunCommit(run_id=run_id, commit_hash=commit_hash))

        await _emit_event(db, project_id, "commit", {
            "hash": commit_hash,
            "message": new_commit.message,
            "author": author,
            "run_id": run_id,
        })
        _audit(db, username, "commit.push", project_id,
               {"hash": commit_hash, "message": new_commit.message}, status_code=201)

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


async def resolve_tree(db: AsyncSession, root_hash: str) -> dict:
    """Rebuilds a commit's full file tree from the DB Merkle Tree.

    Level-order traversal with one batched query per depth level (was one query per node, i.e.
    N+1). Because identical subtrees are deduplicated, the same tree_hash can appear under
    several paths, so we carry a list of path prefixes per hash for each level. Factored out of
    `get_commit` (module-level, not nested) so `list_commits`'s `include_layers` option can
    reuse the exact same logic instead of duplicating it.
    """
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
                            "chunks": getattr(entry, "chunks", None) or [],
                        }
        frontier = next_frontier
    return tree_data


def _full_parents(parent_hash: Optional[str], extra_parents_json: Optional[str]) -> List[str]:
    """Reconstructs a commit's complete parents list from the DB columns.

    parent_hash holds parents[0]; merge commits store the rest in extra_parents as a JSON
    array string. Tolerates corrupt/absent JSON (returns just the primary parent) so one
    bad row can't 500 the whole dashboard.
    """
    parents = [parent_hash] if parent_hash else []
    if extra_parents_json:
        try:
            extras = json.loads(extra_parents_json)
            if isinstance(extras, list):
                parents.extend(extras)
        except (ValueError, TypeError):
            pass
    return parents


def _signature_out(raw: Optional[str]) -> Optional[dict]:
    """Decodes a stored signature blob for API responses; corrupt JSON degrades to None
    so one bad row can't 500 commit reads."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


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

    tree_data = await resolve_tree(db, commit.root_tree_hash) if commit.root_tree_hash else {}

    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "timestamp": commit.timestamp.isoformat() if commit.timestamp else None,
        "parent_hash": commit.parent_hash,
        "parents": _full_parents(commit.parent_hash, commit.extra_parents),
        "root_tree_hash": commit.root_tree_hash,
        "tags": commit.tags or [],
        "metrics": commit.metrics or {},
        "tree": tree_data,
        "project_id": commit.project_id,
        "project_name": commit.project_name,
        "signature": _signature_out(commit.signature),
        "env_snapshot_id": commit.env_snapshot_id,
    }


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------

@app.put("/api/refs/{ref_name:path}")
async def update_ref(
    ref_name: str, payload: RefUpdate, request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    ref_name = validate_ref_name(ref_name)
    stmt = select(DBRef).where(DBRef.name == ref_name).with_for_update()
    result = await db.execute(stmt)
    ref = result.scalar_one_or_none()
    if ref:
        ref.commit_hash = payload.commit_hash
    else:
        db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    project_id = ref_name.split("/", 1)[0] if "/" in ref_name else None
    await _emit_event(db, project_id, "ref", {"ref": ref_name, "commit_hash": payload.commit_hash})
    _audit(db, _identity(request), "ref.update", project_id,
           {"ref": ref_name, "commit_hash": payload.commit_hash}, status_code=200)
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
# Env-overridable (AV_GC_GRACE_SECONDS, integer) so ops — and the e2e suite — can shrink
# it for drills; defaults to the production hour.
GC_GRACE_SECONDS = int(os.environ.get("AV_GC_GRACE_SECONDS", "3600"))

# Delete in batches to stay well under driver bind-parameter limits (asyncpg ~32k).
_GC_DELETE_BATCH = 500


def _collect_alive_in_memory(
    root_hash: Optional[str], tree_map: Dict[str, list], visited: set, alive: set
) -> None:
    """Iteratively mark every object/layer/chunk hash reachable from a root tree as alive.

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
            # CDC chunk shards (opaque .pt/.pth/.ckpt checkpoints) live as their own objects,
            # exactly like safetensors layer shards — unmarked here, GC would reap the pieces
            # a chunked checkpoint needs to reassemble.
            chunks = getattr(entry, "chunks", None) or []
            for chunk in chunks:
                if isinstance(chunk, dict) and "hash" in chunk:
                    alive.add(chunk["hash"])



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

        # Retention sweeps for the autonomous-loop surfaces:
        # - events (default 30 days, AV_EVENT_RETENTION_DAYS)
        # - audit_log (default 90 days, AV_AUDIT_RETENTION_DAYS)
        # - terminal-status webhook deliveries (delivered/dead) ride the event window;
        #   stuck pending/failed rows are never swept here — the retry worker owns them.
        from datetime import timedelta as _td

        event_cutoff = utcnow_naive() - _td(days=EVENT_RETENTION_DAYS)
        await db.execute(delete(DBEvent).where(DBEvent.ts < event_cutoff))

        audit_cutoff = utcnow_naive() - _td(days=AUDIT_RETENTION_DAYS)
        await db.execute(delete(DBAuditLog).where(DBAuditLog.ts < audit_cutoff))

        await db.execute(
            delete(DBWebhookDelivery).where(
                DBWebhookDelivery.status.in_(["delivered", "dead"]),
                DBWebhookDelivery.updated_at < event_cutoff,
            )
        )

        await db.commit()
        await _emit_event(db, None, "gc", {
            "deleted_objects": deleted_count,
            "alive_objects": len(alive_hashes),
            "reused_trees": len(visited_trees),
        })
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
    include_layers: bool = False,
    db: AsyncSession = Depends(get_session)
) -> dict:
    """Paginated commit list for the Web UI dashboard, newest first.

    Optionally scoped to a single project via ?project_id= — without it, commits from every
    project on this shared registry are returned (matches the dashboard's pre-existing
    behavior so it doesn't break for callers that don't know about projects yet).

    ?include_layers=true additionally resolves each returned commit's full tree (same shape
    GET /api/commits/{hash} already returns) in this single response — added specifically to
    replace WeightDiffPanel.tsx's old N-parallel-requests pattern (one GET /api/commits/{hash}
    per candidate checkpoint) with one round trip. Trees are resolved sequentially here (NOT
    via asyncio.gather) — get_session() hands out one AsyncSession per request, backed by a
    single underlying connection, and concurrent queries on the same connection aren't safe
    (asyncpg raises "another operation is in progress"). The win this endpoint provides is
    collapsing N HTTP round trips into one; resolve_tree() itself already eliminated the
    expensive per-node N+1 *within* a single tree, which is the part that actually scales with
    tree size — sequential-but-one-request is still a large improvement over N full requests.
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

    commit_dicts = []
    for c in commits:
        d = {
            "hash": c.hash,
            "message": c.message,
            "author": c.author,
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            "parent_hash": c.parent_hash,
            "parents": _full_parents(c.parent_hash, c.extra_parents),
            "root_tree_hash": c.root_tree_hash,
            "tags": c.tags or [],
            "metrics": c.metrics or {},
            "project_id": c.project_id,
            "project_name": c.project_name,
            "signature": _signature_out(c.signature),
            "env_snapshot_id": c.env_snapshot_id,
        }
        if include_layers:
            d["tree"] = await resolve_tree(db, c.root_tree_hash) if c.root_tree_hash else {}
        commit_dicts.append(d)

    return {
        "commits": commit_dicts,
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


# ---------------------------------------------------------------------------
# Autonomous-loop surface (v1.2.0): runs, event stream, webhooks, audit
# ---------------------------------------------------------------------------
# Design notes:
# * Events are an append-only feed whose autoincrement id IS the resumable cursor
#   (?since=<id> returns strictly newer rows, ascending). Orchestrators long-poll with
#   wait=<secs> instead of hot-looping.
# * Webhooks are signed HMAC-SHA256 over the raw JSON body; the signing secret lives in
#   this database by necessity (deliveries must be signed) and is never returned.
# * Runs are first-class (see models.DBRun); commits link to runs at push time via the
#   payload's optional run_id, lazily creating unknown runs so multi-agent pushes never
#   fail on ordering.

from fastapi import Body  # noqa: E402

import hashlib  # noqa: E402
import hmac as hmac_mod  # noqa: E402

EVENT_RETENTION_DAYS = int(os.environ.get("AV_EVENT_RETENTION_DAYS", "30"))
_WEBHOOK_TIMEOUT_SECS = 10


async def _emit_event(db: AsyncSession, project_id: str | None, kind: str, payload: dict | None):
    """Appends one event row (flushed so the cursor id exists) and schedules signed
    webhook deliveries as a background task — never blocking or failing the mutation.
    With zero active webhooks no task is created at all: fire-and-forget tasks that
    open their own sessions must never become routine overhead (they leak connections
    across TestClient requests otherwise)."""
    row = DBEvent(project_id=project_id, kind=kind, payload=payload)
    db.add(row)
    await db.flush()
    hook_count = (
        await db.execute(
            select(func.count()).select_from(DBWebhook).where(DBWebhook.active.is_(True))
        )
    ).scalar_one()
    if not hook_count:
        return row.id

    async def _deliver_later():
        try:
            async with async_session_factory() as session:
                hooks = (
                    await session.execute(select(DBWebhook).where(DBWebhook.active.is_(True)))
                ).scalars().all()
                await _deliver_webhooks(session, hooks,
                    {"id": row.id, "kind": kind, "project_id": project_id, "payload": payload})
                await session.commit()
        except Exception:  # pragma: no cover — delivery must never break the mutation
            logger.exception("webhook delivery scheduling failed")

    asyncio.create_task(_deliver_later())
    return row.id


def _sign(secret: str, body: bytes) -> str:
    return hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event_body(event: dict) -> bytes:
    """The canonical signed body for an event dict {id, kind, project_id, payload}.

    Shared by first-attempt delivery AND retries so a retry reconstructs the
    byte-identical (and therefore signature-identical) payload."""
    import json as _json

    return _json.dumps(
        {"id": event["id"], "kind": event["kind"], "project_id": event.get("project_id"), "payload": event.get("payload")},
        sort_keys=True,
    ).encode()


async def _deliver_one(hook, delivery: DBWebhookDelivery, event: dict) -> None:
    """One POST attempt for one hook, persisting the outcome onto its delivery row.

    Failure handling: attempt++ with next_retry_at scheduled at the retry interval;
    exhausting AV_WEBHOOK_MAX_ATTEMPTS dead-letters the row (status='dead'). The row is
    created 'pending' BEFORE the POST so a crash mid-delivery still leaves a retryable
    record rather than a silently dropped fan-out."""
    body = _event_body(event)
    headers = {
        "Content-Type": "application/json",
        "X-AV-Event-Id": str(event["id"]),
        "X-AV-Event-Kind": event["kind"],
        "X-AV-Signature": _sign(hook.secret, body),
    }
    loop = asyncio.get_running_loop()

    def _post():
        import requests as _requests

        try:
            resp = _requests.post(hook.url, data=body, headers=headers,
                                  timeout=_WEBHOOK_TIMEOUT_SECS)
            return resp.status_code, None
        except Exception as exc:
            return None, str(exc)

    status_code, error = await loop.run_in_executor(None, _post)
    if status_code is not None and 200 <= status_code < 300:
        delivery.status = "delivered"
        delivery.response_code = status_code
        delivery.last_error = None
        delivery.next_retry_at = None
    else:
        delivery.attempt += 1
        delivery.response_code = status_code
        delivery.last_error = error or f"http_{status_code}"
        if delivery.attempt >= WEBHOOK_MAX_ATTEMPTS:
            delivery.status = "dead"
            logger.warning("webhook %s dead-lettered after %s attempts", hook.url,
                           delivery.attempt)
        else:
            delivery.status = "failed"
            delivery.next_retry_at = utcnow_naive() + timedelta(
                seconds=WEBHOOK_RETRY_INTERVAL_SECS)


async def process_due_webhook_deliveries() -> int:
    """Re-drives every due pending/failed delivery (called by the interval worker and
    exposed to tests). Returns how many rows were re-attempted."""
    now = utcnow_naive()
    delivered = 0
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(DBWebhookDelivery)
            .where(DBWebhookDelivery.status.in_(["pending", "failed"]))
            .where((DBWebhookDelivery.next_retry_at.is_(None))
                   | (DBWebhookDelivery.next_retry_at <= now))
            .limit(100)
        )).scalars().all()
        for delivery in rows:
            hook = (await db.execute(
                select(DBWebhook).where(DBWebhook.id == delivery.webhook_id)
            )).scalar_one_or_none()
            # Hook deleted since scheduling → dead-letter the orphan instead of looping.
            if hook is None:
                delivery.status = "dead"
                delivery.last_error = "webhook deleted"
                continue
            if not hook.active:
                delivery.status = "dead"
                delivery.last_error = "webhook deactivated"
                continue
            event = {"id": delivery.event_id or -1, "kind": delivery.event_kind,
                     "project_id": delivery.project_id, "payload": delivery.payload}
            await _deliver_one(hook, delivery, event)
            delivered += 1
        await db.commit()
    return delivered


async def _deliver_webhooks(db: AsyncSession, hooks: list, event: dict) -> None:
    """POSTs the event to every matching active webhook, signed, in worker threads.

    v1.2.2: every attempt is persisted in webhook_deliveries BEFORE the request goes
    out and updated after — failed deliveries are retried by the background worker
    (startup + interval) until AV_WEBHOOK_MAX_ATTEMPTS exhausts into a dead-letter.
    Delivery rows ride the MUTATION's own session/transaction, so a rolled-back
    mutation never leaves phantom delivery records. Per-URL try/except stays inside
    _deliver_one: a dead subscriber must never fail the original mutation."""
    matching = [
        h for h in hooks
        if (h.project_id is None or h.project_id == event.get("project_id"))
        and (h.kinds is None or event["kind"] in h.kinds)
    ]
    if not matching:
        return

    for hook in matching:
        delivery = DBWebhookDelivery(
            webhook_id=hook.id, event_id=event["id"] if event["id"] >= 0 else None,
            event_kind=event["kind"], project_id=event.get("project_id"),
            payload=event.get("payload"), attempt=1, status="pending",
            next_retry_at=None,
        )
        db.add(delivery)
        await db.flush()
        await _deliver_one(hook, delivery, event)


def _audit(db: AsyncSession, username: str | None, action: str,
           project_id: str | None, details: dict | None = None,
           status_code: int | None = None):
    """Records one mutation. v1.2.2: `status_code` captures the HTTP outcome the caller
    is about to return, so the trail answers "did it land?" — not just "was it tried"."""
    if AUDIT_ENABLED:
        db.add(DBAuditLog(username=username, action=action,
                          project_id=project_id, details=details,
                          status_code=status_code))


AUDIT_ENABLED = os.environ.get("AV_AUDIT_LOG", "1") not in ("", "0", "false")


def _identity(request: Request) -> str | None:
    return getattr(request.state, "username", None)


@app.get("/api/events")
async def list_events(
    since: int = 0,
    project_id: Optional[str] = None,
    kinds: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    wait: int = 0,
    db: AsyncSession = Depends(get_session),
):
    """Resumable ordered event feed. wait=<secs> long-polls for at least one new row."""
    kind_list = [k.strip() for k in kinds.split(",")] if kinds else None
    waited = 0.0

    async def _fetch():
        stmt = select(DBEvent).where(DBEvent.id > since)
        if project_id:
            stmt = stmt.where((DBEvent.project_id == project_id) | (DBEvent.project_id.is_(None)))
        if kind_list:
            stmt = stmt.where(DBEvent.kind.in_(kind_list))
        rows = (await db.execute(stmt.order_by(DBEvent.id.asc()).limit(limit))).scalars().all()
        return [
            {"id": e.id, "ts": e.ts.isoformat() if e.ts else None,
             "kind": e.kind, "project_id": e.project_id, "payload": e.payload}
            for e in rows
        ]

    events = await _fetch()
    while not events and wait > waited:
        await asyncio.sleep(0.5)
        waited += 0.5
        events = await _fetch()

    next_cursor = events[-1]["id"] if events else since
    return {"events": events, "next_cursor": next_cursor}


QUERY_BEFORE_DAYS_DEFAULT = EVENT_RETENTION_DAYS


@app.delete("/api/events")
async def prune_events(request: Request, before_days: int = QUERY_BEFORE_DAYS_DEFAULT,
                       db: AsyncSession = Depends(get_session)):
    """Manual retention pruning (default also applied automatically during GC)."""
    from datetime import timedelta

    cutoff = utcnow_naive() - timedelta(days=max(before_days, 0))
    result = await db.execute(delete(DBEvent).where(DBEvent.ts < cutoff))
    await db.commit()
    _audit(db, _identity(request), "events.prune", None, {"deleted": result.rowcount, "before_days": before_days}, status_code=200)
    await db.commit()
    return {"deleted": result.rowcount}


@app.post("/api/runs")
async def create_run(request: Request, run: Dict[str, Any] = Body(...),
                     db: AsyncSession = Depends(get_session)):
    run_id = run.get("id") or _new_uuid()
    project_id = run.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    exists = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if exists:
        return {"status": "exists", "id": exists.id}  # idempotent create (multi-agent safe)
    now = utcnow_naive()
    db_run = DBRun(
        id=run_id, project_id=project_id, name=run.get("name"),
        status="running", parent_run_id=run.get("parent_run_id"),
        created_by=_identity(request), config_hash=run.get("config_hash"),
        code_pointer=run.get("code_pointer"), env_snapshot_id=run.get("env_snapshot_id"),
        created_at=now, updated_at=now,
    )
    db.add(db_run)
    await _emit_event(db, project_id, "run", {"action": "started", "run_id": run_id, "name": run.get("name")})
    _audit(db, _identity(request), "run.create", project_id, {"run_id": run_id}, status_code=201)
    await db.commit()
    return {"status": "created", "id": run_id}


@app.get("/api/runs")
async def list_runs(project_id: Optional[str] = None, status: Optional[str] = None,
                    parent_run_id: Optional[str] = None, limit: int = 50, offset: int = 0,
                    db: AsyncSession = Depends(get_session)):
    stmt = select(DBRun).order_by(DBRun.created_at.desc())
    if project_id:
        stmt = stmt.where(DBRun.project_id == project_id)
    if status:
        stmt = stmt.where(DBRun.status == status)
    if parent_run_id:
        stmt = stmt.where(DBRun.parent_run_id == parent_run_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"runs": [_run_to_dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def _run_to_dict(r: DBRun) -> dict:
    return {
        "id": r.id, "project_id": r.project_id, "name": r.name, "status": r.status,
        "parent_run_id": r.parent_run_id, "created_by": r.created_by,
        "code_pointer": r.code_pointer, "env_snapshot_id": r.env_snapshot_id,
        "metrics_summary": r.metrics_summary or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_session)):
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    commit_rows = (await db.execute(
        select(DBRunCommit.commit_hash).where(DBRunCommit.run_id == run_id)
    )).scalars().all()
    d = _run_to_dict(r)
    d["commit_hashes"] = commit_rows
    return d


@app.post("/api/runs/{run_id}/complete")
async def complete_run(run_id: str, request: Request,
                       body: Dict[str, Any] = Body(default={}),
                       db: AsyncSession = Depends(get_session)):
    return await _finish_run(run_id, request, "completed", body, db)


@app.post("/api/runs/{run_id}/fail")
async def fail_run(run_id: str, request: Request,
                   body: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_session)):
    return await _finish_run(run_id, request, "failed", body, db)


async def _finish_run(run_id: str, request: Request, status: str, body: dict, db: AsyncSession):
    r = (await db.execute(select(DBRun).where(DBRun.id == run_id))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    r.status = status
    r.completed_at = utcnow_naive()
    r.updated_at = r.completed_at
    if isinstance(body, dict) and body.get("metrics_summary"):
        r.metrics_summary = {**(r.metrics_summary or {}), **body["metrics_summary"]}
    await _emit_event(db, r.project_id, "run", {"action": status, "run_id": run_id})
    _audit(db, _identity(request), f"run.{status}", r.project_id, {"run_id": run_id}, status_code=200)
    await db.commit()
    return {"status": status, "id": run_id}


def _parse_iso_dt(value: str, field: str) -> datetime:
    """ISO-8601 audit filter parsing; invalid input is a 422, never a silent match-all."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field} timestamp: {value!r}")
    # Naive UTC storage throughout the schema (see utcnow_naive) — normalize aware inputs.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@app.get("/api/admin/audit")
async def get_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    project_id: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """Trust surface: recent mutating-call trail with outcome capture. Auth-gated like
    every other route; in Anonymous mode usernames are simply None entries.

    Filters (v1.2.2): action (exact match), project_id, since/until (ISO-8601 ts bounds),
    limit+offset pagination — all composable."""
    stmt = select(DBAuditLog)
    count_stmt = select(func.count()).select_from(DBAuditLog)
    if project_id:
        stmt = stmt.where(DBAuditLog.project_id == project_id)
        count_stmt = count_stmt.where(DBAuditLog.project_id == project_id)
    if action:
        stmt = stmt.where(DBAuditLog.action == action)
        count_stmt = count_stmt.where(DBAuditLog.action == action)
    if since:
        cutoff = _parse_iso_dt(since, "since")
        stmt = stmt.where(DBAuditLog.ts >= cutoff)
        count_stmt = count_stmt.where(DBAuditLog.ts >= cutoff)
    if until:
        cutoff = _parse_iso_dt(until, "until")
        stmt = stmt.where(DBAuditLog.ts <= cutoff)
        count_stmt = count_stmt.where(DBAuditLog.ts <= cutoff)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(
        stmt.order_by(DBAuditLog.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return {"entries": [
        {"id": a.id, "ts": a.ts.isoformat() if a.ts else None, "username": a.username,
         "action": a.action, "project_id": a.project_id, "details": a.details,
         "status_code": a.status_code}
        for a in rows
    ], "total": total, "limit": limit, "offset": offset}


@app.delete("/api/admin/audit")
async def prune_audit_log(request: Request, before_days: int = Query(AUDIT_RETENTION_DAYS, ge=0),
                          db: AsyncSession = Depends(get_session)):
    """Manual audit-trail pruning; the same window is swept automatically during GC."""
    cutoff = utcnow_naive() - timedelta(days=max(before_days, 0))
    result = await db.execute(delete(DBAuditLog).where(DBAuditLog.ts < cutoff))
    await db.commit()
    _audit(db, _identity(request), "audit.prune", None,
           {"deleted": result.rowcount, "before_days": before_days}, status_code=200)
    await db.commit()
    return {"deleted": result.rowcount}


# --- webhook management ------------------------------------------------------

class WebhookCreate(BaseModel):
    url: str
    secret: str
    project_id: Optional[str] = None
    kinds: Optional[List[str]] = None


@app.post("/api/webhooks")
async def create_webhook(request: Request, wh: WebhookCreate,
                         db: AsyncSession = Depends(get_session)):
    if not wh.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must be http(s)")
    row = DBWebhook(url=wh.url, secret=wh.secret, project_id=wh.project_id, kinds=wh.kinds)
    db.add(row)
    _audit(db, _identity(request), "webhook.create", wh.project_id, {"webhook_id": row.id, "url": wh.url}, status_code=201)
    await db.commit()
    return {"id": row.id, "url": wh.url, "active": True}


@app.get("/api/webhooks")
async def list_webhooks(db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(DBWebhook))).scalars().all()
    return {"webhooks": [
        {"id": w.id, "url": w.url, "project_id": w.project_id,
         "kinds": w.kinds, "active": w.active,
         "secret": (w.secret[:3] + "…") if w.secret else None}
        for w in rows
    ]}


@app.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str, request: Request,
                         db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBWebhook).where(DBWebhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await db.execute(delete(DBWebhook).where(DBWebhook.id == webhook_id))
    _audit(db, _identity(request), "webhook.delete", row.project_id, {"webhook_id": webhook_id}, status_code=200)
    await db.commit()
    return {"status": "deleted"}


@app.post("/api/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, db: AsyncSession = Depends(get_session)):
    row = (await db.execute(select(DBWebhook).where(DBWebhook.id == webhook_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await _deliver_webhooks(db, [row], {"id": -1, "kind": "webhook_test",
                                    "project_id": row.project_id, "payload": {"ping": True}})
    await db.commit()
    return {"status": "delivered"}


# --- webhook delivery observability (v1.2.2) ----------------------------------

@app.get("/api/admin/webhook-deliveries")
async def list_webhook_deliveries(
    status: Optional[str] = None,
    webhook_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
):
    """Delivery-ledger observability: attempts, outcomes, retry schedule, dead-letters."""
    stmt = select(DBWebhookDelivery)
    count_stmt = select(func.count()).select_from(DBWebhookDelivery)
    if status:
        stmt = stmt.where(DBWebhookDelivery.status == status)
        count_stmt = count_stmt.where(DBWebhookDelivery.status == status)
    if webhook_id:
        stmt = stmt.where(DBWebhookDelivery.webhook_id == webhook_id)
        count_stmt = count_stmt.where(DBWebhookDelivery.webhook_id == webhook_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(
        stmt.order_by(DBWebhookDelivery.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return {"deliveries": [
        {"id": d.id, "webhook_id": d.webhook_id, "event_id": d.event_id,
         "event_kind": d.event_kind, "project_id": d.project_id,
         "attempt": d.attempt, "status": d.status,
         "response_code": d.response_code, "last_error": d.last_error,
         "next_retry_at": d.next_retry_at.isoformat() if d.next_retry_at else None,
         "created_at": d.created_at.isoformat() if d.created_at else None,
         "updated_at": d.updated_at.isoformat() if d.updated_at else None}
        for d in rows
    ], "total": total, "limit": limit, "offset": offset}
