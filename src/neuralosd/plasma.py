"""Plasma EDR connector (v2.9.0) — read the events.jsonl that Plasma
produces on Windows nodes.

Plasma's strict PlasmaEvent contract: each row is a JSON object with
{ts, host, source, event_type, severity, ...}. The sensors that produce
these rows (WHIDS/Sysmon, krabsetw ETW tap, Event Log identity) all
converge on one events.jsonl per node.

This connector reads that store, validates each row, and normalizes to
the standard event shape so the generated probes work unchanged. Events
that fail validation are counted (`parse_errors`) — never silently
dropped.

For testing: `runner` is injectable (same pattern as logsources), so the
full flow is testable without a Windows node or a running Plasma.
"""
import json
import os
import re


def parse_plasma_event(line):
    """One PlasmaEvent JSONL row -> a validated event dict, or None.

    A row that fails validation is NOT an event: the caller counts it as
    a parse error (a hole), never silently drops it into the rows.
    """
    text = line.strip()
    if not text:
        return None
    try:
        row = json.loads(text)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    # normalize: the standard probe pipeline works on {ts, host, event_type,
    # severity, ...}. PlasmaEvent rows already carry these; we validate
    # rather than transform.
    required = ("ts", "host", "event_type")
    if any(not row.get(k) for k in required):
        return None
    return row


def parse_plasma_events(path, cap=None):
    """Read a Plasma events.jsonl store (bounded). Returns a dict:
    {"rows": [...], "parse_errors": n, "truncated": bool}
    """
    cap = cap or 10000
    rows, errors, truncated = [], 0, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ev = parse_plasma_event(line)
            if ev is None:
                if line.strip():
                    errors += 1
                continue
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(ev)
    return {"rows": rows, "parse_errors": errors, "truncated": truncated}


def plasma_rows(path, cap=None):
    """Convenience: just the rows (for probe functions)."""
    return parse_plasma_events(path, cap=cap)["rows"]


# ── Plasma 1.24+ driver ──────────────────────────────────────────────────
#
# Plasma publishes its whole CLI as `plasma help --json` (schema
# plasma.help/1): every command path, summary, arguments, examples and a
# SAFETY class. That catalog is the contract this connector drives. Nothing
# here builds a shell string: argv lists only, and a command must exist in
# the catalog before it can run.

PLASMA_SCHEMA = "plasma.help/1"
MAX_PLASMA_OUTPUT = 4 << 20
DEFAULT_PLASMA_BIN = "plasma"


class PlasmaError(Exception):
    """Plasma is missing, refused the command, or returned unusable data."""


def plasma_bin(value=None):
    return value or os.environ.get("PLASMA_BIN") or DEFAULT_PLASMA_BIN


def _command_prefix(bin_path):
    """A string executable, or an argv prefix (tests/launchers)."""
    if isinstance(bin_path, (list, tuple)):
        return [str(part) for part in bin_path]
    return [str(bin_path)]


