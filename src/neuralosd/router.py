"""The router — formalized ask pipeline (library form of the verified ask.py).

normalize -> retrieve top-K -> deterministic fast path -> calibration gate ->
model fallback (results-gated) -> guardrails -> emit -> audit -> cache.

Failure contract: raises NoResults when nothing was produced — callers map
that to exit 2 / HTTP 422. function_calls is never read (needle 3.0.3 leaves
it empty even on success); results are the only gate.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

from .probe import ProbeMeta

# The ask cache is an OPTIMISATION: exceeding this only costs a
# re-computation, never correctness. Long-lived instances must not grow it
# without bound (see _cache_store).
CACHE_MAX = 500

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

# Words that carry INTENT but no SUBJECT. A question built mostly from these
# ("how many X exist") can match any probe whose trigger starts the same way,
# which is exactly how a confident wrong answer used to be chosen. Coverage is
# measured over what remains after these are removed.
#
# Kept as WORDS here and stemmed into GENERIC_INTENT with `tokens()` below:
# the gate compares STEMS, so a plural written straight into the set was dead
# text — "status" (-> "statu"), "exists" (-> "exist") and "cheers" (->
# "cheer") never matched the token they were meant to neutralise, which turned
# ordinary phrasing into "unknown domain noun" refusals.
_GENERIC_INTENT_WORDS = """
    how many much show list give tell me find get what which who where when
    top best most least first last sample example preview all any
    are is there exist exists please number status break down
    some few several couple
    kindly thanks thank pls plz asap quickly quick just simply briefly hey hi
    hello ok okay cheers
"""

# Status and time QUALIFIERS. If the question carries one and the winning
# probe's vocabulary does not, the answer will silently ignore it - "how many
# work orders are blocked" answered with an open count is a wrong answer that
# looks right. Checked against the winner's STRONG vocabulary only: a
# description that merely mentions "closed" is not a closed-filter.
# Imperative ACTION verbs. neuralosd is read-only: it cannot play, erase,
# delete or execute anything. When a question OPENS with one of these and no
# probe claims the verb, the honest answer is "that is an action, not a
# question I can answer" - not the nearest data that happens to match. Checked
# on the FIRST word only (imperative form) to keep the false-refusal surface
# minimal; the verb must also be unknown to every probe, so a probe named
# play_logs would still answer play-related questions.
def action_intent_reason(normalized, menu_vocab):
    """'action_intent' when the question opens with an imperative verb the menu
    does not know: the user asked for an ACTION, not for data.

    Shared by the gate AND the floor-miss path — a destructive question that
    scores nothing lexically must still be reported as action_intent rather
    than degrading into a bland "no_probe_matches".
    """
    first = (normalized or "").split()[:1]
    if first and first[0] in ACTION_VERBS and first[0] not in menu_vocab:
        return "action_intent"
    return None


ACTION_VERBS = ("play", "erase", "delete", "remove", "drop", "wipe", "purge",
                "send", "execute", "restart", "shutdown", "kill", "exploit",
                "deploy", "cancel")

QUALIFIERS = ("blocked", "overdue", "rejected", "closed", "resolved",
              "pending", "escalated", "cancelled", "archived", "on hold",
              "last week", "last month", "last year", "this week",
              "this month", "yesterday", "today", "unassigned")


def _envf(name, default):
    """Float from the environment, or the default."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _router_config(instance_name):
    """Per-instance router thresholds from router.json, if present.

    Written by `neuralosd calibrate` after fitting floor/margin against the
    golden and trap banks, so an operator's chosen operating point survives
    restarts without environment variables.
    """
    for candidate in (f"{instance_name}.router.json", "router.json"):
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, dict):
                    return d
            except (ValueError, OSError):
                pass
    return {}

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


def _strip_plural(tokens):
    """'clinics' -> 'clinic'. Scoring is exact-token, so a plural question
    word gave the RIGHT probe zero overlap while a generic 'how many' prefix
    scored full marks - the router then answered from the wrong probe and the
    gate could only refuse. Normalising plurals here applies everywhere the
    tokens are compared (scoring, coverage, ambiguity)."""
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens}


def tokens(text):
    return _strip_plural(set(re.findall(r"[a-z0-9_]+", str(text).lower())) - STOP)


# Every documented filler word, in the STEM form the gate actually compares.
GENERIC_INTENT = frozenset().union(
    *(tokens(w) for w in _GENERIC_INTENT_WORDS.split()))

# ── the refusal-gate contract, versioned ────────────────────────────────────
# GATE_VERSION names the SEMANTICS of a refusal, not the wording of any one
# message. Two instances may only trust each other's refusals (and only reason
# about each other's answers) when they speak the same gate version: a v2 gate
# that refuses for a reason a v1 caller has never heard of would be read as an
# answer-shaped absence. Bump it whenever the reason vocabulary or the
# answered/refused decision rule changes.
GATE_VERSION = 1

# The bare tokens a refusal may carry. Kept next to GATE_VERSION so the two
# cannot drift: a new reason without a version bump is a wire break.
REFUSAL_REASONS = frozenset({
    "action_intent", "args_not_extractable", "no_probe_matches",
    "low_coverage", "dropped_filter", "out_of_range", "no_results",
    "model_abstained",
})

