import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _iso(moment):
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("insp-1", "inspector")
        self.inspector2 = Actor("insp-2", "inspector")
        self.lab = Actor("lab-1", "lab")
        self.panel = Actor("panel-1", "panel")
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def _athlete(self):
        return self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )

    def _sample(self, code="S-001", storage_deadline=None, collect=True):
        athlete = self._athlete()
        data = {
            "athlete_id": athlete["id"],
            "sample_code": code,
            "event": "national-final",
        }
        if storage_deadline:
            data["storage_deadline"] = storage_deadline
        sample = self.service.create(self.inspector, "sample", data)
        if collect:
            sample = self.service.transition(
                self.inspector, sample["id"], "collect",
                {"collected_at": _iso(self.now)},
            )
        return sample

    def _batch(self, capacity=2, deadline=None, box="BOX-1"):
        deadline = deadline or _iso(self.now + timedelta(days=2))
        return self.service.create(
            self.inspector,
            "batch",
            {
                "box_id": box,
                "lab_slot": "2026-10-03 09:00-12:00",
                "storage_deadline": deadline,
                "capacity": capacity,
            },
        )

    def _received_batch(self, sample):
        batch = self._batch()
        self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": sample["id"]}
        )
        self.service.transition(
            self.inspector, batch["id"], "dispatch", {"carrier": "Courier-A"}
        )
        return self.service.transition(self.lab, batch["id"], "receive", {})

    def _report(self, batch_id, sample_id, result, result_id, seq):
        return self.service.transition(
            self.lab,
            batch_id,
            "report_result",
            {
                "sample_id": sample_id,
                "result": result,
                "result_id": result_id,
                "seq": seq,
            },
        )

    def test_batch_registration(self):
        batch = self._batch(capacity=3)
        self.assertEqual(batch["status"], "assembling")
        self.assertEqual(batch["data"]["box_id"], "BOX-1")
        self.assertEqual(batch["data"]["lab_slot"], "2026-10-03 09:00-12:00")
        self.assertEqual(batch["data"]["sample_ids"], [])
        self.assertEqual(batch["data"]["queued_sample_ids"], [])
        with self.assertRaises(ValidationError):
            self._batch(capacity=0)
        with self.assertRaises(ValidationError):
            self.service.create(
                self.inspector,
                "batch",
                {
                    "box_id": "BOX-X",
                    "lab_slot": "slot",
                    "storage_deadline": "not-a-date",
                    "capacity": 1,
                },
            )

    def test_add_sample_requires_collected_and_fresh_sample(self):
        scheduled = self._sample("S-000", collect=False)
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, batch["id"], "add_sample",
                {"sample_id": scheduled["id"]},
            )
        expired_batch = self._batch(
            box="BOX-OLD", deadline=_iso(self.now - timedelta(hours=1))
        )
        fresh = self._sample("S-001")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, expired_batch["id"], "add_sample",
                {"sample_id": fresh["id"]},
            )
        updated = self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": fresh["id"]}
        )
        self.assertEqual(updated["data"]["sample_ids"], [fresh["id"]])
        self.assertEqual(self.service.get(fresh["id"])["status"], "batched")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, batch["id"], "add_sample", {"sample_id": fresh["id"]}
            )

    def test_sample_cannot_join_two_unfinished_batches(self):
        sample = self._sample()
        first = self._batch(box="BOX-1")
        second = self._batch(box="BOX-2")
        self.service.transition(
            self.inspector, first["id"], "add_sample", {"sample_id": sample["id"]}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, second["id"], "add_sample", {"sample_id": sample["id"]}
            )

    def test_queue_when_box_is_full(self):
        first = self._sample("S-001")
        second = self._sample("S-002")
        batch = self._batch(capacity=1)
        self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": first["id"]}
        )
        updated = self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": second["id"]}
        )
        self.assertEqual(updated["data"]["sample_ids"], [first["id"]])
        self.assertEqual(updated["data"]["queued_sample_ids"], [second["id"]])
        queued = self.service.get(second["id"])
        self.assertEqual(queued["status"], "batched")
        self.assertEqual(queued["data"]["batch_placement"], "queued")

    def test_dispatch_and_receive_move_samples(self):
        sample = self._sample()
        batch = self._received_batch(sample)
        self.assertEqual(batch["status"], "received")
        moved = self.service.get(sample["id"])
        self.assertEqual(moved["status"], "received")
        self.assertEqual(moved["data"]["shipped_in_batch"], batch["id"])

    def test_lab_return_voids_expired_samples_and_promotes_queue(self):
        expiring = self._sample(
            "S-EXP", storage_deadline=_iso(self.now + timedelta(hours=1))
        )
        fresh = self._sample("S-FRESH")
        batch = self._batch(capacity=1)
        self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": expiring["id"]}
        )
        self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": fresh["id"]}
        )
        self.service.transition(self.inspector, batch["id"], "dispatch", {})
        returned = self.service.transition(
            self.lab,
            batch["id"],
            "lab_return",
            {
                "reason": "冷链温度超标",
                "occurred_at": _iso(self.now + timedelta(hours=2)),
            },
        )
        self.assertEqual(returned["status"], "assembling")
        self.assertEqual(returned["data"]["sample_ids"], [fresh["id"]])
        self.assertEqual(returned["data"]["queued_sample_ids"], [])
        self.assertEqual(len(returned["data"]["voided_records"]), 1)
        voided = self.service.get(expiring["id"])
        self.assertEqual(voided["status"], "voided")
        self.assertIn("保存期限已过", voided["data"]["void_reason"])
        self.assertIn("冷链温度超标", voided["data"]["void_reason"])
        promoted = self.service.get(fresh["id"])
        self.assertEqual(promoted["status"], "batched")
        self.assertEqual(promoted["data"]["batch_placement"], "box")

    def test_cold_chain_breach_returns_batch_without_voiding_fresh_samples(self):
        first = self._sample("S-001")
        second = self._sample("S-002")
        batch = self._batch()
        for sample in (first, second):
            self.service.transition(
                self.inspector, batch["id"], "add_sample", {"sample_id": sample["id"]}
            )
        self.service.transition(self.inspector, batch["id"], "dispatch", {})
        returned = self.service.transition(
            self.inspector, batch["id"], "cold_chain_breach",
            {"reason": "温度记录仪报警"},
        )
        self.assertEqual(returned["status"], "assembling")
        self.assertEqual(
            sorted(returned["data"]["sample_ids"]), sorted([first["id"], second["id"]])
        )
        self.assertEqual(returned["data"].get("voided_records"), [])
        for sample in (first, second):
            self.assertEqual(self.service.get(sample["id"])["status"], "batched")

    def test_close_releases_attached_samples(self):
        first = self._sample("S-001")
        second = self._sample("S-002")
        batch = self._batch(capacity=1)
        for sample in (first, second):
            self.service.transition(
                self.inspector, batch["id"], "add_sample", {"sample_id": sample["id"]}
            )
        closed = self.service.transition(self.inspector, batch["id"], "close", {})
        self.assertEqual(closed["status"], "closed")
        for sample in (first, second):
            self.assertEqual(self.service.get(sample["id"])["status"], "collected")
        other = self._batch(box="BOX-9")
        updated = self.service.transition(
            self.inspector, other["id"], "add_sample", {"sample_id": second["id"]}
        )
        self.assertEqual(updated["data"]["sample_ids"], [second["id"]])

    def test_results_dedup_out_of_order_and_case_reconfirmation(self):
        sample = self._sample()
        batch = self._received_batch(sample)
        reported = self._report(batch["id"], sample["id"], "adverse", "R-1", 1)
        self.assertEqual(self.service.get(sample["id"])["status"], "adverse")

        replayed = self._report(batch["id"], sample["id"], "adverse", "R-1", 1)
        self.assertEqual(replayed["version"], reported["version"])

        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": sample["data"]["athlete_id"],
                "sample_id": sample["id"],
                "alleged_rule": "substance-1",
            },
        )
        self.service.transition(
            self.panel, case["id"], "provisional_suspend", {"reason": "adverse A sample"}
        )
        self.service.transition(
            self.panel, case["id"], "schedule_hearing", {"hearing_at": "2026-11-01"}
        )
        decided = self.service.transition(
            self.panel, case["id"], "decide", {"decision": "sanction"}
        )
        self.assertEqual(decided["status"], "closed")

        stale = self._report(batch["id"], sample["id"], "negative", "R-0", 0)
        self.assertEqual(self.service.get(sample["id"])["status"], "adverse")
        self.assertEqual(stale["data"]["ignored_results"][0]["result_id"], "R-0")
        self.assertNotIn(
            "needs_reconfirmation", self.service.get(case["id"])["data"]
        )

        self._report(batch["id"], sample["id"], "negative", "R-2", 2)
        self.assertEqual(self.service.get(sample["id"])["status"], "cleared")
        flagged = self.service.get(case["id"])
        self.assertEqual(flagged["status"], "closed")
        self.assertEqual(flagged["data"]["decision"], "sanction")
        self.assertTrue(flagged["data"]["needs_reconfirmation"])

        upheld = self.service.transition(
            self.panel, case["id"], "reconfirm", {"outcome": "upheld"}
        )
        self.assertEqual(upheld["status"], "closed")
        self.assertFalse(upheld["data"]["needs_reconfirmation"])

        self._report(batch["id"], sample["id"], "adverse", "R-3", 3)
        self.assertEqual(self.service.get(sample["id"])["status"], "adverse")
        reflagged = self.service.get(case["id"])
        self.assertTrue(reflagged["data"]["needs_reconfirmation"])
        dismissed = self.service.transition(
            self.panel, case["id"], "reconfirm", {"outcome": "overturned"}
        )
        self.assertEqual(dismissed["status"], "dismissed")

    def test_reconfirm_requires_flag(self):
        sample = self._sample()
        batch = self._received_batch(sample)
        self._report(batch["id"], sample["id"], "adverse", "R-1", 1)
        case = self.service.create(
            self.admin,
            "case",
            {
                "athlete_id": sample["data"]["athlete_id"],
                "sample_id": sample["id"],
                "alleged_rule": "substance-1",
            },
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.panel, case["id"], "reconfirm", {"outcome": "upheld"}
            )

    def test_return_forbidden_after_results_reported(self):
        sample = self._sample()
        batch = self._received_batch(sample)
        self._report(batch["id"], sample["id"], "adverse", "R-1", 1)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, batch["id"], "lab_return", {"reason": "实验室拒收"}
            )

    def test_concurrent_add_same_sample_only_one_succeeds(self):
        sample = self._sample()
        first = self._batch(box="BOX-1")
        second = self._batch(box="BOX-2")
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name, actor, batch_id):
            barrier.wait(timeout=10)
            try:
                self.service.transition(
                    actor, batch_id, "add_sample", {"sample_id": sample["id"]}
                )
                outcomes[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - record the failure type
                outcomes[name] = type(exc).__name__

        threads = [
            threading.Thread(target=worker, args=("a", self.inspector, first["id"])),
            threading.Thread(target=worker, args=("b", self.inspector2, second["id"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(outcomes.values()).count("ok"), 1)
        attached = [
            batch
            for batch in self.service.list("batch")
            if sample["id"] in batch["data"]["sample_ids"]
        ]
        self.assertEqual(len(attached), 1)

    def test_concurrent_add_same_sample_to_same_batch(self):
        sample = self._sample()
        batch = self._batch()
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name, actor):
            barrier.wait(timeout=10)
            try:
                self.service.transition(
                    actor, batch["id"], "add_sample", {"sample_id": sample["id"]}
                )
                outcomes[name] = "ok"
            except Exception as exc:  # noqa: BLE001
                outcomes[name] = type(exc).__name__

        threads = [
            threading.Thread(target=worker, args=("a", self.inspector)),
            threading.Thread(target=worker, args=("b", self.inspector2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(outcomes.values()).count("ok"), 1)
        stored = self.service.get(batch["id"])
        self.assertEqual(stored["data"]["sample_ids"], [sample["id"]])


if __name__ == "__main__":
    unittest.main()
