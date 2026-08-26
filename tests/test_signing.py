"""Signed commits (v1.2.2): ed25519 keygen, canonical serialization, auto-sign, verify.

Roundtrip / tamper / unsigned-ok per the v1.2.2 plan. Skips cleanly when the [sign]
extra (`cryptography`) is missing — CI's plugin-tests job installs it explicitly so the
gate runs there; local dev venvs with cryptography run everything.
"""
import base64
import json

import pytest

from python.av_cli import signing
from python.av_cli.core import commit_staged
from python.av_cli.index import Index
from python.av_cli.signing import (
    canonical_commit_bytes,
    generate_keypair,
    has_signing_key,
    private_key_path,
    public_key_path,
    verify_signature,
)

crypto = pytest.importorskip("cryptography")

from click.testing import CliRunner  # noqa: E402  (after importorskip)

from python.av_cli.main import cli  # noqa: E402


def _init_repo(tmp_path, monkeypatch):
    """Init inside tmp_path with cwd pinned there (the checkout root itself contains an
    .av dir — without chdir, ensure_repo() would silently resolve to THE WRONG repo)."""
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output


@pytest.fixture()
def signed_repo(tmp_path, monkeypatch):
    """An initialized repo WITH a generated signing keypair (cwd stays pinned)."""
    _init_repo(tmp_path, monkeypatch)
    priv, pub = generate_keypair(tmp_path)
    return tmp_path, priv, pub


def _stage_and_commit(repo_root, name="f.txt", content=b"x", message="m"):
    (repo_root / name).write_bytes(content)
    idx = Index(repo_root)
    from python.av_cli.attributes import flags_for, load_attributes
    from python.av_cli.core import stage_one_file, load_config

    rel = name
    stage_one_file(repo_root, idx,
                   load_config(repo_root)["lfs_threshold_mb"] * 1024 * 1024,
                   repo_root / name, rel, flags_for(load_attributes(repo_root), rel))
    idx.save()
    return commit_staged(repo_root, message)


def _commit_file(repo_root, commit_hash):
    return json.loads((repo_root / ".av" / "commits" / f"{commit_hash}.json").read_text())


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

def test_canonical_form_is_sorted_keys_json_minus_signature():
    commit = {
        "signature": {"algo": "ed25519", "sig": "ZZZ"},
        "message": "m",
        "tree": {"a": {"hash": "h"}},
        "hash": "0" * 64,
    }
    canon = canonical_commit_bytes(commit)
    # sorted keys, signature stripped:
    expected = json.dumps(
        {"hash": "0" * 64, "message": "m", "tree": {"a": {"hash": "h"}}},
        sort_keys=True,
    ).encode()
    assert canon == expected


def test_canonical_form_golden_fixture():
    """Golden bytes: any reordering of dict insertion order must serialize identically."""
    a = {"z": 1, "a": {"b": [1, 2], "c": None}, "hash": "ab" * 32}
    b = {"hash": "ab" * 32, "a": {"c": None, "b": [1, 2]}, "z": 1}
    assert canonical_commit_bytes(a) == canonical_commit_bytes(b)
    assert canonical_commit_bytes(a) == (
        b'{"a": {"b": [1, 2], "c": null}, "hash": "' + b"ab" * 32 + b'", "z": 1}'
    )


def test_canonical_form_is_timezone_spelling_insensitive():
    """The registry echoes naive UTC; the authoring client writes '+00:00'. Both must
    canonicalize identically or every cloned verification would fail (manual wire find)."""
    aware = {"hash": "1" * 64, "timestamp": "2026-08-26T10:00:25.960666+00:00"}
    naive = {"hash": "1" * 64, "timestamp": "2026-08-26T10:00:25.960666"}
    zulu = {"hash": "1" * 64, "timestamp": "2026-08-26T10:00:25.960666Z"}
    assert canonical_commit_bytes(aware) == \
        canonical_commit_bytes(naive) == canonical_commit_bytes(zulu)
    # A REAL timezone difference still matters (different instant ⇒ different bytes):
    tokyo = {"hash": "1" * 64, "timestamp": "2026-08-26T19:00:25.960666+09:00"}
    assert canonical_commit_bytes(tokyo) == canonical_commit_bytes(aware)
    shifted = {"hash": "1" * 64, "timestamp": "2026-08-26T10:01:25.960666+00:00"}
    assert canonical_commit_bytes(shifted) != canonical_commit_bytes(aware)


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

