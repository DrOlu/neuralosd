"""Meta-selector — route a question to the right instance first.

Uses needle embeddings (or fallback lexical scoring) to pick which instance
should handle a question. This is the "which box do I ask" layer.
"""
import re
from typing import Callable, Dict, List


class MetaSelector:
    def __init__(self, instances: Dict[str, Dict], embed_fn: Callable = None):
        """
        instances: {name: {"description": str, "examples": [str, ...]}}
        embed_fn:  text → list[float] (needle embeddings or fallback)
        """
        self.instances = instances
        self.embed_fn = embed_fn
        self._vectors = {}   # instance -> [example_vectors]

    def build(self):
        """Pre-compute example embeddings."""
        if not self.embed_fn:
            return
        for name, info in self.instances.items():
            texts = [info.get("description", "")] + info.get("examples", [])
            self._vectors[name] = [self.embed_fn(t) for t in texts]

    def route(self, question: str) -> str:
        """Return the best instance name for the question."""
        if self.embed_fn and self._vectors:
            return self._route_embed(question)
        return self._route_lexical(question)

    def _route_embed(self, question):
        qv = self.embed_fn(question)
        best, best_score = None, -1
        for name, vectors in self._vectors.items():
            for ev in vectors:
                score = sum(a * b for a, b in zip(qv, ev))
                if score > best_score:
                    best, best_score = name, score
        return best

    def _route_lexical(self, question):
        """Fallback: stopword-stripped token-overlap against instance
        descriptions + names."""
        STOP = {"the", "a", "an", "of", "in", "for", "to", "and", "or", "is",
                "what", "how", "show", "me", "my", "does", "do"}
        q_tokens = set(re.findall(r"[a-z0-9]+", question.lower())) - STOP
        best, best_score = None, -1
        for name, info in self.instances.items():
            text = (info.get("description", "") + " " + name).lower()
            tokens = set(re.findall(r"[a-z0-9]+", text)) - STOP
            score = len(q_tokens & tokens)
            if score > best_score:
                best, best_score = name, score
        return best
