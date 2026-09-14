import json
from pathlib import Path

from .fsutil import atomic_write_chunks

class Index:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.index_path = repo_root / '.av' / 'index'
        self.entries = {}
        self.load()

    def load(self) -> None:
        if self.index_path.exists():
            try:
                with open(self.index_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.entries = data.get("entries", {})
            except Exception:
                self.entries = {}
        else:
            self.entries = {}

    def iter_serialized(self):
        """The on-disk index text as fragments: `{"entries":{...}}`, compact separators,
        entries sorted by path (V1.5.0: byte-identical for a given working tree regardless
        of staging order or AV_THREADS). Yielded entry by entry over a sorted view rather
        than built through a second `dict(sorted(...))` copy plus one multi-MB string --
        byte-for-byte the same bytes as
        `json.dumps({"entries": dict(sorted(entries.items()))}, separators=(",", ":"))`."""
        yield '{"entries":{'
        first = True
        for rel_path, entry in sorted(self.entries.items()):
            yield f'{"" if first else ","}{json.dumps(rel_path)}:{json.dumps(entry, separators=(",", ":"))}'
            first = False
        yield "}}"

    def serialize(self) -> str:
        return "".join(self.iter_serialized())

    def save(self) -> None:
        atomic_write_chunks(self.index_path, self.iter_serialized())

    def add_entry(self, rel_path: str, hash: str, size: int, mtime_ns: int, file_type: str, pointer: str | None = None, auto_save: bool = True) -> None:
        existing = self.entries.get(rel_path)
        changed = existing is None or existing.get("hash") != hash
        self.entries[rel_path] = {
            "hash": hash,
            "size": size,
            "mtime_ns": mtime_ns,
            "type": file_type,
            "staged": changed,
            "pointer": pointer
        }
        if auto_save:
            self.save()

    def remove_entry(self, rel_path: str, auto_save: bool = True) -> None:
        if rel_path in self.entries:
            del self.entries[rel_path]
            if auto_save:
                self.save()

    def get_entry(self, rel_path: str) -> dict | None:
        return self.entries.get(rel_path)

    def get_all_entries(self) -> dict:
        return self.entries

    def get_staged_entries(self) -> dict:
        return {k: v for k, v in self.entries.items() if v.get("staged")}

    def clear_staged(self) -> None:
        for entry in self.entries.values():
            entry["staged"] = False
        self.save()

    def classify_file(self, rel_path: str) -> str:
        ext = Path(rel_path).suffix.lower()
        if ext in ['.py', '.json', '.yaml', '.yml', '.toml', '.cfg', '.md', '.txt']:
            return 'code'
        return 'artifact'
