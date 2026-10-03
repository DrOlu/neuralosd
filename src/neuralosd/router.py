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

# Key-NAME hints: a field literally called `email` is masked whatever it holds.
PII_HINTS = ("email", "e-mail", "phone", "mobile", "ssn", "iban", "tax_id",
             "passport", "password", "passwd", "pwd", "pin", "token", "secret",
             "api_key", "apikey", "api-key", "authorization", "auth_token",
             "credential", "private_key", "access_key", "session_id",
             "cookie", "cvv", "card_number", "account_number")

# Key-name matching alone is trivially bypassed: a password stored under a key
# called "notes" is still a password. These match the VALUE's shape instead.
SECRET_PATTERNS = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[aboprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    # scheme://user:password@host — a connection string leaked into an error
    ("conn_string", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.]*://[^\s:/@]+:[^\s:@/]+@")),
)

# Off by default: long hex is ALSO what every hash column in your data looks
# like, so masking it unconditionally would destroy legitimate answers.
# Opt in with NEURALOSD_MASK_AGGRESSIVE=1 when auditing a suspected leak.
AGGRESSIVE_PATTERNS = (
    ("long_hex", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
)

# Value shapes that are PII rather than credentials. Applied at STORAGE time
# only. An email in a RESPONSE may be exactly the answer that was asked for
# ("list customers by email"), so masking it there would be over-redaction; but
# it has no business sitting in an append-only audit log forever.
PII_VALUE_PATTERNS = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(
        r"(?<!\d)(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)[ -]?|\d{3}[ -])\d{3}[ -]\d{4}(?!\d)")),
)

MASKED = "***masked***"

# Arg names whose enum values are MEASURES, not filters. A measure named in a
# question ("sales") is normal. A categorical FILTER named in a question that
# nothing consumed ("Western Europe") means the answer silently ignored it.
MEASURE_ARG_NAMES = {"measure", "metric", "column", "field", "value", "agg"}


def _aggressive():
    return os.environ.get("NEURALOSD_MASK_AGGRESSIVE", "").lower() in ("1", "true", "yes")


class NoResults(Exception):
    def __init__(self, envelope):
        self.envelope = envelope
        super().__init__(envelope.get("error", "no results produced"))


class ArgOutOfRange(Exception):
    """An argument's value fell outside its declared range.

    Never clamp. Silently coercing `user 999` to the maximum returned a
    DIFFERENT user's record than the one that was asked for — a substitution,
    which is worse than a refusal.
    """

    def __init__(self, argname, value, lo, hi):
        self.argname, self.value, self.lo, self.hi = argname, value, lo, hi
        super().__init__(
            f"{argname}={value} is outside the supported range ({lo}..{hi}) — "
            f"widen the argument's min/max to accept it")


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


def _mask_value(v):
    """Mask secrets by SHAPE, whatever the key is called."""
    if not isinstance(v, str):
        return v
    for _name, pat in SECRET_PATTERNS:
        v = pat.sub(MASKED, v)
    if _aggressive():
        for _name, pat in AGGRESSIVE_PATTERNS:
            v = pat.sub(MASKED, v)
    return v


def _mask_storage_value(v):
    """Credentials by shape, plus PII by shape — for records going to disk."""
    v = _mask_value(v)
    if not isinstance(v, str):
        return v
    for _name, pat in PII_VALUE_PATTERNS:
        v = pat.sub(MASKED, v)
    return v


def _storage_mask(x):
    """Key hints AND value shapes (credentials + PII), at every depth."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if isinstance(v, str) and any(h in k.lower() for h in PII_HINTS):
                out[k] = MASKED
            else:
                out[k] = _storage_mask(v)
        return out
    if isinstance(x, list):
        return [_storage_mask(v) for v in x]
    if isinstance(x, str):
        return _mask_storage_value(x)
    return x


def mask_pii(x, deep=True):
    """Mask by key name AND (when deep) by value shape.

    deep=False restores the old key-name-only behaviour, which cannot see a
    secret stored under an innocuous key.
    """
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if isinstance(v, str) and any(h in k.lower() for h in PII_HINTS):
                out[k] = MASKED
            else:
                out[k] = mask_pii(v, deep)
        return out
    if isinstance(x, list):
        return [mask_pii(v, deep) for v in x]
    if deep and isinstance(x, str):
        return _mask_value(x)
    return x


def normalize_possessive(question, enum_values):
    """Ireland's top customers -> top customers in Ireland."""
    for v in enum_values:
        pat = re.compile(r"\b" + re.escape(v) + r"'s\b", re.I)
        if pat.search(question):
            stripped = pat.sub("", question).strip(" -,")
            return f"{stripped} in {v}".strip(), v
    return question, None


