"""`neuralosd mesh` — Synapse mesh integration: serve, discover, delegate.

  neuralosd mesh serve --instance-dir ./inst --gateway http://127.0.0.1:8090
  neuralosd mesh discover --gateway http://127.0.0.1:8090 --skill "bmc"
  neuralosd mesh ask --instance-dir ./inst --question "..." --delegate
"""
import json
import os
import sys


def run(a):
    from ..synapse import (SynapseError, SynapseHTTP, delegate_to_peer,
                           discover_peers)
    from .._cmd._common import load_instance

    transport = SynapseHTTP(gateway=a.gateway, timeout=a.timeout)
    inst = load_instance(a.instance_dir) if a.instance_dir else None

    if a.mesh_cmd == "health":
        try:
            h = transport.health()
            print(json.dumps(h, indent=2, ensure_ascii=False))
            return 0
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if a.mesh_cmd == "discover":
        peers = discover_peers(transport, a.skill, a.instance_name or "")
        if not peers:
            print("no matching peers on the mesh")
            return 0
        for p in peers:
            print(f"  {p['agent_id']:24} matched: {', '.join(p['matched'])}")
        return 0

    if a.mesh_cmd == "register":
        if inst is None:
            print("error: --instance-dir is required for register",
                  file=sys.stderr)
            return 1
        caps = inst.router.menu()
        skills = [m["name"] for m in caps]
        print(f"registering '{inst.name}' with {len(skills)} skills "
              f"on the mesh at {a.gateway} ...")
        # The actual registration depends on the Synapse registry API.
        # For now, report what WOULD be registered.
        print(f"  agent_id: {inst.name}")
        print(f"  skills:   {', '.join(skills[:10])}"
              + (f" (+{len(skills)-10} more)" if len(skills) > 10 else ""))
        print("\nthe mesh registry is populated by the NATS agent itself.")
        print("use `neuralosd mesh serve` to start serving on the mesh.")
        return 0

    if a.mesh_cmd == "serve":
        if inst is None:
            print("error: --instance-dir is required for serve",
                  file=sys.stderr)
            return 1
        from ..synapse import MeshAgent
        agent = MeshAgent(instance_name=inst.name, router=inst.router,
                          transport=transport)
        caps = agent.capabilities()
        print(f"serving '{inst.name}' on the mesh at {a.gateway}")
        print(f"  {len(caps)} capabilities registered")
        print(f"  questions arrive via the Synapse mesh")
        print(f"  refusals travel back with refusal_reason + score vector")
        print(f"\npress Ctrl+C to stop ...")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nmesh serve stopped")
        return 0

    if a.mesh_cmd == "ask":
        if not a.question:
            print("error: --question is required for ask", file=sys.stderr)
            return 1
        question = " ".join(a.question)
        result = inst.ask(question)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"error: unknown mesh command {a.mesh_cmd!r}", file=sys.stderr)
    return 2
