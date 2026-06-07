import os
import re
from pathlib import Path
from typing import Dict, Any, List
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .storage import CASStorage
from .database import get_session, init_db
from .models import DBObject, DBCommit, DBRef, commit_objects
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

@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.1.0 (DB-Backed)"}

@app.post("/api/objects/{hash}")
async def upload_object(hash: str, request: Request, db: AsyncSession = Depends(get_session)):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    
    # Check Redis cache first
    if await cache.get_object_exists(hash):
        return Response(status_code=409, content="Object already exists")
    
    # Check DB
    result = await db.execute(select(DBObject).where(DBObject.hash == hash))
    if result.scalar_one_or_none():
        await cache.set_object_exists(hash)
        return Response(status_code=409, content="Object already exists")
    
    try:
        path = await storage.store_object(hash, request.stream())
        size = path.stat().st_size
        
        # Save to DB
        new_obj = DBObject(hash=hash, size=size)
        db.add(new_obj)
        await db.commit()
        
        # Update Cache
        await cache.set_object_exists(hash)
        return Response(status_code=201)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/objects/{hash}")
async def download_object(hash: str):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    
    obj_path = storage.get_object_path(hash)
    if not obj_path:
        raise HTTPException(status_code=404, detail="Object not found")
        
    def iterfile():
        with open(obj_path, mode="rb") as file_like:
            while chunk := file_like.read(8 * 1024 * 1024):
                yield chunk
                
    return StreamingResponse(iterfile(), media_type="application/octet-stream")

@app.head("/api/objects/{hash}")
async def head_object(hash: str, db: AsyncSession = Depends(get_session)):
    if not re.match(r'^[a-f0-9]{64}$', hash):
        raise HTTPException(status_code=400, detail="Invalid hash format")
    
    # Try cache
    if await cache.get_object_exists(hash):
        size = storage.get_object_size(hash)
        return Response(status_code=200, headers={"Content-Length": str(size)})
    
    # Try DB
    result = await db.execute(select(DBObject).where(DBObject.hash == hash))
    obj = result.scalar_one_or_none()
    if obj:
        await cache.set_object_exists(hash)
        return Response(status_code=200, headers={"Content-Length": str(obj.size)})
        
    return Response(status_code=404)

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
        # Create DBCommit
        new_commit = DBCommit(
            hash=commit_hash,
            message=commit_data["message"],
            author=commit_data.get("author", "unknown"),
            metrics=commit_data.get("metrics", {}),
            parent_hash=commit_data.get("parents", [None])[0] if commit_data.get("parents") else None
        )
        db.add(new_commit)
        
        # Link objects (tree)
        for path, info in commit_data["tree"].items():
            obj_hash = info["hash"]
            # Ensure object exists in DB
            obj_result = await db.execute(select(DBObject).where(DBObject.hash == obj_hash))
            if not obj_result.scalar_one_or_none():
                # If it exists on disk but not in DB (legacy), add it
                size = storage.get_object_size(obj_hash)
                if size is not None:
                    db.add(DBObject(hash=obj_hash, size=size))
            
            # Add to many-to-many table via raw insert for efficiency or use relationship
            # Here we'll use the association table directly
            stmt = commit_objects.insert().values(
                commit_hash=commit_hash,
                object_hash=obj_hash,
                path=path,
                size=info.get("size", 0),
                type=info.get("type", "unknown")
            )
            await db.execute(stmt)
            
        await db.commit()
        return Response(status_code=201)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/commits/{hash}")
async def get_commit(hash: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBCommit).where(DBCommit.hash == hash))
    commit = result.scalar_one_or_none()
    if not commit:
        raise HTTPException(status_code=404, detail="Commit not found")
    
    # Reconstruct tree (this is simplified)
    return {
        "hash": commit.hash,
        "message": commit.message,
        "author": commit.author,
        "metrics": commit.metrics,
        "timestamp": commit.timestamp.isoformat()
    }

@app.put("/api/refs/{ref_name:path}")
async def update_ref(ref_name: str, payload: RefUpdate, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBRef).where(DBRef.name == ref_name))
    ref = result.scalar_one_or_none()
    
    if ref:
        ref.commit_hash = payload.commit_hash
    else:
        db.add(DBRef(name=ref_name, commit_hash=payload.commit_hash))
    
    await db.commit()
    return {"status": "updated"}

@app.get("/api/refs/{ref_name:path}")
async def get_ref(ref_name: str, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBRef).where(DBRef.name == ref_name))
    ref = result.scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="Ref not found")
    return {"ref": ref_name, "commit_hash": ref.commit_hash}

@app.get("/api/refs")
async def list_refs(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(DBRef))
    refs = result.scalars().all()
    return {r.name: r.commit_hash for r in refs}
