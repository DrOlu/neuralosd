"""Tests-first: v2.6.0 — pi as builder and operator (out of the box).

Pi (https://pi.dev, badlogic/pi-mono) is a coding agent that runs headless
(`pi --provider P --model M --print`). neuralosd drives it two ways:

  BUILDER  `neuralosd build "average shipping cost per unit by region"`
           pi works inside the instance directory and writes the probe code;
           NEURALOSD then judges it: forbidden files must be untouched, the
           instance must load, and BOTH banks must replay green. Any
           failure -> the instance is restored to its pre-pi state.
  OPERATOR `neuralosd pi ask "why did refusals go up?"` — pi investigates
           using the closed list of neuralosd commands; its command runs
           are audited like any other subprocess.

Safety model, unchanged: pi proposes, neuralosd verifies. Pi never marks
its own homework as done.

All unit tests run with an INJECTABLE runner — no pi, no model, no network.
One e2e test exercises the REAL pi with the z-ai/glm-5.3-flash model and
is skipped when pi or its auth is absent.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

CSV = "region,units\nNorth,100\nSouth,200\nEast,50\nNorth,30\n"

DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_PROVIDER = "openrouter"


def _build(tmp_path, name="inst", csv=CSV):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(csv, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=None,
                                 join_on=None, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


# ── the runner: command construction + execution ──────────────────────────

def test_pi_command_construction_pins_model_and_scopes_the_agent():
    from neuralosd.pisources import pi_command
    argv = pi_command("check the gaps", model=DEFAULT_MODEL,
                      provider=DEFAULT_PROVIDER)
    joined = " ".join(argv)
    assert "pi" == argv[0]
    assert "--print" in argv                      # headless, non-TUI
    assert "--no-session" in argv                 # stateless one-shot
    assert argv[argv.index("--provider") + 1] == DEFAULT_PROVIDER
    assert argv[argv.index("--model") + 1] == DEFAULT_MODEL
    # scoped: extensions/MCP/skills/context files off — the task prompt is
    # the whole world, nothing else leaks in
    for flag in ("--no-extensions", "--no-mcp", "--no-skills",
                 "--no-context-files"):
        assert flag in argv
    assert "check the gaps" in argv[-1]


def test_pi_run_uses_the_instance_dir_as_cwd_and_names_missing_pi(tmp_path):
    from neuralosd import pisources
    seen = {}

    def fake_execute(argv, cwd=None, timeout=None):
        seen["cwd"] = cwd
        seen["argv"] = argv
        return "pi says: done"

    out = pisources.pi_run(["pi", "--print", "hi"], cwd=str(tmp_path),
                           execute=fake_execute)
    assert out == "pi says: done"
    assert seen["cwd"] == str(tmp_path)

    def boom(argv, cwd=None, timeout=None):
        raise FileNotFoundError("pi")

    with pytest.raises(SystemExit) as e:
        pisources.pi_run(["pi", "--print", "hi"], execute=boom)
    assert "pi" in str(e.value).lower()
    assert "install" in str(e.value).lower()


# ── setup: honest checks, like agent-browser ──────────────────────────────

def test_setup_green_when_pi_and_model_work():
    from neuralosd.pisources import setup
    report = setup(which_fn=lambda n: "/usr/local/bin/pi" if n == "pi"
                   else None,
                   runner=lambda argv, cwd=None, timeout=None:
                       _fake_runner_ok(argv),
                   model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER)
    assert report["installed"] is True
    assert report["ping_ok"] is True
    assert report["model_ok"] is True


def _fake_runner_ok(argv):
    if "--list-models" in argv or "--model" in argv:
        return "ready"                            # model resolves, pi answers
    return "ok"


def test_setup_without_pi_names_the_install():
    from neuralosd.pisources import setup
    with pytest.raises(SystemExit) as e:
        setup(which_fn=lambda n: None,
              runner=lambda argv, cwd=None, timeout=None: "",
              model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER)
    blob = str(e.value).lower()
    assert "pi" in blob and "install" in blob
    assert "npm" in blob or "pi-mono" in blob


def test_setup_with_an_unknown_model_says_so(tmp_path):
    """A model pattern that resolves to nothing is its own honest failure —
    the fix is --list-models, not a silent fallback to some other model."""
    from neuralosd.pisources import setup
    from neuralosd import pisources

    def runner(argv, cwd=None, timeout=None):
        if "--model" in argv:
            raise SystemExit('Error: Model "*nope*" not found. '
                             "Use --list-models to see available models.")
        return "ok"

    report = setup(which_fn=lambda n: "/usr/local/bin/pi", runner=runner,
                   model="*nope*", provider=DEFAULT_PROVIDER)
    assert report["ping_ok"] is False
    assert "list-models" in (report.get("error") or "")


# ── the builder: pi writes, neuralosd judges ──────────────────────────────

def test_build_prompt_carries_the_contract(tmp_path):
    from neuralosd.pisources import build_prompt
    out = _build(tmp_path)
    prompt = build_prompt("average shipping cost per unit by region",
                          instance_dir=out)
    blob = prompt.lower()
    assert "probe" in blob
    assert "@probe" in prompt                    # the decorator contract
    assert "triggers" in prompt.lower()
    assert "golden" in blob and "traps" in blob  # banks are off-limits
    assert "do not" in blob or "never" in blob   # explicit prohibitions


def test_build_restores_the_instance_when_banks_go_red(tmp_path):
    """Pi 'succeeds' but leaves the instance broken (a bank regresses).
    The builder must RESTORE the pre-pi instance and report failure — a
    broken capability must never be presented as delivered."""
    from neuralosd.pisources import build_capability
    out = _build(tmp_path)
    before = open(os.path.join(out, "probes.py"), encoding="utf-8").read()
    golden_before = open(os.path.join(out, "golden.json"),
                         encoding="utf-8").read()

    def bad_pi(argv, cwd=None, timeout=None):
        # corrupt the probes file AND tamper with the trap bank
        with open(os.path.join(cwd, "probes.py"), "a",
                  encoding="utf-8") as fh:
            fh.write("\nTHIS IS NOT PYTHON\n")
        with open(os.path.join(cwd, "traps.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"items": []}')

    report = build_capability("add a probe", instance_dir=out,
                              execute=bad_pi, model=DEFAULT_MODEL,
                              provider=DEFAULT_PROVIDER)
    assert report["ok"] is False
    assert open(os.path.join(out, "probes.py"),
                encoding="utf-8").read() == before
    assert open(os.path.join(out, "golden.json"),
                encoding="utf-8").read() == golden_before


def test_build_reports_success_when_verification_passes(tmp_path):
    from neuralosd.pisources import build_capability
    out = _build(tmp_path)
    before = open(os.path.join(out, "probes.py"), encoding="utf-8").read()

    def harmless_pi(argv, cwd=None, timeout=None):
        pass                                     # pi did nothing harmful

    report = build_capability("add something", instance_dir=out,
                              execute=harmless_pi, model=DEFAULT_MODEL,
                              provider=DEFAULT_PROVIDER)
    assert report["ok"] is True
    assert open(os.path.join(out, "probes.py"),
                encoding="utf-8").read() == before
    assert report["verified"] is True            # banks replayed green


def test_new_probes_are_born_trapped_via_the_builder(tmp_path):
    """The v2.1.0 birth-trap contract holds for pi-built probes too: after a
    successful build, every probe in the menu has its misuse trap banked."""
    from neuralosd.pisources import build_capability
    out = _build(tmp_path)

    def add_real_probe(argv, cwd=None, timeout=None):
        path = os.path.join(cwd, "probes.py")
        src = open(path, encoding="utf-8").read()
        # a VALID edit: define the probe BEFORE registering it in PROBES
        src = src.replace("PROBES = [", """
