---
name: msb-neuralos
description: >-
  End-to-end recipe + runnable scripts for running neuralOS (on-device
  tool-calling model) inside Microsandbox (msb) microVMs — cook ONE template
  sandbox with neuralOS preinstalled, snapshot it (disk-only or FULL live
  RAM+process snapshot), then restore/branch it into any number of disposable
  job sandboxes; plus the data-source instance variant (database-backed agent
  with a self-installing first-boot service), Windows-native support notes,
  measured sizing/timings, and every API gotcha met in practice. Use when the
  user mentions Microsandbox, msb, sandboxes, snap/branch/fork of sandboxes,
  full snapshots, libkrun, or running neuralOS in a Microsandbox sandbox.
---

# Microsandbox (msb) ⇄ neuralOS — snapshot once, restore many

Run the on-device neuralOS model inside hardware-isolated microVMs.
**Microsandbox's superpower vs BoxLite**: a *live* sandbox (disk + RAM +
running processes) can be snapshotted in seconds and restored as a **new
running sandbox** (CoW RAM fork) — so you cook one warmed instance and fork
it N times *without any boot at all*.

```
┌──────────────────── host ────────────────────┐
│  your app / msb CLI                          │
│    └── msb serverd ──┬── chinook sandbox     │  self-installing first boot
│                      ├── snapshot (full)     │  6s → 400MB archive
│                      └── forked restores …    │  3.6s, RAM-accurate
│  libkrun (KVM / HVF / WHP — native Windows)  │
└──────────────────────────────────────────────┘
```

## Requirements

- **macOS** Apple Silicon (Hypervisor.framework) / **Linux** KVM /
  **Windows 10+** x64+ARM64 **native** via Windows Hypervisor Platform
  (`HypervisorPlatform` feature — NOT WSL2; `msb doctor --fix` enables it).
- `msb` CLI: `curl -fsSL https://install.microsandbox.dev | sh`
  (Windows: `irm https://install.microsandbox.dev/windows | iex`).
- Guest is always a **Linux microVM** regardless of host OS.

## Quickstart

```bash
SKILL=/Users/olu/.pi/agent/skills/msb-neuralos
bash $SKILL/scripts/env_setup.sh                       # install msb + doctor

# 1. COOK a pure neuralOS template sandbox + full snapshot archive
bash $SKILL/scripts/cook_template.sh --name neuralos-template

# 2. SPIN UP: fork the live template into a job sandbox and ask
bash $SKILL/scripts/spin_up.sh --from neuralos-template --name job-001 \
    --forked --ask "add 4 and 5"

# 3. data-source instance (chinook: MariaDB + instance + web service)
bash $SKILL/scripts/build_data_instance.sh --name chinook-ms
```

---

## How Microsandbox actually works (model in your head)

- A **sandbox** is a Linux microVM (libkrun) with its **own kernel**. Guest is
  always Linux (e.g. Debian trixie for `python:3.12-slim`).
- **`msb create` boots it in the BACKGROUND with agentd (`/init.krun`) as
  PID 1 — the image entrypoint / `--entrypoint` is NOT executed on create.**
  The entrypoint only runs in attached `msb run` mode. Services must be
  launched by background `exec`, or via attached `run` under nohup.
- **Persistence**: a per-sandbox **`upper.ext4`** writable layer (~4 GB sparse)
  + `~/.microsandbox/db/msb.db` config (SQLite). Installed packages and files
  **survive stop/start**; **processes do not** — re-exec on start.
- **`msb exec`** runs commands through the in-guest portal agent — works while
  the sandbox runs, no SSH needed. Root by default.
- **`--replace` recreates from the image** (fresh rootfs; `--copy-dir` patches
  re-applied) — it does NOT keep installed packages. Declarative recreates need
  a **self-installing entrypoint** (idempotent first-boot apt/pip) or you lose
  the stack.
- **Networking**: per-sandbox /30 subnets from `172.16.0.0/12` (docs) —
  sandbox network identities differ per sandbox (unlike BoxLite's identical
  guest IPs). Rich policy: `--net` profiles (public/private/host/all/none),
  `--net-rule allow@target`, `--net-default-egress/ingress`, egress
  bandwidth/packet limits, TLS interception, SOCKS proxy, DNS controls,
  `--vsock` host exposure. Published ports: `-p HOST:GUEST`.
