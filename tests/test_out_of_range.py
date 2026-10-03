"""Refuse, don't clamp.

`user 999` against an argument declared 1..10 used to be clamped to 10 and the
runtime returned a DIFFERENT user's record — silently. That is a substitution,
not a wrong answer, and it is worse: the caller has no way to know that the
data they received is about someone else.

Never coerce an argument. An out-of-range value is a refusal with the valid
range in the message.
"""
import subprocess
import sys
import os

import pytest

from neuralosd import probe as probe_dec
from neuralosd.router import ArgOutOfRange, NoResults, Router, _extract_one


# ── the extraction primitive ───────────────────────────────────────────────

SPEC = {"type": "integer", "min": 1, "max": 10}


def test_in_range_value_is_returned():
    assert _extract_one("user", SPEC, "show user 7") == 7
    assert _extract_one("user", SPEC, "show user 1") == 1
    assert _extract_one("user", SPEC, "show user 10") == 10


def test_above_range_raises_instead_of_clamping():
    with pytest.raises(ArgOutOfRange) as e:
        _extract_one("user", SPEC, "show user 999")
    assert e.value.value == 999
    assert (e.value.lo, e.value.hi) == (1, 10)


def test_below_range_raises_instead_of_clamping():
    with pytest.raises(ArgOutOfRange):
        _extract_one("user", {"type": "integer", "min": 5, "max": 10},
                     "show user 2")


def test_no_number_falls_back_to_the_default():
    assert _extract_one("user", {**SPEC, "default": 3}, "show the user") == 3


def test_the_error_names_the_argument_and_the_valid_range():
    with pytest.raises(ArgOutOfRange) as e:
        _extract_one("user", SPEC, "show user 999")
    msg = str(e.value)
    assert "user" in msg and "999" in msg and "1..10" in msg


def test_large_numbers_are_captured_not_truncated():
    """\\d{1,4} silently read '9999' as '999'. Capture the whole token."""
    with pytest.raises(ArgOutOfRange) as e:
        _extract_one("user", {"type": "integer", "min": 1, "max": 100},
                     "show user 9999")
    assert e.value.value == 9999


# ── through the router ─────────────────────────────────────────────────────

def _router(tmp_path):
    def show_user(user=1):
        return {"user": user, "name": "someone"}

    p = probe_dec(description="Show a user by id",
                  triggers=["show user", "user record"],
                  args={"user": {"type": "integer", "min": 1, "max": 10,
                                 "required": False, "default": 1}},
                  name="show_user")(show_user)
    return Router(probes=[p], cache_file=str(tmp_path / ".ask_cache.json"),
                  audit_file=str(tmp_path / "ask_audit.jsonl"))


def test_router_refuses_an_out_of_range_id(tmp_path):
    with pytest.raises(NoResults) as e:
        _router(tmp_path).ask("show user 999")
    env = e.value.envelope
    assert env["mode"] == "refused"
    assert env["results"] is None
    assert "999" in env["error"]


def test_the_refusal_travels_in_the_discarded_ledger(tmp_path):
    with pytest.raises(NoResults) as e:
        _router(tmp_path).ask("show user 999")
    oor = e.value.envelope["discarded"]["out_of_range"]
    assert oor["user"] == {"value": 999, "min": 1, "max": 10}


def test_router_still_answers_an_in_range_id(tmp_path):
    env = _router(tmp_path).ask("show user 4")
    assert env["results"][0]["user"] == 4
    assert "discarded" not in env


def test_the_substituted_record_is_never_returned(tmp_path):
    """The whole point: user 999 must NOT come back as user 10."""
    try:
        env = _router(tmp_path).ask("show user 999")
    except NoResults:
        return
    assert env["results"][0]["user"] != 10, "clamping came back"


def test_a_refusal_is_not_cached(tmp_path):
    r = _router(tmp_path)
    with pytest.raises(NoResults):
        r.ask("show user 999")
    assert not os.path.exists(r.cache_file)


# ── CLI contract ───────────────────────────────────────────────────────────

def _console_script():
    exe = os.path.join(os.path.dirname(sys.executable), "neuralosd")
    return exe if os.path.exists(exe) else None


@pytest.mark.skipif(_console_script() is None,
                    reason="console script not installed in this interpreter")
def test_ask_exits_2_on_a_refusal(tmp_path):
    """Printing a JSON envelope and exiting 0 made a refusal look like success."""
    r = _router(tmp_path)
    d = str(tmp_path / "inst")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "probes.py"), "w") as fh:
        fh.write("from neuralosd import probe\n\n")
        fh.write("@probe(description='Show a user', triggers=['show user'],\n")
        fh.write("       args={'user': {'type': 'integer', 'min': 1, 'max': 10,\n")
        fh.write("                        'required': False, 'default': 1}})\n")
        fh.write("def show_user(user=1):\n    return {'user': user}\n\n")
        fh.write("PROBES = [show_user]\n")
    proc = subprocess.run([_console_script(), "ask", "--instance-dir", d,
                           "show user 999"], capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "999" in proc.stdout
