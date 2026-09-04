"""python/av_cli/casobj.py — content-addressed CAS objects for non-file RSI artifacts.

Pure-logic + filesystem tests, no server needed. Signing round-trips are skipped without
the `[sign]` extra, same convention as tests/test_signing.py.
"""
import json

import pytest

from python.av_cli import casobj


def test_canonical_bytes_is_sorted_keys_json_excluding_signature():
    doc = {"b": 1, "a": 2, "signature": {"algo": "ed25519"}}
    out = casobj.canonical_bytes(doc)
    assert out == b'{"a": 2, "b": 1}'


def test_canonical_bytes_custom_exclude():
    doc = {"a": 1, "secret": "shh"}
    assert casobj.canonical_bytes(doc, exclude=("secret",)) == b'{"a": 1}'


def test_object_id_is_sha256_of_canonical_bytes():
    import hashlib

    doc = {"a": 1}
    assert casobj.object_id(doc) == hashlib.sha256(b'{"a": 1}').hexdigest()


def test_object_id_ignores_signature_field():
    doc1 = {"a": 1}
    doc2 = {"a": 1, "signature": {"algo": "ed25519", "sig": "whatever"}}
    assert casobj.object_id(doc1) == casobj.object_id(doc2)


def test_write_object_round_trips(tmp_path):
    doc = {"kind": "improver_manifest", "code": [{"path": "a.py", "hash": "x"}]}
    oid = casobj.write_object(tmp_path, doc)
    assert oid == casobj.object_id(doc)
    back = casobj.read_object(tmp_path, oid)
    assert back == doc


def test_write_object_is_idempotent(tmp_path):
    doc = {"a": 1}
    oid1 = casobj.write_object(tmp_path, doc)
    path = casobj.object_path(tmp_path, oid1)
    original_bytes = path.read_bytes()
    oid2 = casobj.write_object(tmp_path, doc)
    assert oid1 == oid2
    assert path.read_bytes() == original_bytes  # never rewritten


def test_write_object_preserves_full_doc_including_signature(tmp_path):
    doc = {"a": 1, "signature": {"algo": "ed25519", "sig": "abc"}}
    oid = casobj.write_object(tmp_path, doc)
    back = casobj.read_object(tmp_path, oid)
    assert back["signature"] == {"algo": "ed25519", "sig": "abc"}
    # the id itself must NOT depend on the signature block
    assert oid == casobj.object_id({"a": 1})


def test_object_path_uses_two_char_fanout(tmp_path):
    oid = "abcd" + "0" * 60
    p = casobj.object_path(tmp_path, oid)
    assert p == tmp_path / ".av" / "objects" / "ab" / ("cd" + "0" * 60)


def test_read_object_returns_none_when_missing(tmp_path):
    assert casobj.read_object(tmp_path, "0" * 64) is None


def test_read_object_returns_none_on_corrupt_json(tmp_path):
    oid = "1" * 64
    path = casobj.object_path(tmp_path, oid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert casobj.read_object(tmp_path, oid) is None


def test_verify_object_unsigned_reports_unsigned():
    ok, reason = casobj.verify_object({"a": 1})
    assert ok is False
    assert reason == "unsigned"


def test_verify_object_malformed_signature_shape():
    ok, reason = casobj.verify_object({"a": 1, "signature": "not-a-dict"})
    assert ok is False
    assert reason == "unsigned"


def test_verify_object_unsupported_algo():
    ok, reason = casobj.verify_object({"a": 1, "signature": {"algo": "rsa"}})
    assert ok is False
    assert "unsupported algo" in reason


@pytest.mark.parametrize("has_crypto", [True])
def test_sign_and_verify_round_trip(tmp_path, has_crypto):
    pytest.importorskip("cryptography", reason="requires the [sign] extra")
    from python.av_cli import signing

    (tmp_path / ".av").mkdir()
    signing.generate_keypair(tmp_path)

    doc = {"kind": "improver_manifest", "code": []}
    sig = casobj.sign_object(doc, tmp_path)
    assert sig is not None
    assert sig["algo"] == "ed25519"
    doc["signature"] = sig

    ok, reason = casobj.verify_object(doc)
    assert ok is True
    assert reason == "verified"


def test_sign_object_tamper_detected(tmp_path):
    pytest.importorskip("cryptography", reason="requires the [sign] extra")
    from python.av_cli import signing

    (tmp_path / ".av").mkdir()
    signing.generate_keypair(tmp_path)

    doc = {"a": 1}
    sig = casobj.sign_object(doc, tmp_path)
    doc["signature"] = sig
    doc["a"] = 2  # tamper after signing

    ok, reason = casobj.verify_object(doc)
    assert ok is False
    assert "modified after signing" in reason


def test_sign_object_returns_none_without_a_key(tmp_path):
    (tmp_path / ".av").mkdir()
    assert casobj.sign_object({"a": 1}, tmp_path) is None


def test_canonical_commit_bytes_delegates_and_is_unchanged(tmp_path):
    """v1.3.1 refactor guard: signing.canonical_commit_bytes() now calls
    casobj.canonical_bytes() internally — this pins the output byte-identical to the
    pre-refactor inline implementation (also covered by tests/test_signing.py's own
    golden fixtures)."""
    from python.av_cli import signing

    commit = {"hash": "abc", "message": "m", "timestamp": "2026-01-01T00:00:00+00:00",
              "tree": {}}
    out = signing.canonical_commit_bytes(commit)
    expected = json.dumps(commit, sort_keys=True).encode("utf-8")
    assert out == expected
