"""Memory — remember/recall backed by embeddings (no external vector DB).

``embed_fn`` is normally needle's vector function. When no model is
available — the CLI and the frozen base binary may not carry one — a
deterministic lexical embedding (hashed token buckets) keeps remember/recall
working. It is weaker than a real embedding and honest about it: the whole
store is one JSONL per instance, so an operator can inspect it.

A session id comes from an agent, i.e. from outside. It is sanitised into a
filename AND given a short content hash, so ``../../etc/passwd`` cannot walk
out of the memory directory and two ids that sanitise to the same string do
not share a file. Each file is bounded to ``max_records`` rows.
"""
import hashlib
import json
import math
import os
import re
import threading
import time


def safe_name(value, limit: int = 60) -> str:
    """A filesystem-safe, collision-resistant name for a caller-supplied id."""
    raw = str(value)
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:limit] or "item"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


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
    def __init__(self, state_dir: str, embed_fn=None, max_records: int = 5000):
        self.dir = os.path.join(state_dir, "memory")
        os.makedirs(self.dir, exist_ok=True)
        self.embed_fn = embed_fn or lexical_embed   # text -> list[float]
        self.max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._counts = {}

    def path(self, instance) -> str:
        return os.path.join(self.dir, f"{safe_name(instance)}.jsonl")

    def _file(self, instance):
        return self.path(instance)

    @staticmethod
    def _read_rows(path):
        rows = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    @staticmethod
    def _write_rows(path, rows):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _count(self, path):
        if path not in self._counts and len(self._counts) > 5000:
            self._counts.clear()          # rebuilt lazily from the files
        if path not in self._counts:
            try:
                with open(path, encoding="utf-8") as fh:
                    self._counts[path] = sum(1 for line in fh if line.strip())
            except OSError:
                self._counts[path] = 0
        return self._counts[path]

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
        path = self.path(instance)
        with self._lock:
            count = self._count(path)
            if count < self.max_records:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._counts[path] = count + 1
            else:
                rows = self._read_rows(path)[-(self.max_records - 1):]
                rows.append(rec)
                self._write_rows(path, rows)
                self._counts[path] = len(rows)
        return {"remembered": text, "instance": instance}

    def recall(self, instance: str, q: str, k: int = 5):
        qv = self._vec(q)
        scored = []
        for rec in self._read_rows(self.path(instance))[-self.max_records:]:
            if not isinstance(rec, dict) or "vector" not in rec:
                continue
            scored.append((self._cos(qv, rec["vector"]), rec))
        scored.sort(key=lambda x: -x[0])
        return [{"score": round(s, 4), "text": r.get("text"),
                 "meta": r.get("meta"), "ts": r.get("ts")}
                for s, r in scored[:k]]
