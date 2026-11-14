# F-series audit — findings

Branch `audit/f-series`. Baseline before any change: **1254 passed, 8
skipped** (`Python 3.14.7`, `/tmp/nosd/venv`, editable install of this
source). Every finding below was reproduced before it was fixed; the
reproduction is stated so it can be re-run.

Severity: **P0** crash / data corruption / a safety guard that does not
exist; **P1** wrong behaviour on a safety-critical path; **P2** state that
lies or robustness that turns a small failure into a large one; **P3**
hygiene.

| id | sev | area | one line |
|---|---|---|---|
| F1 | P1 | actuation | one-shot guard was “has ever been undone”, so a later apply fired twice |
| F2 | P1 | actuation | a second undo fired the reverse action again |
| F3 | P1 | actuation | apply/undo accepted a substituted action/target and journaled it as the plan |
| F4 | P2 | actuation | one torn trailing journal line crashed every later command |
| F5 | P2 | actuation | pre-v2.9.10 rows (no `op`) were invisible to the guard |
| F6 | P0 | service | `X-NeuralOS-Role` without `acl.json` raised `NameError` and reset the socket |
| F7 | P1 | multi serve | malformed/empty questions were coerced and answered; query strings broke routes |
| F8 | P2 | router | `REFUSAL_REASONS` omitted three reasons the gate emits |
| F9 | P2 | router | corrupt instance `router.json` silently fell through to cwd/defaults |
| F10 | P3 | CLI | dead `_cmd/gossip.py` read attributes no parser sets |
| F11 | P0 | CLI | `--plan <id>` without `--apply` fired the action |
| F12 | P3 | instance/banks | file handles leaked in `export` and four bank readers |
| F13 | P2 | lifecycle | non-atomic `mothballed.json` write + crash-on-corruption reader |
| F14 | P2 | watch | per-rule `webhook` was dropped, so it was never delivered |

---

## G-series — 3.0.0 agentic state machines

The 3.0.0 refresh extended the census to the state the agentic surface
introduced. These defects were reproduced before fixing; each carries a
test.

| id | sev | area | one line |
|---|---|---|---|
| G1 | P2 | async runs | a cancellation observed during the probe was overwritten by the late `done` (the run reported success) |
| G2 | P2 | async runs | terminal states were mutable: a late worker could move `done`/`cancelled` again |
| G3 | P2 | HITL | a second confirm and an expired token both reported "invalid or expired"; an agent could not tell "already did this" from "try again" |
| G4 | P0 | scheduler | `remove()` held a non-reentrant lock and called `_save()`, which took it again — every remove deadlocked |
| G5 | P2 | scheduler | `schedules.json` and each result file were written non-atomically (and a result file leaked its handle) |
| G6 | P3 | traces | `TraceStore.max_events` was never enforced; a long agent run grew one JSONL without bound |

Tests: `tests/test_agentic_state_machines.py` (G1–G3, G6),
`tests/test_scheduler_state.py` (G4–G5).

---

## H-series — 3.0.1 production hardening

The commercial-readiness pass attacked the surface with hostile inputs.
Each finding is reproduced in `tests/test_agentic_hardening.py`.

| id | sev | area | one line |
|---|---|---|---|
| H1 | P0 | sessions | a caller-supplied `session_id` became a filename verbatim — `../../etc/passwd` escaped `.agentic/memory`, and a deeper id could escape the instance directory |
| H2 | P1 | HTTP | no body limit: one request could make the server allocate unbounded RAM (`read(n)` with an enormous `Content-Length`) |
| H3 | P2 | HTTP | `/audit?limit=abc` raised inside the handler and reset the connection instead of answering |
| H4 | P2 | policy | a non-numeric ACL budget (`calls: "many"`) raised `ValueError` on every request |
| H5 | P1 | MCP | any allowed agent could call the mutating tools (`build`/`shadow`/`mothball`/`delegate`) |
| H6 | P2 | HITL | a probe removed between the pending token and the confirmation raised `KeyError` into the surface |
| H7 | P3 | memory | `MemoryStore` had no bound; a long-lived session grew its JSONL forever |
| H8 | P3 | maps | `TraceStore._counts`, `BudgetStore._hits` and `MemoryStore._counts` grew per caller-supplied id |

Fixes: session ids are sanitised and hash-suffixed (`safe_name`); HTTP
bodies are capped at 1 MB with a bounded drain before the 413; the audit
query streams with a safe limit; ACL numbers are parsed defensively;
mutating tools require `"admin": true` once an `agents` block exists;
`_execute_confirmed` refuses by name when the probe is gone; memory rows and
the id maps are bounded and pruned.

---

## I-series — 3.0.2 security review

