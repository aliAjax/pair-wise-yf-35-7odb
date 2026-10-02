import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'athlete', 'kind': 'athlete', 'data': {'name': 'A. Rider', 'discipline': 'cycling'}}, {'op': 'create', 'as': 'sample', 'kind': 'sample', 'data': {'athlete_id': '{athlete}', 'sample_code': 'S-001', 'event': 'national-final'}}, {'op': 'transition', 'target': 'sample', 'action': 'collect', 'data': {'collected_at': '2026-01-01T08:00:00Z'}, 'expect': 'collected'}, {'op': 'transition', 'target': 'sample', 'action': 'seal', 'data': {'seal_id': 'SEAL-1'}, 'expect': 'sealed'}, {'op': 'transition', 'target': 'sample', 'action': 'ship', 'data': {'carrier': 'Courier-A'}, 'expect': 'in_transit'}, {'op': 'transition', 'target': 'sample', 'action': 'receive', 'data': {'lab_id': 'LAB-1'}, 'expect': 'received'}, {'op': 'transition', 'target': 'sample', 'action': 'analyze', 'data': {'result': 'adverse'}, 'expect': 'analyzed'}, {'op': 'transition', 'target': 'sample', 'action': 'report_adverse', 'data': {}, 'expect': 'adverse'}, {'op': 'create', 'as': 'case', 'kind': 'case', 'data': {'athlete_id': '{athlete}', 'sample_id': '{sample}', 'alleged_rule': 'substance-1'}}, {'op': 'transition', 'target': 'case', 'action': 'provisional_suspend', 'data': {'reason': 'adverse A sample'}, 'expect': 'suspended'}, {'op': 'transition', 'target': 'case', 'action': 'schedule_hearing', 'data': {'hearing_at': '2026-02-01'}, 'expect': 'hearing'}, {'op': 'transition', 'target': 'case', 'action': 'decide', 'data': {'decision': 'sanction'}, 'expect': 'closed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
