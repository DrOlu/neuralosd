"""The router — formalized ask pipeline (library form of the verified ask.py).

normalize -> retrieve top-K -> deterministic fast path -> calibration gate ->
model fallback (results-gated) -> guardrails -> emit -> audit -> cache.

Failure contract: raises NoResults when nothing was produced — callers map
that to exit 2 / HTTP 422. function_calls is never read (needle 3.0.3 leaves
it empty even on success); results are the only gate.
"""
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .probe import ProbeMeta

# Only pure function words are stopped. Intent-bearing words (how, many,
# much, show, list, count, top, best) are PRESERVED so that phrase
# specificity survives — "how many rows" must not collapse to "rows".
STOP = set("the a an of in on for to and or is are was were what which who "
           "me give all with their from by at it its do does did "
           "i we you this that those these there have has had more than one not "
           "use between during along per into over under about".split())

PII_HINTS = ("email", "phone", "ssn", "iban", "tax_id", "passport")


class NoResults(Exception):
    def __init__(self, envelope):
        self.envelope = envelope
        super().__init__(envelope.get("error", "no results produced"))


def tokens(text):
    return set(re.findall(r"[a-z0-9_]+", str(text).lower())) - STOP


def score_probe(meta: ProbeMeta, q_tokens):
    # The BEST single trigger match dominates, so a probe with one exact
    # phrase beats a probe with many partially-overlapping triggers.
    # (e.g. "how many rows" -> row_count, not list_rows.)
    overlaps = [len(q_tokens & tokens(trig)) for trig in meta.triggers]
    best = max(overlaps, default=0)
    s = 4.0 * best
    s += 1.0 * sum(overlaps)                 # small bonus for breadth
    s += 1.0 * len(q_tokens & tokens(meta.name.replace("_", " ")))
    s += 0.3 * len(q_tokens & tokens(meta.description))
    return s


def mask_pii(x):
    if isinstance(x, dict):
        return {k: ("***masked***" if any(h in k.lower() for h in PII_HINTS)
                    and isinstance(v, str) else mask_pii(v))
                for k, v in x.items()}
    if isinstance(x, list):
        return [mask_pii(v) for v in x]
    return x


def normalize_possessive(question, enum_values):
    """Ireland's top customers -> top customers in Ireland."""
    for v in enum_values:
        pat = re.compile(r"\b" + re.escape(v) + r"'s\b", re.I)
        if pat.search(question):
            stripped = pat.sub("", question).strip(" -,")
            return f"{stripped} in {v}".strip(), v
    return question, None


def _extract_one(argname, spec, question):
    t = spec.get("type", "string")
    if t == "enum":
        vals = spec.get("values") or []
        hit = next((v for v in vals
                    if re.search(r"\b" + re.escape(v) + r"\b", question, re.I)), None)
        return hit
    if t == "pattern":
        m = re.search(spec["pattern"], question)
        if not m:
            return None
        return (m.group(1) if m.groups() else m.group(0)).strip()
    if t == "integer":
        m = re.search(r"\b(\d{1,4})\b", question)
        if not m:
            return spec.get("default")
        v = int(m.group(1))
        return max(spec.get("min", 1), min(v, spec.get("max", 100)))
    if t == "string" and spec.get("required"):
        m = re.search(r"\b(?:by|for|from|of|does)\s+([A-Z][\w'&./ -]{1,60})", question)
        if not m:
            return None
        return m.group(1).strip()
    return spec.get("default")


def extract_args(meta: ProbeMeta, question):
    """Extract every REQUIRED arg from the question; None when uncertain.
    Optional args use their declared defaults."""
    kwargs = {}
    for argname, spec in meta.args.items():
        required = spec.get("required", True)
        if "values" in spec or spec.get("type") == "enum":
            val = _extract_one(argname, spec, question)
            if val is None:
                if required:
                    return None
                val = spec.get("default") or (spec.get("values") or ["all"])[0]
            kwargs[argname] = val
        elif spec.get("type") == "pattern" and required:
            val = _extract_one(argname, spec, question)
            if val is None:
                return None
            kwargs[argname] = val
        elif spec.get("type") == "integer":
            val = _extract_one(argname, spec, question)
            if val is None and spec.get("required", True):
                return None
            if val is not None:
                kwargs[argname] = val
        elif spec.get("required", True):
            return None      # unknown required extraction -> model fallback
    return kwargs


