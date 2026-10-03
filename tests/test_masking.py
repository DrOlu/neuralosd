"""Masking and the `scrub` command.

Two failure modes, both real:

  1. Key-name matching alone is trivially bypassed. A password stored under a
     key called "notes" is still a password, and an email under "contact" is
     still an email.

  2. Masking the RESPONSE is not masking the DISK. `pii_mask=False` means "do
     not hide fields in the reply"; it was being read as consent to persist
     secrets in the cache and in an append-only audit log that never expires.
     A leaked credential cannot be un-disclosed by re-asking.

Storage and presentation are now separate decisions.
"""
import json
import os
import sys

import pytest

from neuralosd import probe as probe_dec
from neuralosd.router import (MASKED, PII_HINTS, Router, _mask_value,
                              _storage_mask, mask_pii)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
CONN = "postgres://admin:s3cr3t@db.internal:5432/prod"
OPENAI = "sk-" + "a1b2c3d4e5" * 3


# ── masking by key name (existing contract, must not regress) ──────────────

def test_key_hint_masks_the_value():
    assert mask_pii({"email": "a@b.com"})["email"] == MASKED
    assert mask_pii({"user_password": "hunter2"})["user_password"] == MASKED


def test_key_hint_masks_at_every_depth():
    got = mask_pii({"outer": {"inner": {"phone": "555-1234"}}})
    assert got["outer"]["inner"]["phone"] == MASKED


def test_expanded_hints_cover_credentials():
    for key in ("password", "pin", "token", "secret", "api_key",
                "authorization", "private_key", "access_key", "cvv"):
        assert mask_pii({key: "x"})[key] == MASKED, key
        assert key in " ".join(PII_HINTS)


def test_non_hinted_keys_are_untouched_by_key_matching():
    assert mask_pii({"region": "North"})["region"] == "North"


# ── masking by VALUE shape (the bypass) ────────────────────────────────────

def test_jwt_under_an_innocent_key_is_still_masked():
    """'notes' is not a hinted key, so key matching cannot see this."""
    got = mask_pii({"notes": f"token is {JWT}"})
    assert JWT not in got["notes"]
    assert MASKED in got["notes"]


def test_connection_string_in_an_error_is_masked():
    """A DSN leaked into an exception message carries the password."""
    got = _mask_value(f"connection failed: {CONN}")
    assert "s3cr3t" not in got


def test_recognised_credential_shapes():
    for value, label in ((JWT, "jwt"), (OPENAI, "openai"),
                         ("ghp_" + "a" * 30, "github"),
                         ("AKIAIOSFODNN7EXAMPLE", "aws"),
                         ("Bearer abcdefghijklmnopqrstuvwxyz", "bearer"),
                         ("-----BEGIN RSA PRIVATE KEY-----", "pem")):
        assert MASKED in _mask_value(value), label


def test_deep_false_restores_key_only_behaviour():
    got = mask_pii({"notes": JWT}, deep=False)
    assert got["notes"] == JWT          # old behaviour, for compatibility


def test_aggressive_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv("NEURALOSD_MASK_AGGRESSIVE", raising=False)
    h = "a1b2c3d4" * 8                   # 64 hex chars
    assert _mask_value(h) == h, "long hex must NOT be masked by default"


def test_aggressive_mode_opts_in(monkeypatch):
    monkeypatch.setenv("NEURALOSD_MASK_AGGRESSIVE", "1")
    h = "a1b2c3d4" * 8
    assert MASKED in _mask_value(h)


def test_over_redaction_guard_email_in_a_response():
    """An email under an innocent key may BE the answer, so it survives."""
    got = mask_pii({"contact": "ada@example.com"}, deep=True)
    assert got["contact"] == "ada@example.com"


# ── storage masking ────────────────────────────────────────────────────────

def test_storage_masks_pii_by_value():
    assert _storage_mask({"contact": "ada@example.com"})["contact"] == MASKED


def test_storage_masks_credentials_and_key_hints():
    got = _storage_mask({"password": "hunter2", "notes": JWT, "n": 5})
    assert got["password"] == MASKED
    assert MASKED in got["notes"]
    assert got["n"] == 5                 # numbers survive


def test_storage_masks_nested_and_in_lists():
    got = _storage_mask({"rows": [{"contact": "a@b.com"}, {"region": "North"}]})
    assert got["rows"][0]["contact"] == MASKED
    assert got["rows"][1]["region"] == "North"


