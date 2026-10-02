import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _utc(days=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("inspector", "inspector")
        self.lab = Actor("lab", "lab")
        self.athlete = self.service.create(
            self.admin, "athlete", {"name": "A", "discipline": "cycling"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _sample(self, code, collected=True):
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": self.athlete["id"], "sample_code": code, "event": "national"},
        )
        if collected:
            self.service.transition(
                self.admin, sample["id"], "collect", {"collected_at": "2026-01-01T08:00:00Z"}
            )
            self.service.transition(
                self.admin, sample["id"], "seal", {"seal_id": "SEAL-" + code}
            )
        return sample

    def _batch(self, capacity=2, storage_until=None):
        return self.service.create(
            self.inspector,
            "batch",
            {
                "box_id": "BOX-1",
                "lab_slot": "2026-01-10T09:00:00Z",
                "storage_until": storage_until or _utc(30),
                "capacity": capacity,
            },
        )

    def _add(self, batch, sample):
        return self.service.transition(
            self.inspector, batch["id"], "add_sample", {"sample_id": sample["id"]}
        )

    # -- creation ----------------------------------------------------------

    def test_batch_requires_box_slot_deadline_capacity(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "batch", {"lab_slot": "x", "storage_until": _utc(1), "capacity": 2})
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "batch", {"box_id": "B", "storage_until": _utc(1), "capacity": 2})
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "batch", {"box_id": "B", "lab_slot": "x", "capacity": 2})
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "batch", {"box_id": "B", "lab_slot": "x", "storage_until": _utc(1)})
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "batch", {"box_id": "B", "lab_slot": "x", "storage_until": _utc(1), "capacity": 0})

    # -- add to batch ------------------------------------------------------

    def test_add_collected_sample(self):
        sample = self._sample("S1")
        batch = self._batch()
        updated = self._add(batch, sample)
        self.assertEqual(updated["status"], "open")
        self.assertIn(sample["id"], updated["data"]["sample_ids"])
        self.assertEqual(self.repo.find_active_batch_for_sample(sample["id"]), batch["id"])

    def test_add_requires_collected_sample(self):
        sample = self._sample("S1", collected=False)
        batch = self._batch()
        with self.assertRaises(ValidationError):
            self._add(batch, sample)

    def test_expired_sample_is_voided_on_add(self):
        sample = self._sample("S1")
        batch = self._batch(storage_until=_utc(-1))
        result = self._add(batch, sample)
        self.assertEqual(result["kind"], "sample")
        self.assertEqual(result["status"], "void")
        self.assertIn("超过保存期限", result["data"]["void_reason"])
        self.assertIsNone(self.repo.find_active_batch_for_sample(sample["id"]))

    def test_voiding_expired_sample_removes_it_from_holder_batch(self):
        sample = self._sample("S1")
        holder = self._batch(storage_until=_utc(30))
        self._add(holder, sample)
        expired_batch = self._batch(storage_until=_utc(-1))
        result = self._add(expired_batch, sample)
        self.assertEqual(result["status"], "void")
        # holder batch's lists no longer reference the voided sample
        holder = self.service.get(holder["id"])
        self.assertNotIn(sample["id"], holder["data"].get("sample_ids", []))
        self.assertIsNone(self.repo.find_active_batch_for_sample(sample["id"]))

    def test_same_sample_cannot_be_on_two_batches(self):
        sample = self._sample("S1")
        b1 = self._batch()
        b2 = self._batch()
        self._add(b1, sample)
        with self.assertRaises(ConflictError):
            self._add(b2, sample)

    def test_full_box_queues_sample(self):
        s1 = self._sample("S1")
        s2 = self._sample("S2")
        s3 = self._sample("S3")
        batch = self._batch(capacity=2)
        self._add(batch, s1)
        self._add(batch, s2)
        updated = self._add(batch, s3)
        self.assertIn(s1["id"], updated["data"]["sample_ids"])
        self.assertIn(s2["id"], updated["data"]["sample_ids"])
        self.assertIn(s3["id"], updated["data"]["queue"])
        # queued sample is still locked to the batch
        self.assertEqual(self.repo.find_active_batch_for_sample(s3["id"]), batch["id"])

    def test_ship_blocked_when_batched(self):
        sample = self._sample("S1")
        batch = self._batch()
        self._add(batch, sample)
        with self.assertRaises(ValidationError):
            self.service.transition(self.inspector, sample["id"], "ship", {"carrier": "Courier"})

    # -- submit / return / complete ---------------------------------------

    def test_submit_sends_loaded_samples_in_transit(self):
        s1 = self._sample("S1")
        s2 = self._sample("S2")
        batch = self._batch(capacity=1)
        self._add(batch, s1)
        self._add(batch, s2)  # queued, not in the box
        self.service.transition(self.inspector, batch["id"], "submit", {})
        self.assertEqual(self.service.get(s1["id"])["status"], "in_transit")
        self.assertEqual(self.service.get(s2["id"])["status"], "sealed")

    def test_lab_return_returns_batch_to_open(self):
        sample = self._sample("S1")
        batch = self._batch()
        self._add(batch, sample)
        self.service.transition(self.inspector, batch["id"], "submit", {})
        updated = self.service.transition(
            self.lab, batch["id"], "return_batch", {"reason": "lab rejection"}
        )
        self.assertEqual(updated["status"], "open")
        # sample stays on the batch, can be resubmitted
        self.assertEqual(self.repo.find_active_batch_for_sample(sample["id"]), batch["id"])

    def test_cold_chain_abnormal_returns_batch(self):
        sample = self._sample("S1")
        batch = self._batch()
        self._add(batch, sample)
        self.service.transition(self.inspector, batch["id"], "submit", {})
        updated = self.service.transition(
            self.lab, batch["id"], "cold_chain_abnormal", {"reason": "temperature excursion"}
        )
        self.assertEqual(updated["status"], "open")

    def test_return_with_expired_deadline_voids_loaded_and_promotes_queue(self):
        s1 = self._sample("S1")
        s2 = self._sample("S2")
        batch = self._batch(capacity=1)
        self._add(batch, s1)
        self._add(batch, s2)
        self.service.transition(self.inspector, batch["id"], "submit", {})
        # age the storage deadline into the past
        conn = self.repo._connect()
        import json
        conn.execute(
            "UPDATE entities SET data = ? WHERE id = ?",
            (json.dumps({"box_id": "BOX-1", "lab_slot": "2026-01-10T09:00:00Z",
                         "storage_until": _utc(-1), "capacity": 1}, sort_keys=True),
             batch["id"]),
        )
        conn.commit()
        conn.close()
        updated = self.service.transition(
            self.lab, batch["id"], "return_batch", {"reason": "lab rejection"}
        )
        self.assertEqual(updated["status"], "open")
        self.assertEqual(self.service.get(s1["id"])["status"], "void")
        # queued s2 promoted into the freed box slot
        self.assertIn(s2["id"], updated["data"]["sample_ids"])
        self.assertNotIn(s2["id"], updated["data"]["queue"])

    def test_complete_releases_locks(self):
        sample = self._sample("S1")
        batch = self._batch()
        self._add(batch, sample)
        self.service.transition(self.inspector, batch["id"], "submit", {})
        self.service.transition(self.lab, batch["id"], "complete", {})
        self.assertIsNone(self.repo.find_active_batch_for_sample(sample["id"]))

    # -- lab results -------------------------------------------------------

    def _adverse_case(self, sample):
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "adverse", "conclusion_at": "2026-01-05T10:00:00Z"},
        )
        case = self.service.create(
            self.admin,
            "case",
            {"athlete_id": self.athlete["id"], "sample_id": sample["id"], "alleged_rule": "sub-1"},
        )
        self.service.transition(self.admin, case["id"], "provisional_suspend", {"reason": "adverse"})
        self.service.transition(self.admin, case["id"], "schedule_hearing", {"hearing_at": "2026-02-01"})
        case = self.service.transition(self.admin, case["id"], "decide", {"decision": "sanction"})
        return case

    def test_changed_conclusion_reopens_case(self):
        sample = self._sample("S1")
        case = self._adverse_case(sample)
        self.assertEqual(case["status"], "closed")
        self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "cleared", "conclusion_at": "2026-01-06T10:00:00Z"},
        )
        case = self.service.get(case["id"])
        self.assertEqual(case["status"], "reconsider")
        # panel re-confirms and can close again
        case = self.service.transition(self.admin, case["id"], "decide", {"decision": "no_sanction"})
        self.assertEqual(case["status"], "closed")

    def test_late_conclusion_does_not_overwrite(self):
        sample = self._sample("S1")
        self._adverse_case(sample)
        # newer conclusion: cleared
        self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "cleared", "conclusion_at": "2026-01-06T10:00:00Z"},
        )
        # late arrival of an older adverse conclusion must be ignored
        updated = self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "adverse", "conclusion_at": "2026-01-04T10:00:00Z"},
        )
        self.assertEqual(updated["data"]["lab_result"], "cleared")
        self.assertEqual(updated["data"]["conclusion_at"], "2026-01-06T10:00:00Z")

    def test_duplicate_result_is_idempotent(self):
        sample = self._sample("S1")
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        first = self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "adverse", "conclusion_at": "2026-01-05T10:00:00Z"},
        )
        second = self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "adverse", "conclusion_at": "2026-01-05T10:00:00Z"},
        )
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(second["data"]["lab_result"], "adverse")

    def test_conflicting_result_same_time_rejected(self):
        sample = self._sample("S1")
        self.service.transition(self.admin, sample["id"], "ship", {"carrier": "Courier"})
        self.service.transition(self.lab, sample["id"], "receive", {"lab_id": "LAB-1"})
        self.service.transition(
            self.lab, sample["id"], "report_result",
            {"result": "adverse", "conclusion_at": "2026-01-05T10:00:00Z"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.lab, sample["id"], "report_result",
                {"result": "cleared", "conclusion_at": "2026-01-05T10:00:00Z"},
            )

    # -- concurrency -------------------------------------------------------

    def test_concurrent_add_same_sample_only_one_succeeds(self):
        sample = self._sample("S1")
        b1 = self._batch()
        b2 = self._batch()
        barrier = threading.Barrier(2)
        results = {"ok": 0, "conflict": 0}
        lock = threading.Lock()

        def add(actor, batch_id):
            barrier.wait()
            try:
                self.service.transition(actor, batch_id, "add_sample", {"sample_id": sample["id"]})
                with lock:
                    results["ok"] += 1
            except ConflictError:
                with lock:
                    results["conflict"] += 1

        t1 = threading.Thread(target=add, args=(self.inspector, b1["id"]))
        t2 = threading.Thread(target=add, args=(self.inspector, b2["id"]))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(results["ok"], 1)
        self.assertEqual(results["conflict"], 1)
        # exactly one batch holds the lock
        holder = self.repo.find_active_batch_for_sample(sample["id"])
        self.assertIn(holder, (b1["id"], b2["id"]))


if __name__ == "__main__":
    unittest.main()
