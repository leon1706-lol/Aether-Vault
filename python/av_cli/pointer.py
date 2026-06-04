import os
import re
from pathlib import Path

def create_pointer(file_path: Path, sha256_hash: str, file_size: int) -> str:
    if not re.match(r'^[a-f0-9]{64}$', sha256_hash):
        raise ValueError(f"Invalid SHA256 hash: {sha256_hash}")
    if file_size < 0:
        raise ValueError(f"Invalid file size: {file_size}")
    if '\n' in str(file_path.name):
        raise ValueError(f"Invalid filename (contains newline): {file_path.name}")
    return f"version aether-vault-pointer v1\nhash-sha256 {sha256_hash}\nsize {file_size}\noriginal-path {file_path.name}\n"

def parse_pointer(content: str) -> dict | None:
    lines = content.strip().split('\n')
    if not lines or not lines[0].startswith("version aether-vault-pointer"):
        return None
    res = {"version": lines[0].split()[-1]}
    for line in lines[1:]:
        parts = line.split(' ', 1)
        if len(parts) == 2:
            key, val = parts[0], parts[1]
            if key == "hash-sha256":
                res["hash"] = val
            elif key == "size":
                try:
                    res["size"] = int(val)
                except ValueError:
                    return None
            elif key == "original-path":
                res["original_path"] = val
    if "hash" not in res or "size" not in res:
        return None
    return res

def is_pointer_file(file_path: Path) -> bool:
    if not file_path.exists() or not file_path.is_file():
        return False
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            first_line = f.readline()
            return first_line.startswith("version aether-vault-pointer")
    except Exception:
        return False

def get_pointer_path(original_path: Path) -> Path:
    return original_path.with_name(original_path.name + ".av-pointer")
