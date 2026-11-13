# Implicit state machines — census (F-series audit; refreshed for 3.0.0)

Scope: every place in `neuralosd` where behaviour depends on a stored or
in-process state, not just on the current request. For each machine this
records the states, the events, the guards, the actions, the transitions that
are illegal, the observable trace, the file:line anchors, and a verdict.

> **3.0.0 refresh.** Sections 1–12 are the F-series census. Sections 13–19
> cover the state introduced by the agentic surface (async runs, HITL
> confirmations, the scheduler, the door registry, sidecar provisioning,
> the append-only agent stores, and the MCP instance caches). The new
> mutable machines have explicit transition tables in code; the defects
> found in this pass were fixed with tests (`tests/test_agentic_state_machines.py`,
> `tests/test_scheduler_state.py`).

A **mutable** machine is one whose state outlives a single call (a journal, a
marker file, a cache, an offset) or is shared across requests. A **decision**
is a pure function over the request and the menu — it has outcomes, not stored
states; it is listed so the vocabulary is not mistaken for an FSM with hidden
transitions.

Every anchor below is the file as it stands on `audit/f-series`; tests named in
the verdict are the executable contract.

---

## 1. Actuation (mutable, durable) — **enforced**

The journal is the state; the CLI is a new process for every command, so
process memory can never be trusted.

| | |
|---|---|
| **States** | `(none)`, `DRY_RUN`, `APPLIED`, `UNDONE` |
| **Events** | dry-run request, apply(plan_id), undo(plan_id) |
| **Guards** | state from the plan's **last** transition (`_plan_state`); action/target must equal the plan's identity record (`_same_plan`); a torn *trailing* journal line is a crash artifact, corruption in the middle refuses (`_read_journal`); side effect fires **before** the row that claims it |
| **Actions** | fire `side_effect()`; append a journal row (`op`: `dry_run`/`apply`/`undo`) with fsync |
| **Illegal** | apply from `APPLIED`; undo from `UNDONE`/`DRY_RUN`/`(none)`; apply/undo with a substituted action or target; any transition on a corrupt history. Refused **by name**, with the legal set printed; there is no `--force`. |
| **Trace** | `actuation_journal.jsonl` (`op`, `plan_id`, `action`, `target`, `reason`, `operator`, `ts`, `stub`); CLI stdout/stderr; exit code |
| **Anchors** | `src/neuralosd/actuate.py:53` (`LEGAL_TRANSITIONS`), `:130` (`_read_journal`), `:165` (`_plan_state`), `:244` (guard), `:251`/`:296` (transitions), `src/neuralosd/_cmd/actuate.py:38-77` (flag grammar) |
| **Verdict** | Enforced. F1/F2/F3/F4/F5/F11 fixed here; `tests/test_actuation_fsm.py`, `tests/test_actuate_cli.py`. Backwards-compatible reader: pre-v2.9.10 rows without `op` are inferred from `dry_run`/`applied`/`undone`. |

Legal path: `(none) --dry_run--> DRY_RUN --apply--> APPLIED --undo--> UNDONE --apply--> APPLIED`.
A save of `undo` from `APPLIED` is legal; a second `undo` from `UNDONE` is not.

---

## 2. Ask cache (mutable) — **enforced**

| | |
|---|---|
| **States** | `MISS`, `HIT`, `EVICTED` (TTL or cap) |
| **Events** | `ask()`; load at Router construction |
| **Guards** | key = SHA1(normalized question + menu_version); `CACHE_MAX=500`; TTL from `self.ttl`; `_cache_load` rejects a truncated/wrong-shaped file and returns a miss |
| **Actions** | append one entry; atomic write via `tmp` + `os.replace`; evict oldest by `ts` |
| **Illegal** | serving a HIT for a different menu version; a cache file that grows without bound; a partial write becoming visible |
| **Trace** | `<instance>/.ask_cache.json`; `use_cache=False` bypasses reads **and** writes |
| **Anchors** | `src/neuralosd/router.py:1051` (`_cache_load`), `:1077` (`_cache_store`), `:1278` (`ask`) |
| **Verdict** | Enforced (E-series). Malformed-file cases covered by `scripts/malformed_input_sweep.py` (ok: truncated JSON, entry without payload, payload list, top-level string). |

