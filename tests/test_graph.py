"""Unit-level tests for the AST-parsing/call-resolution logic in graph.py — the actual
algorithmic core (CodeVisitor, resolve_targets, sanitize_name, is_ignored). The end-to-end
`av graph --update` path is already covered by test_cli_commands.py; these tests exercise
the pieces that end-to-end test can't easily pin down in isolation.
"""

import ast
from pathlib import Path

from python.av_cli.graph import CodeVisitor, is_ignored, resolve_targets, sanitize_name


def _visit(source: str) -> CodeVisitor:
    tree = ast.parse(source)
    visitor = CodeVisitor()
    visitor.visit(tree)
    return visitor


def test_sanitize_name_replaces_non_identifier_characters():
    assert sanitize_name("foo/bar.py") == "foo_bar.py"
    assert sanitize_name("a b:c") == "a_b_c"
    assert sanitize_name("already_fine-name.v1") == "already_fine-name.v1"


def test_is_ignored_true_for_known_ignore_dirs():
    assert is_ignored(Path("repo/__pycache__/mod.py")) is True
    assert is_ignored(Path("repo/.git/HEAD")) is True
    assert is_ignored(Path("repo/venv/lib/site.py")) is True


def test_is_ignored_false_for_a_normal_path():
    assert is_ignored(Path("repo/python/av_cli/main.py")) is False


def test_visitor_collects_top_level_functions():
    visitor = _visit("def foo():\n    pass\n\ndef bar():\n    pass\n")
    names = [name for name, _, _ in visitor.functions]
    assert names == ["foo", "bar"]


def test_visitor_qualifies_methods_with_their_class_name():
    visitor = _visit(
        "class Widget:\n"
        "    def render(self):\n"
        "        pass\n"
    )
    names = [name for name, _, _ in visitor.functions]
    assert names == ["Widget.render"]


def test_visitor_captures_docstrings():
    visitor = _visit(
        "def foo():\n"
        '    """Does a thing."""\n'
        "    pass\n"
    )
    assert visitor.functions[0][2] == "Does a thing."


def test_visitor_tracks_calls_made_inside_a_function():
    visitor = _visit(
        "def foo():\n"
        "    bar()\n"
        "    self.baz()\n"
    )
    assert visitor.calls["foo"] == {"bar", "self.baz"}


def test_get_name_resolves_a_plain_call():
    visitor = _visit("def foo():\n    helper()\n")
    assert visitor.calls["foo"] == {"helper"}


def test_get_name_resolves_a_dotted_attribute_call():
    visitor = _visit("def foo():\n    module.submodule.func()\n")
    assert visitor.calls["foo"] == {"module.submodule.func"}


def test_resolve_targets_finds_a_direct_function_map_match():
    rel = Path("pkg/mod.py")
    func_map = {"helper": [(rel, "helper")]}
    targets = resolve_targets("helper", rel, None, func_map, Path("/vault"))
    assert targets == [(rel, "helper")]


def test_resolve_targets_resolves_self_dot_method_within_current_class():
    rel = Path("pkg/mod.py")
    func_map = {"Widget.render": [(rel, "Widget.render")]}
    targets = resolve_targets("self.render", rel, "Widget", func_map, Path("/vault"))
    assert (rel, "Widget.render") in targets


def test_resolve_targets_returns_empty_list_for_an_unresolvable_call():
    rel = Path("pkg/mod.py")
    targets = resolve_targets("totally_unknown_call", rel, None, {}, Path("/vault"))
    assert targets == []


def test_resolve_targets_deduplicates_candidates():
    rel = Path("pkg/mod.py")
    # Same target reachable two different ways (direct name + module-qualified) — should only
    # appear once in the result.
    func_map = {
        "helper": [(rel, "helper")],
        "mod.helper": [(rel, "helper")],
    }
    targets = resolve_targets("mod.helper", rel, None, func_map, Path("/vault"))
    assert targets.count((rel, "helper")) == 1
