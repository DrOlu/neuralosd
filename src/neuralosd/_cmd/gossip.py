"""`neuralosd mesh gossip-push` / `gossip-pull` — the federation protocol.

    neuralosd mesh gossip-push --gateway http://127.0.0.1:3000 \\
        --peers "edge-a,edge-b" --origin my-edge [--audit ask_audit.jsonl]
    neuralosd mesh gossip-pull --inbox ./gossip_inbox --instance-dir .

Push publishes the LOCAL fingerprint blob (hashes + counts, never the
questions) to trusted peers via the gateway mailbox. Pull collects blobs
peers left in the inbox directory, validates them, and merges into the
gossip state that `gaps --federation-dir` reads for mesh counts.
"""
import json
import os
import sys


def run(a):
    from ..loop import (export_fingerprints, gossip_collect, gossip_push,
                        cluster_gaps)

    if getattr(a, "gossip_push", False):
        from ..synapse import SynapseHTTP
        transport = SynapseHTTP(gateway=a.gateway, timeout=a.timeout)
        blob = None
        if getattr(a, "fingerprints", None) and os.path.isfile(
                a.fingerprints):
            with open(a.fingerprints, encoding="utf-8") as fh:
                blob = json.load(fh)
        elif getattr(a, "audit", None) and os.path.isfile(a.audit):
            gaps = []
            with open(a.audit, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("results") in (None, [], {}):
                        gaps.append({"question": r.get("question")})
            blob = export_fingerprints(cluster_gaps(gaps))
        else:
            print("error: gossip-push needs --fingerprints or --audit",
                  file=sys.stderr)
            return 2
        peers = [p.strip() for p in (a.peers or "").split(",") if p.strip()]
        if not peers:
            print("error: gossip-push needs --peers", file=sys.stderr)
            return 2
        report = gossip_push(transport, blob, peers, origin=a.origin)
        print(json.dumps(report, indent=2))
        return 0

    if getattr(a, "gossip_pull", False):
        inbox = a.inbox or os.path.join(os.path.abspath(a.instance_dir),
                                        "gossip_inbox")
        os.makedirs(inbox, exist_ok=True)
        state = gossip_collect(inbox)
        print(json.dumps(state, indent=2, ensure_ascii=False))
        n = sum(len(p["fingerprints"]) for p in state["peers"].values())
        print(f"\ncollected {n} fingerprint(s) from "
              f"{len(state['peers'])} peer(s) -> {inbox}/gossip_state.json")
        return 0

    print("error: use gossip-push or gossip-pull", file=sys.stderr)
    return 2
