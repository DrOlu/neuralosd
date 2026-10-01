def run(a):
    import sys, subprocess, os
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "tools", "diff.py")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "..",
                              "agent-skills", "skills", "neuralos", "scripts",
                              "diff.py")
    if os.path.exists(script):
        raise SystemExit(subprocess.call([sys.executable, script] + sys.argv[1:]))
    print(f"tool script not found: {script}")
    raise SystemExit(1)
