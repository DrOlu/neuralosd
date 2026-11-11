"""Tests-first: v2.8.1 — federation gossip (protocol wiring) + streaming
ingestion (`neuralosd watch`).

  GOSSIP  the fingerprint exchange becomes a PROTOCOL: `mesh gossip-push`
          publishes the local fingerprint blob to trusted peers via the
          gateway mailbox (skill `neuralosd.gossip`); blobs received from
          peers are collected from an inbox directory, VALIDATED (16-hex
          fps, bounded counts, bounded blob size — a misbehaving peer
          cannot poison the gaps report), deduped per peer, and merged
          into a persistent gossip state that the gaps report reads for
          `mesh_count`.
  WATCH   `neuralosd watch` — streaming ingestion: tails sources (syslog
          files, journald), normalizes events into a BOUNDED local buffer,
          evaluates threshold rules over sliding windows, and fires
          webhooks. `--once` runs a single poll cycle (testable, no
          daemon); the default loops.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

CSV = "region,units\nNorth,100\nSouth,200\n"


def _build(tmp_path, name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(CSV, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=None,
                                 join_on=None, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


# ── gossip: push ──────────────────────────────────────────────────────────

def _blob(counts=None):
    default = {"0f4790562c61ea16": 12, "15b6ab237d41235e": 3}
    return {"version": 1, "ts": time.time(), "fingerprints": [
        {"fp": fp, "count": n} for fp, n in (counts or default).items()]}


def test_gossip_push_sends_the_blob_to_every_peer():
    from neuralosd.loop import gossip_push

    class FakeTransport:
        def __init__(self):
            self.sent = []

        def mailbox(self, **kw):
            self.sent.append((kw.get("target"), kw.get("skill"),
                              kw.get("input_data")))
            return {"accepted": True}

    t = FakeTransport()
    report = gossip_push(t, _blob(), peers=["edge-a", "edge-b"],
                         origin="inst-1")
    assert len(t.sent) == 2
    assert {s[0] for s in t.sent} == {"edge-a", "edge-b"}
    assert all(s[1] == "neuralosd.gossip" for s in t.sent)
    blob = t.sent[0][2]
    assert blob["origin"] == "inst-1"
    assert blob["version"] == 1
    assert report["pushed"] == ["edge-a", "edge-b"]


def test_gossip_push_validates_and_caps_the_blob():
    from neuralosd.loop import gossip_push

    class T:
        def mailbox(self, **kw):
            return {"accepted": True}

    # oversized count value: a peer cannot claim a billion asks
    blob = _blob({"0f4790562c61ea16": 10**9})
    with pytest.raises(SystemExit) as e:
        gossip_push(T(), blob, peers=["edge-a"])
    assert "count" in str(e.value).lower()

    # malformed fingerprint: not 16 hex chars
    blob = _blob({"NOT-A-FP!": 5})
    with pytest.raises(SystemExit) as e:
        gossip_push(T(), blob, peers=["edge-a"])
    assert "fingerprint" in str(e.value).lower()


def test_gossip_collect_validates_the_inbox(tmp_path):
    """Blobs from peers are validated on arrival: malformed JSON skipped, a
    poisoned count capped, a bad fingerprint dropped — per peer, deduped."""
    from neuralosd.loop import gossip_collect
    inbox = tmp_path / "gossip_inbox"
    inbox.mkdir()
    (inbox / "edge-a.json").write_text(json.dumps(_blob(
        {"0f4790562c61ea16": 300})), encoding="utf-8")
    (inbox / "broken.json").write_text("{not json", encoding="utf-8")
    (inbox / "poison.json").write_text(json.dumps({
        "version": 1, "fingerprints": [
            {"fp": "zzzz", "count": 10**9}]}), encoding="utf-8")

    state = gossip_collect(str(inbox))
    entries = state["peers"]["edge-a"]["fingerprints"]
    hit = [f for f in entries if f["fp"] == "0f4790562c61ea16"]
    assert hit and hit[0]["count"] == 300          # the real value survives
    assert all(len(f["fp"]) == 16 for f in entries), \
        "malformed fingerprints must be dropped, not stored"
    assert "broken.json" not in json.dumps(state)


def test_gossip_state_dedupes_per_peer_across_polls(tmp_path):
    from neuralosd.loop import gossip_collect
    inbox = tmp_path / "gossip_inbox"
    inbox.mkdir()
    (inbox / "edge-a.json").write_text(json.dumps(_blob(
        {"0f4790562c61ea16": 12})), encoding="utf-8")
    s1 = gossip_collect(str(inbox))
    # the same file seen again in the next poll must NOT double the count
    s2 = gossip_collect(str(inbox))
    fp_hits = [f for f in s2["peers"]["edge-a"]["fingerprints"]
               if f["fp"] == s1["peers"]["edge-a"]["fingerprints"][0]["fp"]]
    assert len(fp_hits) == 1
    assert fp_hits[0]["count"] == 12


# ── watch: streaming ingestion ────────────────────────────────────────────

def test_ring_buffer_is_bounded_and_readable(tmp_path):
    from neuralosd.watch import RingBuffer
    rb = RingBuffer(str(tmp_path / "buf.jsonl"), cap=3)
    for i in range(5):
        rb.append({"i": i})
    events = rb.read()
    assert len(events) == 3
    assert [e["i"] for e in events] == [2, 3, 4]   # newest kept, oldest gone


def test_tail_returns_only_new_lines_and_survives_rotation(tmp_path):
    from neuralosd.watch import tail_new_lines
    f = tmp_path / "s.log"
    f.write_text("line1\nline2\n", encoding="utf-8")
    state = {}
    lines, state = tail_new_lines(str(f), state)
    assert lines == ["line1", "line2"]
    lines, state = tail_new_lines(str(f), state)
    assert lines == []                             # no new content
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("line3\n")
    lines, state = tail_new_lines(str(f), state)
    assert lines == ["line3"]
    # rotation: the file shrank below the stored offset -> re-read cleanly
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("fresh\n")
    lines, state = tail_new_lines(str(f), state)
    assert lines == ["fresh"]


def test_rules_fire_on_thresholds_and_filters(tmp_path):
    from neuralosd.watch import evaluate_rules
    now = time.time()
    rules = [{"name": "error burst", "match": {"level": "err"},
              "threshold": 2, "window_minutes": 10}]
    events = [{"ts": now - 60, "level": "err", "app": "sshd",
               "message": "a"},
              {"ts": now - 120, "level": "err", "app": "nginx",
               "message": "b"},
              {"ts": now - 9999, "level": "err", "app": "sshd",
               "message": "outside the window"},
              {"ts": now - 30, "level": "info", "app": "sshd",
               "message": "not an error"}]
    alerts = evaluate_rules(rules, events, now=now)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule"] == "error burst"
    assert a["matches"] == 2                       # window + level filter


def test_webhook_posts_and_fails_honestly(tmp_path):
    from neuralosd.watch import post_webhook
    posted = []

    def fake_poster(url, payload):
        posted.append((url, payload))
        return True

    assert post_webhook("http://hook", {"alert": 1},
                        poster=fake_poster) is True
    assert posted[0][0] == "http://hook"

    def failing(url, payload):
        raise OSError("network down")

    assert post_webhook("http://hook", {"alert": 1},
                        poster=failing) is False


def test_run_cycle_end_to_end(tmp_path):
    """One poll cycle: new syslog lines land in the buffer, the rule fires,
    the alert lands in the report. This is the streaming loop, whole."""
    from neuralosd.watch import run_cycle
    out = _build(tmp_path, name="w")
    log = tmp_path / "system.log"
    log.write_text(
        "Nov  2 03:11:22 web01 sshd[1]: fail a\n"
        "Nov  2 03:12:01 web01 sshd[2]: fail b\n", encoding="utf-8")
    rules = [{"name": "ssh failures", "match": {"app": "sshd"},
              "threshold": 2, "window_minutes": 60}]
    report = run_cycle(instance_dir=out,
                       sources=[{"kind": "syslog", "path": str(log)}],
                       rules=rules, once=True)
    assert report["ingested"] == 2
    assert report["alerts"], "2 sshd failures must trip the threshold-2 rule"
    buf = os.path.join(out, "watch_buffer.jsonl")
    assert os.path.isfile(buf)
    # second cycle: no new lines, no duplicate alerts
    report2 = run_cycle(instance_dir=out,
                        sources=[{"kind": "syslog", "path": str(log)}],
                        rules=rules, once=True)
    assert report2["ingested"] == 0
    assert report2["alerts"] == []


def test_per_rule_webhook_is_kept_and_delivered(tmp_path):
    """F14: evaluate_rules dropped the rule's `webhook` key, so run_cycle's
    `alert.get("webhook") or webhook` always fell back to the global one —
    a per-rule destination was configured and then never used."""
    from neuralosd.watch import evaluate_rules, run_cycle
    now = time.time()
    rules = [{"name": "delivered", "match": {"level": "err"},
              "threshold": 1, "webhook": "http://hooks/one"},
             {"name": "report-only", "match": {"level": "warn"},
              "threshold": 1}]
    events = [{"ts": now, "level": "err", "message": "a"},
              {"ts": now, "level": "warn", "message": "b"}]
    alerts = evaluate_rules(rules, events, now=now)
    by_rule = {a["rule"]: a.get("webhook") for a in alerts}
    assert by_rule == {"delivered": "http://hooks/one",
                       "report-only": None}

    # end to end: the per-rule URL is the one posted to
    out = _build(tmp_path, name="hooks")
    log = tmp_path / "hooks.log"
    log.write_text("Nov  2 03:11:22 web01 sshd[1]: fail a\n", encoding="utf-8")
    posted = []

    def poster(url, payload):
        posted.append(url)
        return True

    report = run_cycle(instance_dir=out,
                       sources=[{"kind": "syslog", "path": str(log)}],
                       rules=[{"name": "ssh", "match": {"app": "sshd"},
                               "threshold": 1, "window_minutes": 60,
                               "webhook": "http://hooks/rule"}],
                       once=True, poster=poster)
    assert posted == ["http://hooks/rule"]
    assert report["notifications"] == [{"rule": "ssh", "delivered": True}]
