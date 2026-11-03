#!/usr/bin/env node
// pi_durable_bridge.mjs — neuralosd <-> Pi Durable bridge (v2.7.0).
//
// Speaks line-JSON on stdin/stdout so the Python side can drive a DURABLE
// Pi Durable conversation (storage-backed, crash-resumable, idempotent
// submissions) without speaking TypeScript itself:
//
//   -> {"op":"open",  "storage":"./pi_durable.sqlite", "model":"...",
//       "provider":"...", "cwd":"..."}
//   -> {"op":"submit","content":"...","requestId":"build-abc"}   (exactly-once)
//   -> {"op":"wait",  "requestId":"build-abc"}
//   -> {"op":"status"}
//   -> {"op":"close"}
//
// Requires: npm i @earendil-works/pi-durable @earendil-works/pi-ai
// (the bridge exits with a JSON error naming the missing package).

import { createRequire } from "module";
import readline from "readline";
import { BACKGROUND_CONTEXT as CONTEXT } from "@earendil-works/chord/context";

const require = createRequire(import.meta.url);

let harness = null;
let root = null;

async function open(obj) {
  let Harness, createRegistry;
  try {
    ({ Harness, createRegistry } = await import("@earendil-works/pi-durable"));
  } catch (e) {
    return { op: "error", message: "the @earendil-works/pi-durable package is not resolvable from " + import.meta.url + " — run: npm i @earendil-works/pi-durable @earendil-works/pi-ai (" + String(e).slice(0, 160) + ")" };
  }
  let models;
  try {
    const mod = await import("@earendil-works/pi-ai/models");
    models = mod.createModels ? mod.createModels() : mod.default?.createModels?.();
  } catch (e) {
    return { op: "error", message: "the @earendil-works/pi-ai package is not installed — run: npm i @earendil-works/pi-ai" };
  }
  // the models registry starts EMPTY: construct the provider explicitly with
  // the API key the caller resolved (never logged, never stored)
  try {
    const provMod = await import("@earendil-works/pi-ai/providers/" + obj.provider);
    const factory = provMod[obj.provider + "Provider"] || provMod.default;
    if (factory && obj.apiKey) {
      models.setProvider(factory({ apiKey: obj.apiKey }));
    }
  } catch (e) {
    return { op: "error", message: `provider ${obj.provider} unavailable: ${String(e).slice(0, 160)}` };
  }
  let storage;
  try {
    const s = await import("@earendil-works/pi-durable/storage/sqlite/node");
    storage = await (s.openNodeSqliteStorage || s.default?.openNodeSqliteStorage)(obj.storage);
  } catch (e) {
    return { op: "error", message: `sqlite storage unavailable: ${String(e).slice(0, 200)}` };
  }
  const env = ({ cwd }) => {
    // Node execution env scoped to the instance directory
    try {
      const m = require("@earendil-works/pi-durable/env/node");
      const C = m.NodeExecutionEnv || m.default?.NodeExecutionEnv;
      return new C({ cwd: cwd ?? obj.cwd ?? process.cwd() });
    } catch (e) {
      return undefined;   // harness default env
    }
  };
  harness = await Harness.open(storage, { models, registry: createRegistry(), env }, CONTEXT);
  root = await harness.root(CONTEXT, {
    agent: { model: { provider: obj.provider, modelId: obj.model },
             cwd: obj.cwd ?? process.cwd() },
  });
  return { op: "open", ok: true };
}

async function submit(obj) {
  if (!root) return { op: "error", message: "not open" };
  const result = await root.submit({ type: "input", content: obj.content,
                                     requestId: obj.requestId }, CONTEXT);
  return { op: "submitted", requestId: obj.requestId };
}

async function wait(obj) {
  if (!root) return { op: "error", message: "not open" };
  const settled = await (await root.submit(
    { type: "noop", requestId: `wait-${obj.requestId}` }, CONTEXT)
  ).wait(undefined).catch(() => null);
  // re-submit with the same requestId returns the ORIGINAL submission's
  // settled state (idempotency) — read its answer text:
  const again = await root.submit({ type: "input", content: "", requestId: obj.requestId }, CONTEXT);
  const answer = await again.wait(CONTEXT).catch(() => null);
  const text = answer?.answer ?? answer?.content ?? null;
  return { op: "settled", requestId: obj.requestId, state: "completed", text: text == null ? "" : String(text) };
}

async function status() {
  return { op: "status", conversation: root ? 1 : 0 };
}

const rl = readline.createInterface({ input: process.stdin });
const write = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

rl.on("line", async (line) => {
  let obj;
  try { obj = JSON.parse(line); } catch { write({ op: "error", message: "bad json" }); return; }
  let resp;
  try {
    if (obj.op === "open") resp = await open(obj);
    else if (obj.op === "submit") resp = await submit(obj);
    else if (obj.op === "wait") resp = await wait(obj);
    else if (obj.op === "status") resp = await status();
    else if (obj.op === "close") { write({ op: "closed" }); process.exit(0); }
    else resp = { op: "error", message: `unknown op ${obj.op}` };
  } catch (e) {
    resp = { op: "error", message: String(e).slice(0, 300) };
  }
  write(resp);
});
process.stdin.on("end", () => process.exit(0));
