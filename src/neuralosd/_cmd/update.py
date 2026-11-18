"""`neuralosd update` — check for, and apply, a newer release.

    neuralosd update --check
    neuralosd update --yes
    neuralosd update --check --json

No silent background updater: the check runs only when asked, and an update
never starts unless confirmed (TTY prompt) or `--yes` is given.
"""
import json
import sys


def _confirm(info):
    if not sys.stdin.isatty():
        return False
    target = info.get("latest")
    print(f"Update neuralosd {info.get('current')} → {target} "
          f"({info.get('install')}, {info.get('channel')})?")
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def run(a):
    from .. import update as updater

    yes = bool(getattr(a, "yes", False))
    check_only = bool(getattr(a, "check", False))
    as_json = bool(getattr(a, "json", False))
    channel = getattr(a, "channel", "stable") or "stable"
    timeout = int(getattr(a, "timeout", 15) or 15)

    def finish(result, code=0):
        if as_json:
            result = dict(result)
            result.setdefault("ok", True)
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_human(result)
        return code

    try:
        result = updater.run(
            check_only=check_only, yes=yes,
            force=bool(getattr(a, "force", False)),
            channel=channel, timeout=timeout,
            confirm=lambda info: True if yes else _confirm(info))
    except updater.UpdateError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    return finish(result)


def _print_human(r):
    current, latest = r.get("current"), r.get("latest")
    action = r.get("action")
    kind = r.get("install")
    if action == "none" and not r.get("update_available"):
        print(f"neuralosd {current} is up to date "
              f"({kind}, {r.get('channel')}).")
        return
    if action == "none":
        print(f"neuralosd {current} → {latest} available "
              f"({kind}, {r.get('channel')}).")
        if r.get("plan"):
            print(f"  update with: {r['plan']}")
        print("  or run: neuralosd update --yes")
        return
    if action == "declined":
        print(f"neuralosd {current} → {latest} not applied. "
              "Re-run with --yes to update.")
        return
    if action == "deferred":
        print(f"Downloaded and verified {latest} → {r.get('path')}")
        print(r.get("message") or "")
        print("On Windows a running executable cannot be replaced; close "
              "neuralosd, rename the file over the old one, and restart.")
        return
    if action == "updated":
        if kind == "pip":
            print(f"Updated neuralosd to {latest}. Restart long-running "
                  "services to load the new version.")
        else:
            print(f"Updated neuralosd to {latest} at {r.get('path')}.")
            if r.get("backup"):
                print(f"  previous binary kept at {r['backup']}")
        return
    print(json.dumps(r, indent=2, sort_keys=True))
