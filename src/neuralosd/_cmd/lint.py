import json, sys
def run(a):
    from neuralosd.lint import TriggerLinter
    menu = json.load(open(a.menu))
    r = TriggerLinter(menu, top=a.top).lint()
    for h in r["hard"]: print(f"HARD {h}")
    for s in r["soft"]: print(f"soft {s}")
    raise SystemExit(1 if r["hard"] else 0)
