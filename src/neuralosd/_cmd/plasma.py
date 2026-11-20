"""`neuralosd plasma` — drive Plasma through its published catalog.

    neuralosd plasma catalog --read-only
    neuralosd plasma init --out ./plasma-estate --root C:\\ProgramData\\Plasma
    neuralosd plasma probes
    neuralosd plasma call health
    neuralosd plasma plan "enforce run"
    neuralosd plasma apply plp_...

Read-only commands become neuralosd probes. Plan-gated commands are two-step:
`plan` records Plasma's dry-run, `apply` executes exactly that recorded plan.
There is no arbitrary execution: every command must exist in
`plasma help --json`, and a command's own safety class decides what it may do.
"""
import json
import sys


def _params(values):
    out = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"error: --param expects key=value (got {item!r})")
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def _command(catalog, words):
    from .. import plasma
    # Accept both `plasma plan enforce run` and `plasma plan "enforce run"`.
    path = tuple(" ".join(words).split())
    cmd = plasma.catalog_index(catalog).get(path)
    if cmd is None:
        raise SystemExit(f"error: no Plasma command {' '.join(path)!r} "
                         "(see: neuralosd plasma catalog)")
    return cmd


def run(a):
    from .. import plasma
    try:
        return _run(a)
    except plasma.PlasmaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run(a):
    from .. import plasma

    cmd_name = getattr(a, "plasma_cmd", None) or "catalog"
    bin_path = getattr(a, "plasma_bin", None)
    timeout = int(getattr(a, "timeout", 60) or 60)
    as_json = bool(getattr(a, "json", False))

    if cmd_name == "init":
        catalog = plasma.load_catalog(bin_path, timeout=timeout)
        groups = getattr(a, "group", None) or None
        include = [tuple(x.split()) for x in (getattr(a, "include", None) or [])]
        out = getattr(a, "out", None) or "plasma-instance"
        path = plasma.write_probe_module(
            out, catalog,
            root=getattr(a, "root", None) or ".plasma",
            events=getattr(a, "events", None),
            bin_path=bin_path,
            groups=groups, include=include or None)
        probes = plasma.build_probes(
            catalog, bin_path=bin_path, groups=groups,
            include=include or None)
        print(json.dumps({"instance": out, "probes": len(probes),
                          "probes_py": path,
                          "catalog_version": catalog.get("version")},
                         indent=2))
        print(f"\nAsk it:  neuralosd ask --instance-dir {out} "
              "\"is the estate healthy\"")
        return 0

    catalog = plasma.load_catalog(bin_path, timeout=timeout)
    index = plasma.catalog_index(catalog)

    if cmd_name == "catalog":
        rows = []
        for path, cmd in index.items():
            safety = plasma.safety_class(cmd)
            if getattr(a, "read_only", False) and safety != "read_only":
                continue
            if getattr(a, "plan_gated", False) and safety != "plan_gated":
                continue
            if getattr(a, "group", None) and cmd.get("group") not in a.group:
                continue
            rows.append({"command": cmd.get("command"), "safety": safety,
                         "group": cmd.get("group"),
                         "summary": cmd.get("summary")})
        if as_json:
            print(json.dumps({"version": catalog.get("version"),
                              "commands": rows}, indent=2))
        else:
            for row in rows:
                print(f"{row['command']:38s} {row['safety']:12s} "
                      f"{row['summary']}")
            print(f"\n{len(rows)} commands "
                  f"(Plasma {catalog.get('version')})")
        return 0

    if cmd_name == "probes":
        probes = plasma.build_probes(
            catalog, bin_path=bin_path,
            groups=getattr(a, "group", None) or None,
            include=[tuple(x.split()) for x in
                     (getattr(a, "include", None) or [])] or None)
        rows = [{"name": p._probe.name,
                 "command": getattr(p, "_plasma_command", None),
                 "triggers": p._probe.triggers,
                 "args": p._probe.args,
                 "description": p._probe.description}
                for p in probes]
        if as_json:
            print(json.dumps({"count": len(rows), "probes": rows}, indent=2))
        else:
            for p in probes:
                print(f"{p._probe.name:42s} {p._probe.description[:80]}")
            print(f"\n{len(probes)} read-only probes")
        return 0

    if cmd_name == "call":
        cmd = _command(catalog, getattr(a, "plasma_command"))
        if plasma.safety_class(cmd) != "read_only":
            print(f"error: {cmd.get('command')} is "
                  f"{plasma.safety_class(cmd)}; use `plasma plan` and "
                  "`plasma apply`", file=sys.stderr)
            return 2
        result = plasma.plasma_call(
            cmd, _params(getattr(a, "param", None)), bin_path=bin_path,
            root=getattr(a, "root", None), events=getattr(a, "events", None),
            timeout=timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result.get("ok") else 1

    if cmd_name in ("pending", "health"):
        name = (("enforce", "pending") if cmd_name == "pending"
                else ("health",))
        cmd = _command(catalog, name)
        result = plasma.plasma_call(
            cmd, {}, bin_path=bin_path,
            root=getattr(a, "root", None), timeout=timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0 if result.get("ok") else 1

    if cmd_name == "plan":
        cmd = _command(catalog, getattr(a, "plasma_command"))
        row = plasma.plan_action(
            cmd, _params(getattr(a, "param", None)), bin_path=bin_path,
            root=getattr(a, "root", None), events=getattr(a, "events", None),
            timeout=timeout)
        print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
        print(f"\nReview the dry-run, then: neuralosd plasma apply "
              f"{row['plan_id']} --root {row.get('root') or '.plasma'}")
        return 0

    if cmd_name == "apply":
        row = plasma.apply_action(
            getattr(a, "plasma_plan_id"), catalog, bin_path=bin_path,
            root=getattr(a, "root", None), timeout=max(timeout, 120))
        print(json.dumps(row, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"error: unknown plasma command {cmd_name}", file=sys.stderr)
    return 2
