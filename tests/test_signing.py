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


# ---------------------------------------------------------------------------
# v1.2.5: key management — fingerprint, list, rotate
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_and_golden():
    """Golden fixture: sha256(pubkey)[:16 hex], grouped xxxx:xxxx:xxxx:xxxx — a real
    contract other tools/humans compare by eye, so the exact rendering is pinned."""
    pubkey = bytes(range(32))  # deterministic 32 bytes
    fp = signing.fingerprint(pubkey)
    import hashlib

    expected_hex = hashlib.sha256(pubkey).hexdigest()[:16]
    expected = ":".join(expected_hex[i:i + 4] for i in range(0, 16, 4))
    assert fp == expected
    assert fp.count(":") == 3
    assert len(fp.replace(":", "")) == 16


def test_fingerprint_differs_for_different_keys():
    fp_a = signing.fingerprint(bytes(range(32)))
    fp_b = signing.fingerprint(bytes(range(1, 33)))
    assert fp_a != fp_b


def test_keys_list_shows_active_key(signed_repo):
    repo_root, priv, pub = signed_repo
    entries = signing.list_keys(repo_root)
    assert len(entries) == 1
    assert entries[0]["active"] is True
    assert entries[0]["fingerprint"] == signing.fingerprint(pub.read_bytes())