---

## 3. Router refusal gate (decision, not stored) — **enforced as one function**

| | |
|---|---|
| **Outcomes** | `CONFIDENT` (`None`) or a reason: `below_floor`, `ambiguous`, `low_coverage`, `comparative`, `dropped_filter`, `no_probe_matches`, `action_intent` |
| **Inputs** | normalized question, menu, calibrated thresholds (`floor`, `margin`, `min_question_coverage`, `refuse_disjunction`), chosen + runner-up candidates |
| **Guards** | entity bypass (`_args_seen > 0`), per-probe `conf_gate`, unknown-domain-noun check, coverage advantage for ambiguity |
| **Actions** | execute the chosen probe, or `_refuse()` (full envelope + audit row) |
| **Illegal** | returning a reason outside `REFUSAL_REASONS`; answering an out-of-range value by clamping (never clamped) |
| **Trace** | one JSON line on stderr under `NEURALOSD_GATE_TRACE=1`; `Router.last_gate_trace`; `ask_audit.jsonl` row with `mode`, `refusal_reason`, `scores`, `menu_version`, `ask_id`, `ts`, `source` |
| **Anchors** | `src/neuralosd/router.py:853` (`_gate`), `:885` (`_gate_decide`), `:1016` (`_refuse`), `:249` (`REFUSAL_REASONS`), `:1278` (`ask`) |
| **Verdict** | Enforced as a single decision function (no duplicated gate). F8 fixed the declared vocabulary to include the three reasons the function already returned; `tests/test_gate_reason_vocabulary.py` AST-checks every string return against the set and pins an audit row for each. `scripts/fp_fn_sweep.py`: PASS. |

---

## 4. Instance lifecycle (mutable markers) — **enforced, one tolerant reader**

| | |
|---|---|
| **States** | `ABSENT`, `INITIALIZED` (`probes.py` or `needle_menu.json`), `DEPLOYED` (deploy marker/serve), `MOTHBALLED` (marker lists ≥1 demoted probe) |
| **Events** | `init`, `deploy`, `serve`, `mothball --apply`, `reinstate` |
| **Guards** | resolution order: `<dir>/probes.py` → exactly one `<dir>/*/probes.py` → `needle_menu.json` → error; multiple sub-instances refused by name; a mothballed marker never empties the menu; role filtering after derived metrics |
| **Actions** | build/load probes; read `derived.json`; apply ACL; filter mothballed probes |
| **Illegal** | treating a multi-instance directory as one instance; a marker/ACL that silently removes **all** probes; loading an instance by firing a probe |
| **Trace** | `probes.py`, `needle_menu.json`, `derived.json`, `acl.json`, `mothballed.json`; `load_instance` error text |
| **Anchors** | `src/neuralosd/_cmd/_common.py:198` (`load_instance`), `:187` (`_mothballed_names`), `src/neuralosd/_cmd/init.py`, `src/neuralosd/_cmd/deploy.py` |
| **Verdict** | Enforced at the single loader; both marker readers are tolerant (see §5). |

---

## 5. Mothball marker (mutable) — **enforced**

| | |
|---|---|
| **States** | `NO_MARKER`, `SOME_DEMOTED`, `CORRUPT` (reads as `NO_MARKER`) |
| **Events** | `mothball --apply`, `reinstate <name>` |
| **Guards** | at least one probe always stays routable; names must be strings; a corrupt marker never crashes fleet/SLO |
| **Actions** | atomic write (`tmp` + fsync + `os.replace`) |
| **Illegal** | demoting every probe; a torn write becoming the visible file |
| **Trace** | `<instance>/mothballed.json` (`names`, `ts`) |
| **Anchors** | `src/neuralosd/loop.py:389` (`_write_mothballed`), `:407` (`mothballed_names`), `:426` (`mothball`), `:465` (`reinstate`); reader contract mirrored at `src/neuralosd/_cmd/_common.py:187` |
| **Verdict** | Enforced. F13 fixed the non-atomic writer and the crash-on-corruption reader; `tests/test_mothball_state.py`. |

---

## 6. Traps and golden banks (mutable) — **guarded at mutation points**

