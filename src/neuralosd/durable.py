"""Temporal durable execution for the neuralosd loop (v2.5.0).

The loop's recurring work — golden bank replay, gap mining, shadow
evaluation, snapshot pinning, drift audit — becomes DURABLE ACTIVITIES:
plain, deterministic, JSON-able functions over an instance directory. They
are unit-testable with NO Temporal server; the Temporal wiring (worker +
workflow + schedules) is a thin, lazily-imported shell over them.

Out of the box:
  neuralosd temporal --setup                     # CLI + server + SDK checks
  neuralosd temporal run-activity golden_run --instance-dir .
  neuralosd temporal worker --instance-dir . --task-queue neuralos
  neuralosd temporal schedule-create --cron "0 2 * * *" --task-queue neuralos

Prereqs, each named honestly when missing:
  Temporal CLI   https://github.com/temporalio/temporal (brew install
                 temporal / curl -sSf https://temporal.download/cli.sh | sh)
  local server   temporal server start-dev
  python SDK     pip install temporalio
"""
import json
import os
import time


# ── the activities: plain functions, no Temporal import anywhere ──────────

def _golden_run(instance_dir, **params):
    from ._cmd._common import load_instance
    inst = load_instance(instance_dir)
    golden_path = os.path.join(instance_dir, "golden.json")
    bank = json.loads(open(golden_path, encoding="utf-8").read()) \
        if os.path.isfile(golden_path) else {"items": []}
    summary = inst.golden_run(bank)
    summary["instance"] = os.path.basename(instance_dir.rstrip("/"))
    summary["ts"] = time.time()
    return summary


def _gaps_mine(instance_dir, **params):
    from .loop import cluster_gaps
    audit_path = os.path.join(instance_dir, "ask_audit.jsonl")
    gaps = []
    if os.path.isfile(audit_path):
        with open(audit_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("results") in (None, [], {}):
                    gaps.append({"question": r.get("question")})
    clusters = cluster_gaps(gaps)
    out_path = os.path.join(instance_dir, "gaps.json")
    blob = {"source": audit_path, "count": len(gaps), "clusters": clusters}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2, ensure_ascii=False)
    return {"count": len(gaps), "clusters": len(clusters), "out": out_path}


def _shadow_evaluate(instance_dir, **params):
    from .derived import load as load_derived
    from .loop import shadow_evaluate
    results = {}
    for m in load_derived(os.path.join(instance_dir, "derived.json")):
        if m.shadow:
            results[m.name] = shadow_evaluate(instance_dir, m.name)
    return {"evaluated": results, "ts": time.time()}


def _snapshot_pin(instance_dir, **params):
    from .loop import pin_snapshot
    label = params.get("label") or f"auto-{time.strftime('%Y%m%d-%H%M%S')}"
    return pin_snapshot(instance_dir, label)


def _drift_audit(instance_dir, source=None, **params):
    if not source:
        return {"error": "drift_audit needs a --source to re-profile"}
    from .loop import audit_drift
    return audit_drift(instance_dir, source)


ACTIVITIES = {
    "golden_run": _golden_run,
    "gaps_mine": _gaps_mine,
    "shadow_evaluate": _shadow_evaluate,
    "snapshot_pin": _snapshot_pin,
    "drift_audit": _drift_audit,
}

LOOP_ACTIVITIES = ["gaps_mine", "shadow_evaluate", "golden_run",
                   "snapshot_pin"]


def run_activity(name, instance_dir, **params):
    fn = ACTIVITIES.get(name)
    if fn is None:
        raise SystemExit(f"error: unknown activity {name!r} — known: "
                         f"{', '.join(sorted(ACTIVITIES))}")
    return fn(instance_dir, **params)


# ── the Temporal shell (lazy temporalio, honest errors) ───────────────────

def temporalio_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("temporalio") is not None


def build_worker(instance_dir, task_queue="neuralos", host="localhost:7233",
                 namespace="default"):
    """Build a Temporal worker that runs the loop activities for one
    instance. Needs the temporalio SDK — named honestly when missing."""
    if not temporalio_available():
        raise SystemExit(
            "error: durable workers need the temporalio SDK — "
            "pip install temporalio\n"
            "    and a Temporal server:  temporal server start-dev\n"
            "    CLI:                    brew install temporal  "
            "(or https://github.com/temporalio/temporal)")
    try:
        import asyncio
        from temporalio.client import Client
        from temporalio.worker import Worker
        from temporalio import workflow

        activities = [make_temporal_activity(name, instance_dir)
                      for name in LOOP_ACTIVITIES]

        @workflow.defn
        class NeuralosLoop:
            """Run the loop's recurring activities in order, durably."""

            @workflow.run
            async def run(self, params: dict = None) -> dict:
                out = {}
                for name in LOOP_ACTIVITIES:
                    out[name] = await workflow.execute_activity(
                        name, start_to_close_timeout=__import__(
                            "datetime").timedelta(minutes=30))
                return out

        workflow_defs = [NeuralosLoop]

        async def _build():
            client = await Client.connect(host, namespace=namespace)
            return Worker(client, task_queue=task_queue,
                          workflows=workflow_defs, activities=activities)

        return asyncio.get_event_loop().run_until_complete(_build())
    except ImportError as e:
        raise SystemExit(f"error: temporalio import failed: {e}\n"
                         f"    pip install temporalio") from e


def make_temporal_activity(name, instance_dir):
    """Wrap a loop activity as a Temporal activity callable (registered by
    name so the workflow can schedule it)."""
    def _activity():
        return run_activity(name, instance_dir)
    _activity.__name__ = name
    return _activity


def schedule_create_args(schedule_id, cron, task_queue="neuralos",
                         workflow_type="neuralos_loop"):
    """The exact Temporal CLI call for a recurring loop schedule."""
    return ["schedule", "create",
            "--schedule-id", schedule_id,
            "--cron", cron,
            "--task-queue", task_queue,
            "--workflow-type", workflow_type]


def _which(name):
    import shutil
    return shutil.which(name)


def setup(which_fn=None, runner=None, has_temporalio=None):
    """`neuralosd temporal --setup` — three checks, each honest:
      1. the Temporal CLI on PATH
      2. a reachable server (namespace describe)
      3. the temporalio Python SDK importable
    """
    import shutil
    which_fn = which_fn or shutil.which
    cli = which_fn("temporal")
    if not cli:
        raise SystemExit(
            "error: the Temporal CLI is not installed.\n"
            "    macOS:   brew install temporal\n"
            "    linux:   curl -sSf https://temporal.download/cli.sh | sh\n"
            "    then:    temporal server start-dev   # local dev server")
    report = {"cli": True, "server": False, "temporalio": False}
    runner = runner or _default_runner
    try:
        runner([cli, "operator", "namespace", "describe", "default"])
        report["server"] = True
    except (Exception, SystemExit) as e:
        raise SystemExit(
            f"error: the Temporal CLI is installed but no reachable server "
            f"answered ({str(e)[:120]}).\n"
            f"    start a local one:  temporal server start-dev") from e
    if has_temporalio is None:
        has_temporalio = temporalio_available()
    report["temporalio"] = bool(has_temporalio)
    if not report["temporalio"]:
        print("note: the temporalio Python SDK is not installed — workers "
              "need it:  pip install temporalio")
    return report


def _default_runner(cmd, timeout=30):
    import subprocess
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or b"").decode("utf-8", errors="replace").strip()[:200]
            or f"exit {r.returncode}")
    return (r.stdout or b"").decode("utf-8", errors="replace")
