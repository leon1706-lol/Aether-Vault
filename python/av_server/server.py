import os
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any

from .storage import CASStorage

app = FastAPI(title="Aether-Vault Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path(os.environ.get("AV_DATA_DIR", "/data"))
storage = CASStorage(DATA_DIR)

class RefUpdate(BaseModel):
    commit_hash: str

@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "1.0.0"}

@app.post("/api/objects/{hash}")
async def upload_object(hash: str, request: Request):
    if storage.object_exists(hash):
        return Response(status_code=409, content="Object already exists")
    
    try:
        await storage.store_object(hash, request.stream())
        return Response(status_code=201)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/objects/{hash}")
def download_object(hash: str):
    obj_path = storage.get_object_path(hash)
    if not obj_path:
        raise HTTPException(status_code=404, detail="Object not found")
        
    def iterfile():
        with open(obj_path, mode="rb") as file_like:
            while chunk := file_like.read(8 * 1024 * 1024):
                yield chunk
                
    return StreamingResponse(iterfile(), media_type="application/octet-stream")

@app.head("/api/objects/{hash}")
def head_object(hash: str):
    size = storage.get_object_size(hash)
    if size is None:
        return Response(status_code=404)
    return Response(status_code=200, headers={"Content-Length": str(size)})

@app.post("/api/commits")
def push_commit(commit_data: Dict[str, Any]):
    required = ["hash", "parents", "message", "tree", "timestamp"]
    if not all(k in commit_data for k in required):
        raise HTTPException(status_code=400, detail="Missing required commit fields")
        
    storage.store_commit(commit_data["hash"], commit_data)
    return Response(status_code=201)

@app.get("/api/commits/{hash}")
def get_commit(hash: str):
    commit = storage.get_commit(hash)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit not found")
    return commit

@app.put("/api/refs/{ref_name:path}")
def update_ref(ref_name: str, payload: RefUpdate):
    storage.update_ref(ref_name, payload.commit_hash)
    return {"status": "updated"}

@app.get("/api/refs/{ref_name:path}")
def get_ref(ref_name: str):
    commit_hash = storage.get_ref(ref_name)
    if not commit_hash:
        raise HTTPException(status_code=404, detail="Ref not found")
    return {"ref": ref_name, "commit_hash": commit_hash}

@app.get("/api/refs")
def list_refs():
    return storage.list_refs()

@app.get("/api/stats")
def get_stats():
    return storage.get_storage_stats()