- **Secrets**: `--secret ENV@HOST` — value read from the HOST env at start,
  stored only as a reference, never inlined; violations can block/terminate.
- **Default resources**: 1 vCPU / 512 MB — **bump to `-m 1G` for neuralOS**
  (model + MariaDB need it).
- **Slim images lack** `ps`/procps and `curl` — use `/proc/*/cmdline`
  enumeration and python `urllib` for in-guest HTTP checks.

---

## Phase 0 — Environment (once)

```bash
bash /Users/olu/.pi/agent/skills/msb-neuralos/scripts/env_setup.sh
```

Installs/updates `msb`, runs `msb doctor` (on Windows use `msb doctor --fix`
to enable WHP elevated), verifies virtualization, checks the CLI version.

---

## Phase 1 — COOK a neuralOS template

`cook_template.sh` creates a sandbox, installs neuralOS (engine + ~35 MB
weights bundled in the wheel), **verifies with a real tool call**, then
captures a **full snapshot archive** (disk + RAM + running verifier) — the
template artifact everything else forks from.

```bash
bash $SKILL/scripts/cook_template.sh \
    --name neuralos-template \
    --image python:3.12-slim \
    --cpus 1 --memory 1G \
    --packages "pymysql pydantic" \
    --archive ~/boxlite-lab/microsandbox-archives/neuralos-template.msb
```

| Gate | Check | Pass |
|---|---|---|
| install | `pip install neuralos` exit 0 | engine + weights on disk |
| import | `import needle; needle.__version__` | version prints |
| **tool call** | `add 4 and 5` → executed | `results` contains `9` |
| template | full snapshot archive written | ~300–400 MB |

**Measured timings** (M-series Mac): apt mariadb ~4 m; pip
(pymysql+neuralos+pydantic) ~3.7 m; full snapshot **6.4 s** (400 MB);
`--forked` restore **3.6 s**.

---

## Phase 2 — SPIN UP: restore > run

`spin_up.sh` restores the template snapshot into a new sandbox and runs a
verification suite / single ask.

```bash
# RAM-accurate fork of the live template (boot-free, ~3.6s)
bash $SKILL/scripts/spin_up.sh --from neuralos-template --name job-001 \
    --forked --ask "add 4 and 5"

# cold disk boot instead (fresh processes, service scripts re-run)
bash $SKILL/scripts/spin_up.sh --from neuralos-template --name job-002 \
    --disk-only --ask "what's it like in Lagos right now?"
```

- `--forked` restores **memory + running processes** — the model is already
  loaded; zero boot. **Caveat (verified)**: restored *listeners* can hold
  stale sockets — if a service in a forked sandbox misbehaves, restart the
  service process inside it.
- `--disk-only` cold-boots the captured disk — clean process state, use this
  for services.
- **Rule (same as BoxLite): never fork a box carrying unique persisted
  identity** (gateway keypairs etc.). neuralOS jobs are stateless → safe.

### Request flow inside the sandbox

| Surface | How |
|---|---|
| one-shot ask (CLI) | `msb exec NAME -- python3 /opt/chinook/ask.py /opt/chinook "…"` |
| web service | `demo_server.py --port 8877` launched at boot; `POST /ask` |
| deterministic country asks | `ask.py` fast-path executes the rank-1 probe directly |

---

## Phase 3 — Data-source instance variant (chinook pattern)

`build_data_instance.sh` builds the chinook instance: MariaDB + the neuralOS
instance + a web service, from `~/boxlite-lab/chinook/` (models, menu,
bridge, instance, ask.py, demo_server.py, dump, start.sh).

What it runs (the exact verified sequence):

```bash
msb create --name chinook-ms --hostname chinook -c 1 -m 1G python:3.12-slim
msb exec chinook-ms -- mkdir -p /opt/chinook
msb copy $INSTANCE_DIR chinook-ms:/opt/chinook      # instance files land flat
msb exec chinook-ms -- chmod +x /opt/chinook/start.sh
msb exec chinook-ms -- sh -c 'nohup /opt/chinook/start.sh >/var/log/chinook-outer.log 2>&1 & echo launched'
# start.sh is SELF-INSTALLING: first boot does apt mariadb + pip deps +
# mariadb init + admin grants + one-time dump import, then serves :8877
```

