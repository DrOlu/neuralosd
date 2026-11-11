#!/usr/bin/env python3
"""E7 differential gate check — published binary vs wheel, one gate decision.

Background: v2.9.10 shipped a cwd bug — router.json (the calibrated
floor/margin) was read from the process CWD only, while `calibrate` writes it
into the INSTANCE directory. The exact same build then answered differently
depending on which directory it was launched from ("list users in the
wallet tier": refused below_floor with cwd=instance, ANSWERED with
cwd=parent, floor silently defaulted). Two different launches of the same
binary reproduce it; a wheel and a binary from different directories
reproduce it too — which is why it was first reported as
"binary vs wheel disagree on the routing gate".

This script builds a small fixture instance, calibrates a floor ABOVE the
question's natural score, then asks the SAME question of the published
binary and the wheel FROM A DIRECTORY THAT IS NOT THE INSTANCE DIRECTORY
(the regression direction). Post-fix both builds must read the instance's
router.json and refuse identically (below_floor); the pre-fix binary
answered. It also asserts the gate decision path (NEURALOSD_GATE_TRACE=1)
is published by BOTH builds — the two-line diff that makes any future
binary/wheel divergence visible immediately.

Usage: differential_gate_check.py <path-to-binary> [--wheel-python python]
Exits 0 on parity, 1 on divergence.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CSV = "user_id,name,tier,signup_date\n" + "".join(
    f"{i},user{i},{tier},2025-01-{i:02d}\n"
    for i, tier in enumerate(["wallet", "savings", "checking"] * 4, start=1))
QUESTION = "list the distinct tiers"


def _first_json(text):
    """First JSON object in mixed stdout (human notices may precede it)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            d, _ = dec.raw_decode(text[m.start():])
            if isinstance(d, dict) and ("mode" in d or "refusal_reason" in d):
                return d
        except ValueError:
            continue
    return None


def ask(cmd, cwd, trace=False):
    env = dict(os.environ)
    if trace:
        env["NEURALOSD_GATE_TRACE"] = "1"
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=600, env=env)
    env_json = _first_json(p.stdout)
    if env_json is None:
        print(f"FAIL: no ask envelope in output (rc={p.returncode})\n"
              f"stdout: {(p.stdout or '')[-500:]}\n"
              f"stderr: {(p.stderr or '')[-500:]}", file=sys.stderr)
        sys.exit(1)
    trace_line = None
    for line in (p.stderr or "").splitlines():
        if line.startswith("gate_trace:"):
            trace_line = line.split("gate_trace:", 1)[1].strip()
    return env_json, trace_line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--wheel-python", default=sys.executable)
    args = ap.parse_args()

    binary = os.path.abspath(args.binary)
    scratch = tempfile.mkdtemp(prefix="e7diff-")
    inst = os.path.join(scratch, "ci-diff")
    try:
        # init reads --source as a path relative to cwd
        with open(os.path.join(scratch, "users.csv"), "w",
                  encoding="utf-8") as fh:
            fh.write(CSV)
        r = subprocess.run([binary, "init", "--source", "users.csv",
                            "--name", "ci-diff"],
                           cwd=scratch, capture_output=True, text=True,
                           timeout=600)
        if r.returncode != 0:
            print(f"FAIL: binary init failed: {r.stderr[-500:]}",
                  file=sys.stderr)
            sys.exit(1)

        # Phase 1: calibrate the floor ABOVE the question's natural score so
        # the gate must fire (data-independent: read the score first — a
        # refusal envelope carries the score vector, an answered one may not).
        with open(os.path.join(inst, "router.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"floor": 999}, fh)
        env0, _ = ask([args.wheel_python, "-m", "neuralosd", "ask",
                       "--instance-dir", inst, QUESTION], scratch)
        top = max((s.get("score", 0) for s in env0.get("scores") or []),
                  default=0)
        floor = float(top) + 5.0
        with open(os.path.join(inst, "router.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"floor": floor}, fh)

        # Phase 2: the SAME question through BOTH builds, launched from the
        # PARENT directory (not the instance dir) — the regression direction.
        wheel_cmd = [args.wheel_python, "-m", "neuralosd", "ask",
                     "--instance-dir", inst, QUESTION]
        bin_cmd = [binary, "ask", "--instance-dir", inst, QUESTION]
        cache = os.path.join(inst, ".ask_cache.json")
        results = {}
        for label, cmd in (("wheel", wheel_cmd), ("binary", bin_cmd)):
            if os.path.exists(cache):
                os.remove(cache)
            env, trace = ask(cmd, scratch, trace=True)
            results[label] = (env.get("mode"), env.get("refusal_reason"),
                              env.get("probe"))
            decision = None
            if trace:
                try:
                    decision = json.loads(trace).get("decision")
                except ValueError:
                    pass
            print(f"  {label:6s} mode={env.get('mode')} "
                  f"reason={env.get('refusal_reason')} "
                  f"probe={env.get('probe')} gate_decision={decision} "
                  f"floor={floor} top={top}")
            if decision != "below_floor":
                print(f"FAIL: {label} gate decision path not published as "
                      f"below_floor (trace={trace})", file=sys.stderr)
                sys.exit(1)

        if results["wheel"] != results["binary"]:
            print(f"FAIL: binary and wheel DISAGREE on the gate decision:\n"
                  f"  wheel : {results['wheel']}\n"
                  f"  binary: {results['binary']}", file=sys.stderr)
            sys.exit(1)
        if results["wheel"][0] != "refused" or \
                results["wheel"][1] != "below_floor":
            print(f"FAIL: expected identical below_floor refusals, got "
                  f"{results['wheel']}", file=sys.stderr)
            sys.exit(1)
        print(f"PARITY OK: wheel and published binary both refuse "
              f"below_floor from cwd={scratch} (floor {floor} honoured "
              f"from the instance directory)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