def _run(argv, bin_path, timeout=60, cwd=None):
    import subprocess
    try:
        proc = subprocess.run(_command_prefix(bin_path) + list(argv),
                              capture_output=True,
                              text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise PlasmaError(f"Plasma executable not found: {bin_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PlasmaError(f"plasma {' '.join(argv)} timed out after "
                          f"{timeout}s") from exc
    except OSError as exc:
        raise PlasmaError(f"cannot run Plasma: {exc}") from exc
    out = proc.stdout or ""
    if len(out) > MAX_PLASMA_OUTPUT:
        out = out[:MAX_PLASMA_OUTPUT]
    return proc.returncode, out, (proc.stderr or "")


def load_catalog(bin_path=None, timeout=60):
    """Fetch and validate `plasma help --json`."""
    bin_path = plasma_bin(bin_path)
    rc, out, err = _run(["help", "--json"], bin_path, timeout=timeout)
    if rc != 0:
        raise PlasmaError(
            f"plasma help --json failed ({rc}): {err.strip()[:300]}")
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise PlasmaError(f"plasma help --json is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != PLASMA_SCHEMA:
        raise PlasmaError(
            f"unsupported Plasma help schema: {data.get('schema')!r} "
            f"(want {PLASMA_SCHEMA})")
    if not isinstance(data.get("commands"), list):
        raise PlasmaError("Plasma catalog has no commands list")
    return data


def catalog_index(catalog):
    """path tuple -> leaf command dict (only leaf, non-hidden commands)."""
    out = {}
    for cmd in catalog.get("commands") or []:
        if not isinstance(cmd, dict) or cmd.get("hidden"):
            continue
        if not cmd.get("leaf"):
            continue
        path = tuple(cmd.get("path") or [])
        if path:
            out[path] = cmd
    return out


def safety_class(command):
    """read_only | plan_gated | mutating, from Plasma's own safety string."""
    safety = (command.get("safety") or "").lower()
    if safety.startswith("read-only"):
        return "read_only"
    if "plan-gated" in safety:
        return "plan_gated"
    return "mutating"


def _option_index(command):
    out = {}
    for arg in command.get("arguments") or []:
        if arg.get("positional"):
            continue
        for flag in arg.get("option_strings") or []:
            out[flag] = arg
        dest = arg.get("dest")
        if dest:
            out[dest] = arg
    return out


def _arg_specs(command):
    """Neuralosd arg specs for a Plasma command's safe, typed options.

    Only enum (choices) and integer options are exposed: a free string from
    a natural-language question is not safe to place on a command line.
    Flags (store_true), paths and required free-form strings are omitted.
    """
    specs = {}
    for arg in command.get("arguments") or []:
        if arg.get("positional"):
            continue
        dest = arg.get("dest")
        flags = arg.get("option_strings") or []
        if not dest or not flags or dest in (
                "root", "events", "apply", "plan", "help", "reason"):
            continue
        if str(arg.get("nargs")) == "0":
            continue                       # boolean flag: no neuralosd type
        if arg.get("choices"):
            spec = {"type": "enum",
                    "values": list(arg["choices"]), "required": False}
        elif arg.get("type") == "int":
            spec = {"type": "integer", "min": 0, "max": 1_000_000,
                    "required": False}
        else:
            continue
        default = arg.get("default")
        if default not in (None, "", []):
            try:
                if spec["type"] == "integer":
                    default = int(default)
            except (TypeError, ValueError):
                default = None
            if default is not None:
                spec["default"] = default
        specs[dest] = spec
    return specs


def _generatable(command):
    if safety_class(command) != "read_only":
        return False
    for arg in command.get("arguments") or []:
        if arg.get("positional") and arg.get("required"):
            return False
        if (arg.get("required") and not arg.get("choices")
                and arg.get("type") != "int"):
            return False
    return True


ALIASES = {
    ("health",): ["is the estate healthy", "estate health", "plasma health",
                  "endpoint health"],
    ("coverage",): ["attack coverage", "att&ck coverage", "blind spots",
                    "detection coverage"],
    ("status",): ["plasma status", "bundle status"],
    ("control", "policy", "show"): ["agent policy", "effective policy",
                                    "show policy", "autonomy policy"],
    ("control", "audit", "verify"): ["verify audit", "verify the ledger",
                                     "audit chain", "ledger integrity"],
    ("control", "audit", "tail"): ["audit tail", "recent ledger",
                                   "recent audit"],
    ("control", "approvals", "list"): ["pending approvals",
                                       "list approvals", "approval queue"],
    ("control", "killswitch", "status"): ["kill switch", "killswitch status"],
    ("enforce", "pending"): ["pending containment", "awaiting approval",
                             "pending enforcement"],
    ("enforce", "status"): ["enforcement status", "containment policy"],
    ("prevent", "status"): ["prevention status", "prevent status"],
    ("prevent", "posture"): ["prevention posture", "hardening posture"],
    ("protect", "status"): ["protection posture", "protect status"],
    ("intercept", "status"): ["intercept status", "enforcement mode"],
    ("intercept", "rules", "list"): ["intercept rules", "list rules"],
    ("fim", "status"): ["file integrity", "fim status"],
    ("whids", "status"): ["whids status", "detection service"],
    ("canary", "status"): ["canary status", "decoys"],
    ("ring", "status"): ["ring membership", "ring status"],
    ("memory", "status"): ["memory scanner", "memory status"],
    ("identity", "schedule-status"): ["identity pull", "identity schedule"],
    ("fleet", "inventory"): ["fleet inventory", "host inventory"],
}


def _triggers(command):
    path = list(command.get("path") or [])
    words = " ".join(path)
    out = [words, "plasma " + words]
    if len(path) > 1:
        out.append(" ".join(path[-2:]))
    summary = (command.get("summary") or "").strip().rstrip(".")
    if summary:
        out.append(" ".join(summary.split()[:8]))
    out.extend(ALIASES.get(tuple(path), []))
    seen, unique = set(), []
    for trig in out:
        key = trig.lower()
        if trig and key not in seen:
            seen.add(key)
            unique.append(trig)
    return unique


def _coerce_params(command, params):
    """Validate params against the catalog and return argv fragments."""
    opts = _option_index(command)
    argv = []
    for name, value in (params or {}).items():
        arg = opts.get(name)
        if arg is None:
            raise PlasmaError(f"{command.get('command')}: unknown option "
                              f"{name!r}")
        dest = arg.get("dest")
        if dest in ("apply", "plan", "root", "events", "help"):
            raise PlasmaError(f"{dest} is controlled by the driver")
        if arg.get("positional"):
            raise PlasmaError("positional parameters are not supported")
        flag = (arg.get("option_strings") or [None])[0]
        if not flag:
            raise PlasmaError(f"{name}: no option string")
        if arg.get("choices") and value not in arg["choices"]:
            raise PlasmaError(f"{name}: must be one of {arg['choices']}")
        if arg.get("type") == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise PlasmaError(f"{name}: integer required")
        argv.extend([flag, str(value)])
    return argv


def plasma_call(command, params=None, bin_path=None, root=None, events=None,
                timeout=60):
    """Run ONE catalog command (read-only by default) and parse its JSON.

    Returns {"command", "ok", "data", "stdout", "stderr", "exit_code"}.
    """
    if safety_class(command) != "read_only":
        raise PlasmaError(
            f"{command.get('command')} is {safety_class(command)}; use "
            "plan_action/apply_action, not a probe")
    argv = list(command.get("path") or []) + _coerce_params(command, params)
    opts = _option_index(command)
    if root and "root" in opts:
        argv += ["--root", str(root)]
    if events and "events" in opts:
        argv += ["--events", str(events)]
    rc, out, err = _run(argv, plasma_bin(bin_path), timeout=timeout)
    data = None
    text = out.strip()
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return {"command": command.get("command"), "ok": rc == 0,
            "data": data,
            "stdout": None if data is not None else text[:4000],
            "stderr": err.strip()[:2000], "exit_code": rc}


def _probe_name(command):
    raw = "plasma_" + "_".join(command.get("path") or [])
    return re.sub(r"[^a-zA-Z0-9_]", "_", raw)


def build_probes(catalog, bin_path=None, root=None, events=None, groups=None,
                 include=(), exclude=("help",), timeout=60, max_probes=120):
    """Turn Plasma's catalog into neuralosd probes (read-only commands).

    Each probe runs exactly one Plasma command with typed arguments and
    returns its JSON. `include` adds explicit command paths (tuples); by
    default every generatable read-only command is exposed.
    """
    from .probe import probe as probe_dec

    index = catalog_index(catalog)
    wanted = {tuple(p) for p in include} if include else None
    out = []
    for path, command in index.items():
        if len(out) >= max_probes:
            break
        if path[0] in exclude:
            continue
        if groups and command.get("group") not in groups:
            continue
        if wanted is not None and path not in wanted:
            continue
        if not _generatable(command):
            continue
        specs = _arg_specs(command)
        name = _probe_name(command)

        def fn(_command=command, _bin=bin_path, _root=root,
               _events=events, _timeout=timeout, **kwargs):
            return plasma_call(_command, kwargs, bin_path=_bin, root=_root,
                               events=_events, timeout=_timeout)

        decorated = probe_dec(
            description=(f"Plasma {command.get('command')}: "
                         f"{command.get('summary')} "
                         f"({command.get('safety')})"),
            triggers=_triggers(command),
            args=specs, name=name, tier="plasma:cli")(fn)
        decorated._plasma_command = command.get("command")
        decorated._plasma_safety = command.get("safety")
        out.append(decorated)
    return out


def write_probe_module(directory, catalog, catalog_path="plasma_catalog.json",
                       root=None, events=None, bin_path=None,
                       groups=None, include=None):
    """Write probes.py + a saved catalog into ``directory``.

    The instance loads without running Plasma: the catalog is on disk and
    only probe EXECUTION calls the binary.
    """
    os.makedirs(directory, exist_ok=True)
    source = f'''"""Plasma probe menu generated by neuralosd (do not edit by hand).

Every probe is one read-only Plasma command from the saved catalog.
Regenerate with: neuralosd plasma init --out {directory} ...
"""
import json
import os

from neuralosd.plasma import build_probes

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, {catalog_path!r}), encoding="utf-8") as _fh:
    CATALOG = json.load(_fh)

PROBES = build_probes(
    CATALOG,
    bin_path={bin_path!r},
    root={root!r},
    events={events!r},
    groups={repr(list(groups) if groups else None)},
    include={repr([list(p) for p in include] if include else None)},
)
'''
    with open(os.path.join(directory, "probes.py"), "w",
              encoding="utf-8") as fh:
        fh.write(source)
    with open(os.path.join(directory, catalog_path), "w",
              encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, sort_keys=True)
    return os.path.join(directory, "probes.py")


# ── plan-gated actions ───────────────────────────────────────────────────

def _plans_path(root):
    return os.path.join(root or ".plasma", "neuralosd_plans.jsonl")


def _read_plans(root):
    path = _plans_path(root)
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _append_plan(root, row):
    path = _plans_path(root)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _plan_id_from(data):
    if not isinstance(data, dict):
        return None
    for key in ("plan", "plan_id", "approval", "approval_id"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, dict):
            inner = value.get("id") or value.get("plan_id")
            if isinstance(inner, int):
                return inner
    return None


def plan_action(command, params=None, bin_path=None, root=None, events=None,
                timeout=60):
    """Dry-run one plan-gated Plasma command; record a local plan.

    Returns {"plan_id", "command", "params", "plasma", "plasma_plan_id"}.
    Nothing is executed: Plasma's own dry-run decides what it would do.
    """
    if safety_class(command) != "plan_gated":
        raise PlasmaError(
            f"{command.get('command')} is not plan-gated; refusing to plan")
    argv = list(command.get("path") or []) + _coerce_params(command, params)
    opts = _option_index(command)
    if root and "root" in opts:
        argv += ["--root", str(root)]
    if events and "events" in opts:
        argv += ["--events", str(events)]
    rc, out, err = _run(argv, plasma_bin(bin_path), timeout=timeout)
    data = None
    if out.strip():
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    if rc != 0:
        raise PlasmaError(
            f"plasma {command.get('command')} dry-run failed ({rc}): "
            f"{(err or out).strip()[:300]}")
    import time as _time
    import uuid as _uuid
    plan_id = "plp_" + _uuid.uuid4().hex[:12]
    row = {"plan_id": plan_id, "command": command.get("command"),
           "params": dict(params or {}), "root": root, "events": events,
           "plasma_plan_id": _plan_id_from(data),
           "created": _time.time(), "applied": None,
           "dry_run": data if data is not None else out.strip()[:4000]}
    _append_plan(root, row)
    return row


def find_plan(root, plan_id):
    # The applied record is APPENDED; the newest row for an id is the truth.
    for row in reversed(_read_plans(root)):
        if row.get("plan_id") == plan_id:
            return row
    return None


def apply_action(plan_id, catalog, bin_path=None, root=None, timeout=120):
    """Execute a recorded plan with Plasma's --apply (and --plan when known)."""
    row = find_plan(root, plan_id)
    if row is None:
        raise PlasmaError(f"unknown plasma plan: {plan_id}")
    if row.get("applied"):
        raise PlasmaError(f"plan {plan_id} was already applied")
    command = catalog_index(catalog).get(tuple(row["command"].split()))
    if command is None:
        raise PlasmaError(f"plan command no longer in the catalog: "
                          f"{row['command']}")
    if safety_class(command) != "plan_gated":
        raise PlasmaError("refusing to apply a non-plan-gated command")
    argv = list(command["path"]) + _coerce_params(command, row.get("params"))
    opts = _option_index(command)
    if row.get("root") and "root" in opts:
        argv += ["--root", row["root"]]
    if row.get("events") and "events" in opts:
        argv += ["--events", row["events"]]
    argv.append("--apply")
    if row.get("plasma_plan_id") is not None and "plan" in opts:
        argv += ["--plan", str(row["plasma_plan_id"])]
    rc, out, err = _run(argv, plasma_bin(bin_path), timeout=timeout)
    data = None
    if out.strip():
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    if rc != 0:
        raise PlasmaError(
            f"plasma apply failed ({rc}): {(err or out).strip()[:300]}")
    row["applied"] = {"ts": __import__("time").time(),
                      "result": data if data is not None
                      else out.strip()[:4000]}
    _append_plan(root, row)
    return row
