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
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            self.entries = json.load(open(self.schedules_file, encoding="utf-8"))
        except Exception:
            self.entries = []

    def _save(self):
        with self._lock:
            json.dump(self.entries, open(self.schedules_file, "w"),
                      indent=2, ensure_ascii=False)

    def add(self, name: str, every_s: int, instance: str, question: str):
        entry = {"name": name, "every_s": every_s, "instance": instance,
                 "question": question, "next_due": time.time() + every_s,
                 "runs": 0}
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
                    self._run(e)
                    with self._lock:
                        e["next_due"] = time.time() + e["every_s"]
                        e["runs"] = e.get("runs", 0) + 1
                    self._save()
            self._stop.wait(2)

    def _run(self, e):
        try:
            env = self.ask_fn(e["instance"], e["question"])
            out = os.path.join(self.out_dir, f"{e['name']}.json")
            json.dump({"name": e["name"], "ts": time.time(),
                       "question": e["question"], "result": env},
                      open(out, "w"), indent=1, ensure_ascii=False,
                      default=str)
            print(f"[scheduler] ran {e['name']} -> {out}", flush=True)
        except Exception as exc:
            print(f"[scheduler] {e['name']} failed: {str(exc)[:200]}",
                  flush=True)

    def stop(self):
        self._stop.set()
