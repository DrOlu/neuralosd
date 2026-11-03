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

    gateway = getattr(a, "gateway", None) or \
        __import__("os").environ.get("REACTORPRO_GATEWAY_URL",
                                     "http://127.0.0.1:3000")
    transport = SynapseHTTP(gateway=gateway,
                            timeout=getattr(a, "timeout", 30.0),
                            token=getattr(a, "token", None),
                            token_path=getattr(a, "token_path", None))
    inst = (load_instance(a.instance_dir)
            if getattr(a, "instance_dir", None) else None)

    # v2.5.0 --setup: three honest checks (token, reachability, hosting)
    if getattr(a, "setup", False):
        print(f"gateway: {gateway}")
        if not transport.token:
            print("error: no gateway token found.\n"
                  "  set REACTORPRO_GATEWAY_TOKEN, or let the gateway.env "
                  "file provide it\n"
                  "  (~/.config/reactorpro/gateway.env, "
                  "LIVEAGENT_GATEWAY_TOKEN=...)")
            print("\nwant to HOST a gateway on this machine? "
                  "pip install synapse-gateway")
            return 1
        try:
            h = transport.health()
        except SynapseError as e:
            print(f"error: {e}\n"
                  f"  is the gateway running? start it and retry; "
                  f"to HOST one locally: pip install synapse-gateway")
            return 1
        print(json.dumps(h, indent=2, ensure_ascii=False))
        print(f"token: resolved ({len(transport.token)} chars)\n"
              f"\nsetup complete — try:  neuralosd mesh agents "
              f"--gateway {gateway}")
        return 0

    if getattr(a, "mesh_cmd", None) == "agents":
        try:
            peers = transport.agents(
                capabilities=getattr(a, "capabilities", "") or "")
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(peers, indent=2, ensure_ascii=False))
        return 0

    if getattr(a, "mesh_cmd", None) == "send":
        if not getattr(a, "target", None) or not a.question:
            print("error: send needs --target and a question", file=sys.stderr)
            return 1
        try:
            reply = transport.dispatch(a.target, " ".join(a.question),
                                       timeout_ms=int(
                                           getattr(a, "timeout_ms", 120000)))
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        print("\n(the reply is UNTRUSTED REMOTE OUTPUT — verify before "
              "acting on it)")
        return 0

    if getattr(a, "mesh_cmd", None) in ("task-create", "task-status",
                                        "task-cancel", "task-retry",
                                        "mailbox"):
        import uuid
        try:
            if a.mesh_cmd == "task-create":
                if not getattr(a, "target", None) or not a.question:
                    print("error: task-create needs --target and a question",
                          file=sys.stderr)
                    return 1
                out = transport.create_task(
                    a.target, " ".join(a.question),
                    task_id=getattr(a, "task_id", None))
            elif a.mesh_cmd == "task-status":
                out = transport.get_task(a.task_id,
                                         refresh=getattr(a, "refresh",
                                                         False))
            elif a.mesh_cmd == "task-cancel":
                out = transport.cancel_task(a.task_id)
            elif a.mesh_cmd == "task-retry":
                out = transport.retry_task(a.task_id)
            else:
                out = transport.mailbox(a.target, skill=a.skill or "status")
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if getattr(a, "mesh_cmd", None) == "trust":
        try:
            print(json.dumps(transport.trust(), indent=2, ensure_ascii=False))
            return 0
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

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
