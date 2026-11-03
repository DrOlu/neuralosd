"""`neuralosd temporal` — durable execution for the loop, out of the box.

    neuralosd temporal --setup                      # CLI + server + SDK checks
    neuralosd temporal run-activity golden_run --instance-dir .
    neuralosd temporal worker --instance-dir . --task-queue neuralos
    neuralosd temporal schedule-create --cron "0 2 * * *" \\
        --task-queue neuralos --schedule-id neuralos-nightly

Prereqs are named honestly when missing — never guessed around.
"""
import json
import os
import sys


def run(a):
    from .. import durable

    if getattr(a, "setup", False):
        try:
            report = durable.setup()
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        print("\nready — run the loop durably:\n"
              f"  neuralosd temporal worker --instance-dir . "
              f"--task-queue neuralos\n"
              f"  neuralosd temporal schedule-create --cron '0 2 * * *' "
              f"--task-queue neuralos --schedule-id neuralos-nightly")
        return 0

    instance_dir = os.path.abspath(getattr(a, "instance_dir", "") or ".")
    if getattr(a, "run_activity", None):
        name = a.run_activity
        try:
            result = durable.run_activity(name, instance_dir,
                                          source=getattr(a, "source", None))
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    if getattr(a, "worker", False):
        try:
            worker = durable.build_worker(instance_dir,
                                          task_queue=a.task_queue)
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"worker up on queue {a.task_queue!r} — Ctrl+C to stop ...")
        try:
            worker.run()
        except KeyboardInterrupt:
            print("\nworker stopped")
        return 0

    if getattr(a, "schedule_create", False):
        runner = durable._default_runner
        cli = durable._which("temporal")
        if not cli:
            print("error: the Temporal CLI is not installed", file=sys.stderr)
            return 1
        args = durable.schedule_create_args(
            schedule_id=a.schedule_id, cron=a.cron,
            task_queue=a.task_queue, workflow_type=a.workflow_type)
        try:
            runner([cli] + args)
        except (Exception, SystemExit) as e:
            print(f"error: schedule create failed: {str(e)[:200]}",
                  file=sys.stderr)
            return 1
        print(f"schedule {a.schedule_id!r} created ({a.cron}) — "
              f"list: temporal schedule list")
        return 0

    print("nothing to do: use --setup, --run-activity <name>, --worker, or "
          "--schedule-create", file=sys.stderr)
    return 2
