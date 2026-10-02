from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import (
    RuleEngine,
    effective_storage_deadline,
    parse_instant,
    sample_storage_expired,
)

UNFINISHED_BATCH_STATUSES = ("assembling", "dispatched", "received")


class DomainService:
    BATCH_ACTIONS = (
        "add_sample",
        "dispatch",
        "receive",
        "lab_return",
        "cold_chain_breach",
        "report_result",
        "close",
    )

    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _tx_lookup(self, tx):
        return lambda kind, field, value: tx.find_entities(
            self.rules.normalize_kind(kind), field, value
        )

    @staticmethod
    def _tx_audit(tx, entity_id, actor, action, from_status, to_status, detail=None):
        tx.append_audit(
            entity_id, actor.user_id, actor.role, action, from_status, to_status, detail or {}
        )

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if (
            self.rules.normalize_kind(entity["kind"]) == "batch"
            and action in self.BATCH_ACTIONS
        ):
            return self._batch_transition(actor, entity_id, action, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # ---- shipment batch orchestration (all steps run inside one transaction) ----

    def _batch_transition(self, actor, batch_id, action, data, expected_version):
        handlers = {
            "add_sample": self._batch_add_sample,
            "dispatch": self._batch_dispatch,
            "receive": self._batch_receive,
            "lab_return": self._batch_return,
            "cold_chain_breach": self._batch_return,
            "report_result": self._batch_report_result,
            "close": self._batch_close,
        }
        handler = handlers[action]
        with self.repository.transaction() as tx:
            batch = tx.get_entity(batch_id)
            if not batch:
                raise NotFoundError("entity not found: " + batch_id)
            expected = (
                int(expected_version) if expected_version is not None else batch["version"]
            )
            return handler(tx, actor, batch, action, data, expected)

    def _batch_add_sample(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, _patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        sample_id = data["sample_id"]
        sample = tx.get_entity(sample_id)
        if not sample:
            raise ValidationError("sample not found: " + str(sample_id))
        for other in tx.list_entities(kind="batch"):
            if other["id"] == batch["id"] or other["status"] not in UNFINISHED_BATCH_STATUSES:
                continue
            other_data = other["data"]
            if sample_id in other_data.get("sample_ids", []) or sample_id in other_data.get(
                "queued_sample_ids", []
            ):
                raise ConflictError(
                    "sample %s is already attached to unfinished batch %s"
                    % (sample_id, other["id"])
                )
        batch_data = dict(batch["data"])
        box = list(batch_data.get("sample_ids", []))
        queue = list(batch_data.get("queued_sample_ids", []))
        if len(box) < int(batch_data.get("capacity", 0)):
            box.append(sample_id)
            placement = "box"
        else:
            queue.append(sample_id)
            placement = "queued"
        batch_data["sample_ids"] = box
        batch_data["queued_sample_ids"] = queue
        updated_batch = tx.update_entity(batch["id"], expected, next_status, batch_data)
        sample_data = dict(sample["data"])
        sample_data["batch_id"] = batch["id"]
        sample_data["batch_placement"] = placement
        tx.update_entity(sample_id, sample["version"], "batched", sample_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], next_status,
            {"sample_id": sample_id, "placement": placement},
        )
        self._tx_audit(
            tx, sample_id, actor, "add_to_batch", sample["status"], "batched",
            {"batch_id": batch["id"], "placement": placement},
        )
        return updated_batch

    def _batch_dispatch(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        batch_data = dict(batch["data"])
        batch_data.update(patch)
        batch_data["dispatched_at"] = data.get("dispatched_at") or utcnow()
        moved = []
        for sample_id in batch_data.get("sample_ids", []):
            sample = tx.get_entity(sample_id)
            if not sample or sample["status"] != "batched":
                continue
            sample_data = dict(sample["data"])
            sample_data["shipped_in_batch"] = batch["id"]
            tx.update_entity(sample_id, sample["version"], "in_transit", sample_data)
            self._tx_audit(
                tx, sample_id, actor, "batch_dispatch", "batched", "in_transit",
                {"batch_id": batch["id"]},
            )
            moved.append(sample_id)
        updated = tx.update_entity(batch["id"], expected, next_status, batch_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], next_status,
            {"samples": moved, "queued": list(batch_data.get("queued_sample_ids", []))},
        )
        return updated

    def _batch_receive(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        batch_data = dict(batch["data"])
        batch_data.update(patch)
        batch_data["received_at"] = data.get("received_at") or utcnow()
        moved = []
        for sample_id in batch_data.get("sample_ids", []):
            sample = tx.get_entity(sample_id)
            if not sample or sample["status"] != "in_transit":
                continue
            tx.update_entity(sample_id, sample["version"], "received", dict(sample["data"]))
            self._tx_audit(
                tx, sample_id, actor, "batch_receive", "in_transit", "received",
                {"batch_id": batch["id"]},
            )
            moved.append(sample_id)
        updated = tx.update_entity(batch["id"], expected, next_status, batch_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], next_status,
            {"samples": moved},
        )
        return updated

    def _batch_return(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        batch_data = dict(batch["data"])
        batch_data.update(patch)
        reference = (
            parse_instant(data["occurred_at"])
            if data.get("occurred_at")
            else datetime.now(timezone.utc)
        )
        history = list(batch_data.get("return_history", []))
        history.append(
            {"action": action, "reason": data.get("reason"), "at": reference.isoformat()}
        )
        batch_data["return_history"] = history
        box = list(batch_data.get("sample_ids", []))
        queue = list(batch_data.get("queued_sample_ids", []))
        for sample_id in box:
            sample = tx.get_entity(sample_id)
            if sample and sample["status"] in ("in_transit", "received"):
                tx.update_entity(
                    sample_id, sample["version"], "batched", dict(sample["data"])
                )
                self._tx_audit(
                    tx, sample_id, actor, "batch_return", sample["status"], "batched",
                    {"batch_id": batch["id"], "reason": data.get("reason")},
                )
        kept_box = []
        kept_queue = []
        voided = []
        for sample_id, target in [(sid, kept_box) for sid in box] + [
            (sid, kept_queue) for sid in queue
        ]:
            sample = tx.get_entity(sample_id)
            if not sample:
                continue
            if sample_storage_expired(sample["data"], batch_data, reference):
                deadline = effective_storage_deadline(sample["data"], batch_data)
                reason = (
                    "保存期限已过（截止 %s）：批次 %s 因「%s」退回待入批，样本就地作废"
                    % (deadline.isoformat(), batch["id"], data.get("reason"))
                )
                sample_data = dict(sample["data"])
                sample_data["void_reason"] = reason
                sample_data["voided_at"] = reference.isoformat()
                tx.update_entity(sample_id, sample["version"], "voided", sample_data)
                self._tx_audit(
                    tx, sample_id, actor, "void", sample["status"], "voided",
                    {"batch_id": batch["id"], "reason": reason},
                )
                voided.append(sample_id)
            else:
                target.append(sample_id)
        capacity = int(batch_data.get("capacity", 0))
        promoted = []
        while kept_queue and len(kept_box) < capacity:
            promoted_id = kept_queue.pop(0)
            kept_box.append(promoted_id)
            promoted.append(promoted_id)
            sample = tx.get_entity(promoted_id)
            sample_data = dict(sample["data"])
            sample_data["batch_placement"] = "box"
            tx.update_entity(promoted_id, sample["version"], sample["status"], sample_data)
            self._tx_audit(
                tx, promoted_id, actor, "batch_promote", sample["status"], sample["status"],
                {"batch_id": batch["id"]},
            )
        batch_data["sample_ids"] = kept_box
        batch_data["queued_sample_ids"] = kept_queue
        records = list(batch_data.get("voided_records", []))
        for sample_id in voided:
            records.append(
                {
                    "sample_id": sample_id,
                    "reason": "storage deadline exceeded",
                    "at": reference.isoformat(),
                }
            )
        batch_data["voided_records"] = records
        updated = tx.update_entity(batch["id"], expected, next_status, batch_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], next_status,
            {"reason": data.get("reason"), "voided": voided, "promoted": promoted},
        )
        return updated

    def _batch_close(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        batch_data = dict(batch["data"])
        batch_data.update(patch)
        released = []
        attached = list(batch_data.get("sample_ids", [])) + list(
            batch_data.get("queued_sample_ids", [])
        )
        for sample_id in attached:
            sample = tx.get_entity(sample_id)
            if not sample or sample["status"] != "batched":
                continue
            sample_data = dict(sample["data"])
            sample_data.pop("batch_id", None)
            sample_data.pop("batch_placement", None)
            sample_data["released_from_batch"] = batch["id"]
            tx.update_entity(sample_id, sample["version"], "collected", sample_data)
            self._tx_audit(
                tx, sample_id, actor, "batch_release", "batched", "collected",
                {"batch_id": batch["id"]},
            )
            released.append(sample_id)
        updated = tx.update_entity(batch["id"], expected, next_status, batch_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], next_status,
            {"released": released},
        )
        return updated

    def _batch_report_result(self, tx, actor, batch, action, data, expected):
        lookup = self._tx_lookup(tx)
        next_status, _patch = self.rules.validate_transition(
            actor, batch, action, data, lookup
        )
        batch_data = dict(batch["data"])
        processed = list(batch_data.get("processed_result_ids", []))
        result_id = str(data["result_id"])
        if result_id in processed:
            return batch
        sample_id = data["sample_id"]
        results = dict(batch_data.get("results", {}))
        current = results.get(sample_id)
        seq = data["seq"]
        if current and seq <= current.get("seq", -1):
            ignored = list(batch_data.get("ignored_results", []))
            ignored.append(
                {
                    "result_id": result_id,
                    "sample_id": sample_id,
                    "seq": seq,
                    "reason": "stale_or_out_of_order",
                    "applied_seq": current.get("seq"),
                }
            )
            batch_data["ignored_results"] = ignored
            batch_data["processed_result_ids"] = processed + [result_id]
            updated = tx.update_entity(batch["id"], expected, batch["status"], batch_data)
            self._tx_audit(
                tx, batch["id"], actor, action, batch["status"], batch["status"],
                {"ignored": True, "result_id": result_id, "sample_id": sample_id},
            )
            return updated
        sample = tx.get_entity(sample_id)
        if not sample:
            raise ValidationError("sample not found: " + str(sample_id))
        old_conclusion = current["result"] if current else None
        new_conclusion = data["result"]
        if old_conclusion != new_conclusion:
            sample = self._apply_result_to_sample(
                tx, actor, sample, new_conclusion, batch["id"]
            )
        results[sample_id] = {
            "result": new_conclusion,
            "seq": seq,
            "result_id": result_id,
            "reported_at": data.get("reported_at"),
        }
        processed.append(result_id)
        batch_data["results"] = results
        batch_data["processed_result_ids"] = processed
        flagged = []
        if old_conclusion and old_conclusion != new_conclusion:
            flagged = self._flag_cases_for_reconfirmation(
                tx, actor, sample_id, old_conclusion, new_conclusion, batch["id"]
            )
        updated = tx.update_entity(batch["id"], expected, batch["status"], batch_data)
        self._tx_audit(
            tx, batch["id"], actor, action, batch["status"], batch["status"],
            {
                "sample_id": sample_id,
                "result": new_conclusion,
                "seq": seq,
                "result_id": result_id,
                "conclusion_changed": bool(old_conclusion)
                and old_conclusion != new_conclusion,
                "cases_flagged": flagged,
            },
        )
        return updated

    def _apply_sample_action(self, tx, actor, sample, action, data):
        next_status, patch = self.rules.validate_transition(
            actor, sample, action, data, self._tx_lookup(tx)
        )
        merged = dict(sample["data"])
        merged.update(patch)
        updated = tx.update_entity(sample["id"], sample["version"], next_status, merged)
        self._tx_audit(
            tx, sample["id"], actor, action, sample["status"], next_status,
            {"patch": patch},
        )
        return updated

    def _apply_result_to_sample(self, tx, actor, sample, result, batch_id):
        if sample["status"] in ("adverse", "cleared"):
            sample = self._apply_sample_action(tx, actor, sample, "revert_result", {})
        if sample["status"] in ("received", "analyzed"):
            sample = self._apply_sample_action(
                tx, actor, sample, "analyze", {"result": result, "lab_batch": batch_id}
            )
        else:
            raise ValidationError(
                "sample %s cannot accept a lab result in status %s"
                % (sample["id"], sample["status"])
            )
        if result == "adverse":
            return self._apply_sample_action(tx, actor, sample, "report_adverse", {})
        return self._apply_sample_action(
            tx, actor, sample, "clear",
            {"reason": "lab result negative (batch %s)" % batch_id},
        )

    def _flag_cases_for_reconfirmation(self, tx, actor, sample_id, old, new, batch_id):
        flagged = []
        for case in tx.find_entities("case", "sample_id", sample_id):
            if case["status"] == "dismissed":
                continue
            case_data = dict(case["data"])
            case_data["needs_reconfirmation"] = True
            case_data["reconfirmation_reason"] = (
                "实验室结论变更：%s → %s（批次 %s），案件需重新确认是否成立"
                % (old, new, batch_id)
            )
            tx.update_entity(case["id"], case["version"], case["status"], case_data)
            self._tx_audit(
                tx, case["id"], actor, "flag_reconfirmation", case["status"], case["status"],
                {"sample_id": sample_id, "old": old, "new": new, "batch_id": batch_id},
            )
            flagged.append(case["id"])
        return flagged

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
