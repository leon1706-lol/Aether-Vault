import ast
import os
import re
import shutil
from pathlib import Path

IGNORE_DIRS = {'__pycache__', '.av', '.git', 'venv', '.venv', 'Aether-Graph', 'Aether-vault-Obsidian-Vault'}
IMPORT_MAP = {}

def is_ignored(path: Path):
    for p in path.parts:
        if p.startswith('.') and p not in ['.', '..']:
            pass
        if p in IGNORE_DIRS:
            return True
    return False

def find_py_files(root: Path):
    for p in root.rglob('*.py'):
        if is_ignored(p): continue
        yield p

class CodeVisitor(ast.NodeVisitor):
    def __init__(self):
        self.functions = []
        self.calls = {}
        self.current = None
        self.current_class = None
        self.imports = {}
        self.from_imports = {}

    def visit_Import(self, node):
        for alias in node.names:
            self.imports[alias.asname or alias.name] = alias.name

    def visit_ImportFrom(self, node):
        module = node.module or ''
        for alias in node.names:
            name = alias.asname or alias.name
            self.from_imports[name] = f"{module}.{alias.name}" if module else alias.name

    def visit_ClassDef(self, node):
        prev = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = prev

    def visit_FunctionDef(self, node):
        qual = f"{self.current_class}.{node.name}" if self.current_class else node.name
        self.functions.append((qual, node.lineno, ast.get_docstring(node) or ''))
        prev = self.current
        self.current = qual
        self.calls.setdefault(qual, set())
        self.generic_visit(node)
        self.current = prev

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_Call(self, node):
        name = self.get_name(node.func)
        if name and self.current:
            self.calls.setdefault(self.current, set()).add(name)
        self.generic_visit(node)

    def get_name(self, node):
        if isinstance(node, ast.Name): return node.id
        if isinstance(node, ast.Attribute):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
                parts.reverse()
                return '.'.join(parts)
        return None

def add_parents(node):
    for child in ast.iter_child_nodes(node):
        child.parent = node
        add_parents(child)

def read_source(path: Path):
    try: return path.read_text(encoding='utf-8')
    except: return ''

def sanitize_name(s: str):
    return re.sub(r'[^0-9A-Za-z_.-]+', '_', s)

def make_wiki_link(source: Path, target: Path):
    try: rel = target.relative_to(source.parent)
    except ValueError: rel = Path(os.path.relpath(target, source.parent))
    if rel.suffix == '.md': rel = rel.with_suffix('')
    return rel.as_posix()

def write_module_note(vault: Path, relpath: Path, functions):
    out_dir = vault / 'code' / relpath.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / (relpath.name + '.md')
    lines = [f'# Module: {relpath.as_posix()}\n\nFunctions:\n\n']
    for fn in functions:
        fn_name = fn[0]
        fn_path = sanitize_name(relpath.as_posix() + '__' + fn_name) + '.md'
        target = vault / 'code' / relpath.parent / fn_path
        link = make_wiki_link(out_file, target)
        lines.append(f'- [[{link}]]\n')
    out_file.write_text(''.join(lines), encoding='utf-8')

    if relpath.name == '__init__.py':
        folders_dir = vault / 'code' / 'folders'
        folders_dir.mkdir(parents=True, exist_ok=True)
        folder_note = folders_dir / (sanitize_name(relpath.parent.as_posix()) + '.md')
        link = make_wiki_link(folder_note, out_file)
        header = f'# Folder: {relpath.parent.as_posix()}\n\n'
        entry = f'- [[{link}]]\n'
        if folder_note.exists():
            txt = folder_note.read_text(encoding='utf-8')
            if link not in txt:
                if 'Contents:' not in txt: txt += '\nContents:\n\n'
                txt += entry
                folder_note.write_text(txt, encoding='utf-8')
        else:
            folder_note.write_text(header + 'Contents:\n\n' + entry, encoding='utf-8')
    return out_file

def resolve_targets(call_name: str, relpath: Path, current_class: str | None, func_map: dict, vault: Path):
    candidates = []
    if call_name in func_map: candidates.extend(func_map[call_name])
    if current_class and call_name.startswith('self.'):
        method = call_name.split('.', 1)[1]
        candidates.extend(func_map.get(f'{current_class}.{method}', []))
        candidates.extend(func_map.get(method, []))
    if '.' in call_name:
        module, rest = call_name.split('.', 1)
        candidates.extend(func_map.get(f'{module}.{rest}', []))
        if '.' not in rest: candidates.extend(func_map.get(rest, []))
        full = IMPORT_MAP.get(module)
        if full:
            candidates.append(ensure_external_note(vault, module, full))
    seen, unique = set(), []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique

