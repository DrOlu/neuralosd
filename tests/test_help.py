"""The help surface is a contract: every command has real text and examples.

An orchestrating agent is expected to be able to drive neuralosd from
`--help` / `help --json` alone, so these tests pin completeness, runnable
examples and the machine-readable shape.
"""
import argparse
import json
import shlex

import pytest

from neuralosd import help as H
from neuralosd.cli import build_parser


def _subparsers(parser):
    return next(a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction))


@pytest.fixture(scope="module")
def parser():
    return build_parser()


def test_every_parser_has_a_catalogue_entry(parser):
    top = set(_subparsers(parser).choices)
    assert top == set(H.TOPICS)
    for group, table in (("mesh", H.MESH), ("door", H.DOORS)):
        gp = _subparsers(parser).choices[group]
        assert set(_subparsers(gp).choices) == set(table)


def test_every_entry_is_complete():
    for topic, entry in H.TOPICS.items():
        assert entry["summary"].strip(), topic
        assert len(entry["description"].split()) >= 12, topic
        assert entry["usage"].startswith("neuralosd "), topic
        assert entry["examples"], topic
        assert entry["exits"], topic


def test_examples_are_runnable_and_name_real_commands(parser):
    top = _subparsers(parser).choices
    for topic, entry in H.TOPICS.items():
        for ex in entry["examples"]:
            assert ex.startswith("neuralosd "), (topic, ex)
            argv = shlex.split(ex)
            assert argv[0] == "neuralosd"
            assert argv[1] in top or argv[1] in ("--version", "-h",
                                                 "--help"), (topic, ex)
            if argv[1] not in top:
                continue
            if argv[1] in ("mesh", "door"):
                assert len(argv) > 2, (topic, ex)
                assert argv[2] in _subparsers(top[argv[1]]).choices, (topic, ex)
            for ch in argv[2:]:
                if ch.startswith("-"):
                    break


def test_help_pages_render_description_and_examples(parser):
    top = _subparsers(parser)
    for name, sub in top.choices.items():
        text = sub.format_help()
        entry = H.TOPICS[name]
        assert entry["summary"].split(".")[0][:20] in text or \
            entry["description"].splitlines()[0][:20] in text, name
        assert "Examples:" in text, name
        assert entry["examples"][0] in text, name
        assert "Exit codes:" in text, name
    for group in ("mesh", "door"):
        subchoices = _subparsers(top.choices[group])
        for name, sub in subchoices.choices.items():
            text = sub.format_help()
            entry = H.lookup("%s.%s" % (group, name))
            assert "Examples:" in text, (group, name)
            assert entry["examples"][0] in text, (group, name)


def test_root_help_states_the_contract(parser):
    text = parser.format_help()
    for reason in ("no_probe_matches", "action_intent", "dropped_filter",
                   "low_coverage"):
        assert reason in text
    assert "help --json" in text
    assert "neuralosd help <command>" in text


def test_machine_catalogue_shape(parser):
    doc = H.catalogue(parser)
    assert doc["name"] == "neuralosd"
    assert doc["commands"].keys() == H.TOPICS.keys()
    assert len(doc["groups"]["mesh"]["commands"]) == 20
    assert len(doc["groups"]["door"]["commands"]) == 3
    ask = doc["commands"]["ask"]
    flags = {f for item in ask["flags"] for f in item["flags"]}
    assert "--instance-dir" in flags and "--strict" in flags
    assert ask["mutates"] is False
    assert doc["commands"]["actuate"]["mutates"] is True
    assert doc["groups"]["mesh"]["commands"]["serve"]["flags"]


def test_help_command_outputs(parser, capsys):
    class A:
        topic = None
        json = False

    assert H.run(A()) == 0
    out = capsys.readouterr().out
    assert "command catalogue" in out and "ask" in out

    class J:
        topic = None
        json = True

    assert H.run(J()) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "commands" in doc and doc["version"]

    class Topic:
        topic = "ask"
        json = False

    assert H.run(Topic()) == 0
    page = capsys.readouterr().out
    assert "Examples:" in page and "--instance-dir" in page

    class Nested:
        topic = "mesh.serve"
        json = False

    assert H.run(Nested()) == 0
    assert "heartbeat" in capsys.readouterr().out.lower()

    class Bad:
        topic = "nope"
        json = False

    assert H.run(Bad()) == 2

    class BadJson:
        topic = "nope"
        json = True

    assert H.run(BadJson()) == 2


def test_short_and_long_help_flags_exit_zero(parser):
    for flag in ("-h", "--help"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([flag])
        assert exc.value.code == 0
