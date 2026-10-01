def run(a):
    import subprocess, sys, os
    out = a.out or f"./{a.name}"
    scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "..", "..",
                           "agent-skills", "skills", "neuralos", "scripts")
    profile = os.path.join(out, "profile.json")
    os.makedirs(out, exist_ok=True)
    for step in [
        [sys.executable, os.path.join(scripts, "profile_data.py"),
         "--source", a.source, "--out", profile],
        [sys.executable, os.path.join(scripts, "gen_pydantic.py"),
         "--profile", profile, "--out", os.path.join(out, "models.py")],
        [sys.executable, os.path.join(scripts, "gen_needle_instance.py"),
         "--profile", profile, "--models", os.path.join(out, "models.py"),
         "--out", out, "--agent-name", a.name],
    ]:
        r = subprocess.run(step, capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-500:]); raise SystemExit(r.returncode)
    print(f"instance '{a.name}' generated at {out}")