def ensure_external_note(vault: Path, prefix: str, fullname: str):
    ext_dir = vault / 'code' / 'external'
    ext_dir.mkdir(parents=True, exist_ok=True)
    note = ext_dir / (sanitize_name(prefix) + '.md')
    if not note.exists():
        url = f'https://www.google.com/search?q={fullname}'
        note.write_text(f'# External: {prefix}\n\n- Module: {fullname}\n\n- Docs: {url}\n', encoding='utf-8')
    return ('external', sanitize_name(prefix), fullname)

def write_function_note(vault: Path, relpath: Path, func_name: str, lineno: int, doc: str, calls, current_class: str | None, func_map):
    out_dir = vault / 'code' / relpath.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = sanitize_name(relpath.as_posix() + '__' + func_name) + '.md'
    out_file = out_dir / fname
    lines = [f'# Function: {func_name}\n\n- Module: {relpath.as_posix()}\n- Defined at: line {lineno}\n\n']
    if doc: lines.append(f'## Docstring\n\n{doc}\n\n')
    if calls:
        lines.append('## Calls\n\n')
        for c in sorted(calls):
            targets = resolve_targets(c, relpath, current_class, func_map, vault)
            if targets:
                for t in targets:
                    if isinstance(t, tuple) and t[0] == 'external':
                        ext_target = vault / 'code' / 'external' / (t[1] + '.md')
                        lines.append(f'- [[{make_wiki_link(out_file, ext_target)}]] (external `{c}`)\n')
                    else:
                        t_path = sanitize_name(t[0].as_posix() + '__' + t[1]) + '.md'
                        target = vault / 'code' / t[0].parent / t_path
                        lines.append(f'- [[{make_wiki_link(out_file, target)}]] (from `{c}`)\n')
            else:
                lines.append(f'- {c}\n')
    out_file.write_text(''.join(lines), encoding='utf-8')

def generate_full_graph(repo_root: Path, vault: Path):
    code_dir = vault / 'code'
    if code_dir.exists(): shutil.rmtree(code_dir)

    module_functions = {}
    func_map = {}
    global IMPORT_MAP
    IMPORT_MAP.clear()

    # Pass 1
    for py in find_py_files(repo_root):
        rel = py.relative_to(repo_root)
        src = read_source(py)
        try: tree = ast.parse(src)
        except: continue
        add_parents(tree)
        vis = CodeVisitor()
        vis.visit(tree)
        if vis.functions:
            module_functions[rel] = vis.functions
            module_key = rel.stem
            for name, lineno, doc in vis.functions:
                simple_name = name.split('.')[-1]
                for k in [name, simple_name, f'{module_key}.{simple_name}']:
                    func_map.setdefault(k, []).append((rel, name))
                if '.' in name: func_map.setdefault(f'{module_key}.{name}', []).append((rel, name))
        for k, v in getattr(vis, 'imports', {}).items(): IMPORT_MAP[k] = v
        for k, v in getattr(vis, 'from_imports', {}).items(): IMPORT_MAP[k] = v

    # Pass 2
    for py in find_py_files(repo_root):
        rel = py.relative_to(repo_root)
        src = read_source(py)
        try: tree = ast.parse(src)
        except: continue
        add_parents(tree)
        vis = CodeVisitor()
        vis.visit(tree)
        functions = module_functions.get(rel, [])
        write_module_note(vault, rel, functions)
        for name, lineno, doc in functions:
            calls = vis.calls.get(name, set())
            current_class = name.rsplit('.', 1)[0] if '.' in name else None
            write_function_note(vault, rel, name, lineno, doc, calls, current_class, func_map)

    # Write project map
    out = vault / 'Project-Map.md'
    lines = ['# Project Map\n\n']
    for p in sorted(repo_root.iterdir()):
        if p.name.startswith('.') or p.name in IGNORE_DIRS: continue
        lines.append(f'- [{p.name}](../{p.name})\n')
    out.write_text(''.join(lines), encoding='utf-8')
