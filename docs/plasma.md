# Driving Plasma with neuralOSd

[Plasma](https://github.com/DrOlu/plasma) is an agent-based EDR: WHIDS gene
detection, an on-device neuralOS engine, a measured prevention plane, a
policy engine, a hash-chained ledger and a single-use approval queue.

Since Plasma 1.24 every command is published as a machine-readable catalog:

```sh
plasma help --json      # schema plasma.help/1: 179 commands, safety classes
```

neuralOSd treats that catalog as the contract. It does not scrape help text,
build shell strings, or guess flags: a command must exist in the catalog, and
Plasma's own **safety class** decides whether it can run.

## Quick start

```sh
# 1. Scaffold an instance from the live Plasma catalog
neuralosd plasma init --out ./plasma-estate --root .plasma

# 2. Ask in plain English
neuralosd ask --instance-dir ./plasma-estate "is the estate healthy"
neuralosd ask --instance-dir ./plasma-estate "attack coverage"
neuralosd ask --instance-dir ./plasma-estate "agent policy"
neuralosd ask --instance-dir ./plasma-estate "verify the ledger"

# 3. Serve the same instance over the authenticated HTTP API
neuralosd serve --instance-dir ./plasma-estate --port 9998 --api-key "$KEY"
```

`init` writes `probes.py` and a saved `plasma_catalog.json`. The instance
loads without Plasma running; only probe **execution** calls the binary.

## What becomes a probe

Every leaf command whose safety is `read-only` and whose required inputs are
typed (enum choices or integers) becomes a probe. On Plasma 1.24.1 that is 46
probes covering:

| Area | Examples |
|---|---|
| Health and posture | `health`, `status`, `coverage`, `protect status`, `prevent posture` |
| Trust and control | `control policy show`, `control audit verify`, `control audit tail`, `control approvals list`, `control killswitch status` |
| Containment | `enforce pending`, `enforce status` |
| Prevention / intercept | `prevent status`, `intercept status`, `intercept rules list` |
| Detection | `whids status`, `fim status`, `memory status`, `canary status`, `canary hits` |
| Fleet / identity | `fleet inventory`, `identity schedule-status`, `ring status` |
| Evidence | `case list`, `drift list`, `actuate actions`, `actuate journal` |

A probe returns Plasma's JSON unchanged:

```json
{"command": "health", "ok": true,
 "data": {"status": "ok", "checks": [...]},
 "stderr": "", "exit_code": 0}
```

Commands that need a free-form required argument (a SQL string, a file path,
a rule id list) are **not** probes. Run those explicitly with
`neuralosd plasma call`, where you supply the value:

```sh
neuralosd plasma call query --param sql="select host, count(*) from events group by host"
```

Read-only commands are refused by `plasma call` only if their safety class is
not `read-only`; unknown commands are refused before execution.

## Plan-gated actions are two-step

Plasma marks containment and prevention commands `plan-gated and reversible;
requires --apply --plan`. neuralOSd keeps that gate:

```sh
# 1. Dry-run. Plasma decides what it would do; neuralOSd records the plan.
neuralosd plasma plan "enforce run" --root .plasma

# 2. Review the dry-run, then execute exactly that plan.
neuralosd plasma apply plp_0123456789ab --root .plasma
```

- The plan is written to `<root>/neuralosd_plans.jsonl` with the command,
  validated parameters, Plasma's dry-run output and its plan/approval id
  when it returns one.
- `apply` re-validates the command against the catalog, requires it to still
  be plan-gated, adds `--apply` (and `--plan <id>` when Plasma returned one),
  and refuses a second application of the same plan.
- Parameters are validated against the catalog: unknown options, wrong
  choices and `--apply`/`--plan` injection are refused.
- No shell is involved: arguments are an argv list.

Rollback is another plan-gated command, so it uses the same two steps:

```sh
neuralosd plasma plan "prevent rollback" --param id=12 --root .plasma
neuralosd plasma apply plp_... --root .plasma
```

## Catalog, probes and scoping

```sh
neuralosd plasma catalog --read-only
neuralosd plasma catalog --plan-gated --json
neuralosd plasma probes --json

# Only the areas you care about
neuralosd plasma init --out ./plasma-ro --group trust --group detect
neuralosd plasma init --out ./plasma-core \
    --include "control audit verify" --include "enforce pending"
```

`--plasma-bin` (or `$PLASMA_BIN`) selects the executable; `--root` and
`--events` are baked into the generated probes so `neuralosd ask` needs no
extra flags.

## Safety model

| Layer | Guarantee |
|---|---|
| Catalog | Only published Plasma commands; unknown paths refused |
| Read-only probes | `safety` must start with `read-only`; no `--apply` |
| Plan-gated | Dry-run first; explicit local plan id; one-shot apply |
| Arguments | Typed enum/int only for probes; explicit validated params for actions |
| Execution | argv list, no shell, bounded output, timeout |
| Journal | Plans appended under the Plasma root; audit rows in neuralOSd's ledger |

The neuralOSd model is never in the action path: it routes a question to a
read-only probe. Actions are chosen explicitly by an operator, dry-run first,
and applied from a recorded plan.

## Limitations

- Windows-only commands return Plasma's own error on other platforms; it is
  data, not a crash.
- Commands with required free-form arguments are intentionally not probes.
- `neuralosd plasma apply` executes the recorded command; it does not bypass
  Plasma's policy, approval queue or kill switch.
