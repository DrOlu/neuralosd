"""The refusal contract at the HTTP boundary — where it used to evaporate.

`Router` has always attached `refusal_reason` (a bare token) to its envelope.
`service.py` then caught `NoResults` and sent `{"error": "no results produced"}`
— so over the wire, the one surface a client actually talks to, every refusal
collapsed into the same string: "my subject is out of scope", "you asked for a
filter I would have had to drop" and "that is an action, not a question" became
indistinguishable. These tests pin the wire format, in both directions.

They run the real server on a real socket (no test double): a 422 is a promise
to another process, and a promise tested only in-process is not tested.
"""
import decimal
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from neuralosd.probe import probe as probe_dec
from neuralosd.router import Router
from neuralosd.service import serve


def _probe(name, triggers, fn, args=None):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args=args or {}, name=name)(fn)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live(tmp_path):
    """A real HTTP instance with a Decimal-returning probe, on a real port."""
    def money(**_kw):
        return [{"amount": decimal.Decimal("19.99"), "n": 2}]

    router = Router(
        probes=[_probe("money_rows", ["total money"], money),
                _probe("how_many_rows", ["how many rows"], lambda **_k: [{"n": 3}]),
                _probe("empty_rows", ["empty rows"], lambda **_k: [])],
        cache_file=str(tmp_path / "c.json"),
        audit_file=str(tmp_path / "a.jsonl"),
    )
    port = _free_port()
    threading.Thread(target=serve, args=(router,),
                     kwargs={"port": port, "host": "127.0.0.1",
                             "auth_token": "tok"}, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    else:                                             # pragma: no cover
        pytest.fail("server did not start")
    yield base


AUTH = {"Authorization": "Bearer tok", "Content-Type": "application/json"}


def _post(url, body, headers=AUTH):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _ask(base, question, headers=AUTH):
    return _post(f"{base}/ask", json.dumps({"question": question}).encode(),
                 headers)


def test_refusal_carries_the_bare_token_and_the_envelope(live):
    code, body = _ask(live, "what is the weather in lagos")
    assert code == 422
    assert body["refusal_reason"] == "no_probe_matches"
    assert " " not in body["refusal_reason"], "reason must be ONE bare token"
    assert "scores" in body, "a refusal must explain itself with its candidates"
    assert body["error"], "the human-readable sentence is still there"


def test_different_refusals_are_distinguishable_over_the_wire(live):
    """"out of scope" and "an action, not a question" must not be the same
    bytes: the first means rephrase, the second means wrong product."""
    c1, b1 = _ask(live, "how many invoices exists in lagos")
    c2, b2 = _ask(live, "delete all the rows")
    assert c1 == c2 == 422
    assert b1["refusal_reason"] != b2["refusal_reason"], (b1, b2)
    assert b2["refusal_reason"] == "action_intent"


def test_probe_that_returns_nothing_refuses_with_no_results(live):
    code, body = _ask(live, "empty rows")
    assert code == 422
    assert body["refusal_reason"] == "no_results"


def test_answer_with_decimal_and_date_survives_the_socket(live):
    """The envelope must be JSON-encodable BEFORE it reaches a socket."""
    code, body = _ask(live, "total money")
    assert code == 200, body
    assert body["results"][0]["amount"] == "19.99"


@pytest.mark.parametrize("body,status,fragment", [
    (b"{not json", 400, "malformed JSON"),
    (b"[]", 400, "must be a JSON"),
    (b'"a string"', 400, "must be a JSON"),
    (b'{"question": 42}', 400, "question must be a string"),
    (b'{"question": ""}', 400, "empty question"),
])
def test_malformed_bodies_get_a_useful_400_not_a_python_traceback(
        live, body, status, fragment):
    code, parsed = _post(f"{live}/ask", body)
    assert code == status, parsed
    assert fragment in parsed["error"], parsed
    # the internal exception type must never leak to a client
    assert "object has no attribute" not in parsed["error"], parsed


def test_no_token_is_401_and_healthz_stays_open(live):
    code, _ = _post(f"{live}/ask", b'{"question":"how many rows"}',
                    {"Content-Type": "application/json"})
    assert code == 401
    code, _ = _post(f"{live}/ask", b'{"question":"how many rows"}',
                    {"Authorization": "Bearer wrong"})
    assert code == 401
    with urllib.request.urlopen(f"{live}/healthz", timeout=5) as r:
        assert r.status == 200


def test_role_header_without_acl_is_a_403_not_a_dead_socket(live):
    """F6 (P0): the boundary tested an undefined `_acl_roles` name, so a
    request carrying X-NeuralOS-Role raised NameError inside the handler and
    the client saw RemoteDisconnected instead of the documented 403."""
    req = urllib.request.Request(
        f"{live}/ask", data=json.dumps({"question": "how many rows"}).encode(),
        headers={**AUTH, "X-NeuralOS-Role": "analyst"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode())
            code = r.status
    except urllib.error.HTTPError as e:
        code, body = e.code, json.loads(e.read().decode() or "{}")
    assert code == 403, body
    assert "acl.json" in body["error"]
    assert "NameError" not in body["error"]


def test_sdk_exposes_the_bare_reason(live):
    from neuralosd_sdk import NeuralOSDClient, RefusalError

    client = NeuralOSDClient(base_url=live, token="tok")
    assert client.ask("how many rows").probe == "how_many_rows"
    with pytest.raises(RefusalError) as exc:
        client.ask("what is the weather in lagos")
    assert exc.value.reason == "no_probe_matches"
    assert exc.value.envelope["probe"] is None


def test_acl_reader_survives_a_torn_or_non_object_file(tmp_path):
    """F6 hardening: a damaged acl.json must read as "no roles" (a 403),
    never as an AttributeError inside the request handler."""
    from neuralosd.service import acl_roles

    (tmp_path / "acl.json").write_text('{"roles": {"analyst": ', encoding="utf-8")
    assert acl_roles(tmp_path) == {}
    (tmp_path / "acl.json").write_text('["analyst"]', encoding="utf-8")
    assert acl_roles(tmp_path) == {}
    (tmp_path / "acl.json").write_text(
        '{"roles": {"analyst": "inst-a"}}', encoding="utf-8")
    assert acl_roles(tmp_path) == {"analyst": "inst-a"}