def _leaky_router(tmp_path, **kw):
    def leaky():
        return {"contact": "ada@example.com", "notes": f"key {JWT}",
                "n": 5}

    p = probe_dec(description="Leaky probe", triggers=["show the leak"],
                  name="leaky")(leaky)
    return Router(probes=[p], cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"), **kw)


def test_cache_on_disk_never_holds_a_secret(tmp_path):
    r = _leaky_router(tmp_path)
    r.ask("show the leak")
    blob = open(r.cache_file, encoding="utf-8").read()
    assert JWT not in blob
    assert "ada@example.com" not in blob


def test_audit_on_disk_never_holds_a_secret(tmp_path):
    r = _leaky_router(tmp_path)
    r.ask("show the leak")
    blob = open(r.audit_file, encoding="utf-8").read()
    assert JWT not in blob
    assert "ada@example.com" not in blob


def test_storage_masking_applies_even_when_pii_mask_is_off(tmp_path):
    """THE finding: pii_mask=False governs the RESPONSE, not the disk.

    The cache expires; the audit log does not. Reading `pii_mask=False` as
    consent to persist secrets is how 41 audit records ended up holding them.
    """
    r = _leaky_router(tmp_path, pii_mask=False)
    env = r.ask("show the leak")
    assert env["results"][0]["contact"] == "ada@example.com"   # reply intact
    assert JWT not in open(r.audit_file, encoding="utf-8").read()
    assert "ada@example.com" not in open(r.audit_file, encoding="utf-8").read()


def test_the_question_itself_is_masked_at_storage(tmp_path):
    r = _leaky_router(tmp_path)
    r.ask(f"show the leak {JWT}")
    assert JWT not in open(r.audit_file, encoding="utf-8").read()


# ── the scrub command ──────────────────────────────────────────────────────

class _Args:
    def __init__(self, d, **kw):
        self.instance_dir = d
        self.dry_run = kw.get("dry_run", False)
        self.purge_cache = kw.get("purge_cache", False)


def _seed(dirpath, secret=True):
    contact = "ada@example.com" if secret else "North"
    payload = {"question": "show the leak" if secret else "show the rows",
               "results": [{"contact": contact, "n": 5}]}
    if secret:
        payload["results"][0]["notes"] = f"key {JWT}"
    json.dump({"abc": {"ts": 1, "menu_version": "v", "payload": payload}},
              open(os.path.join(dirpath, ".ask_cache.json"), "w"))
    with open(os.path.join(dirpath, "ask_audit.jsonl"), "w") as fh:
        fh.write(json.dumps(payload) + "\n")
        fh.write(json.dumps({"question": "clean one", "results": [{"n": 1}]}) + "\n")


def test_scrub_rewrites_cache_and_audit(tmp_path):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path))
    scrub.run(_Args(str(tmp_path)))
    for f in (".ask_cache.json", "ask_audit.jsonl"):
        blob = open(os.path.join(tmp_path, f), encoding="utf-8").read()
        assert JWT not in blob, f
        assert "ada@example.com" not in blob, f


def test_scrub_dry_run_changes_nothing(tmp_path):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path))
    before = open(os.path.join(tmp_path, "ask_audit.jsonl"), encoding="utf-8").read()
    scrub.run(_Args(str(tmp_path), dry_run=True))
    after = open(os.path.join(tmp_path, "ask_audit.jsonl"), encoding="utf-8").read()
    assert before == after
    assert JWT in after


def test_scrub_purge_cache_deletes_it(tmp_path):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path))
    scrub.run(_Args(str(tmp_path), purge_cache=True))
    assert not os.path.exists(os.path.join(tmp_path, ".ask_cache.json"))
    assert os.path.exists(os.path.join(tmp_path, "ask_audit.jsonl"))


def test_scrub_is_safe_on_a_clean_instance(tmp_path, capsys):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path), secret=False)
    rc = scrub.run(_Args(str(tmp_path)))
    assert rc == 0
    assert "0 of 3 records were rewritten" in capsys.readouterr().out


def test_scrub_reports_what_it_found(tmp_path, capsys):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path))
    scrub.run(_Args(str(tmp_path), dry_run=True))
    out = capsys.readouterr().out
    assert "jwt" in out
    assert "email" in out


def test_scrub_on_an_empty_dir_says_so(tmp_path, capsys):
    from neuralosd._cmd import scrub
    rc = scrub.run(_Args(str(tmp_path)))
    assert rc == 0
    assert "nothing to scrub" in capsys.readouterr().out


def test_scrub_reports_zero_when_clean(tmp_path, capsys):
    from neuralosd._cmd import scrub
    _seed(str(tmp_path), secret=False)
    scrub.run(_Args(str(tmp_path), dry_run=True))
    assert "  (none" in capsys.readouterr().out
