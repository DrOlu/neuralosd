"""`neuralosd ask` — route one question through the instance."""
import json
import sys


def run(a):
    from ._common import load_instance
    from ..router import NoResults

    # v2.4.0 time travel: --as-of answers from a pinned snapshot
    if getattr(a, "as_of", None):
        from ..loop import ask_as_of
        env = ask_as_of(a.instance_dir, " ".join(a.question or []),
                        label=a.as_of)
        print(json.dumps(env, indent=2, ensure_ascii=False, default=str))
        return 0 if env.get("results") not in (None, [], {}) else 2

    inst = load_instance(a.instance_dir, with_model=getattr(a, "model", False),
                         role=getattr(a, "role", None))
    if getattr(a, "strict", False):
        # Refuse an answer that would have dropped a filter the user named,
        # rather than returning it with a `discarded` ledger.
        inst.router.strict_discards = True
    question = " ".join(a.question)
    # Dry run: the decision half of ask() with the execution half removed.
    # Nothing runs, nothing is cached, nothing is audited.
    if getattr(a, "explain", False):
        print(json.dumps(inst.router.dry_run(question), indent=2,
                         ensure_ascii=False, default=str))
        return 0
    try:
        env = inst.ask(question)
    except NoResults as e:
        env = getattr(e, "envelope", None) or {"question": question,
                                               "probe": None, "results": None}
        env.setdefault("note", "no probe matched")
    print(json.dumps(env, indent=2, ensure_ascii=False, default=str))
    # Exit 2 on a refusal so scripts and CI can tell an answer from a refusal.
    # Printing a JSON envelope and exiting 0 made a refusal look like success.
    if env.get("results") is None:
        raise SystemExit(2)