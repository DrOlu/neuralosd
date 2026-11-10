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


def _similar(a, b):
    """True when two trigger token-sets share >=80%% of the smaller set."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared / min(len(a), len(b)) >= 0.8


class TriggerLinter:
    def __init__(self, menu: List[Dict], top: int = 8):
        self.menu = menu
        self.top = top

    def lint(self):
        hard, soft, bloat, spam = [], [], [], []
        for probe in self.menu:
            name = probe["name"]
            n_trig = len(probe.get("triggers", []))
            if n_trig > 40:
                bloat.append((name, n_trig))
            # TRIGGER-SPAM: >=3 trigger variants sharing >=80%% of their
            # tokens. This is the shape that once gamed the routing score's
            # breadth term (22 variants of "<qualifier> detections"
            # outranked the probe triggered by the question verbatim) — a
            # probe should earn coverage by covering the QUESTION, not by
            # repeating one noun across many near-duplicate synonyms.
            tt = [_tokens(trig) for trig in probe.get("triggers", [])]
            parent = list(range(len(tt)))

            def _find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i in range(len(tt)):
                for j in range(i + 1, len(tt)):
                    if _similar(tt[i], tt[j]):
                        parent[_find(i)] = _find(j)
            clusters: Dict[int, List[int]] = {}
            for i in range(len(tt)):
                clusters.setdefault(_find(i), []).append(i)
            for members in clusters.values():
                if len(members) >= 3:
                    spam.append((name, len(members),
                                 probe.get("triggers", [])[members[0]]))
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
        return {"hard": hard, "soft": soft, "bloat": bloat,
                "trigger_spam": spam}