def coverage(meta: ProbeMeta, q_tokens) -> float:
    """How much of the probe's best trigger the question actually contains.

    A 2-token question matching 2 of an 8-token trigger scores the same as one
    matching 2 of 2; coverage tells them apart. Used only by probes that opt in
    via ``min_coverage``, so nothing that exists today changes.
    """
    best = 0.0
    for tr in meta.triggers or []:
        tt = tokens(tr)
        if tt:
            best = max(best, len(q_tokens & tt) / len(tt))
    return best


def _passes_coverage(meta: ProbeMeta, q_tokens) -> bool:
    floor = getattr(meta, "min_coverage", None)
    if floor is None:
        return True
    return coverage(meta, q_tokens) >= float(floor)


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
        m = re.search(r"\b(\d{1,9})\b", question)
        if not m:
            return spec.get("default")
        v = int(m.group(1))
        lo, hi = spec.get("min", 1), spec.get("max", 100)
        if v < lo or v > hi:
            # Never clamp — see ArgOutOfRange.
            raise ArgOutOfRange(argname, v, lo, hi)
        return v
    if t == "string" and spec.get("required"):
        m = re.search(r"\b(?:by|for|from|of|does)\s+([A-Z][\w'&./ -]{1,60})", question)
        if not m:
            return None
        return m.group(1).strip()
    return spec.get("default")


# NOTE: there is exactly ONE extract_args, at the bottom of this module.
#
# It used to be defined twice. This earlier copy was shadowed by the later one,
# so ANY fix applied here silently did nothing — including the clamp fix that
# was written here first, which is how the real one kept returning user 10 for
# `user 999`. Two definitions of one function is a silent no-op generator.