def test_keygen_creates_keypair_and_refuses_overwrite(signed_repo):
    repo_root, priv, pub = signed_repo
    assert priv.exists() and pub.exists() and has_signing_key(repo_root)
    # Private key mode: 0600 where the OS honors POSIX modes.
    import os
    if os.name != "nt":
        assert (priv.stat().st_mode & 0o777) == 0o600
    # Rotation requires an explicit delete:
    with pytest.raises(FileExistsError):
        generate_keypair(repo_root)


def test_av_registry_keygen_creates_keys_in_repo(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    res = CliRunner().invoke(cli, ["registry", "keygen"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert private_key_path(tmp_path).exists() and has_signing_key(tmp_path)


def test_keygen_without_cryptography_fails_with_install_hint(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)

    def _no_crypto():
        raise signing.SigningUnavailable(
            "cryptography is not installed — run `pip install aether-vault[sign]`.")

    monkeypatch.setattr(signing, "_crypto", _no_crypto)
    res = CliRunner().invoke(cli, ["registry", "keygen"], standalone_mode=False)
    assert res.exit_code == 15  # validation
    assert "aether-vault[sign]" in res.output


def test_sign_payload_without_key_is_none_and_never_raises(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    assert signing.sign_payload({"hash": "0" * 64}, tmp_path) is None


def test_sign_payload_with_key_file_but_missing_extra_returns_none(tmp_path, monkeypatch):
    """A key file without the [sign] extra degrades to unsigned — never breaks a commit."""
    _init_repo(tmp_path, monkeypatch)
    priv, _pub = generate_keypair(tmp_path)
    assert priv.exists()

    def _no_crypto():
        raise signing.SigningUnavailable("cryptography is not installed")

    monkeypatch.setattr(signing, "_crypto", _no_crypto)
    assert signing.sign_payload({"hash": "0" * 64}, tmp_path) is None


# ---------------------------------------------------------------------------
# Roundtrip / tamper / unsigned-ok
# ---------------------------------------------------------------------------

def test_sign_then_verify_roundtrip():
    payload = {"hash": "1" * 64, "message": "hello", "tree": {}, "metrics": {"loss": 0.1}}
    # A throwaway keypair via a scratch repo dir:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".av").mkdir()
        generate_keypair(root)
        sig = signing.sign_payload(payload, root)
    assert sig is not None and sig["algo"] == "ed25519"

    signed = dict(payload, signature=sig)
    ok, reason = verify_signature(signed)
    assert ok, reason
    assert reason == "verified"


def test_tamper_detection_on_every_meaningful_field():
    import tempfile
    from pathlib import Path

    payload = {"hash": "2" * 64, "message": "original",
               "tree": {"f": {"hash": "aa"}}, "metrics": {}}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".av").mkdir()
        generate_keypair(root)
        sig = signing.sign_payload(payload, root)
    signed = dict(payload, signature=sig)

    for field, mutated in [
        ("message", "evil edit"),
        ("hash", "f" * 64),
        ("tree", {"f": {"hash": "bb"}}),
        ("metrics", {"loss": 999}),
    ]:
        tampered = dict(signed)
        tampered[field] = mutated
        ok, reason = verify_signature(tampered)
        assert not ok, f"tampering {field} was not detected"
        assert "mismatch" in reason or "malformed" in reason

    # Corrupting the signature blob itself:
    bad_sig = dict(signed)
    bad_sig["signature"] = dict(sig, sig=base64.b64encode(b"\x00" * 64).decode())
    ok, reason = verify_signature(bad_sig)
    assert not ok and "mismatch" in reason


def test_unsigned_commit_verifies_as_unsigned_not_failure():
    ok, reason = verify_signature({"hash": "3" * 64})
    assert not ok and reason == "unsigned"


# ---------------------------------------------------------------------------
# Integration: auto-sign through the single commit writer + av verify CLI
# ---------------------------------------------------------------------------

def test_commit_staged_auto_signs_when_key_configured(signed_repo):
    repo_root, _, _ = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    stored = _commit_file(repo_root, commit_hash)

    assert isinstance(stored.get("signature"), dict)
    ok, reason = verify_signature(stored)
    assert ok, reason
    # Signature covers the payload INCLUDING its hash; public key is this repo's:
    assert stored["signature"]["public_key"] == \
        public_key_path(repo_root).read_bytes().hex()


def test_commits_stay_unsigned_without_a_key(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    commit_hash = _stage_and_commit(tmp_path)
    stored = _commit_file(tmp_path, commit_hash)
    assert "signature" not in stored
    ok, reason = verify_signature(stored)
    assert not ok and reason == "unsigned"  # unsigned-ok: valid state, honest verdict


def test_av_verify_cli_reports_verified_then_tamper_exit_15(signed_repo):
    repo_root, _, _ = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    runner = CliRunner()

    res = runner.invoke(cli, ["registry", "verify", commit_hash], standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "VERIFIED" in res.output

    res_json = runner.invoke(cli, ["--output", "json", "registry", "verify", commit_hash],
                             standalone_mode=False)
    envelope = json.loads(res_json.output)
    assert envelope["ok"] is True
    assert envelope["data"]["scheme"] == "ed25519"
    assert envelope["data"]["signed_with_this_repos_key"] is True

    # Tamper AFTER signing (the attack verify exists for):
    path = repo_root / ".av" / "commits" / f"{commit_hash}.json"
    stored = json.loads(path.read_text())
    stored["message"] = "tampered after the fact"
    path.write_text(json.dumps(stored))

    res = runner.invoke(cli, ["registry", "verify", commit_hash], standalone_mode=False)
    assert res.exit_code == 15  # EXIT_VALIDATION
    assert "TAMPERED" in res.output


def test_av_verify_cli_honest_unsigned_verdict(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    commit_hash = _stage_and_commit(tmp_path)
    res = CliRunner().invoke(cli, ["registry", "verify", commit_hash],
                             standalone_mode=False)
    assert res.exit_code == 0  # unsigned-ok: NOT an error
    assert "UNSIGNED" in res.output


def test_legacy_hmac_attest_path_still_verifies(tmp_path, monkeypatch):
    """integrity-v0 repos keep working: attest tag check when no signature is present."""
    import hashlib
    import hmac as hmac_mod

    _init_repo(tmp_path, monkeypatch)
    commit_hash = _stage_and_commit(tmp_path)

    cfg_path = tmp_path / ".av" / "config"
    cfg = json.loads(cfg_path.read_text())
    cfg["attest_key"] = "legacy-key"
    cfg_path.write_text(json.dumps(cfg))

    commit = _commit_file(tmp_path, commit_hash)
    expected = hmac_mod.new(b"legacy-key", commit["hash"].encode(),
                            hashlib.sha256).hexdigest()
    commit["tags"] = list(commit.get("tags", [])) + [f"attest:{expected[:16]}"]
    (tmp_path / ".av" / "commits" / f"{commit_hash}.json").write_text(json.dumps(commit))

    res = CliRunner().invoke(cli, ["registry", "verify", commit_hash],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "VERIFIED" in res.output


def test_verify_unknown_commit_is_validation_error(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    res = CliRunner().invoke(cli, ["registry", "verify", "f" * 64],
                             standalone_mode=False)
    assert res.exit_code == 15