class Router:
    def __init__(self, probes: List[Callable], model_fallback: Optional[Callable] = None,
                 cache_file: str = ".ask_cache.json",
                 audit_file: str = "ask_audit.jsonl",
                 pii_mask: bool = True, ttl: int = 3600,
                 name: str = "instance"):
        self.probes = list(probes)
        self.metas = [getattr(p, "_probe") for p in self.probes]
        self.by_name = {m.name: p for p, m in zip(self.probes, self.metas)}
        self.model_fallback = model_fallback
        self.cache_file = cache_file
        self.audit_file = audit_file
        self.pii_mask = pii_mask
        self.ttl = ttl
        self.name = name
        self.menu_version = self._menu_version()

    def _menu_version(self):
        blob = json.dumps([{"n": m.name, "t": m.triggers, "d": m.description,
                            "a": m.args} for m in self.metas],
                          sort_keys=True, default=str).encode()
        return hashlib.sha1(blob).hexdigest()[:12]

    def menu(self):
        out = []
        for m in self.metas:
            props, req = {}, []
            for an, spec in m.args.items():
                props[an] = {k: v for k, v in spec.items()
                             if k in ("type", "enum", "pattern", "min", "max",
                                      "description")}
                if spec.get("required", True):
                    req.append(an)
            entry = {"name": m.name, "description": m.description,
                     "parameters": {"type": "object", "properties": props}}
            if req:
                entry["parameters"]["required"] = req
            entry["triggers"] = m.triggers
            out.append(entry)
        return out

    def lint(self, top: int = 8, max_triggers: int = 40):
        from .lint import TriggerLinter
        return TriggerLinter(self.menu(), top=top).lint()

    def openapi(self, title=None):
        title = title or f"{self.name} — neuralOS probes"
        paths = {}
        for m in self.metas:
            props = {k: {kk: vv for kk, vv in spec.items()
                         if kk in ("type", "enum", "pattern", "min", "max")}
                     for k, spec in m.args.items()}
            paths[f"/probes/{m.name}"] = {
                "post": {"operationId": m.name, "summary": m.description,
                         "requestBody": {"content": {"application/json": {
                             "schema": {"type": "object", "properties": props}}}},
                         "responses": {"200": {"description": "envelope"}}}}
        return {"openapi": "3.1.0", "info": {"title": title, "version": "1.0.0"},
                "paths": paths}

    # -- internal helpers --------------------------------------------------
    def _retrieve(self, q, k):
        qt = tokens(q)
        scored = sorted(((score_probe(m, qt), m) for m in self.metas),
                        key=lambda x: -x[0])
        return [(m, s) for s, m in scored if s > 0][:k]

    def _normalize(self, q):
        vals = []
        for m in self.metas:
            for spec in m.args.values():
                vals.extend(spec.get("values") or [])
        return normalize_possessive(q, [v for v in dict.fromkeys(vals)
                                         if len(v) >= 3])

    def _key(self, normalized):
        return hashlib.sha1((normalized.lower() + "|" +
                             self.menu_version).encode()).hexdigest()

    def _cache_load(self, key):
        try:
            c = json.load(open(self.cache_file, encoding="utf-8"))
            e = c.get(key)
            if e and e.get("menu_version") == self.menu_version \
                    and time.time() - e["ts"] < self.ttl:
                return e
        except Exception:
            pass
        return None

    def _cache_store(self, key, env):
        try:
            c = {}
            if os.path.exists(self.cache_file):
                c = json.load(open(self.cache_file, encoding="utf-8"))
            c[key] = {"ts": time.time(), "menu_version": self.menu_version,
                      "payload": env}
            json.dump(c, open(self.cache_file, "w", encoding="utf-8"))
        except Exception:
            pass

    def _audit(self, rec):
        try:
            with open(self.audit_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    # -- public ask --------------------------------------------------------
    def ask(self, question: str, k: int = 8, full: bool = False,
            use_cache: bool = True) -> Dict[str, Any]:
        t0 = time.time()
        ask_id = uuid.uuid4().hex[:12]
        normalized, moved = self._normalize(question.strip())
        if moved:
            print("normalized:", normalized, flush=True)
        key = self._key(normalized)

        if use_cache:
            hit = self._cache_load(key)
            if hit:
                env = dict(hit["payload"])
                env.update({"ask_id": ask_id, "cached": True,
                            "latency_ms": int((time.time() - t0) * 1000)})
                self._audit(env)
                return env

        retrieved = self._retrieve(normalized, k)

        results, used_probe, mode, conf = None, None, "retrieval", None

        if retrieved:
            used_probe = retrieved[0][0].name

            # deterministic fast path: walk top-K in rank order and execute the
            # FIRST probe whose required args are extractable with certainty.
            # (verified live: rank-1 alone misroutes bare phrases like
            # "top customers" to a country-caged probe.)
            for _meta, _score in retrieved:
                kwargs = extract_args(_meta, normalized)
                if kwargs is None:
                    continue
                # Strong-match rule: a probe with NO caged args is ambiguous
                # (any question could hit it) — only auto-execute it if it is a
                # strong lexical match (>= 1/2 of rank-1's score). Caged probes
                # with all args extracted are always confident.
                has_caged = any(s.get("required", True) or s.get("pattern")
                                for s in _meta.args.values())
                if not has_caged and _score < 0.5 * retrieved[0][1]:
                    continue
                fn = self.by_name[_meta.name]
                mode = "deterministic"
                used_probe = _meta.name
                try:
                    r = fn(**kwargs)
                    if isinstance(r, dict):
                        r = {**r, "_tool": _meta.name}
                        results = [r]
                    elif isinstance(r, list):
                        results = r
                except Exception as exc:
                    results = [{"error":
                                f"{type(exc).__name__}: {str(exc)[:200]}",
                                "_tool": _meta.name}]
                    mode = "deterministic-error"
                break

        if results is None:
            # Deterministic routing produced nothing. Consult the model when one
            # is configured — with the retrieved subset if lexical retrieval
            # found something, otherwise with the whole menu (a paraphrase with
            # no shared tokens is exactly what the model is for).
            if self.model_fallback is None:
                reason = ("no probe matched this question" if not retrieved
                          else "no results produced (no model fallback "
                               "configured)")
                env = {"ask_id": ask_id, "question": question,
                       "normalized": normalized, "probe": None,
                       "menu_version": self.menu_version,
                       "error": reason, "results": None}
                self._audit(env)
                raise NoResults(env)
            conf = None
            candidates = ([m for m, _s in retrieved] if retrieved
                          else list(self.metas))
            mode = "model"
            results = self.model_fallback(normalized, candidates)
            # The model may pick a different probe than rank-1, so trust the
            # tool tag the bridge attaches rather than the top-ranked name.
            used_probe = (_tool_of(results)
                          or (retrieved[0][0].name if retrieved else None))

        if self.pii_mask:
            results = mask_pii(results)
        empty = results in (None, [], {}) or (isinstance(results, list)
                                              and len(results) == 0)
        env = {"ask_id": ask_id, "ts": time.time(), "question": question,
               "normalized": normalized, "probe": used_probe,
               "menu_version": self.menu_version, "mode": mode,
               "confidence": conf,
               "latency_ms": int((time.time() - t0) * 1000),
               "results": None if empty else results}
        if empty:
            env["error"] = "no results produced for this question"
            self._audit(env)
            raise NoResults(env)

        self._audit(env)
        # Never cache a failure. Errors here are usually environmental and
        # fixable (a missing library, a DB that was down, a timeout); caching
        # one for the whole TTL means the user installs the missing piece,
        # retries, and keeps getting the stale error for an hour.
        if not _is_error_envelope(env):
            self._cache_store(key, env)
        return env


def _tool_of(results):
    """The probe name a result list reports, if any."""
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0].get("_tool")
    return None