class Router:
    def __init__(self, probes: List[Callable], model_fallback: Optional[Callable] = None,
                 cache_file: str = ".ask_cache.json",
                 audit_file: str = "ask_audit.jsonl",
                 pii_mask: bool = True, ttl: int = 3600,
                 name: str = "instance", strict_discards: bool = False):
        self.probes = list(probes)
        self.metas = [getattr(p, "_probe") for p in self.probes]
        self.by_name = {m.name: p for p, m in zip(self.probes, self.metas)}
        self.model_fallback = model_fallback
        self.cache_file = cache_file
        self.audit_file = audit_file
        self.pii_mask = pii_mask
        self.ttl = ttl
        self.name = name
        # When True, an answer that would have dropped a filter the user named
        # is refused instead of returned. Off by default so existing callers
        # keep working; the ledger is still always present in the envelope.
        self.strict_discards = strict_discards
        self.menu_version = self._menu_version()
        self._filter_vocab = self._build_filter_vocab()

    def _build_filter_vocab(self):
        """Enum values across the menu that act as FILTERS, not measures."""
        filters, measures = {}, set()
        for m in self.metas:
            for an, spec in m.args.items():
                for v in spec.get("values") or []:
                    if an.lower() in MEASURE_ARG_NAMES:
                        measures.add(str(v).lower())
                    else:
                        filters.setdefault(str(v).lower(), str(v))
        for v in measures:
            filters.pop(v, None)   # a measure is never a "dropped filter"
        return filters

    def _named_filters(self, question):
        """Known FILTER values the user named in the question."""
        return [orig for low, orig in self._filter_vocab.items()
                if re.search(r"\b" + re.escape(low) + r"\b", question, re.I)]

    def _unconsumed_for(self, meta, kwargs, question):
        """Named filters that this probe had no way to consume.

        Used in two places on purpose — to penalise such a probe at ROUTING
        time, and to report it in the ledger at ANSWER time — so the two can
        never drift apart the way two copies of one function did.
        """
        consumed = {str(v).lower() for v in (kwargs or {}).values()}
        if meta is not None:
            # An enum value this probe COULD have taken counts as consumed.
            for spec in meta.args.values():
                for v in spec.get("values") or []:
                    if re.search(r"\b" + re.escape(str(v)) + r"\b",
                                 question, re.I):
                        consumed.add(str(v).lower())
            # A value the probe is NAMED after, or TRIGGERED by, is consumed by
            # identity: `distinct_region` answers "distinct region" without
            # taking an argument at all, and reporting that as a dropped filter
            # was a false positive. It does NOT rescue total_revenue, whose name
            # and triggers never mention a region.
            identity = tokens(meta.name.replace("_", " "))
            for tr in meta.triggers:
                identity |= tokens(tr)
            for low in self._filter_vocab:
                if low in identity:
                    consumed.add(low)
        return [v for v in self._named_filters(question)
                if v.lower() not in consumed]

    def _discarded(self, probe_name, kwargs, question, results):
        """What this answer threw away.

        Two vectors, one ledger:
          terms — a known FILTER value named in the question that no extracted
                  arg consumed. "who spends the most on jazz" used to answer
                  the UNFILTERED top customers, with 'jazz' silently dropped.
          rows  — rows the probe itself excluded, reported by the probe. 1418
                  rows with no date are still counted by the grand total but
                  are absent from every group in the breakdown.

        A non-empty ledger means the answer is not an answer to the question
        that was asked.
        """
        out = {}
        meta = next((m for m in self.metas if m.name == probe_name), None)
        terms = sorted(set(self._unconsumed_for(meta, kwargs, question)))
        if terms:
            out["terms"] = terms
        rows = {}
        if isinstance(results, list):
            for r in results:
                if isinstance(r, dict) and isinstance(r.get("skipped"), dict):
                    for k, v in r["skipped"].items():
                        if isinstance(v, (int, float)) and v:
                            rows[k] = rows.get(k, 0) + v
        if rows:
            out["rows"] = rows
        return out

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
            env = self._for_storage(env)
            c = {}
            if os.path.exists(self.cache_file):
                c = json.load(open(self.cache_file, encoding="utf-8"))
            c[key] = {"ts": time.time(), "menu_version": self.menu_version,
                      "payload": env}
            json.dump(c, open(self.cache_file, "w", encoding="utf-8"))
        except Exception:
            pass

    def _for_storage(self, env):
        """Masking applied at WRITE time, unconditionally.

        `pii_mask=False` means "do not hide fields in the RESPONSE". It is not
        consent to persist secrets in the cache and the append-only audit log:
        the cache expires, the audit log does not, and a leaked credential
        cannot be un-disclosed by re-asking. Storage and presentation are
        separate decisions.
        """
        out = dict(env)
        for k in ("question", "normalized"):
            if isinstance(out.get(k), str):
                out[k] = _mask_storage_value(out[k])
        if isinstance(out.get("error"), str):
            out["error"] = _mask_storage_value(out["error"])
        out["results"] = _storage_mask(out.get("results"))
        return out

    def _audit(self, rec):
        try:
            rec = self._for_storage(rec)
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
        exec_kwargs = {}

        if retrieved:
            used_probe = retrieved[0][0].name

            # deterministic fast path: consider every probe in the top-K whose
            # required args are extractable with certainty, and execute the one
            # that explains the MOST of the question.
            #
            # Taking the first executable candidate instead was a false
            # positive: "sales by quarter" matched total_sales and returned a
            # single grand total — a confident answer to a question that was
            # not asked. A probe that accounts for the question's own words
            # through its extracted arguments is the better match.
            best = None   # (effective score, rank score, meta, kwargs)
            oor = None    # first out-of-range argument we hit
            for _meta, _score in retrieved:
                try:
                    kwargs = extract_args(_meta, normalized)
                except ArgOutOfRange as exc:
                    oor = exc
                    continue
                if kwargs is None:
                    continue
                # A probe that opts into min_coverage must be substantially
                # present in the question, not merely touching it.
                if not _passes_coverage(_meta, tokens(normalized)):
                    continue
                # Strong-match rule: a probe with NO caged args is ambiguous
                # (any question could hit it) — only auto-execute it if it is a
                # strong lexical match (>= 1/2 of rank-1's score). Caged probes
                # with all args extracted are always confident.
                has_caged = any(s.get("required", True) or s.get("pattern")
                                for s in _meta.args.values())
                if not has_caged and _score < 0.5 * retrieved[0][1]:
                    continue
                # A probe that ignores a value the user NAMED is answering a
                # different question, so it must lose to one that consumes it.
                # Without this, "total revenue by region" matched the flat
                # total_revenue (trigger "total revenue", overlap 2) and
                # returned ONE grand total while 'region' was dropped.
                ignored = self._unconsumed_for(_meta, kwargs, normalized)
                effective = (_score
                             + ARG_MATCH_BONUS * _args_seen(kwargs, normalized)
                             - UNCONSUMED_PENALTY * len(ignored))
                if best is None or effective > best[0]:
                    best = (effective, _score, _meta, kwargs)

            if best is None and oor is not None:
                # The user named a value outside every candidate's declared
                # range. No model can repair that, so refuse with the reason
                # rather than quietly answering about something else.
                env = {"ask_id": ask_id, "ts": time.time(), "question": question,
                       "normalized": normalized, "probe": None,
                       "menu_version": self.menu_version, "mode": "refused",
                       "error": str(oor), "results": None,
                       "discarded": {"out_of_range": {
                           oor.argname: {"value": oor.value,
                                         "min": oor.lo, "max": oor.hi}}}}
                self._audit(env)
                raise NoResults(env)

            if best is not None:
                _meta, kwargs = best[2], best[3]
                exec_kwargs = dict(kwargs)
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
        discarded = self._discarded(used_probe, exec_kwargs, normalized, results)
        env = {"ask_id": ask_id, "ts": time.time(), "question": question,
               "normalized": normalized, "probe": used_probe,
               "menu_version": self.menu_version, "mode": mode,
               "confidence": conf,
               "latency_ms": int((time.time() - t0) * 1000),
               "results": None if empty else results}
        if discarded:
            env["discarded"] = discarded
        if empty:
            env["error"] = "no results produced for this question"
            self._audit(env)
            raise NoResults(env)

        if discarded and self.strict_discards:
            # Asked for a filtered answer; would have returned an unfiltered
            # one. Refuse rather than answer a different question.
            env["error"] = (f"would have ignored {_discard_summary(discarded)} "
                            f"— refusing in strict mode")
            env["results"] = None
            self._audit(env)
            raise NoResults(env)

        self._audit(env)
        # Never cache a failure. Errors here are usually environmental and
        # fixable (a missing library, a DB that was down, a timeout); caching
        # one for the whole TTL means the user installs the missing piece,
        # retries, and keeps getting the stale error for an hour.
        #
        # Nor cache a PARTIAL answer: memoizing it as though it were complete
        # is how a dropped filter becomes permanent for the whole TTL.
        if not _is_error_envelope(env) and not discarded:
            self._cache_store(key, env)
        return env


