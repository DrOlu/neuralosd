"""`neuralosd mesh` — Synapse mesh integration.

Serve a verified instance on the mesh, discover peers with the SAME scorer
that routes local questions, preflight peers before paying for an answer,
and delegate refused questions — refusal-aware, loop-safe, hop-limited,
deadline-bounded.

  neuralosd mesh serve --instance-dir ./inst --gateway http://127.0.0.1:3000
  neuralosd mesh discover --gateway http://127.0.0.1:3000 --skill "bmc"
  neuralosd mesh preflight --instance-dir ./inst "how many incidents"
  neuralosd mesh ask --instance-dir ./inst "..." --delegate
  neuralosd mesh gaps --instance-dir ./inst --audit ./inst/ask_audit.jsonl
"""
import json
import os
import sys


def run(a):
    from ..synapse import (MESH_PROTOCOL_VERSION, SynapseError, SynapseHTTP,
                           Deadline, MeshAgent, delegate_to_peer,
                           discover_peers, refusals_from_audit,
                           propose_triggers, verify_signature)
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
    mesh_cmd = getattr(a, "mesh_cmd", None)

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

    if mesh_cmd == "agents":
        try:
            peers = transport.agents(
                capabilities=getattr(a, "capabilities", "") or "")
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(peers, indent=2, ensure_ascii=False))
        return 0

    if mesh_cmd == "send":
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

    if mesh_cmd in ("task-create", "task-status", "task-cancel",
                    "task-retry", "mailbox"):
        import uuid
        try:
            if mesh_cmd == "task-create":
                if not getattr(a, "target", None) or not a.question:
                    print("error: task-create needs --target and a question",
                          file=sys.stderr)
                    return 1
                out = transport.create_task(
                    a.target, " ".join(a.question),
                    task_id=getattr(a, "task_id", None))
            elif mesh_cmd == "task-status":
                out = transport.get_task(a.task_id,
                                         refresh=getattr(a, "refresh",
                                                         False))
            elif mesh_cmd == "task-cancel":
                out = transport.cancel_task(a.task_id)
            elif mesh_cmd == "task-retry":
                out = transport.retry_task(a.task_id)
            else:
                out = transport.mailbox(a.target, skill=a.skill or "status")
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if mesh_cmd == "trust":
        try:
            print(json.dumps(transport.trust(), indent=2, ensure_ascii=False))
            return 0
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if mesh_cmd == "health":
        try:
            h = transport.health()
            print(json.dumps(h, indent=2, ensure_ascii=False))
            return 0
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if mesh_cmd == "discover":
        question = getattr(a, "skill", "") or ""
        peers = discover_peers(transport, question,
                               a.instance_name or "",
                               preflight=getattr(a, "preflight", False))
        if getattr(a, "json", False):
            print(json.dumps(peers, indent=2, ensure_ascii=False))
            return 0
        if not peers:
            print("no matching peers on the mesh")
            return 0
        for p in peers:
            line = (f"  {p['agent_id']:24} matched: "
                    f"{', '.join(p['matched'])}  score {p['score']}")
            if p.get("would") is not None:
                line += f"  would: {p['would']}"
                if p.get("refusal_reason"):
                    line += f" ({p['refusal_reason']})"
            print(line)
        return 0

    if mesh_cmd == "preflight":
        if inst is None:
            print("error: --instance-dir is required for preflight",
                  file=sys.stderr)
            return 1
        if not a.question:
            print("error: a question is required for preflight",
                  file=sys.stderr)
            return 1
        agent = MeshAgent(instance_name=inst.name, router=inst.router,
                          transport=transport)
        pf = agent.preflight(" ".join(a.question))
        print(json.dumps(pf, indent=2, ensure_ascii=False, default=str))
        return 0 if pf.get("would_answer") else 2

    if mesh_cmd == "delegate":
        if not a.question:
            print("error: a question is required for delegate",
                  file=sys.stderr)
            return 1
        question = " ".join(a.question)
        visited = [v.strip() for v in
                   (getattr(a, "visited", "") or "").split(",") if v.strip()]
        try:
            peers = discover_peers(transport, question,
                                   getattr(a, "instance_name", "") or "",
                                   preflight=getattr(a, "preflight", False))
            if not peers:
                print("no peers claim to answer this", file=sys.stderr)
                return 2
            deadline = (Deadline.from_ms(a.deadline_ms)
                        if getattr(a, "deadline_ms", None) else None)
            out = delegate_to_peer(
                transport, peers[0], question, visited=visited,
                max_hops=int(getattr(a, "max_hops", 3)),
                deadline=deadline,
                trust=getattr(a, "trust", "known"))
        except SynapseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        if out is None or not out.get("verified"):
            return 2
        return 0

    if mesh_cmd == "gaps":
        if inst is None:
            print("error: --instance-dir is required for gaps",
                  file=sys.stderr)
            return 1
        audit = getattr(a, "audit", None) or \
            os.path.join(a.instance_dir, "ask_audit.jsonl")
        refused = refusals_from_audit(audit, limit=int(
            getattr(a, "limit", 2000)))
        if not refused:
            print(f"no refused questions in {audit}")
            return 0
        proposals = propose_triggers(inst.router, refused, min_count=int(
            getattr(a, "min_count", 2)))
        if getattr(a, "json", False):
            print(json.dumps({"audit": audit, "refused": len(refused),
                              "proposals": proposals},
                             indent=2, ensure_ascii=False))
            return 0
        print(f"{len(refused)} refused questions in {audit}\n")
        if not proposals:
            print("no clusters large enough to propose from")
            return 0
        for p in proposals:
            print(f"  [{p['verdict']:>14}] trigger: {p['trigger']!r}  "
                  f"evidence: {p['evidence']}")
            if p.get("probe"):
                print(f"                  nearest probe: {p['probe']} "
                      f"(score {p['score']})")
            if p.get("unknown_tokens"):
                print(f"                  unknown tokens: "
                      f"{', '.join(p['unknown_tokens'])}")
        print("\nproposals are advisory — nothing is applied without you.")
        return 0

    if mesh_cmd == "verify":
        raw = None
        if getattr(a, "file", None):
            try:
                with open(a.file, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        else:
            raw = sys.stdin.read()
        try:
            body = json.loads(raw)
        except ValueError as e:
            print(f"error: not JSON: {e}", file=sys.stderr)
            return 1
        ok, why = verify_signature(body, getattr(a, "key", None))
        print(json.dumps({"verified": ok, "reason": why,
                          "alg": (body.get("signature") or {}).get("alg"),
                          "authenticated":
                          (body.get("signature") or {}).get("authenticated")},
                         indent=2))
        return 0 if ok else 1

    if mesh_cmd == "register":
        if inst is None:
            print("error: --instance-dir is required for register",
                  file=sys.stderr)
            return 1
        agent = MeshAgent(instance_name=inst.name, router=inst.router,
                          transport=transport)
        reg = agent.registration()
        if getattr(a, "json", False):
            print(json.dumps(reg, indent=2, ensure_ascii=False))
        else:
            print(f"registering '{inst.name}' with {reg['probes']} "
                  f"capabilities (protocol {MESH_PROTOCOL_VERSION}, "
                  f"gate {reg['gate_version']}, menu {reg['menu_version']})")
            print(f"  skills: {', '.join(reg['skills'][:10])}"
                  + (f" (+{len(reg['skills'])-10} more)"
                     if len(reg["skills"]) > 10 else ""))
        if getattr(a, "publish", False):
            try:
                transport.register(reg)
                print(f"published to {a.gateway}")
            except SynapseError as e:
                print(f"error: publish failed: {e}", file=sys.stderr)
                return 1
        else:
            print("\n(dry report — add --publish to POST it to the gateway)")
        return 0

    if mesh_cmd == "serve":
        if inst is None:
            print("error: --instance-dir is required for serve",
                  file=sys.stderr)
            return 1
        agent = MeshAgent(instance_name=inst.name, router=inst.router,
                          transport=transport)
        caps = agent.capabilities()
        reg = agent.registration()
        print(f"serving '{inst.name}' on the mesh at {a.gateway}")
        print(f"  {len(caps)} capabilities registered")
        print(f"  contract: mesh protocol {MESH_PROTOCOL_VERSION}, "
              f"gate {reg['gate_version']}, menu {reg['menu_version']}")
        if getattr(a, "publish", False):
            try:
                transport.register(reg)
                print(f"  registration published to {a.gateway}")
            except SynapseError as e:
                print(f"  warning: registration publish failed: {e}",
                      file=sys.stderr)
        print("\npress Ctrl+C to stop ...")
        try:
            beats = agent.heartbeat_loop(
                interval=float(getattr(a, "heartbeat", 30.0)),
                publish=getattr(a, "publish", False))
        except KeyboardInterrupt:
            pass
        print("\nmesh serve stopped")
        return 0

    if mesh_cmd == "ask":
        from ..router import NoResults
        if not a.question:
            print("error: a question is required for ask", file=sys.stderr)
            return 1
        question = " ".join(a.question)
        from ..router import audit_source
        try:
            # a mesh ask is a SERVED question — same ledger class as /ask
            with audit_source("serve"):
                env = inst.ask(question)
        except NoResults as e:
            env = getattr(e, "envelope", None) or {"question": question,
                                                   "results": None}
            env.setdefault("note", "no probe matched")
        except AttributeError:
            print("error: --instance-dir is required for ask", file=sys.stderr)
            return 1
        if env.get("results") is None and getattr(a, "delegate", False):
            # The honest rescue: the gate says "not me", the mesh may say
            # "but them" — and the answer arrives marked as delegated and
            # verified (or not) against the peer's own capabilities.
            try:
                peers = discover_peers(
                    transport, question, inst.name,
                    preflight=getattr(a, "preflight", False))
            except SynapseError as e:
                peers = []
                env["delegation_error"] = e.code
            if peers:
                deadline = (Deadline.from_ms(a.deadline_ms)
                            if getattr(a, "deadline_ms", None) else None)
                env["delegation"] = delegate_to_peer(
                    transport, peers[0], question,
                    max_hops=int(getattr(a, "max_hops", 3)),
                    deadline=deadline,
                    trust=getattr(a, "trust", "known"))
        print(json.dumps(env, indent=2, ensure_ascii=False, default=str))
        delegated = bool((env.get("delegation") or {}).get("response"))
        if env.get("results") is None and not delegated:
            # Exit 2 on a refusal so scripts and CI can tell an answer
            # from a refusal — same contract as `neuralosd ask`.
            raise SystemExit(2)
        return 0

    print(f"error: unknown mesh command {mesh_cmd!r}", file=sys.stderr)
    return 2