| id | sev | area | one line |
|---|---|---|---|
| I1 | P0 | identity | `agent_id` was self-asserted over HTTP; any valid token could claim an `admin: true` agent |
| I2 | P1 | data access | any authenticated caller could read every agent's `/audit` rows and `/runs/{id}` |
| I3 | P1 | binding | library `serve()` defaulted to `0.0.0.0` with no auth; multi mode only warned on wide bind |
| I4 | P0 | deploy | deploy start commands interpolated the directory into a shell string and a generated `os.system` call — quote/`$()`/`;` executable |
| I5 | P1 | deploy | the inner service bound `0.0.0.0` unauthenticated inside the VM |
| I6 | P2 | snapshots | a snapshot label was a filename; `../` escaped `snapshots/` |
| I7 | P2 | scheduler | a schedule name was a filename; `../` escaped `scheduled/` |
| I8 | P2 | HTTP | unbounded worker threads and no socket timeout (slowloris/resource exhaustion) |
| I9 | P2 | roles | an agent token could select a role via `X-NeuralOS-Role` instead of its ACL role |

Fixes: per-agent `token_sha256` binds identity, role, PII and budget; agents
see only their own audit/runs; wide binds fail closed without a credential;
deploy uses `shlex.quote` and a list-based `Popen` and binds the inner
service to loopback; snapshot labels and schedule names reject separators;
HTTP caps workers at 64 with a 30 s socket timeout; an agent token's role is
its ACL role. `docs/security.md` documents the model and its limits.
Tests: `tests/test_security_hardening.py`.

---

## F1 — the one-shot guard was order-insensitive (P1)

**Reproduction** (library): dry-run `p`; apply `p`; undo `p`; apply `p`;
apply `p` again. All four side effects fire, including two applies in a row
with no undo between them.

**Cause** — `actuate.py` computed `undone = [any row with op undo]` and then
allowed the apply whenever `already and undone`. Once a plan had *ever* been
undone, the guard was permanently disabled for it.

**Fix** — the state is the plan's **last** transition, not a set of
historical flags (`_plan_state`). `APPLIED` permits only `UNDO`; `UNDONE`
permits only `APPLY`; a refusal prints the state and the legal set.

**Test** — `tests/test_actuation_fsm.py::test_one_earlier_undo_does_not_disable_the_guard`.

## F2 — double undo (P1)

**Reproduction**: apply; `--undo`; `--undo` again → the undo side effect
fires twice and the journal carries two `undone: true` rows.

**Cause** — the undo branch looked up the applied row but never checked
whether the plan was already undone.

**Fix** — `UNDONE` + undo is an illegal transition named in the error
(`a second undo would fire the reverse action twice`). Re-apply is the legal
way forward.

**Test** — `test_actuation_fsm.py::test_double_undo_never_fires_twice`,
`::test_undo_from_undone_is_the_only_reverse`.

## F3 — plan substitution (P1)

**Reproduction**: dry-run `block_ip 203.0.113.9`; then
`--apply --plan p --action quarantine_file --target /etc/passwd`. The
library fires the side effect and journals `applied: true` for the
substituted action/target — an effect that was never reviewed.

