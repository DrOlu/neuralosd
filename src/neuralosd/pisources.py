"""Pi (https://pi.dev) as builder and operator — out of the box (v2.6.0).

Pi is a coding agent that runs HEADLESS:
    pi --provider P --model M --print [--no-session] "task"

Two roles, one safety model — pi proposes, neuralosd verifies:

  BUILDER  `neuralosd build "<capability description>" --instance-dir .`
           Pi works inside the instance directory and writes probe code.
           Afterwards NEURALOSD judges the work:
             1. banks (golden.json / traps.json / acl.json) byte-identical
                to before — pi never grades its own homework
             2. the instance still loads
             3. both banks replay green
           Any failure -> every pi-touched file is restored to its pre-pi
           state and the report says so. Nothing broken is ever presented
           as delivered.

  OPERATOR `neuralosd pi ask "why did refusals go up?"`
           Pi investigates using the CLOSED LIST of neuralosd commands.
           Its runs are subprocesses in the instance directory, audited
           like every other tool neuralosd drives.

Everything injectable (which_fn / execute / runner) — the whole flow is
unit-testable with no pi, no model, no network.
"""
import json
import os
import re

DEFAULT_MODEL = os.environ.get("NEURALOSD_PI_MODEL", "z-ai/glm-5.3-flash")
DEFAULT_PROVIDER = os.environ.get("NEURALOSD_PI_PROVIDER", "openrouter")

# files pi must NEVER touch — the banks are the judge, not the builder
_FORBIDDEN = ("golden.json", "traps.json", "acl.json", "derived.json",
              "candidates.json", "synonym_proposals.json", "mothballed.json")

_BUILDER_CONTRACT = """You are extending a neuralosd instance. neuralosd is a
verified data-answer runtime: the model NEVER invents answers — questions
route to "probes", which are pure Python functions over the data layer.

THE CONTRACT for every probe you add to probes.py:
  - decorate it with @probe from neuralosd: from neuralosd import probe
  - give it a clear description and AT LEAST 3 natural-language triggers
    (phrases a user would actually type)
  - the function must be PURE: no model calls, no network, no file writes;
    it reads data through `import bridge` (bridge.rows() returns the rows)
  - return a small JSON-able dict; include large lists only under a "rows"
    key; report what you excluded (nulls, bad values) instead of dropping
    silently

HARD RULES:
  - NEVER edit golden.json, traps.json, acl.json, derived.json, or any
    file other than probes.py and bridge.py. The banks are graded by
    neuralosd itself, not by you.
  - do not remove or weaken existing probes
  - do not install anything; do not touch anything outside this directory
When done, reply with a one-paragraph summary of what you added.
"""