| | |
|---|---|
| **States** | trap `ABSENT` → `SYNTHESIZED` → (`PROMOTED`); golden `CANDIDATE` → `VERIFIED` (`oracle.value` non-null) → `STALE` (retracted) |
| **Events** | probe registration (`synthesize_trap_for`), feedback (`ingest_feedback`), `apply_feedback`, `record_golden`, `retract_stale_traps` |
| **Guards** | auto-traps idempotent by question; a trap is promoted only when the refusal is **proven** at apply time; golden candidates require a human-verified value; auto-traps disabled by env |
| **Actions** | atomic append/write of `traps.json` / `golden.json` / `candidates.json`; receipt attached to verified cases |
| **Illegal** | a golden case with no expected probe silently passing (skipped and reported); a trap promoted without proof; duplicate auto-trap |
| **Trace** | `traps.json`, `golden.json`, `candidates.json`; receipts; self-test ledger rows (`source` = `trap`/`golden`) |
| **Anchors** | `src/neuralosd/banks.py:52`, `:149`, `:230`, `:88` (`_load_bank`); `src/neuralosd/instance.py:142`; `src/neuralosd/loop.py:214-220` |
| **Verdict** | Enforced at the mutation points. F12 replaced four `json.loads(open(...).read())` readers with `_load_bank` (closed handle); `tests/test_instance_export_handle.py`, existing bank tests. |

---

## 7. Watch cycle (mutable offsets + decision) — **enforced**

| | |
|---|---|
| **States** | per-source offset/rotation state; buffer has `0..cap` events |
| **Events** | one poll cycle, new lines, rotation, threshold breach |
| **Guards** | bounded buffer (newest `cap`); rotation detection resets the offset; a rule without a webhook lands in the report only; a failed webhook is reported, never fatal |
| **Actions** | append normalized events; evaluate rules over the window; post per-rule or global webhook |
| **Illegal** | unbounded buffer growth; a failed POST killing the loop; a configured webhook never being delivered |
| **Trace** | `watch_buffer.jsonl`; report `{ingested, buffer, alerts, notifications}` |
| **Anchors** | `src/neuralosd/watch.py:65` (`tail_new_lines`), `:87` (`evaluate_rules`), `:159` (`run_cycle`) |
| **Verdict** | Enforced. F14 kept the rule's `webhook` key in the alert; `tests/test_v2_8_1_gossip_watch.py` covers the per-rule destination end to end. |

---

## 8. Audit provenance and the self-test ledger (mutable, context-scoped) — **enforced**

| | |
|---|---|
| **States** | request provenance `user` (default), `watchdog`, `golden`, `trap`, `serve`; ledger target = user `ask_audit.jsonl` or `ask_audit.selftest.jsonl` |
| **Events** | a ContextVar scope around a call; `selftest_ledger` redirect |
| **Guards** | ContextVar wins over the Router default; the user ledger is restored even on exception; every refusal path goes through `_refuse` and carries `ts`/`mode` |
| **Actions** | append one row per ask; stamp `source` |
| **Illegal** | self-test traffic counting in the user refusal SLO; a refusal row missing `ts`/`mode` (would be skipped by `refusal_rate`) |
| **Trace** | `ask_audit.jsonl`, `ask_audit.selftest.jsonl`; `refusal_rate`, `fleet_report`, SLO output |
| **Anchors** | `src/neuralosd/router.py:278` (`_AUDIT_SOURCE`), `:281` (`audit_source`), `:1155` (`_audit`); `src/neuralosd/instance.py:21` (`selftest_ledger`) |
| **Verdict** | Enforced; `tests/test_refusal_audit_schema.py` walks every refusal path and asserts the on-disk row. |

---

## 9. Multi-instance server (mutable registry + decision) — **enforced**

