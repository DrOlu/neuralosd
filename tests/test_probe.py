"""Unit tests for the @probe decorator."""
import pytest
from neuralosd import probe, enum_arg, pattern_arg, int_arg


class TestProbeDecorator:
    def test_basic(self):
        @probe(description="Add two numbers",
               triggers=["add two numbers", "sum"],
               args={"a": int_arg(), "b": int_arg()})
        def add(a: int, b: int) -> int:
            return a + b
        meta = add._probe
        assert meta.name == "add"
        assert "add two numbers" in meta.triggers
        assert "sum" in meta.triggers

    def test_custom_name(self):
        @probe(description="x", triggers=[], name="custom_name")
        def my_fn(): pass
        assert my_fn._probe.name == "custom_name"

    def test_confirm_flag(self):
        @probe(description="risky", triggers=[], confirm=True)
        def risky(): pass
        assert risky._probe.confirm is True

    def test_pii_fields(self):
        @probe(description="x", triggers=[], pii=["email"])
        def get_user(): pass
        assert "email" in get_user._probe.pii

    def test_tier(self):
        @probe(description="x", triggers=[], tier="sandbox:boxlite:my-box")
        def sandboxed(): pass
        assert "boxlite" in sandboxed._probe.tier

    def test_args_specs(self):
        @probe(description="x", triggers=[],
               args={"country": enum_arg(["USA", "UK"]),
                     "name": pattern_arg(r"by (.+)"),
                     "limit": int_arg(1, 25, default=10)})
        def multi(country: str, name: str, limit: int): pass
        assert "values" in multi._probe.args["country"]
        assert "pattern" in multi._probe.args["name"]
        assert multi._probe.args["limit"]["default"] == 10


class TestEnumArg:
    def test_enum_arg(self):
        spec = enum_arg(["a", "b"])
        assert spec["type"] == "enum"
        assert spec["values"] == ["a", "b"]

    def test_optional_enum(self):
        spec = enum_arg(["a"], required=False)
        assert spec.get("required") is False


class TestPatternArg:
    def test_pattern_arg(self):
        spec = pattern_arg(r"by (.+)")
        assert spec["type"] == "pattern"
        assert spec["pattern"] == r"by (.+)"


class TestIntArg:
    def test_int_arg_with_default(self):
        spec = int_arg(1, 25, default=10)
        assert spec["min"] == 1
        assert spec["max"] == 25
        assert spec["default"] == 10

    def test_int_arg_without_default(self):
        spec = int_arg(1, 25)
        assert "default" not in spec or spec.get("default") is None
