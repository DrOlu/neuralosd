"""Tests for the component-drift detector that drives the auto-update.

Hermetic: PyPI is never contacted — `latest_version` is monkeypatched — so the
drift logic, the snapshot round-trip and the version bump are all exercised
offline.
"""
import json
import sys

import pytest

sys.path.insert(0, "scripts")
import component_versions as cv  # noqa: E402


@pytest.fixture(autouse=True)
def _protect_the_real_repository():
    """No test may modify the real repo.

    Added after a bound-default bug in bump_version() made one of these tests
    bump the repository's own pyproject.toml (1.2.3 -> 1.2.4) instead of the
    temp copy it was handed. Tests that touch version files must be provably
    sandboxed.
    """
    before = {p: p.read_text(encoding="utf-8")
              for p in (cv.PYPROJECT, cv.INIT_PY, cv.SNAPSHOT) if p.is_file()}
    yield
    changed = [str(p) for p, text in before.items()
               if p.read_text(encoding="utf-8") != text]
    assert not changed, f"test modified the real repository: {changed}"


# ── pre-release handling ───────────────────────────────────────────────────

@pytest.mark.parametrize("v", ["1.0.0rc1", "2.0.0a3", "1.2.3b1", "1.0.0.dev4",
                               "3.0.0b2", "1.0.0-alpha1"])
def test_prereleases_are_recognised(v):
    assert cv.is_prerelease(v) is True


@pytest.mark.parametrize("v", ["1.0.0", "2.13.5", "0.10.5", "3.10.2"])
def test_stable_versions_are_not_prereleases(v):
    assert cv.is_prerelease(v) is False


# ── snapshot round-trip ────────────────────────────────────────────────────

def test_snapshot_round_trip(tmp_path):
    p = tmp_path / "components.json"
    cv.save_snapshot({"neuralos": "1.2.3", "boxlite": "4.5.6"}, path=p)
    got = cv.load_snapshot(p)["components"]
    assert got["neuralos"] == "1.2.3"
    assert got["boxlite"] == "4.5.6"
    # every watched component is present, even ones we did not pass
    assert set(got) == set(cv.COMPONENTS)


def test_missing_snapshot_is_empty(tmp_path):
    assert cv.load_snapshot(tmp_path / "nope.json") == {}


def test_corrupt_snapshot_is_empty_not_fatal(tmp_path):
    p = tmp_path / "components.json"
    p.write_text("{not json", encoding="utf-8")
    assert cv.load_snapshot(p) == {}


# ── drift detection ────────────────────────────────────────────────────────