**Cause** — the apply branch only looked up the dry-run by `plan_id`; it
never compared the action/target with what the plan declared. Undo had the
same gap (it journaled the caller's action/target, not the applied one).

**Fix** — the plan's identity record (last dry-run/apply row) must match
`action` and `target` at both ends (`_same_plan`). Reason and operator stay
per-entry (a second operator may undo the first operator's action).

**Test** — `test_actuation_fsm.py::test_apply_cannot_substitute_the_action`,
`::test_apply_cannot_substitute_the_target`,
`::test_undo_cannot_substitute_the_target`.

## F4 — a torn journal line bricked actuation (P2)

**Reproduction**: append `{"op": "apply", "plan_id": "torn` (no newline) to
`actuation_journal.jsonl`; every later `actuate` raises
`JSONDecodeError: Unterminated string`.

**Cause** — `_load_journal` was `[json.loads(line) for line in fh]`.

**Fix** — `_read_journal` tolerates exactly one torn **trailing** line (the
normal result of a crash between write and newline). Malformed data with
valid rows after it means rows were lost or reordered, so the machine
refuses by name (`actuation journal is corrupt at line N ... refusing to
derive state from a partial history`) instead of guessing. The writer
(`_repair_torn_tail`) truncates an unparseable tail before appending, so the
artifact cannot become a permanent middle-line corruption. Appends are
fsync'd.

**Test** — `test_actuation_fsm.py::test_torn_trailing_line_is_tolerated_and_repaired`,
`::test_corrupt_middle_line_is_refused_by_name`;
`tests/test_actuate_cli.py::TestF4CliSurvivesATornJournal` (subprocess: no
traceback, refused by state).

## F5 — legacy rows were invisible to the guard (P2)

**Reproduction**: write an apply row in the pre-v2.9.10 shape (no `op`,
`applied: true`, `dry_run: false`); apply the plan again → allowed.

**Cause** — the guard selected `e.get("op") == "apply"`; older rows have no
`op`.

**Fix** — `_op_of` infers the op from the old flags (`dry_run` → dry-run,
`applied` → apply, `undone` → undo), so an upgraded process still refuses a
pre-upgrade duplicate. The writer keeps emitting `op`.

**Test** — `test_actuation_fsm.py::test_legacy_applied_row_blocks_reapply`,
`::test_legacy_dry_run_row_still_allows_apply`.

## F6 — role header without ACL reset the socket (P0)

**Reproduction**: `POST /ask` with `X-NeuralOS-Role: analyst` to a server
without `acl.json` → `NameError: name '_acl_roles' is not defined` inside
the handler; the client sees `RemoteDisconnected`, not a response.

**Cause** — `service.py` tested `not _acl_roles`, a name that exists only as
a local inside `resolve_role_instance`.

**Fix** — one `acl_roles(instance_dir)` reader used by both the resolver and
the boundary; the documented 403 is returned. Also corrected the module
docstring: `/ready` is authenticated (it executes a probe).

**Test** — `tests/test_service_refusal_contract.py::test_role_header_without_acl_is_a_403_not_a_dead_socket`.

## F7 — the multi door coerced malformed questions (P1)

**Reproduction**: `POST /alpha/ask` with `{"question": 42}` answers the
question “42”; `{"question": ""}` performs a real ask; `POST
/alpha/ask?trace=1` 404s even though auth treated it as `/ask`.
`GET /healthz?lb=1` also 404'd.

**Cause** — `multi.py` used `str(payload.get("question") or "").strip()`
(no type check, no empty check) and dispatched on the raw `self.path`.

**Fix** — the multi door now mirrors `service.serve`: body must be a JSON
object, `question` must be a string, empty/whitespace is a 400. GET/POST
normalize the path (query string and trailing slash) for both auth and
dispatch. The docstring's `POST /query` claim is marked **not implemented**
instead of advertising a route that 404s.

**Test** — `tests/test_multi_instance.py::test_multi_ask_rejects_malformed_bodies_like_serve`,
`::test_multi_routes_ignore_the_query_string`.

## F8 — refusal vocabulary drift (P2)

**Reproduction**: set `floor` above a probe's score → `below_floor`;
two equally covering probes → `ambiguous`; “higher or lower” with no
comparison-claiming probe → `comparative`. All three are written to
`ask_audit.jsonl` and returned over HTTP, but none is in
`REFUSAL_REASONS`.

**Cause** — the declared set beside `GATE_VERSION` was never updated when
the calibrated gate added the reasons.

**Fix** — the three tokens are declared. `GATE_VERSION` stays 1: this
corrects the declaration of tokens the code already emitted; no new
semantics. An AST test now fails if `_gate_decide` ever returns a literal
outside the set.

**Test** — `tests/test_gate_reason_vocabulary.py` (declaration + one
refusal/audit row per reason).

## F9 — corrupt instance config silently changed the operating point (P2)

**Reproduction**: write torn JSON to `<instance>/router.json`; construct
the Router from that instance dir. It silently uses the cwd's
`router.json` or the defaults.

**Cause** — instance-scoped and legacy-cwd candidates shared one
`except: pass`.

**Fix** — an **instance-scoped** candidate that exists but cannot be read
(non-object, torn JSON, unreadable) refuses by name and path, and says it
will not fall back. Legacy cwd candidates stay tolerant (a stray
`router.json` in the shell must not break a process).

**Test** — `tests/test_gate_config_corruption.py` (4 cases: torn, non-object,
corrupt legacy tolerated, instance file wins).

## F10 — dead gossip shim (P3)

**Reproduction** — the generalized dest-contract sweep: `_cmd/gossip.py`
reads `a.gossip_push` and `a.gossip_pull`; `cli.build_parser()` never sets
either (its subparser sets `mesh_cmd`, and `cli.py` handles gossip-push/pull
inline). The module was unreachable dead code — the E5 bug class with no
live caller.

**Fix** — removed `src/neuralosd/_cmd/gossip.py`; the inline CLI branches
remain the single implementation. The actuate-only dest test is generalized
to every shim, with the fallback-group rule (`or`-chained reads resolve if
any name is a real dest) so deliberate backwards-compatible shims pass.

**Test** — `tests/test_cli_dest_contract.py` (36 shims).

## F11 — `--plan` alone fired the action (P0)

**Reproduction**: dry-run `p`, then
`neuralosd actuate ... --plan p` (no `--apply`) → exit 0, journal gains an
`apply` row, the side effect fires. `--undo --plan p` (the spelling the
success notice used to print) was also routed to APPLY. `--undo` alone
silently created a dry-run.

**Cause** — the shim passed `a.plan` straight through and only checked
`--apply` without a plan, never a plan without `--apply`.

**Fix** — the CLI flag grammar is explicit: `--apply` and `--undo` are
mutually exclusive; `--apply` requires `--plan`; **`--plan` without
`--apply` is refused** (a selector is not a trigger); `--undo` requires a
plan, accepts the legacy `--undo --plan` spelling, and the notice prints the
canonical `--undo --undo-plan`.

**Test** — `tests/test_actuate_cli.py::TestF11PlanIsASelectorNotATrigger`
(5 cases).

## F12 — leaked file handles (P3)

**Reproduction** — sweep for `open(...).read()` in expressions. `Instance.export`
was `json.dump(self.menu, open(path, "w", ...))`; `banks.py` had four
`json.loads(open(path, encoding="utf-8").read())` sites.

**Fix** — context managers for `export`; one `_load_bank(path)` reader for
the banks. The same expression form was then cleared in `durable.py`,
`_cmd/doctor.py`, `mcp.py`, `_cmd/scrub.py`, `scheduler.py` (load and save)
and `loop.write_badge`. Behaviour unchanged (absent file → `{"items": []}`).

**Test** — `tests/test_instance_export_handle.py` (handle closed after
`export`, using a tracking `open`).

## F13 — mothballed marker could tear or crash (P2)

**Reproduction**: write torn JSON to `mothballed.json` → `mothballed_names`
raises (fleet/SLO crash); a failed in-place write leaves a truncated file,
which a tolerant reader would interpret as “nothing is mothballed” and
silently reinstate every demoted probe.

**Fix** — writes stage a `tmp` file, fsync, then `os.replace`; the reader
is tolerant and ignores non-string entries (matching
`_cmd/_common._mothballed_names`). `reinstate` returns `False` on damage
rather than raising.

**Test** — `tests/test_mothball_state.py` (corrupt marker, non-string
entries, atomic failure leaves the old file intact).

## F14 — per-rule webhooks never fired (P2)

**Reproduction**: a rule with `"webhook": "http://hooks/one"` trips; a
`poster` passed to `run_cycle` records nothing for it, because
`evaluate_rules` built the alert without the key and `run_cycle` fell back
to the global webhook.

**Fix** — the alert carries `rule.get("webhook")`; delivery uses it.

**Test** — `tests/test_v2_8_1_gossip_watch.py::test_per_rule_webhook_is_kept_and_delivered`
(unit + end-to-end through `run_cycle`).

---

## Observations not fixed (recorded, not hidden)

- **Pi Durable `readline()` has no independent timeout**
  (`src/neuralosd/pidurable.py:155`). `wait(timeout=…)` bounds the caller,
  but a child that hangs mid-reply is only reclaimed by the process timeout.
  A proper fix needs `select`/a reader thread; out of scope for a
  backwards-compatible patch release.
- **Remaining `open(...).read()` one-liners** now only in
  `src/neuralosd/pisources.py` (`:203,214,228,250,285,313,342,480,520,544`),
  the Pi scaffolding that reads a few small files while assembling a prompt.
  Same P3 class; CPython closes them at refcount zero, and they all pass
  `encoding="utf-8"` already.
- **`POST /query` (cross-instance)** was advertised in `multi.py`'s
  docstring but never routed (404). The docstring now says so; implementing
  it is a feature, not a fix.
- **`_cmd/init.py` generated-code template** embeds `open(spec["path"],
  newline="", ...)` inside a string; the generated file opens CSV with
  `newline=""` (correct for the `csv` module). No action.

## Sweeps run

| sweep | result |
|---|---|
| `python -m pytest -q` (baseline) | 1254 passed, 8 skipped |
| `scripts/malformed_input_sweep.py` | 14/14 cases held |
| `scripts/fp_fn_sweep.py` | PASS — trigger recall 100%, answers 100%, refusal precision 100%, 0 collisions, 0 crashes |
| `open(` without `encoding=` in `src/` | only binary/URL/subprocess/JS opens; no text read/write missing an encoding |
| `getattr(a, …)` vs argparse dests, all shims | one offender (F10); fixed |
| `json.loads(open(...).read())` / `json.load(open(...))` | F12 sites fixed; remainder listed above |
| mutated journals (torn tail, torn middle, legacy rows) | F4/F5 fixed |
| role header / malformed multi bodies over real sockets | F7/F6 fixed |