**`start.sh` contract** (self-installing first boot — required because
`create` doesn't run entrypoints and `--replace` wipes rootfs):

1. idempotent `apt-get install mariadb-server` (+procps) if `mariadbd` missing
2. idempotent `pip install pymysql neuralos pydantic` if imports missing
3. init datadir if empty → start `mysqld_safe` in background → wait
4. ensure `admin`/`admin` user (**grants for BOTH `%` and `localhost`**)
5. import dump **once** (sentinel: skip if `chinook_mysql.Track` exists)
6. `exec python3 demo_server.py --port 8877`

**Poll for readiness** with in-guest python (no curl on slim images):
`POST http://127.0.0.1:8877/ask` until HTTP 200 and the payload contains the
expected sentinel (e.g. `"Track": 3503`). Verified first-boot-to-serving:
**~8 minutes** (apt + pip + model); subsequent boots **~5–15 s**.

**Verification gates** (suite in `scripts/suite.py`, run in-guest):
possessive country asks return ONLY that country; global ask returns 10
multi-country rows; overview contains `"Track": 3503`; employees contain
Jane/Steve; AC/DC albums present. Plus SQL truth: 3,503 tracks / $2,328.60.

---

## Phase 4 — Portability & lifecycle

```bash
# export a LIVE sandbox (disk+RAM+processes) to a portable archive
msb snapshot create --sandbox chinook-ms --full -o ~/archives/chinook.msb

# restore elsewhere / later (RAM-accurate fork, port published)
msb snapshot restore ~/archives/chinook.msb --name chinook-restore \
    --forked -p 8878:8877

# cold boot from the captured disk only
msb snapshot restore ~/archives/chinook.msb --name chinook-cold --disk-only
```

Snapshots can also be installed as **named groups** (omit `-o`) and restored
by group name. `--forked` = CoW RAM (child shares pages); plain restore
materializes the full RAM image.

## Sizing (measured)

| Sandbox | vCPU | RAM | Disk | Observed |
|---|---|---|---|---|
| neuralOS-only | 1 | 1 G | 4 G | model peak ~95 MB |
| chinook (MariaDB+instance+web) | 1 | **1 G** | 6 G | idle 165–178 MB; suite peak ~357 MB; disk 642 MB |

Defaults are 512 MB — **too small**; always pass `-m 1G` (or more) for
neuralOS. `--max-memory`/`--max-cpus` allow hotplug headroom.

## Windows notes (native — a real differentiator)

- Native `msb-windows-x86_64.exe` / `msb-windows-aarch64.exe` (Windows on ARM
  supported), `libkrunfw-windows-*.dll` using **WHP** — no WSL2.
- Requires **Windows 10+**, `HypervisorPlatform` optional feature (separate
  from `VirtualMachinePlatform` that Docker/WSL2 enable), VT-x/AMD-V in
  firmware. `msb doctor --fix` configures it; runtime root
  `%USERPROFILE%\.microsandbox`.
- BoxLite by contrast supports Windows only via WSL2.

---

## msb cheat sheet (0.7.4, verified)

```bash
msb create --name SB -c 1 -m 1G --hostname H --entrypoint /path.sh \
    --copy-dir ./src:/dst --script boot=/path.sh --port 8877:8877 \
    --net-rule allow@api.github.com --secret TOKEN@api.example.com \
    --root-disk 6G python:3.12-slim          # create (background boot)
msb exec SB -- sh -c '…'                      # run commands (root)
msb copy ./file SB:/root/file                 # host → guest (dest dir must exist)
msb stop SB; msb start SB                     # processes die; disk persists
msb branch SB --name child                    # CoW fork of RUNNING sandbox
msb snapshot create --sandbox SB --full -o out.msb
msb snapshot restore out.msb --name N --forked -p 8878:8877
msb snapshot restore out.msb --name N --disk-only
msb list                                      # all sandboxes + status
msb remove SB                                 # delete
msb install SB                                # expose sandbox as a host command
msb doctor [--fix]                            # host virtualization checks
```

---

## Gotchas — every one hit in practice

1. **`create` never runs your entrypoint** (background boot; agentd is PID 1).
   Launch services with background `exec` + nohup, or attached `msb run`.
2. **`--replace` recreates from the image** — installed packages are lost;
   re-apply `--copy-dir`/`--script` and make the entrypoint self-installing.
3. **`msb copy` requires the guest destination directory to exist**
   (pre-`mkdir`), and never target `/tmp`-style special mounts for SDK
   copy-ins.
4. **`function_calls` is empty in needle 3.0.3** even when a tool executed —
   gate on `results`, never on `function_calls` (mirror of the BoxLite lesson).
5. **Stale results**: with no parsed call, `resp["results"]` holds the previous
   ask's data — `ask.py`'s deterministic country fast-path + empty-results
   guard (exit 2) prevent silently mis-answering.
6. **`apt`/`pip` need the policy-rc.d trick** (block service start during
   install) and `DEBIAN_FRONTEND=noninteractive`.
7. **MariaDB admin grants**: create for BOTH `'admin'@'%'` and
   `'admin'@'localhost'` with explicit `IDENTIFIED BY` — a missing localhost
   grant fails the dump import with `ERROR 1044`.
8. **`mysql_install_db` / `mysqld_safe` are deprecated names** on MariaDB
   11.8 — they warn and work; `mariadb-install-db`/`mariadbd-safe` are the new
   names. "A mysqld process already exists" = it's already running.
9. **Slim images lack `ps` and `curl`** — enumerate `/proc/*/cmdline` for
   process checks and use python `urllib` for HTTP probes.
10. **Forked-restores can hold stale sockets** — restart the service inside a
    `--forked` restore if its HTTP misbehaves; prefer `--disk-only` for
    service boxes.
11. **One msb serverd per host** manages all sandboxes; sandbox state lives in
    `~/.microsandbox/` (`sandboxes/<name>/upper.ext4`, `db/msb.db`,
    `logs/{exec,kernel,runtime}.log` — read these to debug boots).
12. **Default memory is 512 MB** — neuralOS + MariaDB need `-m 1G` minimum.
13. **Guest is always Linux** (Debian trixie for python:3.12-slim); Windows
    hosts run Linux microVMs via WHP — the Windows part is the tooling/VMM.
14. **Idle timeouts**: `--idle-timeout` stops inactive sandboxes — set `0` or
    a generous value for long-lived services.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| service never starts after `create` | entrypoints don't run on background create | background `exec` of start.sh (see Phase 3) |
| packages gone after recreate | `--replace`/recreate rebuilds rootfs | self-installing start.sh or snapshot restore |
| `ERROR 1044` on dump import | missing localhost grant | gotcha 7 |
| `ERROR 1193 GTID_PURGED` / `utf8mb4_0900_ai_ci` errors | MySQL-8 dump into MariaDB | `--set-gtid-purged=OFF` + sed collation → `utf8mb4_general_ci` |
| wrong answer to country question | stale results printed | results-based ask.py (deployed by this skill) |
| fork HTTP dead | stale restored sockets | restart service in the fork; or use `--disk-only` |
| `can't find '__main__'` when copying scripts | nested extracted path | exec the real path `<dest>/extracted/<file>` or copy to a flat dir |
| hypervisor unavailable (Windows) | WHP not enabled | `msb doctor --fix`, reboot |

## Fleet operations

| Script | Purpose |
|---|---|
| `scripts/spin_up.sh` | restore full/disk-only/forked + ask + suite (see Phase 2) |
| `scripts/backup.sh` (via boxlite-neuralos) | `--runtime msb` → `msb snapshot create --full -o` with retention |
| `scripts/upgrade.sh` (via boxlite-neuralos pattern) | snapshot rollback point → in-place upgrade → suite → rollback |
| egress lockdown | `--net-default deny` / `--net-rule` at CREATE time (install phase needs network — bake deps into an OCI image for offline boxes; the network policy cannot be changed after create) |

## Deliverables contract

A completed run leaves: the **template snapshot archive**, the **running job
sandboxes**, and a **verification record** — install gate, tool-call gate,
per-question expected results, SQL truth-check (3,503 / $2,328.60), and
boot-persistence evidence. If any gate is unverified, the job is not done.
