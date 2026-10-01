"""MCP server — expose probes as MCP tools over JSON-RPC (stdio).

Reads newline-delimited JSON-RPC from stdin, writes to stdout. Any MCP host
(Claude Desktop, Cursor, etc.) can connect and discover/call probes.

usage: python3 -m neuralosd.mcp --dir /opt/chinook
       (or pipe from the daemon: neuralosd --mcp)
"""
import importlib
import json
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def handle(request, instance_dir: str):
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "neuralos-probes",
                                          "version": "1.0.0"}}}

    if method == "tools/list":
        os.chdir(instance_dir)
        sys.path.insert(0, instance_dir)
        import importlib.util
        inst_path = os.path.join(instance_dir, "instance.py")
        spec = importlib.util.spec_from_file_location("neuralos_mcp_inst", inst_path)
        inst = importlib.util.module_from_spec(spec)
        sys.modules["neuralos_mcp_inst"] = inst
        spec.loader.exec_module(inst)
        menu = json.load(open("needle_menu.json", encoding="utf-8"))
        tools = []
        for p in menu:
            tools.append({
                "name": p["name"],
                "description": p.get("description", p["name"]),
                "inputSchema": p.get("parameters",
                                     {"type": "object", "properties": {}}),
            })
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"tools": tools}}

    if method == "tools/call":
        params = request.get("params") or {}
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        os.chdir(instance_dir)
        sys.path.insert(0, instance_dir)
        import importlib.util
        inst_path = os.path.join(instance_dir, "instance.py")
        spec = importlib.util.spec_from_file_location("neuralos_mcp_inst", inst_path)
        inst = importlib.util.module_from_spec(spec)
        sys.modules["neuralos_mcp_inst"] = inst
        spec.loader.exec_module(inst)
        fn = {t.__name__: t for t in inst.TOOLS}.get(tool_name)
        if not fn:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601,
                              "message": f"unknown tool: {tool_name}"}}
        try:
            result = fn(**args)
            content = json.dumps(result, ensure_ascii=False, default=str)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": content}]}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603,
                              "message": str(exc)[:300]}}

    if method is None:
        return None
    return {"jsonrpc": "2.0", "id": request.get("id"),
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main():
    instance_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        response = handle(request, instance_dir)
        if response:
            print(json.dumps(response, ensure_ascii=False, default=str),
                  flush=True)


if __name__ == "__main__":
    main()
