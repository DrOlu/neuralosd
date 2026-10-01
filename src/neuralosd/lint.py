"""Trigger collision linter — library + CLI."""
import re
from typing import Dict, List


def _tokens(text):
    STOP = set("the a an of in on for to and or is are was were what which who "
               "how many show me give list all with their from by at it its do "
               "does did i we you this that those these there have has had more "
               "than one not use between during along per into over under about "
               "their".split())
    return set(re.findall(r"[a-z0-9_]+", str(text).lower())) - STOP


def _score(probe, q_tokens):
    s = 0.0
    for trig in probe.get("triggers", []):
        s += 3.0 * len(q_tokens & _tokens(trig))
    s += 1.0 * len(q_tokens & _tokens(probe["name"].replace("_", " ")))
    s += 0.3 * len(q_tokens & _tokens(probe.get("description", "")))
    return s


class TriggerLinter:
    def __init__(self, menu: List[Dict], top: int = 8):
        self.menu = menu
        self.top = top

    def lint(self):
        hard, soft, bloat = [], [], []
        for probe in self.menu:
            name = probe["name"]
            n_trig = len(probe.get("triggers", []))
            if n_trig > 40:
                bloat.append((name, n_trig))
            for trig in probe.get("triggers", []):
                qt = _tokens(trig)
                scored = sorted(((_score(p, qt), p) for p in self.menu),
                                key=lambda x: -x[0])
                winners = [p["name"] for s, p in scored[:self.top] if s > 0]
                if winners and winners[0] != name:
                    if name in winners:
                        soft.append((name, trig, winners[0]))
                    else:
                        hard.append((name, trig, winners[0]))
        return {"hard": hard, "soft": soft, "bloat": bloat}