# ── audit provenance ──────────────────────────────────────────────────────────
# Every audit row is stamped with WHO asked: a live user ("user", the
# default), the health-check tick ("watchdog"), or a self-test replay
# ("golden" / "trap"). The refusal SLO must measure USERS — a fleet that
# replays its golden bank through the same router otherwise multiplies the
# human denominator several-fold and the "user refusal rate" quietly becomes
# a bank-replay rate. The stamp rides a ContextVar so a threaded HTTP server
# (ThreadingHTTPServer) isolates per-request sources without a lock, and so
# `service.py` can wrap one endpoint without touching Router's signature.
#
# Producers:
#   user     — the default; also assumed for legacy rows with no stamp
#   watchdog — the /ready health tick (it runs a real probe ask)
#   golden   — `neuralosd golden` / `truth` bank replays (positive items)
#   trap     — the same replays, for items that expect a refusal
#   serve    — a /ask served over HTTP (multi-instance + service)
_AUDIT_SOURCE: ContextVar = ContextVar("neuralosd_audit_source", default=None)


class audit_source:
    """Context manager stamping every audit row written inside the block.

        with audit_source("watchdog"):
            inst.ask(hc_question)   # row: "source": "watchdog"

    Nesting is a bug (the inner block wins and the outer is forgotten), but
    that cannot happen with the producers above — none nest. Restores the
    previous value on exit, however the block leaves (exception-safe).
    """

    def __init__(self, name: str):
        self._name = name
        self._token = None

    def __enter__(self):
        self._token = _AUDIT_SOURCE.set(self._name)
        return self

    def __exit__(self, *exc):
        _AUDIT_SOURCE.reset(self._token)
        return False


def score_probe(meta: ProbeMeta, q_tokens):
    # The BEST single trigger match dominates, so a probe with one exact
    # phrase beats a probe with many partially-overlapping triggers.
    # (e.g. "how many rows" -> row_count, not list_rows.)
    trig_tokens = [tokens(trig) for trig in meta.triggers]
    overlaps = [len(q_tokens & tt) for tt in trig_tokens]
    best = max(overlaps, default=0)
    s = 4.0 * best
    # Breadth = DISTINCT question tokens any trigger covers, not the sum of
    # per-trigger overlaps. sum(overlaps) made the score grow with the NUMBER
    # of near-duplicate triggers, so a probe with 22 variants of
    # "<qualifier> detections" outranked a probe whose single trigger was the
    # question verbatim ("are detections increasing" -> wrong probe).
    # Distinct coverage is bounded by the question length and therefore
    # cannot be gamed by adding synonyms. ("how many rows" -> row_count,
    # the case the best-term comment below cites, still holds: best is
    # untouched.)
    covered_by_any = set()
    for tt in trig_tokens:
        covered_by_any |= (q_tokens & tt)
    s += 1.0 * len(covered_by_any)
    s += 1.0 * len(q_tokens & tokens(meta.name.replace("_", " ")))
    s += 0.3 * len(q_tokens & tokens(meta.description))
    # EXACT PHRASE bonus: a trigger the question fully covers. Plural
    # normalisation made token overlaps coarse - "top genres" now matches a
    # probe merely mentioning "genre" as strongly as the probe triggered by
    # the phrase "top genres". A fully-covered trigger is the strongest lexical
    # evidence there is, so it is rewarded in proportion to its specificity.
    covered = [len(tt) for tt in trig_tokens if tt and tt <= q_tokens]
    if covered:
        s += 5.0 * max(covered)
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


def _is_secret_key(k) -> bool:
    return isinstance(k, str) and any(h in k.lower() for h in PII_HINTS)


