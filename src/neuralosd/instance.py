"""Instance — a named set of @probe capabilities with a model fallback."""
import json
import os
import re
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

    @property
    def menu(self):
        return self.router.menu()

    def ask(self, question: str, **kw) -> Dict[str, Any]:
        return self.router.ask(question, **kw)

    def lint(self, top: int = 8, max_triggers: int = 40):
        return lint_menu(self.menu, top=top, max_triggers=max_triggers)

    def golden_run(self, golden: Dict, base_port: Optional[int] = None):
        """Run every golden question through the router."""
        passed = 0
        for item in golden.get("items", []):
            try:
                env = self.ask(item["q"])
                probe = env.get("probe")
                ok = (item.get("expect_probe") in (None, probe)) and \
                     env.get("results") not in (None, [], {})
            except NoResults:
                probe, ok = None, False
            passed += ok
            print(f"{'PASS' if ok else 'FAIL'}  {item['q']!r} -> {probe}")
        print(f"golden: {passed}/{len(golden.get('items', []))} PASS")
        return passed

    def openapi(self, title=None):
        return self.router.openapi(title)

    def export(self, path: str):
        json.dump({"name": self.name, "menu": self.menu()},
                  open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


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