| | |
|---|---|
| **States** | per-name `UNLOADED` → `LOADED` or `ERROR`; server has `auth off`/`auth on` |
| **Events** | first request for a name; directory appears/disappears |
| **Guards** | lazy load under a lock; a load failure is memoized per instance and does not affect others; request body validated exactly like `service.serve` |
| **Actions** | load an instance; route `GET /{name}/{action}` or `POST /{name}/ask` |
| **Illegal** | answering a non-string or empty question; dispatching on a raw path with a query string; leaking one instance's data into another |
| **Trace** | `/instances` (`names`, `errors`); HTTP statuses 200/400/401/404/422/500 |
| **Anchors** | `src/neuralosd/multi.py:38` (`MultiInstanceServer`), `:106` (`_PathRouter`), `:166` (`do_GET`), `:202` (`do_POST`) |
| **Verdict** | Enforced. F7 made the door's validation match `serve` and normalized query strings; `tests/test_multi_instance.py`. Cross-instance `POST /query` is documented as **not implemented** rather than silently 404ing. |

---

## 10. Single-instance HTTP door (decision) — **enforced**

| | |
|---|---|
| **Outcomes** | `401` (no/bad token), `403` (role without ACL / unknown role), `400` (malformed JSON, non-object body, non-string question, empty question), `200` (answer), `422` (refusal), `500` (internal), `404` (unknown path) |
| **Guards** | constant-time token compare (`_token_ok`); `/healthz` open by design; `/ready` authenticated because it executes a probe; role resolved through one ACL reader |
| **Actions** | run `inst.ask()` under `audit_source("serve")`; serialize with `default=str` |
| **Illegal** | reaching `inst.ask()` with an empty/non-string question; treating an unknown role as the default instance; a 403 path raising instead of responding |
| **Trace** | HTTP status/body; audit row stamped `serve` |
| **Anchors** | `src/neuralosd/service.py:42` (`acl_roles`), `:64` (`resolve_role_instance`), `:109` (`do_GET`), `:138` (`do_POST`) |
| **Verdict** | Enforced. F6 replaced the undefined `_acl_roles` name; `tests/test_service_refusal_contract.py` covers the 403 over a real socket. |

---

## 11. Pi Durable bridge (mutable, external process) — **guarded; known limit**

| | |
|---|---|
| **States** | bridge `NOT_STARTED` → `READY` → `REQUEST_SENT` → `REPLY_RECEIVED`; node `RUNNING` → `EXITED` |
| **Events** | `open()`, `request`, `wait()`, process exit |
| **Guards** | line-JSON round trip; provider/model from env only for built-in providers; `wait()` polls with a timeout |
| **Actions** | write one request line; read one reply line; stage/execute the node bridge |
| **Illegal** | a request without a live process; assuming durability from a provider the bridge cannot persist |
| **Trace** | node stdout line-JSON; stderr diagnostics |
| **Anchors** | `src/neuralosd/pidurable.py:155` (`_roundtrip`), `:176` (`open`), `:197` (`wait`) |
| **Verdict** | Guarded, with a documented limit: `readline()` on the pipe has no independent timeout — overall `wait(timeout=…)` bounds the caller, but a hung child is reclaimed by the process timeout, not by `readline`. Not changed in this audit (requires a select/threading redesign); recorded as an observation. |

---

## 12. Synapse reply classification (decision) — **enforced**

