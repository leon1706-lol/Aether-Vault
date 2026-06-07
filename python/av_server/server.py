import os
import re
import hashlib
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from .storage import CASStorage
from .database import get_session, init_db
from .models import DBObject, DBCommit, DBRef, DBTree
from .redis_cache import cache

logger = logging.getLogger("av_server")

app = FastAPI(title="Aether-Vault Server")
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

@app.on_event("startup")
async def startup_event():
    await init_db()
    await cache.init_filter()

async def build_merkle_tree(db: AsyncSession, tree_data: Dict[str, Any]) -> str:
    """Recursively builds a Merkle Tree from a nested dictionary."""
    nodes = {}
    for path, info in tree_data.items():
        parts = path.split('/', 1)
        name = parts[0]
        if len(parts) == 1:
            nodes[name] = {"is_dir": False, "info": info}
        else:
            if name not in nodes: nodes[name] = {"is_dir": True, "children": {}}
            nodes[name]["children"][parts[1]] = info

    current_level_entries = []
    for name, node in sorted(nodes.items()):
        if node["is_dir"]:
            child_hash = await build_merkle_tree(db, node["children"])
            current_level_entries.append({
                "name": name, "child_hash": child_hash, "obj_hash": None, "type": "tree", "size": 0
            })
        else:
            info = node["info"]
            current_level_entries.append({
                "name": name, "child_hash": None, "obj_hash": info["hash"], "type": info.get("type", "file"), "size": info.get("size", 0)
            })
    
    tree_content = json.dumps(current_level_entries, sort_keys=True)
    tree_hash = hashlib.sha256(tree_content.encode()).hexdigest()
    
    result = await db.execute(select(DBTree).where(DBTree.tree_hash == tree_hash))
    if not result.first():
        for entry in current_level_entries:
            db.add(DBTree(
                tree_hash=tree_hash, path_name=entry["name"], child_tree_hash=entry["child_hash"],
                object_hash=entry["obj_hash"], type=entry["type"], size=entry["size"]
            ))
        await db.flush()
    return tree_hash

@app.post("/api/commits")
async def push_commit(commit_data: Dict[str, Any], db: AsyncSession = Depends(get_session)):
    commit_hash = commit_data["hash"]
    result = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
    if result.scalar_one_or_none():
        return Response(status_code=409, content="Commit already exists")

    try:
        root_tree_hash = await build_merkle_tree(db, commit_data["tree"])
        new_commit = DBCommit(
            hash=commit_hash, 
            message=commit_data["message"], 
            author=commit_data.get("author", "unknown"),
            tags=commit_data.get("tags", []),
            metrics=commit_data.get("metrics", {}), 
            parent_hash=commit_data.get("parents", [None])[0] if commit_data.get("parents") else None,
            root_tree_hash=root_tree_hash
        )
        db.add(new_commit)
        await db.commit()
        return Response(status_code=201)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.4.0 (Dynamic Metadata Enabled)"}

@app.get("/api/commits/{hash}")
async def get_commit(hash: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBCommit).where(DBCommit.hash == hash))
    commit = result.scalar_one_or_none()
    if not commit:
        raise HTTPException(status_code=404, detail="Commit not found")
    
    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "tags": commit.tags,
        "metrics": commit.metrics,
        "timestamp": commit.timestamp.isoformat(),
        "root_tree_hash": commit.root_tree_hash
    }

@app.post("/api/objects/{hash}")
async def upload_object(hash: str, request: Request, db: AsyncSession = Depends(get_session)):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    
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
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.head("/api/objects/{hash}")
async def head_object(hash: str, db: AsyncSession = Depends(get_session)):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    
    might_exist = await cache.check_hash_exists(hash)
    if might_exist:
        result = await db.execute(select(DBObject).where(DBObject.hash == hash))
        obj = result.scalar_one_or_none()
        if obj:
            return Response(status_code=200, headers={"Content-Length": str(obj.size)})
    return Response(status_code=404)

@app.put("/api/refs/{ref_name:path}")
async def update_ref(ref_name: str, payload: RefUpdate, db: AsyncSession = Depends(get_session)):
    stmt = select(DBRef).where(DBRef.name == ref_name).with_for_update()
    result = await db.execute(stmt)
    ref = result.scalar_one_or_none()
    if ref: ref.commit_hash = payload.commit_hash
    else: db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    await db.commit()
    return {"status": "updated"}

@app.get("/api/refs")
async def list_refs(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBRef))
    refs = result.scalars().all()
    return {r.name: r.commit_hash for r in refs}

async def get_all_referenced_hashes(db: AsyncSession, tree_hash: str, visited_trees: set, alive_hashes: set):
    if tree_hash in visited_trees: return
    visited_trees.add(tree_hash)
    result = await db.execute(select(DBTree).where(DBTree.tree_hash == tree_hash))
    entries = result.scalars().all()
    for entry in entries:
        if entry.child_tree_hash: await get_all_referenced_hashes(db, entry.child_tree_hash, visited_trees, alive_hashes)
        if entry.object_hash: alive_hashes.add(entry.object_hash)

@app.post("/api/admin/gc")
async def run_garbage_collection(db: AsyncSession = Depends(get_session)):
    try:
        alive_hashes, visited_trees = set(), set()
        result = await db.execute(select(DBCommit))
        commits = result.scalars().all()
        for commit in commits: await get_all_referenced_hashes(db, commit.root_tree_hash, visited_trees, alive_hashes)
        
        result = await db.execute(select(DBObject.hash))
        all_db_hashes = set(result.scalars().all())
        dead_hashes = all_db_hashes - alive_hashes
        
        if dead_hashes:
            await db.execute(delete(DBObject).where(DBObject.hash.in_(list(dead_hashes))))
            await db.execute(delete(DBTree).where(DBTree.tree_hash.notin_(list(visited_trees))))
        
        deleted_count = 0
        for obj_path in storage.data_dir.glob("objects/*/*"):
            if obj_path.is_file():
                h = obj_path.parent.name + obj_path.name
                if h not in alive_hashes:
                    obj_path.unlink()
                    deleted_count += 1
        
        await cache.reset_filter()
        await cache.init_filter()
        for h in alive_hashes: await cache.add_hash(h)
        await db.commit()
        return {"status": "success", "alive_objects": len(alive_hashes), "deleted_objects": deleted_count, "reused_trees": len(visited_trees)}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
