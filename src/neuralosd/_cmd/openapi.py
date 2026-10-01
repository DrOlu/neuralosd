"""`neuralosd openapi` — OpenAPI 3.1 + MCP manifest from a menu or instance."""
import json
import os


def run(a):
    from ._common import load_instance

    menu_path = a.menu
    if os.path.isfile(menu_path) and menu_path.endswith(".json"):
        with open(menu_path) as f:
            data = json.load(f)
        menu = data.get("menu", data) if isinstance(data, dict) else data
        title = a.agent or "neuralos-instance"
        spec = _openapi_from_menu(menu, title)
    else:
        inst = load_instance(menu_path)
        spec = inst.router.openapi(a.agent)
        menu = inst.menu

    print(json.dumps(spec, indent=2, ensure_ascii=False, default=str))

    # MCP manifest next to it
    mcp = {"name": (a.agent or "neuralos-instance"),
           "tools": [{"name": m["name"], "description": m["description"],
                      "inputSchema": m["parameters"]} for m in menu]}
    out = os.path.join(os.path.dirname(os.path.abspath(menu_path)) or ".",
                       "mcp_manifest.json")
    try:
        with open(out, "w") as f:
            json.dump(mcp, f, indent=2, ensure_ascii=False)
        print(f"\n# MCP manifest written to {out}", file=__import__("sys").stderr)
    except OSError:
        pass


def _openapi_from_menu(menu, title):
    paths = {}
    for m in menu:
        props = m.get("parameters", {}).get("properties", {})
        paths[f"/probes/{m['name']}"] = {
            "post": {"operationId": m["name"], "summary": m.get("description", ""),
                     "requestBody": {"content": {"application/json": {
                         "schema": {"type": "object", "properties": props}}}},
                     "responses": {"200": {"description": "envelope"}}}}
    return {"openapi": "3.1.0",
            "info": {"title": title, "version": "1.0.0"},
            "paths": paths}