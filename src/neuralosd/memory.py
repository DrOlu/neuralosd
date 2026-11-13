"""Memory — remember/recall backed by embeddings (no external vector DB).

``embed_fn`` is normally needle's vector function. When no model is
available — the CLI and the frozen base binary may not carry one — a
deterministic lexical embedding (hashed token buckets) keeps remember/recall
working. It is weaker than a real embedding and honest about it: the whole
store is one JSONL per instance, so an operator can inspect it.
"""
import hashlib
import json
import math
import os
import re
import threading
import time


def lexical_embed(text: str, dim: int = 64):
    """Deterministic fallback embedding: hashed token buckets, L2-normalised.

    Same text in any process (sha1, not Python's salted ``hash``), so a
    recalled memory survives a restart.
    """
    vec = [0.0] * dim
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
        digest = hashlib.sha1(tok.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class MemoryStore:
    def __init__(self, state_dir: str, embed_fn=None):
        self.dir = os.path.join(state_dir, "memory")
        os.makedirs(self.dir, exist_ok=True)
        self.embed_fn = embed_fn or lexical_embed   # text -> list[float]
        self._lock = threading.Lock()

    def _file(self, instance):
        return os.path.join(self.dir, f"{instance}.jsonl")

    def _vec(self, text):
        v = self.embed_fn(text)
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    @staticmethod
    def _cos(a, b):
        return sum(x * y for x, y in zip(a, b))

    def remember(self, instance: str, text: str, meta=None):
        rec = {"ts": time.time(), "text": text, "meta": meta or {},
               "vector": self._vec(text)}
        with self._lock:
            with open(self._file(instance), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"remembered": text, "instance": instance}

    def recall(self, instance: str, q: str, k: int = 5):
        qv = self._vec(q)
        scored = []
        try:
            with open(self._file(instance), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    scored.append((self._cos(qv, rec["vector"]), rec))
        except FileNotFoundError:
            pass
        scored.sort(key=lambda x: -x[0])
        return [{"score": round(s, 4), "text": r["text"], "meta": r["meta"],
                 "ts": r["ts"]} for s, r in scored[:k]]
