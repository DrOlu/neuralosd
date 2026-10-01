"""`neuralosd invariants` — structural property checks on an instance menu."""
import collections


def run(a):
    from ._common import load_instance

    inst = load_instance(a.dir)
    menu = inst.menu
    problems = []

    names = [m["name"] for m in menu]
    dupes = [n for n, c in collections.Counter(names).items() if c > 1]
    if dupes:
        problems.append(f"duplicate probe names: {dupes}")

    for m in menu:
        if not m.get("triggers"):
            problems.append(f"probe '{m['name']}' has no triggers")
        if not m.get("description"):
            problems.append(f"probe '{m['name']}' has no description")

    # every trigger token must be distinguishable enough (no empty menus)
    all_triggers = [t for m in menu for t in m.get("triggers", [])]
    trig_dupes = [t for t, c in collections.Counter(all_triggers).items() if c > 1]
    if trig_dupes:
        problems.append(f"triggers repeated across probes: {trig_dupes}")

    print(f"instance: {inst.name}")
    print(f"probes:   {len(menu)}")
    print(f"triggers: {len(all_triggers)}")
    if problems:
        print("\nINVARIANT VIOLATIONS:")
        for p in problems:
            print(f"  ✗ {p}")
        raise SystemExit(1)
    print("\nall invariants hold ✓")