import os
import re
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .storage import CASStorage
from .database import get_session, init_db
from .models import DBObject, DBCommit, DBRef, DBTree
from .redis_cache import cache

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

async def build_merkle_tree(db: AsyncSession, tree_data: Dict[str, Any]) -> str:
    """
    Recursively builds a Merkle Tree from a nested dictionary.
    Returns the root tree_hash.
    """
    # 1. Group entries by their immediate path segment
    nodes = {} # path_segment -> {is_dir, data}
    
    for path, info in tree_data.items():
        parts = path.split('/', 1)
        name = parts[0]
        
        if len(parts) == 1:
            # It's a file or layer in the current directory
            nodes[name] = {"is_dir": False, "info": info}
        else:
            # It's in a subdirectory
            if name not in nodes:
                nodes[name] = {"is_dir": True, "children": {}}
            nodes[name]["children"][parts[1]] = info

    # 2. Process each node and compute its hash
    current_level_entries = []
    
    for name, node in sorted(nodes.items()):
        if node["is_dir"]:
            child_hash = await build_merkle_tree(db, node["children"])
            current_level_entries.append({
                "name": name,
                "child_hash": child_hash,
                "obj_hash": None,
                "type": "tree",
                "size": 0
            })
        else:
            info = node["info"]
            current_level_entries.append({
                "name": name,
                "child_hash": None,
                "obj_hash": info["hash"],
                "type": info.get("type", "file"),
                "size": info.get("size", 0)
            })
            
            # Special case for Safetensors layers (nested under the file)
            if info.get("layers"):
                layer_data = {l["name"]: {"hash": l["hash"], "size": l["size"], "type": "layer"} for l in info["layers"]}
                layer_tree_hash = await build_merkle_tree(db, layer_data)
                # We could link the file to its layers tree here if needed
    
    # 3. Compute hash for the current tree level
    tree_content = json.dumps(current_level_entries, sort_keys=True)
    tree_hash = hashlib.sha256(tree_content.encode()).hexdigest()
    
    # 4. Check if tree already exists in DB
    result = await db.execute(select(DBTree).where(DBTree.tree_hash == tree_hash))
    if not result.first():
        for entry in current_level_entries:
            db_tree = DBTree(
                tree_hash=tree_hash,
                path_name=entry["name"],
                child_tree_hash=entry["child_hash"],
                object_hash=entry["obj_hash"],
                type=entry["type"],
                size=entry["size"]
            )
            db.add(db_tree)
            
            # Ensure objects exist in DB if they are files/layers
            if entry["obj_hash"]:
                obj_result = await db.execute(select(DBObject).where(DBObject.hash == entry["obj_hash"]))
                if not obj_result.first():
                    db.add(DBObject(hash=entry["obj_hash"], size=entry["size"]))
        
        await db.flush() # Send to DB but don't commit yet
        
    return tree_hash

@app.post("/api/commits")
async def push_commit(commit_data: Dict[str, Any], db: AsyncSession = Depends(get_session)):
    required = ["hash", "message", "tree", "timestamp"]
    if not all(k in commit_data for k in required):
        raise HTTPException(status_code=400, detail="Missing required commit fields")
    
    commit_hash = commit_data["hash"]
    
    # Check if commit already exists
    result = await db.execute(select(DBCommit).where(DBCommit.hash == commit_hash))
    if result.scalar_one_or_none():
        return Response(status_code=409, content="Commit already exists")

    try:
        # 1. Build hierarchical Merkle Tree
        root_tree_hash = await build_merkle_tree(db, commit_data["tree"])
        
        # 2. Create DBCommit
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data["message"],
            author=commit_data.get("author", "unknown"),
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

# ... (Other endpoints like health, objects, refs remain similar but might need tree traversal for get_commit)

@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.2.0 (Merkle-Tree Enabled)"}

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
        "metrics": commit.metrics,
        "timestamp": commit.timestamp.isoformat(),
        "root_tree_hash": commit.root_tree_hash
    }

@app.put("/api/refs/{ref_name:path}")
async def update_ref(ref_name: str, payload: RefUpdate, db: AsyncSession = Depends(get_session)):
    stmt = select(DBRef).where(DBRef.name == ref_name).with_for_update()
    result = await db.execute(stmt)
    ref = result.scalar_one_or_none()
    
    if ref:
        ref.commit_hash = payload.commit_hash
    else:
        db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    
    await db.commit()
    return {"status": "updated"}

@app.get("/api/refs")
async def list_refs(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBRef))
    refs = result.scalars().all()
    return {r.name: r.commit_hash for r in refs}

@app.post("/api/objects/{hash}")
async def upload_object(hash: str, request: Request, db: AsyncSession = Depends(get_session)):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    if await cache.get_object_exists(hash):
        return Response(status_code=409, content="Object already exists")
    
    try:
        path = await storage.store_object(hash, request.stream())
        size = path.stat().st_size
        db.add(DBObject(hash=hash, size=size))
        await db.commit()
        await cache.set_object_exists(hash)
        return Response(status_code=201)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
