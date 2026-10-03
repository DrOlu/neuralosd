"""`neuralosd ask` — route one question through the instance."""
import json
import sys


def run(a):
    from ._common import load_instance
    from ..router import NoResults

    inst = load_instance(a.instance_dir, with_model=getattr(a, "model", False))
    if getattr(a, "strict", False):
        # Refuse an answer that would have dropped a filter the user named,
        # rather than returning it with a `discarded` ledger.
        inst.router.strict_discards = True
    question = " ".join(a.question)
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