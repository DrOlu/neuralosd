import os, sys, json
def run(a):
    d = os.path.abspath(a.instance_dir)
    os.chdir(d); sys.path.insert(0, d)
    import ask
    sys.argv = ["ask.py"] + [a.question] if hasattr(a, 'question') else sys.argv
    ask.main() if hasattr(ask, 'main') else None