def pi_command(text, model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER,
               session=None, system_prompt=None):
    """Build the headless pi argv: non-TUI, one-shot, scoped."""
    argv = ["pi",
            "--provider", provider,
            "--model", model,
            "--print",
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files"]
    if session:
        argv += ["--resume", session]
    else:
        argv += ["--no-session"]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    argv += ["--", text]
    return argv


def pi_run(argv, cwd=None, timeout=900, execute=None):
    """Execute a pi command in `cwd` (default: here). Raises an honest
    error when pi is not installed."""
    execute = execute or _execute
    try:
        return execute(argv, cwd=cwd, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(
            "error: the pi coding agent is not installed on this host.\n"
            "    install it with:  npm install -g @mariozechner/pi-coding-agent\n"
            "    (https://github.com/badlogic/pi-mono)") from None


def _execute(argv, cwd=None, timeout=900):
    import subprocess
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(
            "error: the pi coding agent is not installed on this host.\n"
            "    install it with:  npm install -g @mariozechner/pi-coding-agent\n"
            "    (https://github.com/badlogic/pi-mono) and configure a "
            "provider\n"
            "    (pi auth / models.json), e.g. the z-ai/glm-5.3-flash model "
            "via openrouter")
    out = _decode(r.stdout)
    if r.returncode != 0:
        err = _decode(r.stderr).strip()
        raise SystemExit(f"error: pi failed ({r.returncode}): "
                         f"{err[:300] or out[-300:]}")
    return out


def _decode(data):
    if isinstance(data, str):
        return data
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8", errors="replace")


# ── setup ──────────────────────────────────────────────────────────────────

def setup(model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER, which_fn=None,
          runner=None, timeout=240):
    """`neuralosd pi --setup` — three honest checks:
      1. pi on PATH
      2. the model resolves for the provider (pi errors otherwise)
      3. a live one-shot ping actually answers
    """
    import shutil
    which_fn = which_fn or shutil.which
    if not which_fn("pi"):
        raise SystemExit(
            "error: the pi coding agent is not installed.\n"
            "    npm install -g @mariozechner/pi-coding-agent\n"
            "    (https://github.com/badlogic/pi-mono) then configure a "
            "provider:  pi auth")
    report = {"installed": True, "model_ok": False, "ping_ok": False,
              "model": model, "provider": provider, "error": None}
    try:
        _execute(pi_command(
            "Reply with exactly one word: ready", model=model,
            provider=provider) , timeout=timeout) if runner is None else None
        if runner is None:
            report["model_ok"] = report["ping_ok"] = True
        else:
            out = runner(pi_command("Reply with exactly one word: ready",
                                    model=model, provider=provider))
            report["model_ok"] = True
            report["ping_ok"] = bool(out and out.strip())
            if not report["ping_ok"]:
                report["error"] = "the live ping returned an empty answer"
    except SystemExit as e:
        msg = str(e)
        if "not found" in msg and "Model" in msg:
            report["error"] = (f"model {model!r} does not resolve for "
                               f"provider {provider!r} — run "
                               f"`pi --list-models {model.strip('*')}` to "
                               f"see what matches")
        else:
            report["error"] = msg[:300]
        return report
    return report


# ── the builder ────────────────────────────────────────────────────────────

def build_prompt(question, instance_dir, enrich=True):
    """The full builder task: the contract, the ask, and the ground rules.

    With enrich=True (default) the prompt carries the bridge's function
    inventory, the existing probe names, and a sample data row UP FRONT —
    pi needs fewer exploration steps, so the first shot lands more often."""
    cols = _describe_columns(instance_dir)
    extra = ""
    if enrich:
        inv = _inventory(instance_dir)
        if inv:
            extra = ("\nBRIDGE FUNCTIONS (call these via bridge.<name>):\n"
                     f"{inv}\n"
                     "EXISTING PROBES (do not duplicate):\n"
                     f"{_probe_names(instance_dir)}\n"
                     "DATA SAMPLE (first row, for reference only):\n"
                     f"{_data_sample(instance_dir)}\n")
    return (f"{_BUILDER_CONTRACT}\n"
            f"THE INSTANCE: {instance_dir}\n"
            f"Data columns: {cols}\n{extra}\n"
            f"TASK: {question}\n\n"
            f"Read probes.py first to match its existing style, then make "
            f"your edits. Reply with a one-paragraph summary.")


def _probe_names(instance_dir):
    import re
    path = os.path.join(instance_dir, "probes.py")
    try:
        src = open(path, encoding="utf-8").read()
        names = re.findall(r"^def (\w+)", src, flags=re.M)
        return ", ".join(names[:40])
    except OSError:
        return "(none)"


def _inventory(instance_dir):
    import re
    path = os.path.join(instance_dir, "bridge.py")
    try:
        src = open(path, encoding="utf-8").read()
        fns = re.findall(r"^def (\w+\([^:\n]*\)):", src, flags=re.M)
        # domain functions (no leading underscore) first — they are what
        # the new probe will actually call; internals follow if room remains
        fns = [f for f in fns if not f.startswith("_")] + \
              [f for f in fns if f.startswith("_")]
        return "\n".join(f"  def {f}" for f in fns[:60])
    except OSError:
        return "(bridge.py unreadable)"


def _data_sample(instance_dir, cap=1):
    """First row(s) of the source, as a literal — bounded, no probe calls."""
    try:
        src = open(os.path.join(instance_dir, "bridge.py"),
                   encoding="utf-8").read()
        m = re.search(r'SOURCE = "([^"]+)"', src)
        if not m:
            return "(dynamic source)"
        s = m.group(1)
        if s.endswith((".csv", ".tsv")):
            delim = "\t" if s.endswith(".tsv") else ","
            import csv as _csv
            with open(s, newline="", encoding="utf-8",
                      errors="replace") as fh:
                rows = list(_csv.DictReader(fh, delimiter=delim))[:cap]
            return json.dumps(rows, ensure_ascii=False)[:600]
        return "(non-csv source — read bridge.py)"
    except Exception:                                          # noqa: BLE001
        return "(sample unavailable)"


def _describe_columns(instance_dir):
    """Cheap column description from the bridge source (no probe calls)."""
    path = os.path.join(instance_dir, "bridge.py")
    try:
        src = open(path, encoding="utf-8").read()
        m = [line for line in src.splitlines() if line.startswith("SOURCE")]
        return m[0] if m else "see bridge.py"
    except OSError:
        return "see bridge.py"


def build_capability(question, instance_dir, model=DEFAULT_MODEL,
                     provider=DEFAULT_PROVIDER, timeout=900, execute=None,
                     session=None):
    """Natural language in, VERIFIED capability out (or a clean restore).

    1. snapshot every instance file
    2. pi works in the instance dir
    3. judge: forbidden files byte-identical, instance loads, both banks
       replay green
    4. failure -> restore everything pi touched; report honestly
    """
    from ._cmd._common import load_instance
    from .router import NoResults

    instance_dir = os.path.abspath(instance_dir)

    # baseline: replay the banks BEFORE pi so the gate measures the DELTA.
    # This runs FIRST because the baseline load also births the auto-traps —
    # the backup below must capture the post-birth state, or the builder
    # would flag its own setup as tampering.
    _bank_baseline = {}
    try:
        _inst0 = load_instance(instance_dir)
        for bank_name in ("golden.json", "traps.json"):
            bp = os.path.join(instance_dir, bank_name)
            if os.path.isfile(bp):
                try:
                    _bank_baseline[bank_name] = _inst0.golden_run(
                        json.loads(open(bp, encoding="utf-8").read()))
                except ValueError:
                    pass
    except SystemExit:
        _bank_baseline = {}

    backup = {}
    for name in os.listdir(instance_dir):
        p = os.path.join(instance_dir, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                backup[name] = fh.read()

    execute = execute or _execute
    argv = pi_command(build_prompt(question, instance_dir), model=model,
                      provider=provider, session=session)
    try:
        pi_out = execute(argv, cwd=instance_dir, timeout=timeout)
    except SystemExit as e:
        _restore(instance_dir, backup)
        return {"ok": False, "error": f"pi failed: {str(e)[:300]}",
                "restored": True}

    # 1. the banks are untouchable
    tampered = []
    for name in _FORBIDDEN:
        p = os.path.join(instance_dir, name)
        before = backup.get(name)
        now = open(p, "rb").read() if os.path.isfile(p) else None
        if before != now:
            tampered.append(name)
    if tampered:
        _restore(instance_dir, backup)
        return {"ok": False,
                "error": f"pi tampered with the banks: {', '.join(tampered)} "
                         f"— instance restored",
                "restored": True, "verified": False}

    # 2. the instance must load
    try:
        inst = load_instance(instance_dir)
    except SystemExit as e:
        _restore(instance_dir, backup)
        return {"ok": False, "error": f"instance no longer loads: "
                                      f"{str(e)[:200]} — instance restored",
                "restored": True, "verified": False}

    # 3. both banks replay green — gated on the DELTA. A bank that was
    # already red before pi ran is a pre-existing condition (reported as a
    # warning, never attributed to the builder); only failures the new
    # probe INTRODUCED block the delivery.
    problems, pre_existing = [], []
    for bank_name in ("golden.json", "traps.json"):
        bp = os.path.join(instance_dir, bank_name)
        if not os.path.isfile(bp):
            continue
        try:
            bank = json.loads(open(bp, encoding="utf-8").read())
        except ValueError:
            problems.append(f"{bank_name} is corrupt")
            continue
        baseline = _bank_baseline.get(bank_name)
        summary = inst.golden_run(bank)
        after_bad = summary["wrong"] + summary["answered_traps"]
        before_bad = None
        if baseline:
            before_bad = baseline["wrong"] + baseline["answered_traps"]
        if after_bad > (before_bad or 0):
            problems.append(f"{bank_name}: {after_bad - (before_bad or 0)} "
                            f"NEW failure(s) "
                            f"({before_bad or 0} pre-existing)")
        elif after_bad:
            pre_existing.append(f"{bank_name}: {after_bad} pre-existing "
                                f"failure(s) — not caused by this build")
    if problems:
        _restore(instance_dir, backup)
        return {"ok": False, "error": "; ".join(problems),
                "pre_existing": pre_existing,
                "restored": True, "verified": False,
                "pi_output": (pi_out or "")[-400:]}

    return {"ok": True, "verified": True, "restored": False,
            "pre_existing": pre_existing,
            "pi_output": (pi_out or "")[-400:]}


def _restore(instance_dir, backup):
    for name, data in backup.items():
        with open(os.path.join(instance_dir, name), "wb") as fh:
            fh.write(data)
    # files pi CREATED that were never in the snapshot: remove them (they
    # were never verified) — but only known instance-code names, never data
    for name in list(os.listdir(instance_dir)):
        if name not in backup and name.endswith((".py", ".json")) \
                and name not in ("ask_audit.jsonl", ".ask_cache.json",
                                 "gaps.json", "badge.json", "candidates.json",
                                 "synonym_proposals.json", "mothballed.json",
                                 "acl.json"):
            try:
                os.unlink(os.path.join(instance_dir, name))
            except OSError:
                pass


# ── the operator ───────────────────────────────────────────────────────────

def operator_prompt(question, instance_dir):
    """The closed command list — pi picks from the menu, the runtime
    verifies. Same philosophy as the probe menu itself."""
    return f"""You are operating a neuralosd instance at {instance_dir}.
You may run these neuralosd commands (subprocess) to investigate:
  neuralosd gaps ask_audit.jsonl --out gaps.json     # refusal patterns
  neuralosd slo --instance-dir .                     # refusal-rate SLO
  neuralosd golden --dir . --receipts                # the evidence chain
  neuralosd synonyms --instance-dir .                # trigger proposals
  neuralosd drift --instance-dir . --source <file>   # schema re-audit
  neuralosd mothball --instance-dir . --age-days 180 # menu hygiene
  neuralosd ask --instance-dir . "question"          # answer a question
  neuralosd fleet <dirs>                             # fleet health

Run only what you need (they are read-only except mothball --apply and
synonyms --apply — do NOT use --apply). Then answer the user's question in
plain language, citing the command output you relied on.

USER QUESTION: {question}
"""


def operate(question, instance_dir, model=DEFAULT_MODEL,
            provider=DEFAULT_PROVIDER, timeout=900, execute=None,
            session=None):
    instance_dir = os.path.abspath(instance_dir)
    execute = execute or _execute
    argv = pi_command(operator_prompt(question, instance_dir), model=model,
                      provider=provider, session=session)
    return execute(argv, cwd=instance_dir, timeout=timeout)


def resolve_pi_api_key(provider, runner=None):
    """The provider API key from the pi CLI's own credential store — the
    same source the one-shot pi run uses. None when unavailable."""
    import subprocess
    if runner is not None:
        try:
            out = runner(["pi", "auth", "print-api-key",
                          "--provider", provider])
            return (out or "").strip() or None
        except Exception:                                      # noqa: BLE001
            return None
    try:
        r = subprocess.run(["pi", "auth", "print-api-key",
                            "--provider", provider],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return _decode(r.stdout).strip() or None


def pi_durable_command(storage, model=DEFAULT_MODEL,
                       provider=DEFAULT_PROVIDER):
    """The node argv for the durable bridge (delegates to pidurable)."""
    from .pidurable import pi_durable_command as _cmd
    return _cmd(storage, model=model, provider=provider)


def build_durable(question, instance_dir, model=DEFAULT_MODEL,
                  provider=DEFAULT_PROVIDER, timeout=900, execute=None,
                  spawn=None, session=None, resume=False,
                  expect_routing=None):
    """Durable build: SAME verification pipeline as build_capability, but
    the conversation lives in <instance>/pi_durable.sqlite.

    request_id is STABLE (hash of instance+question): a retried or resumed
    build reuses the original submission — exactly-once across crashes. On
    verification failure the files are restored, but the conversation
    storage is KEPT: the reasoning survives for --resume."""
    from .pidurable import DurableBuilder, stable_request_id
    from ._cmd._common import load_instance
    from .router import NoResults

    instance_dir = os.path.abspath(instance_dir)
    rid = stable_request_id(instance_dir, question)

    # baseline BEFORE backup: the baseline load births the auto-traps, and
    # the backup must capture the post-birth state
    _bank_baseline = {}
    try:
        _inst0 = load_instance(instance_dir)
        for bank_name in ("golden.json", "traps.json"):
            bp = os.path.join(instance_dir, bank_name)
            if os.path.isfile(bp):
                try:
                    _bank_baseline[bank_name] = _inst0.golden_run(
                        json.loads(open(bp, encoding="utf-8").read()))
                except ValueError:
                    pass
    except SystemExit:
        _bank_baseline = {}

    backup = {}
    for name in os.listdir(instance_dir):
        p = os.path.join(instance_dir, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                backup[name] = fh.read()

    api_key = resolve_pi_api_key(provider)
    storage_path = os.path.join(instance_dir, "pi_durable.sqlite")
    pi_out = ""
    b = None
    try:
        b = DurableBuilder(storage_path, model=model, provider=provider,
                           spawn=spawn, cwd=instance_dir)
        if spawn is None:
            b.open(model=model, provider=provider, api_key=api_key)
        b.submit(build_prompt(question, instance_dir), request_id=rid)
        settled = b.wait(rid, timeout=timeout)
        pi_out = settled.get("text", "")
    except SystemExit as e:
        return {"ok": False, "error": str(e)[:300], "restored": False,
                "storage": storage_path}
    finally:
        if spawn is None and b is not None:
            try:
                b.close()
            except Exception:                                  # noqa: BLE001
                pass

    # verification — identical to the one-shot builder
    tampered = []
    for name in _FORBIDDEN:
        p = os.path.join(instance_dir, name)
        before = backup.get(name)
        now = open(p, "rb").read() if os.path.isfile(p) else None
        if before != now:
            tampered.append(name)
    if tampered:
        _restore(instance_dir, backup)
        return {"ok": False,
                "error": f"pi tampered with the banks: "
                         f"{', '.join(tampered)} — instance restored",
                "restored": True, "verified": False,
                "storage": storage_path}
    try:
        inst = load_instance(instance_dir)
    except SystemExit as e:
        _restore(instance_dir, backup)
        return {"ok": False, "error": f"instance no longer loads: "
                                      f"{str(e)[:200]} — restored",
                "restored": True, "verified": False,
                "storage": storage_path}
    problems, pre_existing = [], []
    for bank_name in ("golden.json", "traps.json"):
        bp = os.path.join(instance_dir, bank_name)
        if not os.path.isfile(bp):
            continue
        try:
            bank = json.loads(open(bp, encoding="utf-8").read())
        except ValueError:
            problems.append(f"{bank_name} is corrupt")
            continue
        baseline = _bank_baseline.get(bank_name)
        summary = inst.golden_run(bank)
        after_bad = summary["wrong"] + summary["answered_traps"]
        before_bad = (baseline["wrong"] + baseline["answered_traps"]) \
            if baseline else 0
        if after_bad > before_bad:
            problems.append(f"{bank_name}: {after_bad - before_bad} NEW "
                            f"failure(s)")
        elif after_bad:
            pre_existing.append(f"{bank_name}: {after_bad} pre-existing "
                                f"failure(s) — not caused by this build")

    closure_failures = []
    from .instance import selftest_ledger
    from .router import audit_source
    for q in (expect_routing or []):
        try:
            # build-closure routing checks are SELF-TEST traffic (v2.9.6):
            # the builder proving the menu routes must not write user rows
            with selftest_ledger(inst), audit_source("golden"):
                env = inst.ask(q, use_cache=False)
            routed = bool(env.get("results") not in (None, [], {}))
        except NoResults:
            routed = False
        if not routed:
            closure_failures.append(
                f"CLOSURE: the built capability does not answer {q!r} — "
                f"the question still refuses")
    if problems or closure_failures:
        _restore(instance_dir, backup)
        return {"ok": False,
                "error": "; ".join(problems + closure_failures),
                "pre_existing": pre_existing, "restored": True,
                "verified": False, "storage": storage_path,
                "pi_output": (pi_out or "")[-400:]}
    return {"ok": True, "verified": True, "restored": False,
            "pre_existing": pre_existing, "storage": storage_path,
            "request_id": rid, "pi_output": (pi_out or "")[-400:]}