def test_keys_list_empty_when_no_key(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    assert signing.list_keys(tmp_path) == []


def test_rotate_archives_old_key_and_generates_new_one(signed_repo):
    repo_root, priv, pub = signed_repo
    old_pub_bytes = pub.read_bytes()
    old_fp = signing.fingerprint(old_pub_bytes)

    new_priv, new_pub = signing.rotate_keypair(repo_root)
    new_fp = signing.fingerprint(new_pub.read_bytes())

    assert new_fp != old_fp
    assert new_priv.exists() and new_pub.exists()
    entries = signing.list_keys(repo_root)
    fps = {e["fingerprint"]: e["active"] for e in entries}
    assert fps[new_fp] is True
    assert fps[old_fp] is False
    # The archived PRIVATE key survives too (rotation never deletes signing capability).
    archive_dir = signing.archived_keys_dir(repo_root)
    archived_privs = list(archive_dir.rglob(signing.SIGNING_KEY_PATH))
    assert len(archived_privs) == 1


def test_rotate_without_existing_key_raises(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError):
        signing.rotate_keypair(tmp_path)


def test_commit_signed_before_and_after_rotation_both_verify(signed_repo):
    """The whole point of rotation: old commits keep verifying against their OWN
    embedded (now-archived) public key; new commits verify against the new one."""
    repo_root, priv, pub = signed_repo
    old_hash = _stage_and_commit(repo_root, name="before.txt", message="before rotation")
    old_commit = _commit_file(repo_root, old_hash)
    assert verify_signature(old_commit) == (True, "verified")

    signing.rotate_keypair(repo_root)

    new_hash = _stage_and_commit(repo_root, name="after.txt", message="after rotation")
    new_commit = _commit_file(repo_root, new_hash)
    assert verify_signature(new_commit) == (True, "verified")
    assert old_commit["signature"]["public_key"] != new_commit["signature"]["public_key"]

    # Re-verify the OLD commit again post-rotation — nothing about it changed.
    old_commit_again = _commit_file(repo_root, old_hash)
    assert verify_signature(old_commit_again) == (True, "verified")


def test_cli_keys_list_and_fingerprint(signed_repo):
    repo_root, priv, pub = signed_repo
    res = CliRunner().invoke(cli, ["registry", "keys", "list"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "active" in res.output

    fp_res = CliRunner().invoke(cli, ["registry", "keys", "fingerprint"], standalone_mode=False)
    assert fp_res.exit_code == 0, fp_res.output
    assert fp_res.output.strip() == signing.fingerprint(pub.read_bytes())


def test_cli_keys_rotate_with_yes_skips_prompt(signed_repo):
    repo_root, priv, pub = signed_repo
    old_fp = signing.fingerprint(pub.read_bytes())
    res = CliRunner().invoke(cli, ["registry", "keys", "rotate", "--yes"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert old_fp in res.output
    new_fp = signing.fingerprint(pub.read_bytes())
    assert new_fp != old_fp


def test_cli_keys_rotate_declines_without_yes(signed_repo):
    repo_root, priv, pub = signed_repo
    old_fp = signing.fingerprint(pub.read_bytes())
    res = CliRunner().invoke(cli, ["registry", "keys", "rotate"], input="n\n", standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "Aborted" in res.output
    assert signing.fingerprint(pub.read_bytes()) == old_fp  # untouched


# ---------------------------------------------------------------------------
# v1.2.5: detached signature export/verify
# ---------------------------------------------------------------------------

def test_export_and_verify_detached_signature_roundtrip(signed_repo):
    repo_root, priv, pub = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    commit = _commit_file(repo_root, commit_hash)

    blob = signing.export_signature_blob(commit_hash, commit)
    assert blob["hash"] == commit_hash
    assert blob["algo"] == "ed25519"
    assert blob["fingerprint"] == signing.fingerprint(pub.read_bytes())

    ok, reason = signing.verify_detached(commit, blob)
    assert (ok, reason) == (True, "verified")


def test_verify_detached_rejects_hash_mismatch(signed_repo):
    repo_root, priv, pub = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    commit = _commit_file(repo_root, commit_hash)
    blob = signing.export_signature_blob(commit_hash, commit)
    blob["hash"] = "f" * 64  # tampered/mismatched record

    ok, reason = signing.verify_detached(commit, blob)
    assert ok is False
    assert "hash mismatch" in reason


def test_verify_detached_rejects_tampered_canonical_bytes(signed_repo):
    repo_root, priv, pub = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    commit = _commit_file(repo_root, commit_hash)
    blob = signing.export_signature_blob(commit_hash, commit)

    tampered_commit = dict(commit)
    tampered_commit["message"] = "not what was signed"
    ok, reason = signing.verify_detached(tampered_commit, blob)
    assert ok is False


def test_export_signature_blob_raises_for_unsigned_commit(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    commit_hash = _stage_and_commit(tmp_path)
    commit = _commit_file(tmp_path, commit_hash)
    assert commit.get("signature") is None
    with pytest.raises(ValueError):
        signing.export_signature_blob(commit_hash, commit)


def test_cli_export_signature_writes_file_and_cli_verify_roundtrips(signed_repo, tmp_path):
    repo_root, priv, pub = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    out_file = tmp_path / "sig.json"

    export_res = CliRunner().invoke(
        cli, ["registry", "export-signature", commit_hash, "--out", str(out_file)],
        standalone_mode=False,
    )
    assert export_res.exit_code == 0, export_res.output
    assert out_file.exists()
    exported = json.loads(out_file.read_text())
    assert exported["hash"] == commit_hash

    verify_res = CliRunner().invoke(
        cli, ["registry", "verify", commit_hash, "--signature", str(out_file)],
        standalone_mode=False,
    )
    assert verify_res.exit_code == 0, verify_res.output
    assert "VERIFIED" in verify_res.output


def test_cli_verify_detached_exits_15_on_tamper(signed_repo, tmp_path):
    repo_root, priv, pub = signed_repo
    commit_hash = _stage_and_commit(repo_root)
    out_file = tmp_path / "sig.json"
    CliRunner().invoke(cli, ["registry", "export-signature", commit_hash, "--out", str(out_file)],
                       standalone_mode=False)

    blob = json.loads(out_file.read_text())
    blob["sig"] = base64.b64encode(b"\x00" * 64).decode()
    out_file.write_text(json.dumps(blob))

    res = CliRunner().invoke(cli, ["registry", "verify", commit_hash, "--signature", str(out_file)],
                             standalone_mode=False)
    assert res.exit_code == 15


# ---------------------------------------------------------------------------
# v1.2.5: signature requirement branch policy (require_signature)
# ---------------------------------------------------------------------------

def test_promote_denies_unsigned_candidate_when_require_signature_armed(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    _stage_and_commit(tmp_path, name="base.txt", message="baseline")

    # Arm require_signature on main WITHOUT a signing key ever having been generated.
    policies_path = tmp_path / ".av" / "policies.json"
    policies_path.write_text(json.dumps({"main": {"require_signature": True}}))

    _stage_and_commit(tmp_path, name="candidate.txt", message="unsigned candidate")

    res = CliRunner().invoke(cli, ["promote", "--into", "main"], standalone_mode=False)
    assert res.exit_code == 16, res.output
    assert "require_signature" in res.output
    assert "unsigned" in res.output.lower()


def test_promote_allows_signed_candidate_when_require_signature_armed(signed_repo):
    repo_root, priv, pub = signed_repo
    _stage_and_commit(repo_root, name="signed-candidate.txt", message="signed candidate")

    policies_path = repo_root / ".av" / "policies.json"
    policies_path.write_text(json.dumps({"main": {"require_signature": True}}))

    res = CliRunner().invoke(cli, ["promote", "--into", "main"], standalone_mode=False)
    assert res.exit_code == 0, res.output


def test_require_signature_policy_does_not_affect_policies_without_it(tmp_path, monkeypatch):
    """Additive-only: an existing metric-only policy behaves exactly as before."""
    _init_repo(tmp_path, monkeypatch)
    _stage_and_commit(tmp_path, name="m.txt", message="baseline", )

    policies_path = tmp_path / ".av" / "policies.json"
    policies_path.write_text(json.dumps({"main": {"metric": "val_loss", "op": "<", "threshold": 999}}))

    # No metric on this commit at all -> denied for the pre-existing metric reason,
    # not any new require_signature reason (the field is simply absent from the policy).
    res = CliRunner().invoke(cli, ["promote", "--into", "main"], standalone_mode=False)
    assert res.exit_code == 16, res.output
    assert "require_signature" not in res.output


# ---------------------------------------------------------------------------
# "not PKI / not identity binding" must appear on --help for every command under av
# registry keys / verify / export-signature, not just the parent group, since click
# doesn't roll a group's help into its subcommands' --help.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["registry", "keys", "--help"],
    ["registry", "keys", "list", "--help"],
    ["registry", "keys", "fingerprint", "--help"],
    ["registry", "keys", "rotate", "--help"],
    ["registry", "verify", "--help"],
    ["registry", "export-signature", "--help"],
    ["policy", "set", "--help"],
])
def test_every_signing_command_help_states_not_pki(args):
    res = CliRunner().invoke(cli, args, standalone_mode=False)
    assert res.exit_code == 0, res.output
    lowered = res.output.lower()
    assert "not" in lowered and "pki" in lowered and "identity" in lowered, (
        f"{' '.join(args)} --help doesn't restate the not-PKI/not-identity-binding "
        f"disclaimer:\n{res.output}"
    )
