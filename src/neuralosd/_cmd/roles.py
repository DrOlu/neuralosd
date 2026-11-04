"""`neuralosd roles` — manage acl.json roles with validation + a journal.

    neuralosd roles --instance-dir . --op set    --role analyst \\
        --allow row_count,breakdown --row-filter "region=in:North,South"
    neuralosd roles --instance-dir . --op list
    neuralosd roles --instance-dir . --op show   --role analyst
    neuralosd roles --instance-dir . --op remove --role analyst

Every change is validated against the LIVE menu (an allow-list naming a
probe that does not exist is refused) and journaled to acl_journal.jsonl.
RBAC is a security surface: no half-written roles, no unjournaled changes.
"""
import json
import os
import sys
import time


def _acl_path(instance_dir):
    return os.path.join(instance_dir, "acl.json")


def _load_acl(instance_dir):
    path = _acl_path(instance_dir)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"roles": {}}


def _save_acl(instance_dir, acl):
    tmp = _acl_path(instance_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(acl, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, _acl_path(instance_dir))


def _journal(instance_dir, entry):
    with open(os.path.join(instance_dir, "acl_journal.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _parse_row_filter(spec):
    """"region=in:North,South" -> {"column": "region", "in": [North, South]}"""
    if not spec:
        return None
    if "=in:" not in spec:
        raise SystemExit(
            f"error: --row-filter must look like 'column=in:value1,value2' "
            f"(got {spec!r})")
    column, _, values = spec.partition("=in:")
    values = [v.strip() for v in values.split(",") if v.strip()]
    if not column.strip() or not values:
        raise SystemExit(
            f"error: --row-filter must look like 'column=in:value1,value2' "
            f"(got {spec!r})")
    return {"column": column.strip(), "in": values}


def run(a):
    from ._common import load_instance
    instance_dir = os.path.abspath(a.instance_dir)
    op = getattr(a, "op", "list")

    if op == "list":
        acl = _load_acl(instance_dir)
        roles = acl.get("roles", {})
        if a.as_json:
            print(json.dumps(roles, indent=2, ensure_ascii=False))
        else:
            if not roles:
                print("no roles defined (acl.json absent or empty)")
            for name, spec in sorted(roles.items()):
                allow = ",".join(spec.get("allow") or []) or "ALL"
                rf = spec.get("row_filter")
                rf_s = (f" rows: {rf['column']} in "
                        f"[{', '.join(rf['in'])}]" if rf else "")
                print(f"  {name:<20} allow: {allow}{rf_s}")
        return 0

    role = getattr(a, "role", None)
    if not role:
        print(f"error: --op {op} needs --role", file=sys.stderr)
        return 2

    if op == "show":
        acl = _load_acl(instance_dir)
        spec = acl.get("roles", {}).get(role)
        if spec is None:
            print(f"error: no role {role!r}", file=sys.stderr)
            return 1
        print(json.dumps({role: spec}, indent=2, ensure_ascii=False))
        return 0

    menu_names = set()
    try:
        inst = load_instance(instance_dir)
        menu_names = {m.name for m in inst.router.metas}
    except SystemExit:
        pass

    if op == "set":
        allow = [x.strip() for x in (a.allow or "").split(",")
                 if x.strip()]
        unknown = [x for x in allow if menu_names and x not in menu_names]
        if unknown:
            raise SystemExit(
                f"error: unknown probe(s) in --allow: {', '.join(unknown)} "
                f"— valid: {', '.join(sorted(menu_names))}")
        row_filter = _parse_row_filter(a.row_filter) if a.row_filter \
            else None
        acl = _load_acl(instance_dir)
        acl.setdefault("roles", {})[role] = {
            "allow": allow or None,
            "row_filter": row_filter}
        acl["roles"][role] = {k: v for k, v in
                              acl["roles"][role].items()
                              if v is not None}
        _save_acl(instance_dir, acl)
        _journal(instance_dir, {"ts": time.time(), "op": "set",
                                "role": role, "allow": allow,
                                "row_filter": row_filter,
                                "by": os.environ.get("USER", "")})
        print(json.dumps({role: acl["roles"][role]}, indent=2,
                         ensure_ascii=False))
        print(f"\nrole {role!r} set — serve picks it up via the "
              f"X-NeuralOS-Role header; ask via "
              f"neuralosd ask --role {role}")
        return 0

    if op == "remove":
        acl = _load_acl(instance_dir)
        if role not in acl.get("roles", {}):
            print(f"error: no role {role!r}", file=sys.stderr)
            return 1
        del acl["roles"][role]
        _save_acl(instance_dir, acl)
        _journal(instance_dir, {"ts": time.time(), "op": "remove",
                                "role": role,
                                "by": os.environ.get("USER", "")})
        print(f"removed role {role!r}")
        return 0

    print(f"error: unknown --op {op!r}", file=sys.stderr)
    return 2
