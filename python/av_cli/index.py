import json
from pathlib import Path

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

    def save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, 'w', encoding='utf-8') as f:
            json.dump({"entries": self.entries}, f, indent=2)

    def add_entry(self, rel_path: str, hash: str, size: int, mtime_ns: int, file_type: str, pointer: str | None = None, auto_save: bool = True) -> None:
        self.entries[rel_path] = {
            "hash": hash,
            "size": size,
            "mtime_ns": mtime_ns,
            "type": file_type,
            "staged": True,
            "pointer": pointer
        }
        if auto_save:
            self.save()

    def remove_entry(self, rel_path: str) -> None:
        if rel_path in self.entries:
            del self.entries[rel_path]
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