| | |
|---|---|
| **Outcomes** | `answered`, `refused`, `error`, `unknown` (plus the peer envelope's `refusal_reason`) |
| **Inputs** | peer response dict; `GATE_VERSION`; `REFUSAL_REASONS` vocabulary |
| **Guards** | an envelope is either answer-shaped or refusal-shaped; provenance/signature framing is versioned by `MESH_PROTOCOL_VERSION` |
| **Actions** | record a verdict; never execute a peer's instruction |
| **Illegal** | reading a refusal as an answer-shaped absence; trusting a peer on a different gate version |
| **Trace** | mesh envelope (`gate_version`, `refusal_reason`, status); `TASK_STORE`/mailbox records |
| **Anchors** | `src/neuralosd/synapse.py:71` (imports), `:1274` (`_classify_reply`) |
| **Verdict** | Enforced by a single classifier; F8's vocabulary fix keeps the reason tokens inside the declared set. |

---

## 13. Async runs (`RunStore`) — **enforced**

| | |
|---|---|
| **States** | `queued`, `running`, `done`, `failed`, `cancelled` |
| **Events** | `submit(fn)`, worker start, worker finish, `cancel(run_id)` |
| **Guards** | `RUN_LEGAL_TRANSITIONS`; every transition under one lock; `cancel` is an event allowed from `queued`/`running`; terminal states never move |
| **Actions** | run `fn` in a daemon thread; publish `result` only on `done`; set `started`/`finished`/`updated` |
| **Illegal** | `running→running`; any move out of a terminal state; publishing a result after a cancellation; `cancel` of a finished run reporting success |
| **Trace** | in-memory run dict (`status`, times, `result`/`error`, `cancel`); `GET /runs/{id}` |
| **Anchors** | `src/neuralosd/agentic.py:278` (`RUN_STATES`), `:282` (`RUN_LEGAL_TRANSITIONS`), `:292` (`_transition`), `:309` (`submit`), `:373` (`cancel`) |
| **Verdict** | Enforced. Fixed: a cancellation observed during the probe was overwritten by the late `done`, and terminal states were mutable. `tests/test_agentic_state_machines.py`. |

---

## 14. HITL confirmations (`ConfirmStore`) — **enforced**

| | |
|---|---|
| **States** | `pending`, `confirmed`, `expired` |
| **Events** | `create`, `confirm(token)`, `expire()` |
| **Guards** | TTL; a token is single-use; terminal tokens are remembered (pruned after `max(2×ttl, 1h)`); execution happens outside the lock |
| **Actions** | run the stored closure; the router masks/audits it via `_execute_confirmed` |
| **Illegal** | confirming twice; confirming after expiry; executing an unknown token |
| **Trace** | pending/used maps; HTTP 202/404; envelope `mode: pending_confirmation` / `confirmed`; audit row `mode: confirmed` |
| **Anchors** | `src/neuralosd/hitl.py:14` (`CONFIRM_STATES`), `:32` (`create`), `:53` (`confirm`), `:97` (`expire`) |
| **Verdict** | Enforced. Fixed: a second confirm and an expired token were both reported as "invalid or expired"; `already_used`/`expired`/`invalid` are now distinct. `tests/test_agentic_state_machines.py`, `tests/test_agentic_surface.py`. |

---

## 15. Scheduler (mutable, persisted) — **enforced**

| | |
|---|---|
| **States** | per entry `scheduled → due → running → scheduled` (by `next_due`); outcome `last_status` = `ok`/`error` |
| **Events** | `add`, `remove`, interval tick, run success/failure |
| **Guards** | `RLock` (`remove` holds the lock and calls `_save`); atomic `schedules.json`; atomic result file; a failure records `last_error` |
| **Actions** | `ask_fn`; write `state/scheduled/<name>.json` |
| **Illegal** | a lost schedule from a truncated JSON; a leaked file handle; a silent failure |
| **Trace** | `schedules.json` (`next_due`, `runs`, `last_run`, `last_status`, `last_error`); `scheduled/<name>.json` |
| **Anchors** | `src/neuralosd/scheduler.py:35` (`_save`), `:50` (`add`), `:72` (`_loop`), `:86` (`_run`) |
| **Verdict** | Enforced. Fixed: `remove()` deadlocked on a non-reentrant lock; both writes were non-atomic; a failure left no state. `tests/test_scheduler_state.py`. |

---

## 16. Door registry (`~/.neuralosd/doors.json`) — **enforced**

| | |
|---|---|
| **States** | `absent` → `registered` (`native`/`relay`) → `stopped` |
| **Events** | deploy/proxy registers; `door stop` |
| **Guards** | atomic `tmp` + `os.replace`; corrupt/missing reads as empty; `stop` refuses an unknown name |
| **Actions** | record `sandbox`/`host_port`/`vm_port`/`kind`/`backend`; remove on stop |
| **Illegal** | a torn write becoming the registry; stopping an unregistered name and reporting success |
| **Trace** | `doors.json`; `door list`/`stop` output |
| **Anchors** | `src/neuralosd/_cmd/door.py:54` (`_load`), `:64` (`_save`), `:76` (`_forget`), `:345` (`stop`) |
| **Verdict** | Enforced (atomic save, tolerant load). |

---

## 17. Sidecar provisioning — **guarded (no transition table needed)**

| | |
|---|---|
| **States** | `absent` → `provisioned`; a failed provision leaves `absent` |
| **Events** | `sidecar --setup [--force]`; `is_provisioned()` |
| **Guards** | the executable's existence IS the state; provisioning is explicit (never during an ask); `--force` recreates |
| **Actions** | create a dedicated venv; report `action` = `created`/`recreated`/`already` |
| **Illegal** | treating a partial venv as provisioned; provisioning silently on a query |
| **Trace** | status dict (`uv`, `provisioned`, executable); action |
| **Anchors** | `src/neuralosd/provision.py:66` (`is_provisioned`), `:98` (`status`), `:138` (`provision`) |
| **Verdict** | Guarded. |

---

## 18. Agent append-only stores — **bounded, not FSMs**

Sessions (`.agentic/memory/<session>.jsonl`), traces
(`.agentic/traces/<run>.jsonl`), idempotency (`IdempotencyStore`) and budgets
(`BudgetStore`) have no transitions: they are logs, maps and a sliding
window. They are listed so "stateful" is not mistaken for "FSM".

| Store | Bound | Anchor |
|---|---|---|
| Sessions | append-only, one JSONL per session | `agentic.py:200` (`SessionStore`) |
| Traces | `max_events` per run, enforced on write with an atomic rewrite | `agentic.py:237` (`TraceStore`) |
| Idempotency | `max_entries` (500), FIFO eviction | `agentic.py:150` (`IdempotencyStore`) |
| Budgets | sliding window per agent | `agentic.py:176` (`BudgetStore`) |

---

## 19. MCP instance and role caches — **memoized loads, no illegal edge**

| | |
|---|---|
| **States** | per directory `UNLOADED` → `LOADED`; per (dir, role) the same |
| **Events** | first tool call; `build` invalidates both |
| **Guards** | load under one process; `build` drops the cache so the new menu is seen |
| **Illegal** | serving a stale menu after a build; loading a directory as a different instance |
| **Trace** | process memory; `health`/`catalog` menu versions |
| **Anchors** | `src/neuralosd/mcp.py:73` (`_instance`), `:110` (`_role_instance`) |
| **Verdict** | Enforced by invalidation; `absent→loaded` is the only edge. |

---

## Enforcement summary

| Machine | Single source of truth | Guard at every mutation | Illegal refused by name | Tests |
|---|---|---|---|---|
| Actuation | `LEGAL_TRANSITIONS` + `_plan_state` | yes (apply/undo) | yes | `test_actuation_fsm.py`, `test_actuate_cli.py` |
| Ask cache | `_cache_load`/`_cache_store` | yes | n/a (miss) | malformed sweep + cache tests |
| Gate | `_gate_decide` | n/a (pure) | reason token | `test_gate_reason_vocabulary.py` |
| Lifecycle | `load_instance` | yes | yes (multi-instance) | lifecycle tests |
| Mothball | `_write_mothballed` | yes | n/a | `test_mothball_state.py` |
| Banks | `_load_bank` + mutators | yes | yes (proof required) | bank tests |
| Watch | `run_cycle` | yes | n/a | `test_v2_8_1_gossip_watch.py` |
| Audit provenance | `audit_source` ContextVar | yes | n/a | `test_refusal_audit_schema.py` |
| Multi serve | `make_handler` | yes | 400/403/404 | `test_multi_instance.py` |
| Single serve | `service.serve` | yes | 400/401/403/422 | `test_service_refusal_contract.py` |
| Pi bridge | `_roundtrip`/`wait` | partial (timeout limit) | n/a | provider tests |
| Synapse | `_classify_reply` | n/a (pure) | unknown → not trusted | mesh protocol tests |
| Async runs | `RUN_LEGAL_TRANSITIONS` | yes (one lock) | yes (`IllegalRunTransition`) | `test_agentic_state_machines.py` |
| HITL tokens | `CONFIRM_STATES` | yes (single-use/TTL) | yes (`invalid`/`expired`/`already_used`) | `test_agentic_state_machines.py` |
| Scheduler | `_save`/`_run` | yes (RLock + atomic) | n/a | `test_scheduler_state.py` |
| Door registry | `_save`/`_load` | yes (atomic) | unknown name refused | `test_scheduler_state.py` (pattern) |
| Sidecar | `is_provisioned` | yes (explicit) | n/a | provision tests |
| Agent stores | bounds in `agentic.py` | yes (caps) | n/a | `test_agentic_state_machines.py` |
