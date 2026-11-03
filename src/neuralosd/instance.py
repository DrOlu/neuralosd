"""Instance — a named set of @probe capabilities with a model fallback."""
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional

from .probe import ProbeMeta
from .router import Router, NoResults, extract_args


class Instance:
    """A named instance = probes + optional model fallback + storage paths."""

    def __init__(self, name: str, probes: List[Callable],
                 model_fallback: Optional[Callable] = None,
                 state_dir: Optional[str] = None,
                 pii_mask: bool = True, ttl: int = 3600):
        self.name = name
        self.probes = list(probes)
        self.state_dir = state_dir or "."
        os.makedirs(self.state_dir, exist_ok=True)
        self.router = Router(
            probes=self.probes,
            model_fallback=model_fallback,
            cache_file=os.path.join(self.state_dir, ".ask_cache.json"),
            audit_file=os.path.join(self.state_dir, "ask_audit.jsonl"),
            pii_mask=pii_mask, ttl=ttl, name=name,
        )
        self._synthesize_birth_traps()

    def _synthesize_birth_traps(self):
        """Every registered probe gets its misuse trap in the bank at birth
        (v2.1.0). The trap embeds the probe's raw name — a token no trigger
        knows — so it refuses while routing is conformant and fails the moment
        the router starts answering out-of-vocabulary phrasings. Idempotent.
        Golden items are NOT auto-added: they need human verification.
        """
        from .banks import auto_traps_enabled, synthesize_trap_for
        if not auto_traps_enabled() or not self.probes:
            return
        path = os.path.join(self.state_dir, "traps.json")
        for p in self.probes:
            meta = getattr(p, "_probe", None)
            if meta is None:
                continue
            try:
                synthesize_trap_for(meta, path)
            except OSError as e:
                print(f"warning: could not auto-trap {meta.name}: {e}",
                      file=sys.stderr)

    @property
    def menu(self):
        return self.router.menu()

    def ask(self, question: str, **kw) -> Dict[str, Any]:
        return self.router.ask(question, **kw)

    def lint(self, top: int = 8, max_triggers: int = 40):
        return lint_menu(self.menu, top=top, max_triggers=max_triggers)

    def golden_run(self, golden: Dict, base_port: Optional[int] = None):
        """Run every golden question through the router, positives and TRAPS.

        A positive item declares the probe that must answer it:

            {"q": "...", "expect_probe": "open_incidents"}

        A negative item (a trap) declares that the router must REFUSE, and
        optionally why:

            {"q": "...", "expect_refusal": true,
             "refusal_reason": "no_probe_matches"}

        A confident answer to a trap is the worst failure this system has, so
        it is counted separately from a plain wrong routing and it fails the
        run. Returns a summary dict; exit code is the caller's decision.
        """
        correct = refused_ok = answered_trap = wrong = refused_wrong = 0
        value_checked = wrong_values = 0
        skipped = 0
        total = len(golden.get("items", []))
        for item in golden.get("items", []):
            q = item.get("q")
            wants_refusal = bool(item.get("expect_refusal"))
            want_reason = item.get("refusal_reason")
            try:
                # a bank replay is a VERIFICATION: it must exercise the real
                # code path, never the ask cache (a cached answer would let a
                # broken probe pass on the strength of its old, correct self)
                env = self.ask(q, use_cache=False)
                probe, refusal_reason = env.get("probe"), env.get("refusal_reason")
                answered = env.get("results") not in (None, [], {})
            except NoResults as e:
                env = getattr(e, "envelope", {}) or {}
                probe = env.get("probe")
                refusal_reason = env.get("refusal_reason")
                answered = False
            if wants_refusal:
                if not answered and (not want_reason
                                     or refusal_reason == want_reason):
                    ok, refused_ok = True, refused_ok + 1
                    print(f"PASS  REFUSED  {q!r} [{refusal_reason}]")
                else:
                    ok = False
                    answered_trap += answered
                    wrong += 1
                    print(f"FAIL  {'CONFIDENTLY ANSWERED' if answered else 'refused, wrong reason'}"
                          f"  {q!r} -> {probe} [{refusal_reason}]")
            else:
                expect = item.get("expect_probe")
                if expect is None:
                    # THE SCHEMA TRAP: an item with no expected probe used to
                    # auto-pass on ANY answer, so a trap encoded as
                    # expect_probe:null silently passed everything. A case that
                    # declares no expectation asserts nothing and is skipped -
                    # write it as expect_refusal instead.
                    skipped += 1
                    print(f"SKIP  {q!r} (no expect_probe / expect_refusal)")
                    continue
                ok = (expect == probe) and answered
                # VALUE-LEVEL ORACLE (v2.4.0): routing right is not enough —
                # a golden case with a typed oracle asserts the ANSWER too.
                ok_value = None
                if ok:
                    o = item.get("oracle") or {}
                    if o.get("value") is not None:
                        from .oracles import check_answer, extract_answer
                        shape = o.get("shape") or "count"
                        actual = extract_answer(env.get("results"),
                                                o.get("path"))
                        cands = actual if isinstance(actual, list) \
                            else [actual]
                        ok_value = any(check_answer(shape, o["value"], c)
                                       for c in cands)
                        value_checked += 1
                        if not ok_value:
                            ok = False
                            wrong_values += 1
                            shown = actual if not isinstance(actual, list) \
                                or len(actual) <= 3 else actual[:3]
                            print(f"FAIL  WRONG VALUE  {q!r} -> {probe}: "
                                  f"oracle {o['value']} ({shape}), got {shown}")
                if ok:
                    correct += 1
                    print(f"PASS  {q!r} -> {probe}")
                else:
                    wrong += 1
                    if ok_value is None:
                        refused_wrong += (not answered)
                    print(f"FAIL  {q!r} -> {probe} (expected {expect})")
        summary = {"total": total, "correct": correct, "refused_ok": refused_ok,
                   "answered_traps": answered_trap, "wrong": wrong,
                   "wrong_values": wrong_values,
                   "value_checked": value_checked,
                   "refused_when_should_answer": refused_wrong,
                   "skipped": skipped}
        graded = correct + refused_ok + wrong
        print(f"golden: {correct + refused_ok}/{graded} PASS "
              f"({wrong} wrong, {answered_trap} trap(s) confidently answered, "
              f"{skipped} skipped)")
        return summary

    def openapi(self, title=None):
        return self.router.openapi(title)

    def export(self, path: str):
        """Write the probe menu as a bare JSON array.

        The array is the canonical shape: the engine's --tools parser and the
        app's generator both expect one, and a wrapped {"name": ..., "menu":
        [...]} object reads as an EMPTY menu to anything that does not know
        the wrapper. The instance name already lives in the directory name,
        so the wrapper carries no information that is not recoverable.
        """
        json.dump(self.menu,
                  open(path, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)


import json  # noqa: E402
import os  # noqa: E402


def _tokens(text):
    return set(re.findall(r"[a-z0-9_]+", str(text).lower())) - set(
        "the a an of in on for to and or is are was were what which who how "
        "many show me give list all with their from by at it its do does did "
        "i we you this that those these there have has had more than one not "
        "use between during along per into over under about their".split())


def _score_probe_dict(probe, q_tokens):
    s = 0.0
    for trig in probe.get("triggers", []):
        s += 3.0 * len(q_tokens & _tokens(trig))
    s += 1.0 * len(q_tokens & _tokens(probe["name"].replace("_", " ")))
    s += 0.3 * len(q_tokens & _tokens(probe.get("description", "")))
    return s


def lint_menu(menu: List[Dict], top: int = 8, max_triggers: int = 40):
    """Trigger collision report: HARD (owner not in top-K) vs soft."""
    hard, soft, bloat = [], [], []
    for probe in menu:
        name = probe["name"]
        if len(probe.get("triggers", [])) > max_triggers:
            bloat.append((name, len(probe["triggers"])))
        for trig in probe.get("triggers", []):
            qt = _tokens(trig)
            scored = sorted(((_score_probe_dict(p, qt), p) for p in menu),
                            key=lambda x: -x[0])
            winners = [p["name"] for s, p in scored[:top] if s > 0]
            if winners and winners[0] != name:
                (soft if name in winners else hard).append((name, trig, winners[0]))
    return {"hard": hard, "soft": soft, "bloat": bloat}