# How much to favour a probe whose extracted arguments are actually present
# in the question (i.e. it consumed the user's words instead of ignoring
# them). Kept above the per-trigger weight so it can outrank a probe that
# merely shares vocabulary.
ARG_MATCH_BONUS = 3.0

# Per named-but-ignored filter value. Kept above the per-trigger weight (4.0)
# so that ignoring ONE word a candidate could have consumed is enough to lose
# to a probe that consumes it: 17.6 - 5.0 < 9.6 + 2*3.0.
UNCONSUMED_PENALTY = 5.0


def _args_seen(kwargs, question: str) -> int:
    """How many extracted argument values appear in the question itself."""
    q = question.lower()
    n = 0
    for value in (kwargs or {}).values():
        if isinstance(value, str) and value and value.lower() in q:
            n += 1
    return n


def _tool_of(results):
    """The probe name a result list reports, if any."""
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0].get("_tool")
    return None


def _discard_summary(discarded):
    """Human-readable ledger, for the refusal message."""
    bits = []
    if discarded.get("terms"):
        bits.append("filter(s) " + ", ".join(map(str, discarded["terms"])))
    if discarded.get("rows"):
        bits.append("row(s) " + ", ".join(
            f"{k}={v}" for k, v in discarded["rows"].items()))
    if discarded.get("out_of_range"):
        bits.append("out-of-range " + ", ".join(
            f"{k}={v['value']} (max {v['max']})"
            for k, v in discarded["out_of_range"].items()))
    return "; ".join(bits) or "something"


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
            # Delegate to _extract_one so there is ONE integer implementation.
            # It refuses rather than clamping, so `user 999` can never come
            # back as user 10.
            v = _extract_one(argname, spec, question)
            if v is None:
                if required:
                    return None
                continue
            kwargs[argname] = v
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