def _is_error_envelope(env) -> bool:
    """True when this envelope represents a failure rather than an answer."""
    if env.get("error"):
        return True
    if str(env.get("mode") or "").endswith("-error"):
        return True
    results = env.get("results")
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and "error" in r:
                return True
    return False


def extract_args(meta: ProbeMeta, question: str) -> Optional[Dict[str, Any]]:
    """Extract ALL args of the probe (required + optional-with-defaults) from
    the question. Returns None when a REQUIRED arg cannot be extracted."""
    kwargs = {}
    for argname, spec in meta.args.items():
        required = spec.get("required", True)
        t = spec.get("type", "string")
        val = None
        if "values" in spec or t == "enum":
            vals = spec.get("values") or []
            val = next((v for v in vals
                        if re.search(r"\b" + re.escape(v) + r"\b", question, re.I)),
                       None)
            if val is None:
                if required:
                    return None
                val = spec.get("default") or (vals[0] if vals else "all")
            kwargs[argname] = val
        elif t == "pattern":
            m = re.search(spec.get("pattern", ""), question)
            if not m:
                if required:
                    return None
                continue
            val = (m.group(1) if m.groups() else m.group(0)).strip()
            if not val:
                if required:
                    return None
                continue
            kwargs[argname] = val
        elif t == "integer":
            m = re.search(r"\b(\d{1,4})\b", question)
            v = int(m.group(1)) if m else None
            if v is None:
                if required:
                    return None
                continue
            kwargs[argname] = max(spec.get("min", 1), min(v, spec.get("max", 100)))
        else:
            m = re.search(r"\b(?:by|for|from|of|does)\s+([A-Z][\w'&./ -]{1,60})",
                          question)
            if not m:
                if required:
                    return None
                continue
            val = m.group(1).strip()
            for stop in (" have", " ?", "?"):
                val = val.replace(stop, "").strip()
            kwargs[argname] = val
    return kwargs
