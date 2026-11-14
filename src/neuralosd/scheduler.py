"""Scheduler — interval-based asks on a background thread.

Entries: [{name, every_s, instance, question, webhook?}]. Due entries run
through the router; results land in state/scheduled/<name>.json. Persisted
to state/schedules.json across restarts. (cron expressions: M3+)
"""
import json
import os
import threading
import time


class Scheduler:
    def __init__(self, state_dir: str, ask_fn):
        self.state_dir = state_dir
        self.out_dir = os.path.join(state_dir, "scheduled")
        os.makedirs(self.out_dir, exist_ok=True)
        self.schedules_file = os.path.join(state_dir, "schedules.json")
        self.ask_fn = ask_fn
        self.entries = []
        self._stop = threading.Event()
        # Re-entrant: remove() holds the lock and calls _save(), which also
        # takes it. A plain Lock deadlocked every remove() call.
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        try:
            with open(self.schedules_file, encoding="utf-8") as fh:
                self.entries = json.load(fh)
        except Exception:
            self.entries = []

    def _save(self):
        with self._lock:
            # Atomic: a crash mid-write must not truncate schedules.json.
            tmp = self.schedules_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.entries, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.schedules_file)

    def add(self, name: str, every_s: int, instance: str, question: str):
        # The name is a filename component (`scheduled/<name>.json`).
        if (not isinstance(name, str) or not name or len(name) > 80
                or name in (".", "..")
                or any(c in name for c in "/\\")
                or any(ord(c) < 32 for c in name)):
            raise ValueError(f"invalid schedule name {name!r}")
        try:
            every_s = max(1, int(every_s))
        except (TypeError, ValueError):
            raise ValueError(f"invalid every_s {every_s!r}")
        entry = {"name": name, "every_s": every_s, "instance": instance,
                 "question": question, "next_due": time.time() + every_s,
                 "runs": 0, "last_run": None, "last_status": None,
                 "last_error": None}
        with self._lock:
            self.entries = [e for e in self.entries if e["name"] != name]
            self.entries.append(entry)
        self._save()
        return entry

    def remove(self, name: str):
        with self._lock:
            before = len(self.entries)
            self.entries = [e for e in self.entries if e["name"] != name]
            self._save()
            return before - len(self.entries)

    def list(self):
        with self._lock:
            return list(self.entries)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            for e in list(self.entries):
                if now >= e.get("next_due", 0):
                    status, error = self._run(e)
                    with self._lock:
                        e["next_due"] = time.time() + e["every_s"]
                        e["runs"] = e.get("runs", 0) + 1
                        e["last_run"] = time.time()
                        e["last_status"] = status
                        e["last_error"] = error
                    self._save()
            self._stop.wait(2)

    def _run(self, e):
        """Run one schedule; return (status, error) so the entry records the
        outcome instead of only incrementing a counter."""
        try:
            env = self.ask_fn(e["instance"], e["question"])
            out = os.path.join(self.out_dir, f"{e['name']}.json")
            tmp = out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"name": e["name"], "ts": time.time(),
                           "question": e["question"], "result": env},
                          fh, indent=1, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, out)
            print(f"[scheduler] ran {e['name']} -> {out}", flush=True)
            return "ok", None
        except Exception as exc:
            msg = str(exc)[:200]
            print(f"[scheduler] {e['name']} failed: {msg}", flush=True)
            return "error", msg

    def stop(self):
        self._stop.set()
