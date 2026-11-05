import json
from pathlib import Path

from .exceptions import CorruptStateError
from .fsutil import atomic_write_chunks, read_text_with_retry

class Index:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.index_path = repo_root / '.av' / 'index'
        self.entries = {}
        self.removed: list[str] = []
        self.load()

    def load(self) -> None:
        self.entries = {}
        self.removed = []
        if not self.index_path.exists():
            return
        try:
            data = json.loads(read_text_with_retry(self.index_path))
            entries = data["entries"]
            removed = data.get("removed", [])
            if not isinstance(entries, dict) or not isinstance(removed, list):
                raise ValueError("unexpected shape")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CorruptStateError(
                f"{self.index_path} can't be read ({exc}); left as it is. Repair it or restore it from a backup."
            ) from exc
        self.entries = entries
        self.removed = removed

    def iter_serialized(self):
        """The on-disk index text as fragments: `{"entries":{...}}`, compact separators,
        entries sorted by path (V1.5.0: byte-identical for a given working tree regardless
        of staging order or AV_THREADS). `removed` appears only while a deletion is staged, so
        an index without one keeps its exact bytes. Yielded entry by entry over a sorted view rather
        than built through a second `dict(sorted(...))` copy plus one multi-MB string --
        byte-for-byte the same bytes as
        `json.dumps({"entries": dict(sorted(entries.items()))}, separators=(",", ":"))`."""
        yield '{"entries":{'
        first = True
        for rel_path, entry in sorted(self.entries.items()):
            yield f'{"" if first else ","}{json.dumps(rel_path)}:{json.dumps(entry, separators=(",", ":"))}'
            first = False
        yield "}"
        if self.removed:
            yield f',"removed":{json.dumps(sorted(self.removed), separators=(",", ":"))}'
        yield "}"

    def serialize(self) -> str:
        return "".join(self.iter_serialized())

    def save(self) -> None:
        atomic_write_chunks(self.index_path, self.iter_serialized())

    def add_entry(self, rel_path: str, hash: str, size: int, mtime_ns: int, file_type: str, pointer: str | None = None, auto_save: bool = True) -> None:
        existing = self.entries.get(rel_path)
        changed = existing is None or existing.get("hash") != hash
        if rel_path in self.removed:
            self.removed.remove(rel_path)
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

    def stage_removal(self, rel_path: str) -> None:
        """Drops a tracked path and records that the next commit must carry the deletion, even
        if nothing else is staged."""
        del self.entries[rel_path]
        if rel_path not in self.removed:
            self.removed.append(rel_path)

    def get_entry(self, rel_path: str) -> dict | None:
        return self.entries.get(rel_path)

    def get_all_entries(self) -> dict:
        return self.entries

    def get_staged_entries(self) -> dict:
        return {k: v for k, v in self.entries.items() if v.get("staged")}

    def clear_staged(self) -> None:
        for entry in self.entries.values():
            entry["staged"] = False
        self.removed = []
        self.save()

    def classify_file(self, rel_path: str) -> str:
        ext = Path(rel_path).suffix.lower()
        if ext in ['.py', '.json', '.yaml', '.yml', '.toml', '.cfg', '.md', '.txt']:
            return 'code'
        return 'artifact'