@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pin the network and the snapshot path; return a setter for versions."""
    latest = {}
    monkeypatch.setattr(cv, "latest_version", lambda name, timeout=20.0: latest.get(name))
    snap = tmp_path / "components.json"
    monkeypatch.setattr(cv, "SNAPSHOT", snap)
    return {"latest": latest, "path": snap,
            "record": lambda versions: cv.save_snapshot(versions, path=snap)}


def test_no_snapshot_means_nothing_changed(frozen):
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    rep = cv.check()
    assert rep["changed"] is False
    assert rep["needs_record"] is True          # first run: record a baseline
    assert all(r["status"] == "new" for r in rep["components"])


def test_quiet_when_versions_match(frozen):
    v = {k: "1.0.0" for k in cv.COMPONENTS}
    frozen["record"](v)
    frozen["latest"].update(v)
    rep = cv.check()
    assert rep["changed"] is False
    assert rep["needs_record"] is False
    assert all(r["status"] == "same" for r in rep["components"])


def test_runtime_drift_is_detected(frozen):
    frozen["record"]({**{k: "1.0.0" for k in cv.COMPONENTS},
                      "neuralos": "0.9.0"})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    rep = cv.check()
    assert rep["changed"] is True
    assert rep["runtime_changed"] == ["neuralos"]
    assert rep["build_changed"] == []


def test_build_scope_drift_is_separate(frozen):
    """A Nuitka bump must rebuild binaries WITHOUT a new PyPI release."""
    frozen["record"]({**{k: "1.0.0" for k in cv.COMPONENTS},
                      "nuitka": "0.1.0"})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    rep = cv.check()
    assert rep["changed"] is True
    assert rep["runtime_changed"] == []
    assert rep["build_changed"] == ["nuitka"]


def test_multiple_runtime_components(frozen):
    frozen["record"]({**{k: "1.0.0" for k in cv.COMPONENTS},
                      "boxlite": "0.0.1", "microsandbox": "0.0.1"})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    rep = cv.check()
    assert sorted(rep["runtime_changed"]) == ["boxlite", "microsandbox"]


def test_prerelease_is_skipped_by_default(frozen):
    frozen["record"]({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"]["neuralos"] = "2.0.0rc1"
    rep = cv.check()
    assert rep["changed"] is False
    status = {r["name"]: r["status"] for r in rep["components"]}
    assert status["neuralos"] == "prerelease-skipped"


def test_prerelease_can_be_allowed(frozen):
    frozen["record"]({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"]["neuralos"] = "2.0.0rc1"
    rep = cv.check(allow_prerelease=True)
    assert rep["changed"] is True


def test_unreachable_pypi_is_reported_not_fatal(frozen):
    """A PyPI outage must not look like 'everything changed'."""
    frozen["record"]({k: "1.0.0" for k in cv.COMPONENTS})
    # latest stays empty -> None for every component
    rep = cv.check()
    assert rep["changed"] is False
    assert set(rep["unknown"]) == set(cv.COMPONENTS)
    assert all(r["status"] == "unknown" for r in rep["components"])


def test_unknown_components_do_not_trigger_a_release(frozen):
    frozen["record"]({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    frozen["latest"]["neuralos"] = None            # one flaky lookup
    frozen["latest"]["boxlite"] = "9.9.9"          # and one real change
    rep = cv.check()
    assert rep["changed"] is True
    assert "neuralos" in rep["unknown"]
    assert rep["runtime_changed"] == ["boxlite"]


def test_resolved_keeps_recorded_when_lookup_fails(frozen):
    frozen["record"]({k: "7.7.7" for k in cv.COMPONENTS})
    rep = cv.check()                                # all lookups fail
    assert rep["resolved"]["neuralos"] == "7.7.7"   # never record a blank


# ── outputs / summary ──────────────────────────────────────────────────────

def test_outputs_are_written_in_actions_format(frozen, tmp_path):
    frozen["record"]({**{k: "1.0.0" for k in cv.COMPONENTS}, "boxlite": "0.1.0"})
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    out = tmp_path / "out.txt"
    cv.write_outputs(cv.check(), path=str(out))
    body = out.read_text()
    assert "changed=true" in body
    assert "runtime_changed=true" in body
    assert "components=boxlite" in body
    assert "summary<<" in body and "EOF" in body


def test_outputs_are_a_noop_without_actions(frozen, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    cv.write_outputs(cv.check(), path=None)       # must not raise


def test_summary_table_lists_every_component(frozen):
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    text = cv.table(cv.check())
    for name in cv.COMPONENTS:
        assert f"`{name}`" in text


# ── version bumping ────────────────────────────────────────────────────────

def _fake_repo(tmp_path, version="1.2.3"):
    py = tmp_path / "pyproject.toml"
    py.write_text(f'[project]\nname = "neuralosd"\nversion = "{version}"\n',
                  encoding="utf-8")
    init = tmp_path / "__init__.py"
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return py, init


@pytest.mark.parametrize("part,expect", [("patch", "1.2.4"),
                                         ("minor", "1.3.0"),
                                         ("major", "2.0.0")])
def test_bump_version(tmp_path, part, expect):
    py, init = _fake_repo(tmp_path)
    old, new = cv.bump_version(part, pyproject=py, init_py=init)
    assert (old, new) == ("1.2.3", expect)
    assert f'version = "{expect}"' in py.read_text()
    assert expect in init.read_text()


def test_bump_keeps_other_version_strings(tmp_path):
    py, init = _fake_repo(tmp_path)
    py.write_text(py.read_text() + 'requires-python = ">=3.10"\n', encoding="utf-8")
    cv.bump_version("patch", pyproject=py, init_py=init)
    assert 'requires-python = ">=3.10"' in py.read_text()


def test_bump_handles_short_versions(tmp_path):
    py, init = _fake_repo(tmp_path, version="1.2")
    _, new = cv.bump_version("patch", pyproject=py, init_py=init)
    assert new == "1.2.1"


def test_current_version_reads_pyproject(tmp_path):
    py, _ = _fake_repo(tmp_path, version="9.8.7")
    assert cv.current_version(py) == "9.8.7"


def test_current_version_errors_without_version(tmp_path):
    py = tmp_path / "pyproject.toml"
    py.write_text("[project]\nname='x'\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cv.current_version(py)


# ── CLI ────────────────────────────────────────────────────────────────────

def test_cli_record_requires_check(capsys):
    assert cv.main(["--record"]) == 2
    assert "--record must be combined with --check" in capsys.readouterr().err


def test_cli_check_json(frozen, capsys):
    frozen["latest"].update({k: "1.0.0" for k in cv.COMPONENTS})
    assert cv.main(["--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["needs_record"] is True


def test_cli_bump_writes_actions_output(tmp_path, monkeypatch, capsys):
    py, init = _fake_repo(tmp_path)
    monkeypatch.setattr(cv, "PYPROJECT", py)
    monkeypatch.setattr(cv, "INIT_PY", init)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cv.main(["--bump", "patch"]) == 0
    body = out.read_text()
    assert "old_version=1.2.3" in body and "new_version=1.2.4" in body


# ── the watch list itself ──────────────────────────────────────────────────

def test_every_component_declares_a_scope_and_reason():
    for name, meta in cv.COMPONENTS.items():
        assert meta["scope"] in ("runtime", "build"), name
        assert meta["why"].strip(), name


def test_the_known_integrations_are_watched():
    for required in ("neuralos", "boxlite", "microsandbox", "openpyxl"):
        assert required in cv.COMPONENTS


def test_components_declared_in_pyproject_are_watched():
    """Nothing may be integrated without also being watched for drift."""
    import re
    text = cv.PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"\[project\.optional-dependencies\](.*?)(\n\[|\Z)",
                      text, re.S)
    assert block, "no optional-dependencies section"
    declared = set()
    for line in block.group(1).splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        rhs = line.split("=", 1)[1]
        for pkg in re.findall(r'"([A-Za-z0-9_.-]+)', rhs):
            declared.add(pkg.lower())
    unwatched = {p for p in declared if p not in cv.COMPONENTS}
    # pytest/pytest-asyncio are dev-only tooling, not integrated components
    unwatched -= {"pytest", "pytest-asyncio"}
    assert not unwatched, f"unwatched extras: {sorted(unwatched)}"