def _mask_scalar_under_key(v):
    """A secret key's value is masked whatever its TYPE.

    The old code only masked `isinstance(v, str)`, so `{"password": 12345678}`
    and `{"pin": 4321}` went to disk and to the response in clear — a numeric
    PIN is not less of a secret than a textual password.
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return MASKED
    if isinstance(v, (int, float, complex)):
        return MASKED
    return v                       # containers are masked element-wise


def _mask_all_leaves(v):
    """Mask every scalar inside a CONTAINER that sits under a secret key.

    `{"password": {"hint": "my dog", "value": "hunter2"}}` used to keep the
    inner value: the key-hint rule only applied to string leaves, and the inner
    keys ("hint", "value") are not hint words. Once a key is declared secret,
    everything under it is secret.
    """
    if isinstance(v, dict):
        return {k: _mask_all_leaves(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_mask_all_leaves(vv) for vv in v]
    if v is None or isinstance(v, bool):
        return v
    return MASKED


def refusal_body(exc):
    """The HTTP body for a refusal: the ENVELOPE, not a status string.

    A 422 that says only "no results produced" throws away the gate's reason at
    the exact boundary where a client has to act on it — "that subject is not
    in this instance" (rephrase), "you asked for a filter I would have had to
    drop" (the data is here, the filter is not), "that is an action, not a
    question" (wrong product). Three different reactions, one useless string.
    """
    env = dict(getattr(exc, "envelope", None) or {})
    env.setdefault("error", "no results produced")
    # A refusal always names its reason, and the reason is a BARE TOKEN so it
    # can be branched on without matching prose.
    env.setdefault("refusal_reason", "no_probe_matches")
    return env


def jsonable(x, _seen=None):
    """Coerce a probe result into types `json.dumps` encodes natively.

    A probe is user code over user data: a Postgres NUMERIC arrives as
    `Decimal`, a DATE as `datetime.date`, a BLOB as `bytes`. None of those
    encode without a `default=` hook, so the envelope left the router
    un-serialisable — a 500 from `/ask` and a client-side crash in the SDK,
    produced by an ordinary money column rather than by anything exotic.

    `Decimal` becomes a STRING on purpose: a price is not a float, and
    round-tripping 19.99 through binary floating point is how the number in
    the answer stops matching the number in the database. Cycles return
    "<cycle>" for the same reason masking does.
    """
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    _seen = _seen if _seen is not None else set()
    oid = id(x)
    if oid in _seen and isinstance(x, (dict, list, tuple, set, frozenset)):
        return "<cycle>"
    if isinstance(x, dict):
        _seen.add(oid)
        try:
            return {str(k): jsonable(v, _seen) for k, v in x.items()}
        finally:
            _seen.discard(oid)
    if isinstance(x, (list, tuple, set, frozenset)):
        _seen.add(oid)
        try:
            items = sorted(x, key=str) if isinstance(x, (set, frozenset)) \
                else list(x)
            return [jsonable(v, _seen) for v in items]
        finally:
            _seen.discard(oid)
    if isinstance(x, bytes):
        try:
            return x.decode("utf-8")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(x).decode("ascii")
    if isinstance(x, (bytearray, memoryview)):
        return jsonable(bytes(x), _seen)
    iso = getattr(x, "isoformat", None)      # date / datetime / time / Decimal?
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(x)


def _storage_mask(x, _seen=None):
    """Key hints AND value shapes (credentials + PII), at every depth.

    Cycle-safe: probe results are arbitrary user data and may contain a
    self-referential structure (or a shared child reachable twice). Without
    this guard a cyclic result raised RecursionError *inside* the audit write
    and killed an otherwise successful ask.
    """
    if isinstance(x, (dict, list)):
        if _seen is None:
            _seen = set()
        if id(x) in _seen:
            return "<cycle>"
        _seen = _seen | {id(x)}
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if _is_secret_key(k):
                out[k] = _mask_scalar_under_key(v) if not isinstance(
                    v, (dict, list)) else _mask_all_leaves(v)
            else:
                out[k] = _storage_mask(v, _seen)
        return out
    if isinstance(x, list):
        return [_storage_mask(v, _seen) for v in x]
    if isinstance(x, str):
        return _mask_storage_value(x)
    return x


def mask_pii(x, deep=True, _seen=None):
    """Mask by key name AND (when deep) by value shape.

    deep=False restores the old key-name-only behaviour, which cannot see a
    secret stored under an innocuous key. Cycle-safe for the same reason as
    `_storage_mask`: a self-referential result must not crash the answer.
    """
    if isinstance(x, (dict, list)):
        if _seen is None:
            _seen = set()
        if id(x) in _seen:
            return "<cycle>"
        _seen = _seen | {id(x)}
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if _is_secret_key(k):
                out[k] = _mask_scalar_under_key(v) if not isinstance(
                    v, (dict, list)) else _mask_all_leaves(v)
            else:
                out[k] = mask_pii(v, deep, _seen)
        return out
    if isinstance(x, list):
        return [mask_pii(v, deep, _seen) for v in x]
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


def match_enum_value(vals, question):
    """The enum value the question names — LONGEST match wins.

    `next(...)` returned whichever value happened to be listed first, so a
    menu holding ["Rock", "Rock and Roll"] answered "Rock and Roll" questions
    with `genre=Rock`. Longest-match is the only rule that keeps a value that
    is a prefix of another value from shadowing it.
    """
    best = None
    for v in vals or []:
        if v is None:
            continue
        if re.search(r"\b" + re.escape(str(v)) + r"\b", question, re.I):
            if best is None or len(str(v)) > len(str(best)):
                best = v
    return best

# A number the user wrote with thousands separators, or with a sign in front
# of it. `\b(\d{1,9})\b` read "top 1,000 customers" as `limit=1` (answering a
# question nobody asked) and read "last -5 days" as 5 (flipping the sign).
_INT_IN_QUESTION = re.compile(
    r"(?<![\d,.])(-?)(\d{1,3}(?:,\d{3})+|\d{1,9})(?![\d,.]\d)")

# Dates are numbers the user did not offer as an argument. "top customers on
# 2026-10-31" must not be read as limit=2026 (or 31). Removed before number
# extraction; the same question text is still matched against triggers.
_DATE_LIKE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b\d{4}-\d{2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")


def _int_from_question(question):
    """(value, spelled_with_separator) or (None, False)."""
    m = _INT_IN_QUESTION.search(_DATE_LIKE.sub(" ", question))
    if not m:
        return None, False
    raw = m.group(2)
    return int(m.group(1) + raw.replace(",", "")), ("," in raw)


def _extract_one(argname, spec, question):
    t = spec.get("type", "string")
    if t == "enum":
        return match_enum_value(spec.get("values") or [], question)
    if t == "pattern":
        m = re.search(spec["pattern"], question)
        if not m:
            return None
        return (m.group(1) if m.groups() else m.group(0)).strip()
    if t == "integer":
        v, _sep = _int_from_question(question)
        if v is None:
            return spec.get("default")
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
                 name: str = "instance", strict_discards: bool = False,
                 floor: Optional[float] = None, margin: Optional[float] = None,
                 min_question_coverage: Optional[float] = None,
                 refuse_disjunction: Optional[bool] = None,
                 audit_source: str = "user"):
        self.probes = list(probes)
        self.metas = [getattr(p, "_probe") for p in self.probes]
        # Duplicate probe names used to be accepted silently: the menu listed
        # both, `by_name` kept only the LAST one, so chains, meta-selection and
        # the OpenAPI paths all resolved to one function while the router could
        # execute the other. A name collision is a build error, not a coin flip.
        seen, dupes = set(), []
        for m in self.metas:
            if m is None:
                continue
            if m.name in seen:
                dupes.append(m.name)
            seen.add(m.name)
        if dupes:
            raise ValueError(
                "duplicate probe name(s): " + ", ".join(sorted(set(dupes))) +
                "\nhint: every probe in a menu needs a unique name — two probes "
                "sharing one shadow each other in by_name/openapi/chains")
        self.by_name = {m.name: p for p, m in zip(self.probes, self.metas)}
        self.model_fallback = model_fallback
        self.cache_file = cache_file
        self.audit_file = audit_file
        # Provenance stamped on every row `source` is missing from. The
        # ContextVar (`with audit_source("watchdog"): ...`) takes precedence
        # per-request; this is the fallback for plain Router users.
        self.audit_source = audit_source
        self.pii_mask = pii_mask
        self.ttl = ttl
        self.name = name
        # When True, an answer that would have dropped a filter the user named
        # is refused instead of returned. Off by default so existing callers
        # keep working; the ledger is still always present in the envelope.
        self.strict_discards = strict_discards
        # ── calibrated refusal thresholds ──────────────────────────────────
        # The router used to execute the argmax unconditionally, so a question
        # whose only match was a generic prefix ("how many ...") answered
        # confidently from an unrelated probe. Defaults: refuse-over-guess.
        # Per-instance override: router.json next to probes.py / the menu.
        cfg = _router_config(name)
        self.floor = float(floor if floor is not None
                           else cfg.get("floor", _envf("NEURALOSD_FLOOR", 3.0)))
        self.margin = float(margin if margin is not None
                            else cfg.get("margin", _envf("NEURALOSD_MARGIN", 2.0)))
        self.min_question_coverage = float(
            min_question_coverage if min_question_coverage is not None
            else cfg.get("min_question_coverage",
                         _envf("NEURALOSD_MIN_QUESTION_COVERAGE", 0.5)))
        self.refuse_disjunction = bool(
            refuse_disjunction if refuse_disjunction is not None
            else cfg.get("refuse_disjunction", True))
        # What the winner is allowed to know about, per probe: triggers, name,
        # description AND enum values. A question token that appears nowhere in
        # that set is a domain noun the probe has never heard of.
        self._vocab = {m.name: m._vocab_tokens() for m in self.metas
                       if hasattr(m, "_vocab_tokens")}
        self._menu_vocab = frozenset().union(*self._vocab.values()) \
            if self._vocab else frozenset()
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
                # Compare both sides in the SAME space. `identity` holds stems
                # (`tokens()` strips plurals) while `_filter_vocab` keys are
                # surface forms, so plain membership never matched a
                # plural-ending value: "status" was compared against "statu"
                # and the identity rescue was dead. That let `breakdown` (which
                # happens to declare `status` as an enum) out-score
                # `distinct_status` on its own trigger "status list", because
                # distinct_status was charged UNCONSUMED_PENALTY for ignoring
                # the very word it is named after.
                if tokens(low) and tokens(low) <= identity:
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
                allowed = {k: v for k, v in spec.items()
                           if k in ("type", "values", "enum", "pattern",
                                    "min", "max", "description", "default")}
                # OpenAPI convention: the allowed values are under `enum`, not
                # `values`. Emit both so the engine and the generator each see
                # the key they expect.
                if "values" in allowed and "enum" not in allowed:
                    allowed["enum"] = allowed["values"]
                props[an] = allowed
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
            props = {}
            for k, spec in m.args.items():
                allowed = {kk: vv for kk, vv in spec.items()
                           if kk in ("type", "enum", "values", "pattern",
                                     "min", "max", "default")}
                # OpenAPI convention: the constraint list goes under `enum`.
                # `values` is kept too (existing contract: neither consumer has
                # to know the other's spelling), but `type: enum` must not
                # survive into a JSON Schema — "enum" is a constraint, not a
                # type, and generated clients reject the document.
                if "values" in allowed and "enum" not in allowed:
                    allowed["enum"] = allowed["values"]
                if allowed.get("type") == "enum":
                    allowed["type"] = "string"
                props[k] = allowed
            paths[f"/probes/{m.name}"] = {
                "post": {"operationId": m.name, "summary": m.description,
                         "requestBody": {"content": {"application/json": {
                             "schema": {"type": "object", "properties": props}}}},
                         "responses": {"200": {"description": "envelope"}}}}
        return {"openapi": "3.1.0", "info": {"title": title, "version": "1.0.0"},
                "paths": paths}

    # -- internal helpers --------------------------------------------------
    def _gate(self, normalized, runner_up, chosen):
        """The three-outcome decision. Returns None (CONFIDENT) or a reason.

        None            -> execute the chosen probe (today's behaviour)
        "below_floor"   -> the best match is too weak to be trusted
        "ambiguous"     -> a second probe scores within `margin` of the first
                           AND explains the question no worse
        "low_coverage"  -> the question's DOMAIN NOUNS are unknown to the
                           winner (it won on generic words like "how many")
        "comparative"   -> the question compares alternatives and no probe
                           claims to compare

        Deliberately computed from the MENU and the QUESTION only: no probe
        runs, no model is called, microseconds.

        Coverage is measured over the question's DOMAIN nouns, not all content
        words. "how many worklog entries exist" is five content tokens of which
        three are generic intent ("how many ... exist") — measuring over all
        five let a winner that knew only "how many" pass at 0.6. Measuring over
        {worklog, entries, exist} scores it 0.0, which is the truth.

        Ambiguity needs a coverage-advantage test as well as a margin: a
        question sharing one generic token with many probes ("top genres" vs
        "top customers") ties on raw score, and refusing an answerable question
        is its own failure. The winner must explain the question strictly
        better than the runner-up, or the tie is real ambiguity.

        All thresholds are skipped when the question explicitly names an entity
        the winner extracted (an id, an email, a status word). A caged pattern
        or enum hit IS the confidence signal there; prose scores are not the
        evidence, and applying them would refuse the exact questions caged
        probes exist for.
        """
        if not chosen:
            return None                      # nothing executable; handled upstream
        meta, score, kwargs = chosen

        # An imperative ACTION is not a question. This sits BEFORE the
        # entity bypass on purpose: "play the rock music" extracts genre=Rock
        # cleanly, and the extracted enum is strong evidence of SUBJECT - but
        # the user asked for an action no probe can perform, and answering
        # with the nearest data would be a confident wrong answer about what
        # the system just did.
        action = action_intent_reason(normalized, self._menu_vocab)
        if action:
            return action

        if _args_seen(kwargs, normalized) > 0:
            return None                      # explicit entity reference
        per_probe = meta.conf_gate if meta.conf_gate else 0.0
        if max(self.floor, per_probe) > 0 and score < max(self.floor, per_probe):
            return "below_floor"

        q_words = set(re.findall(r"[a-z0-9]+", normalized.lower()))
        for qual in QUALIFIERS:
            head = qual.split()[0]
            hit = head in q_words if len(qual.split()) == 1 else qual in normalized.lower()
            if hit and head not in self._vocab.get(meta.name, frozenset()):
                return "dropped_filter"
        qt = tokens(normalized)
        # Numbers are entity references ("user 4"), not subjects.
        domain = {t for t in qt - GENERIC_INTENT if not t.isdigit()}
        domain = domain or (qt - GENERIC_INTENT) or qt

        def _plural_insensitive(tokens):
            # "clinics" must count as knowing "clinic": a question in the
            # plural should not fail a probe that stores the singular.
            return {t[:-1] if len(t) > 3 and t.endswith("s") else t
                    for t in tokens}

        def cover(name):
            d = _plural_insensitive(domain)
            return (len(d & _plural_insensitive(
                        self._vocab.get(name, frozenset())))
                    / max(1, len(d)))

        # A domain noun NO probe in the menu knows means the subject itself is
        # out of scope - refusing is correct no matter which probe scored top.
        unknown = [t for t in sorted(domain) if not self._known_anywhere(t)]
        if unknown:
            return "no_probe_matches"

        if runner_up is not None:
            # Ambiguity is measured between the things we could actually
            # EXECUTE. Comparing the chosen candidate against a higher-scoring
            # probe that was skipped (its arguments did not extract) produced
            # false refusals on questions whose best prose match was not
            # routable anyway.
            if (score - runner_up[1] < self.margin
                    and cover(meta.name) <= cover(runner_up[0].name)):
                return "ambiguous"
        if self.min_question_coverage > 0 and cover(meta.name) < \
                self.min_question_coverage:
            return "low_coverage"
        if self.refuse_disjunction:
            q = normalized.lower()
            comparative = re.search(
                r"\b(more|less|fewer|greater|higher|lower|bigger|smaller|"
                r"compared?|versus|vs)\b", q)
            if comparative and re.search(r"\bor\b", q):
                claims = self._vocab.get(meta.name, frozenset()) & {
                    "compare", "comparison", "compares", "versus", "vs",
                    "between"}
                if not claims:
                    return "comparative"
        return None

    def _known_anywhere(self, token: str) -> bool:
        """Is this domain noun in ANY probe's strong vocabulary?

        Plural-insensitive: 'genres' matches 'genre' and vice versa. Exact
        otherwise - this is a SUBJECT check, not a fuzziness contest.
        """
        v = self._menu_vocab
        if token in v:
            return True
        if len(token) > 3 and token.endswith("s") and token[:-1] in v:
            return True
        if len(token) > 3 and (token + "s") in v:
            return True
        return False

    def _refuse(self, ask_id, ts, question, normalized, reason, retrieved=None,
                extra=None):
        """Build, audit and raise a refusal envelope with its score vector."""
        scores = [{"probe": m.name, "score": round(s, 3)}
                  for m, s in (retrieved or [])[:3]]
        env = {"ask_id": ask_id, "ts": ts, "question": question,
               "normalized": normalized, "probe": None,
               "menu_version": self.menu_version, "mode": "refused",
               "error": f"refused ({reason}): the menu cannot answer this "
                        f"confidently, and guessing is worse",
               "results": None, "refusal_reason": reason,
               "scores": scores}
        if extra:
            env.update(extra)
        self._audit(env)
        raise NoResults(env)

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
        """A cache hit must be a well-formed entry, or it is a miss.

        Truncated/partial cache files (a kill during the write, a hand edit, a
        sync client racing us) used to crash `ask()` with KeyError['payload']
        or ValueError — the cache is an OPTIMISATION, so a bad entry can only
        ever cost a re-computation and never the answer.
        """
        try:
            with open(self.cache_file, encoding="utf-8") as fh:
                c = json.load(fh)
            if not isinstance(c, dict):
                return None
            e = c.get(key)
            if not isinstance(e, dict):
                return None
            if e.get("menu_version") != self.menu_version:
                return None
            if not isinstance(e.get("payload"), dict):
                return None
            if time.time() - float(e.get("ts") or 0) >= self.ttl:
                return None
            return e
        except Exception:
            return None

    def _cache_store(self, key, env):
        """Write atomically (tmp + os.replace).

        A plain open("w") truncates first, so a crash or a concurrent writer
        mid-write left invalid JSON behind and every later ask paid the
        re-computation (or, before the load guard, crashed).
        """
        tmp = None
        try:
            env = self._for_storage(env)
            c = {}
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        c = loaded
                except Exception:
                    c = {}
            # EVICT DEAD STATES: anything _cache_load would refuse must not
            # be retained, or a long-lived instance accumulates megabytes of
            # unreachable rows (observed in the wild: 13 live keys inside a
            # 2.5 MB .ask_cache.json — and every ask re-serialised the whole
            # file). Nothing migrates or rewrites legacy entries here beyond
            # dropping them on the next write; existing files keep loading.
            now = time.time()
            live = {}
            for k, v in c.items():
                if not isinstance(v, dict):
                    continue
                if v.get("menu_version") != self.menu_version:
                    continue
                try:
                    if now - float(v.get("ts") or 0) >= self.ttl:
                        continue
                except (TypeError, ValueError):
                    continue
                live[k] = v
            if len(live) >= CACHE_MAX:
                newest = sorted(live.items(),
                                key=lambda kv: float(kv[1].get("ts") or 0),
                                reverse=True)[: CACHE_MAX - 1]
                live = dict(newest)
            c = live
            c[key] = {"ts": now, "menu_version": self.menu_version,
                      "payload": env}
            tmp = f"{self.cache_file}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(c, fh)
            os.replace(tmp, self.cache_file)
            tmp = None
        except Exception:
            pass
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
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
        # THE choke point: every row passes through here (refusals, answers,
        # cache hits), so the provenance stamp is applied exactly once. The
        # per-request context wins; the constructor default covers everyone
        # else. Legacy producers keep their rows untouched — this is a
        # "missing key fills in", not an overwrite.
        #
        # The stamp goes on a COPY: env dicts are cached and re-audited on a
        # cache hit, and a mutated dict would carry the golden run's stamp
        # into the next USER's audit row for the same question.
        src = _AUDIT_SOURCE.get() or self.audit_source
        rec = {**rec, "source": rec.get("source") or src}
        try:
            rec = self._for_storage(rec)
            with open(self.audit_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    # -- public dry run ----------------------------------------------------
    def dry_run(self, question: str, k: int = 8) -> Dict[str, Any]:
        """Would this question be answered — WITHOUT running a probe.

        The decision half of ask() with the execution half removed: the same
        normalisation, the same retrieval, the same argument extraction, the
        same calibrated gate. No probe runs, nothing is cached, nothing is
        added to the audit log.

        This exists because a mesh peer needs to know whether a question is
        routable *before* paying for a round trip, and because advertising a
        capability we would refuse is worse than advertising none. It is kept
        honest by ``tests/test_router_dry_run.py``, which asserts agreement
        with ask() on every conformance case — a second implementation of the
        decision is only safe while a test proves they are the same decision.
        """
        t0 = time.time()
        if not isinstance(question, str):
            raise TypeError(
                f"question must be a str, got {type(question).__name__}")
        normalized, _moved = self._normalize(question.strip())
        out = {"question": question, "normalized": normalized,
               "menu_version": self.menu_version,
               "gate_version": GATE_VERSION,
               "would": "refused", "would_answer": False,
               "probe": None, "refusal_reason": None,
               "scores": [], "candidates": []}

        def _done(reason=None, probe=None, status="refused"):
            out["refusal_reason"] = reason
            out["probe"] = probe
            out["would"] = status
            out["would_answer"] = status in ("answered", "defer_to_model")
            out["ms"] = round((time.time() - t0) * 1000, 3)
            return out

        # Mirror ask(): a destructive imperative is refused before retrieval so
        # it reports action_intent rather than degrading into no_probe_matches.
        if action_intent_reason(normalized, self._menu_vocab):
            return _done("action_intent")

        retrieved = self._retrieve(normalized, k)
        out["scores"] = [{"probe": m.name, "score": round(s, 3)}
                         for m, s in retrieved[:3]]

        best, oor, args_failed, exec_cands = None, None, False, []
        if retrieved:
            for _meta, _score in retrieved:
                try:
                    kwargs = extract_args(_meta, normalized)
                except ArgOutOfRange as exc:
                    oor = exc
                    continue
                if kwargs is None:
                    args_failed = True
                    continue
                if not _passes_coverage(_meta, tokens(normalized)):
                    continue
                has_caged = any(s.get("required", True) or s.get("pattern")
                                for s in _meta.args.values())
                if not has_caged and _score < 0.5 * retrieved[0][1]:
                    continue
                ignored = self._unconsumed_for(_meta, kwargs, normalized)
                effective = (_score
                             + ARG_MATCH_BONUS * _args_seen(kwargs, normalized)
                             - UNCONSUMED_PENALTY * len(ignored))
                exec_cands.append({"probe": _meta.name,
                                   "score": round(_score, 3)})
                if best is None or effective > best[0]:
                    best = (effective, _score, _meta, kwargs)

        if best is None and oor is not None:
            return _done("out_of_range")

        chosen = best
        runner_up = None
        if chosen is not None and len(exec_cands) > 1:
            # The runner-up must be an EXECUTABLE candidate, exactly as in
            # ask(): comparing against a higher-scoring probe whose arguments
            # did not extract produced false ambiguity refusals.
            names = {c["probe"] for c in exec_cands} - {chosen[2].name}
            pick = [(_m, _s) for _m, _s in retrieved
                    if _m.name in names and _m.name != chosen[2].name]
            if pick:
                best_c = max(pick, key=lambda x: x[1])
                runner_up = (best_c[0], best_c[1])
        gate_reason = self._gate(
            normalized, runner_up,
            (chosen[2], chosen[1], chosen[3]) if chosen else None)
        out["candidates"] = exec_cands[:3]
        if gate_reason is not None:
            if self.model_fallback is not None:
                return _done(None, None, "defer_to_model")
            return _done(gate_reason)
        if chosen is not None:
            return _done(None, chosen[2].name, "answered")
        if self.model_fallback is not None and retrieved:
            return _done(None, None, "defer_to_model")
        if args_failed:
            return _done("args_not_extractable")
        return _done(action_intent_reason(normalized, self._menu_vocab)
                     or "no_probe_matches")

    # -- public ask --------------------------------------------------------
    def ask(self, question: str, k: int = 8, full: bool = False,
            use_cache: bool = True) -> Dict[str, Any]:
        t0 = time.time()
        ask_id = uuid.uuid4().hex[:12]
        # A non-string question raised AttributeError deep inside normalisation
        # ("'NoneType' object has no attribute 'strip'"). Say what is wrong.
        if not isinstance(question, str):
            raise TypeError(
                f"question must be a str, got {type(question).__name__}")
        normalized, moved = self._normalize(question.strip())
        if moved:
            print("normalized:", normalized, flush=True)
        key = self._key(normalized)

        # Destructive questions that score ZERO lexically ("delete all
        # invoices") must still get the SPECIFIC action_intent reason, not
        # the generic no_probe_matches. The check runs BEFORE the walk so it
        # fires even when no probe is executable.
        if action_intent_reason(normalized, self._menu_vocab):
            self._refuse(ask_id, time.time(), question, normalized,
                         "action_intent")

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
        gate_reason_for_audit = None
        args_failed = False   # a probe matched but its args could not be
                              # extracted - its own honest refusal reason

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
            best = None   # (effective, lexical, meta, kwargs)
            oor = None    # first out-of-range argument we hit
            exec_cands = []   # every candidate we could actually execute -
                              # ambiguity is judged between THESE, not between
                              # the chosen one and a higher-scoring probe whose
                              # arguments did not extract
            for _meta, _score in retrieved:
                try:
                    kwargs = extract_args(_meta, normalized)
                except ArgOutOfRange as exc:
                    oor = exc
                    continue
                if kwargs is None:
                    args_failed = True
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
                exec_cands.append((effective, _score, _meta, kwargs))
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
                       "refusal_reason": "out_of_range",
                       "discarded": {"out_of_range": {
                           oor.argname: {"value": oor.value,
                                         "min": oor.lo, "max": oor.hi}}}}
                self._audit(env)
                raise NoResults(env)

            # ── the three-outcome decision ────────────────────────────────
            # CONFIDENT: execute. UNSURE: do not execute - let the fallback
            # try, and refuse if it abstains. The decision is made from the
            # menu and the question alone.
            chosen = best if best is not None else None
            runner_up = None
            if chosen is not None and len(exec_cands) > 1:
                others = [c for c in exec_cands if c[2].name != chosen[2].name]
                if others:
                    best_c = max(others, key=lambda c: c[1])
                    runner_up = (best_c[2], best_c[1])   # (meta, lexical score)
            gate_reason = self._gate(
                normalized, runner_up,
                (chosen[2], chosen[1], chosen[3]) if chosen else None)
            if gate_reason is not None:
                if self.model_fallback is None:
                    self._refuse(ask_id, time.time(), question, normalized,
                                 gate_reason, retrieved)
                # UNSURE with a fallback: skip the deterministic execute and
                # let the model path below try. If it abstains we refuse with
                # model_abstained. The reason is carried so the audit shows WHY
                # the deterministic layer stood down.
                gate_reason_for_audit = gate_reason
            else:
                gate_reason_for_audit = None

            if best is not None and gate_reason is None:
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
            # Nothing the deterministic layer would stand behind. Either no
            # probe matched, or the calibrated gate stood down (UNSURE). Consult
            # the model when one is configured - with the retrieved subset if
            # lexical retrieval found something, otherwise with the whole menu.
            if self.model_fallback is None:
                if gate_reason_for_audit:
                    # Gate stood down with no model to consult: refused, same
                    # as the confident-guess case below would have been.
                    self._refuse(ask_id, time.time(), question, normalized,
                                 gate_reason_for_audit, retrieved=retrieved)
                if args_failed:
                    refusal_reason = "args_not_extractable"
                else:
                    refusal_reason = (action_intent_reason(normalized,
                                                           self._menu_vocab)
                                      or "no_probe_matches")
                # THE v2.9.6 FIX: this branch used to build its audit
                # envelope inline and omit `ts` and `mode`. Every other
                # refusal path emits the full schema via _refuse; these rows
                # did not, so `refusal_rate()` — which skips rows without a
                # numeric ts — counted the ask as NEITHER ask NOR refusal and
                # the fleet SLO silently undercounted. Route it through the
                # same door; the per-reason prose lives in _refuse now.
                self._refuse(ask_id, time.time(), question, normalized,
                             refusal_reason, retrieved=retrieved)
            conf = None
            candidates = ([m for m, _s in retrieved] if retrieved
                          else list(self.metas))
            mode = "model"
            results = self.model_fallback(normalized, candidates)
            # The model may pick a different probe than rank-1, so trust the
            # tool tag the bridge attaches rather than the top-ranked name.
            used_probe = (_tool_of(results)
                          or (retrieved[0][0].name if retrieved else None))
            if _model_abstained(results):
                # The fallback looked at the menu and declined - "none of
                # these". That is an HONEST answer and must be surfaced as one:
                # converting it into a refusal keeps the no-guessing contract
                # one layer up, instead of answering whatever ranked first.
                abstain_env = {"ask_id": ask_id, "ts": time.time(),
                               "question": question, "normalized": normalized,
                               "probe": None,
                               "menu_version": self.menu_version,
                               "mode": "refused",
                               "error": "the model fallback examined the menu "
                                        "and abstained - none of the "
                                        "available probes answer this",
                               "results": None,
                               "refusal_reason": "model_abstained",
                               "scores": [{"probe": m.name, "score": round(s2, 3)}
                                          for m, s2 in (retrieved or [])[:3]]}
                self._audit(abstain_env)
                raise NoResults(abstain_env)

        if self.pii_mask:
            results = mask_pii(results)
        # Mask first (it needs the original key types), then make the payload
        # encodable: Decimal/date/bytes in a result must not 500 the service.
        results = jsonable(results)
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
            # Every refusal carries a BARE TOKEN, including the one produced by
            # a probe that ran and returned nothing. Clients branch on it; an
            # envelope with only a sentence forces them to string-match prose.
            env["refusal_reason"] = "no_results"
            self._audit(env)
            raise NoResults(env)

        if discarded and self.strict_discards:
            # Asked for a filtered answer; would have returned an unfiltered
            # one. Refuse rather than answer a different question. This is a
            # REFUSAL, so it carries the refusal schema — including the bare
            # token. "dropped_filter" has sat in REFUSAL_REASONS next to
            # GATE_VERSION all along; this branch just never emitted it, so
            # strict-mode refusals reached clients with results=None and no
            # token to branch on.
            env["mode"] = "refused"
            env["probe"] = None
            env["refusal_reason"] = "dropped_filter"
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

# Per named-but-ignored filter value. Must outweigh the per-trigger weight
# (4.0), the exact-phrase bonus (5.0 x the covered trigger's length - the
# question "total revenue by region" contains the exact phrase "total
# revenue"), AND the arg-match bonus: consuming "region" must beat phrasing.
# 27.6 + 6 - 12 < 13.6 + 6 + 12.
UNCONSUMED_PENALTY = 12.0


def _args_seen(kwargs, question: str) -> int:
    """How many extracted argument values appear in the question itself."""
    q = question.lower()
    n = 0
    for value in (kwargs or {}).values():
        if isinstance(value, str) and value and value.lower() in q:
            n += 1
    return n


ABSTAIN_MARKERS = ("none_of_these", "none of these", "abstain", "no_probe",
                   "no_match", "no match")


def _model_abstained(results) -> bool:
    """Did the model fallback DECLINE rather than answer?

    The 121M selector can abstain (needle's none_of_these) and any custom
    fallback can signal the same by returning None/empty or a result tagged
    with an abstain marker. Treating a decline as an answer would be the
    confident-wrong failure one layer up.
    """
    if results in (None, [], {}):
        return True
    if isinstance(results, list):
        return any(_model_abstained(r) for r in results)
    if isinstance(results, dict):
        tool = str(results.get("_tool", "")).lower()
        if any(marker in tool for marker in ABSTAIN_MARKERS):
            return True
        err = str(results.get("error", "")).lower()
        return any(marker in err for marker in ABSTAIN_MARKERS)
    return False


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
            val = match_enum_value(vals, question)
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
