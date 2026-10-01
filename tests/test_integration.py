"""Integration tests: probe → router → deterministic execution."""
import pytest
import json
from neuralosd import probe, enum_arg, pattern_arg, int_arg, Instance, Router


class TestIntegration:
    """End-to-end: declare probes, route, execute, verify."""

    def _make_instance(self):
        @probe(description="Employee database: list all employees",
               triggers=["list employees", "show employees", "employee list"],
               tier="in-process")
        def list_employees():
            return {"employees": [
                {"id": 1, "name": "Alice", "team": "Engineering"},
                {"id": 2, "name": "Bob", "team": "Security"},
            ]}

        @probe(description="Filter employees by team",
               triggers=["employees in team", "employees by team", "team employees"],
               args={"team": enum_arg(["Engineering", "Security", "Product"])})
        def employees_by_team(team: str):
            data = {"Engineering": ["Alice"], "Security": ["Bob"], "Product": []}
            return {"team": team, "employees": data.get(team, [])}

        @probe(description="Get incident count by severity",
               triggers=["incident count", "how many incidents", "open incidents"],
               args={"severity": enum_arg(["P1", "P2", "P3"])})
        def incident_count(severity: str):
            return {"severity": severity, "count": {"P1": 2, "P2": 5, "P3": 10}.get(severity, 0)}

        @probe(description="Delete a record (dangerous)",
               triggers=["delete record", "remove record"],
               args={"id": int_arg(1, 99999)},
               confirm=True)
        def delete_record(id: int):
            return {"deleted": id}

        return Instance(name="hr-system", probes=[
            list_employees, employees_by_team,
            incident_count, delete_record])

    def test_deterministic_enum(self):
        inst = self._make_instance()
        env = inst.ask("employees by team Engineering")
        assert env["mode"] == "deterministic"
        assert "Engineering" in json.dumps(env["results"])

    def test_deterministic_int(self):
        inst = self._make_instance()
        env = inst.ask("incident count P1")
        assert "P1" in json.dumps(env["results"])

    def test_zero_arg_deterministic(self):
        inst = self._make_instance()
        env = inst.ask("list employees")
        assert env["mode"] == "deterministic"
        assert "Alice" in json.dumps(env["results"])

    def test_no_results_refuses(self):
        inst = self._make_instance()
        from neuralosd import NoResults
        with pytest.raises(NoResults):
            inst.ask("quantum entanglement statistics")

    def test_cache_hit(self):
        inst = self._make_instance()
        env1 = inst.ask("list employees")
        env2 = inst.ask("list employees")
        assert env2.get("cached") is True
        assert env1["results"] == env2["results"]

    def test_audit_written(self):
        import tempfile, os
        state = tempfile.mkdtemp()
        inst = Instance(name="audit-test", probes=self._make_instance().probes,
                        state_dir=state)
        inst.ask("list employees")
        audit = os.path.join(state, "ask_audit.jsonl")
        assert os.path.exists(audit)

    def test_pii_mask(self):
        @probe(description="Get user with PII",
               triggers=["get user", "user profile"],
               args={"uid": int_arg(1, 99999)},
               pii=["email", "phone"])
        def get_user(uid: int):
            return {"email": "secret@corp.com", "phone": "+1234", "name": "Test"}

        rt = self._make_instance.__func__(self) if False else None
        # just test the mask function directly
        from neuralosd.router import mask_pii
        data = {"email": "x@y.com", "phone": "+1", "name": "Test"}
        masked = mask_pii(data)
        assert "x@y.com" not in json.dumps(masked)

    def test_menu_export(self):
        inst = self._make_instance()
        menu = inst.menu
        assert len(menu) == 4
        assert all("triggers" in m for m in menu)
        assert all("parameters" in m for m in menu)

    def test_lint_menu(self):
        inst = self._make_instance()
        result = inst.lint()
        assert "hard" in result
        assert "soft" in result