@probe(description="The single largest units value",
       triggers=["max units", "largest units"])
def max_units():
    vals = [float(r["units"]) for r in bridge.rows()
            if r.get("units") not in (None, "")]
    return {"max": max(vals), "_tool": "max_units"}

PROBES = [max_units, """)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)

    report = build_capability("add a max-units probe", instance_dir=out,
                              execute=add_real_probe, model=DEFAULT_MODEL,
                              provider=DEFAULT_PROVIDER)
    assert report["ok"] is True
    from neuralosd._cmd._common import load_instance
    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    qs = [t["q"] for t in traps["items"]]
    assert any("max_units" in q for q in qs), (
        "the pi-built probe must be born with its misuse trap")

    inst = load_instance(out)
    env = inst.ask("max units", use_cache=False)
    assert env["probe"] == "max_units"


# ── the operator: pi drives the closed command list ───────────────────────

def test_operator_prompt_lists_the_closed_command_set(tmp_path):
    from neuralosd.pisources import operator_prompt
    prompt = operator_prompt("why did refusals go up?", instance_dir=str(
        _build(tmp_path)))
    for cmd in ("gaps", "slo", "golden", "mothball", "synonyms", "drift"):
        assert cmd in prompt


def test_operator_runs_in_the_instance_dir(tmp_path):
    from neuralosd.pisources import operate
    out = _build(tmp_path)
    seen = {}

    def fake_execute(argv, cwd=None, timeout=None):
        seen["cwd"] = cwd
        return "refusals clustered around shipping cost"

    answer = operate("why did refusals go up?", instance_dir=out,
                     execute=fake_execute, model=DEFAULT_MODEL,
                     provider=DEFAULT_PROVIDER)
    assert "shipping cost" in answer
    assert seen["cwd"] == out


# ── the REAL thing: glm-5.3-flash builds and the instance answers ─────────

@pytest.mark.skipif(os.environ.get("NEURALOSD_PI_E2E") != "1",
                    reason="set NEURALOSD_PI_E2E=1 to run the real "
                           "glm-5.3-flash build (needs pi + auth + network)")
def test_e2e_real_pi_builds_a_working_probe(tmp_path):
    from neuralosd.pisources import build_capability
    out = _build(tmp_path)
    report = build_capability(
        'Add a new probe named max_units that answers "what is the maximum '
        'units": return {"max": <largest units value>} from the units '
        'column. Give it triggers ["max units", "largest units"].',
        instance_dir=out, model=DEFAULT_MODEL, provider=DEFAULT_PROVIDER,
        timeout=900)
    assert report["ok"] is True, f"pi build failed: {report}"
    from neuralosd._cmd._common import load_instance
    inst = load_instance(out)
    env = inst.ask("max units", use_cache=False)
    assert env["probe"] == "max_units"
    assert _first(_res(env)) == 200             # North 100 + 30, South 200
    traps = json.loads(open(os.path.join(out, "traps.json"),
                            encoding="utf-8").read())
    assert any("max_units" in t["q"] for t in traps["items"])


def _first(res):
    res = res[0] if isinstance(res, list) else res
    return res.get("max")


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res
