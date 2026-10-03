"""neuralosd scrub — sanitize the state already on disk.

Masking at write time protects *future* records. This fixes the ones already
written: the cache expires, but `ask_audit.jsonl` is append-only and permanent,
so a credential logged before the fix is still sitting there.

  neuralosd scrub --instance-dir ./inst --dry-run     # report only
  neuralosd scrub --instance-dir ./inst               # rewrite masked
  neuralosd scrub --instance-dir ./inst --purge-cache # drop the cache entirely

Remediation for a suspected leak should not require hand-scripting JSON.
"""
import json
import os
import sys

from ..router import (PII_HINTS, PII_VALUE_PATTERNS, SECRET_PATTERNS,
                      _mask_storage_value, _storage_mask)

CACHE = ".ask_cache.json"
AUDIT = "ask_audit.jsonl"


def _sanitize(env):
    """Apply the STORAGE masker.

    These records are on disk, so the response/presentation rules do not
    apply: an email that is a legitimate reply is still an email that has no
    business living in an append-only audit log.
    """
    if not isinstance(env, dict):
        return env
    out = dict(env)
    for k in ("question", "normalized"):
        if isinstance(out.get(k), str):
            out[k] = _mask_storage_value(out[k])
    if isinstance(out.get("error"), str):
        out["error"] = _mask_storage_value(out["error"])
    out["results"] = _storage_mask(out.get("results"))
    return out


def _scan(value, hits):
    """Count which secret shapes / hinted keys appear, without keeping them."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and any(h in k.lower() for h in PII_HINTS):
                hits["key:" + k.lower()] = hits.get("key:" + k.lower(), 0) + 1
            else:
                _scan(v, hits)
    elif isinstance(value, list):
        for v in value:
            _scan(v, hits)
    elif isinstance(value, str):
        for name, pat in SECRET_PATTERNS + PII_VALUE_PATTERNS:
            n = len(pat.findall(value))
            if n:
                hits[name] = hits.get(name, 0) + n


def _jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    yield None


def _write_atomic(path, text):
    tmp = path + ".scrub-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def run(a):
    d = a.instance_dir
    cache_path = os.path.join(d, CACHE)
    audit_path = os.path.join(d, AUDIT)
    dry = getattr(a, "dry_run", False)
    hits = {}
    changed = 0
    total = 0
    touched = []

    if os.path.exists(cache_path):
        if getattr(a, "purge_cache", False):
            if not dry:
                os.remove(cache_path)
            print(f"cache    : {'would purge' if dry else 'purged'} {cache_path}")
            touched.append("cache")
        else:
            try:
                blob = json.load(open(cache_path, encoding="utf-8"))
            except ValueError:
                blob = {}
            out = {}
            for key, entry in blob.items():
                total += 1
                payload = (entry or {}).get("payload", {})
                _scan(payload, hits)
                new = dict(entry or {})
                new["payload"] = _sanitize(payload)
                if new != entry:
                    changed += 1
                out[key] = new
            if not dry:
                _write_atomic(cache_path, json.dumps(out, ensure_ascii=False))
            print(f"cache    : {len(blob)} entries examined"
                  f"{' (dry run — not written)' if dry else ''}")
            touched.append("cache")

    if os.path.exists(audit_path):
        lines = list(_jsonl(audit_path))
        rewritten = []
        for rec in lines:
            total += 1
            if rec is None:
                continue
            _scan(rec, hits)
            new = _sanitize(rec)
            if new != rec:
                changed += 1
            rewritten.append(json.dumps(new, ensure_ascii=False, default=str))
        if not dry:
            _write_atomic(audit_path, "\n".join(rewritten) + "\n")
        print(f"audit    : {len(rewritten)} records examined"
              f"{' (dry run — not written)' if dry else ''}")
        touched.append("audit")

    if not touched:
        print(f"nothing to scrub under {d}")
        print("  (no .ask_cache.json and no ask_audit.jsonl)")
        return 0

    print(f"\nsecret shapes found: {sum(v for k, v in hits.items() if ':' not in k)}"
          f"   hinted keys: {sum(v for k, v in hits.items() if ':' in k)}")
    for name, n in sorted(hits.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {name}")
    if not hits:
        print("  (none — nothing matched a secret shape or a key hint)")
    print(f"\n{changed} of {total} records "
          f"{'would be' if dry else 'were'} rewritten.")
    if dry:
        print("re-run without --dry-run to apply.")
    return 0
