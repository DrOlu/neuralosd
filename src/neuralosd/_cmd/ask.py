"""`neuralosd ask` — route one question through the instance."""
import json
import sys


def run(a):
    from ._common import load_instance
    from ..router import NoResults

    inst = load_instance(a.instance_dir, with_model=True)
    question = " ".join(a.question)
    try:
        env = inst.ask(question)
    except NoResults as e:
        env = getattr(e, "envelope", None) or {"question": question,
                                               "probe": None, "results": None}
        env.setdefault("note", "no probe matched")
    print(json.dumps(env, indent=2, ensure_ascii=False, default=str))