from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "batch":
            return self._batch_action(actor, entity, action, data or {}, expected_version)
        if kind == "sample" and action == "report_result":
            return self._report_result(actor, entity, data or {}, expected_version)
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

    def _batch_action(self, actor, entity, action, data, expected_version):
        # Validate role / status / required fields up front; the repository
        # performs the state change and side effects in one transaction.
        self.rules.validate_transition(actor, entity, action, dict(data), self._lookup)
        if action == "add_sample":
            return self._add_sample_to_batch(actor, entity, data, expected_version)
        if action == "submit":
            return self._submit_batch(actor, entity, expected_version)
        if action in ("return_batch", "cold_chain_abnormal"):
            return self._return_batch(actor, entity, action, data, expected_version)
        if action == "complete":
            return self._complete_batch(actor, entity, expected_version)
        raise InvalidTransition("unknown batch action: " + action)

    def _add_sample_to_batch(self, actor, entity, data, expected_version):
        sample_id = data.get("sample_id")
        batch, sample, state = self.repository.add_batch_sample(
            entity["id"], sample_id, actor.user_id, expected_version
        )
        if state == "voided":
            self.audit.record(
                sample["id"],
                actor,
                "void",
                sample["status"],
                "void",
                {"reason": "超过保存期限，无法入批", "batch_id": entity["id"]},
            )
            return sample
        self.audit.record(
            entity["id"],
            actor,
            "add_sample",
            entity["status"],
            batch["status"],
            {"sample_id": sample_id, "state": state},
        )
        return batch

    def _submit_batch(self, actor, entity, expected_version):
        batch, shipped = self.repository.submit_batch(
            entity["id"], actor.user_id, expected_version
        )
        for sample in shipped:
            self.audit.record(
                sample["id"],
                actor,
                "ship",
                sample["status"],
                "in_transit",
                {"batch_id": entity["id"]},
            )
        self.audit.record(entity["id"], actor, "submit", "open", "submitted", {})
        return batch

    def _return_batch(self, actor, entity, action, data, expected_version):
        reason = data.get("reason")
        batch, voided = self.repository.return_batch(
            entity["id"], reason, actor.user_id, expected_version
        )
        for sample in voided:
            self.audit.record(
                sample["id"],
                actor,
                "void",
                sample["status"],
                "void",
                {"reason": "退回时超过保存期限，样本作废", "batch_id": entity["id"]},
            )
        self.audit.record(
            entity["id"],
            actor,
            action,
            "submitted",
            "open",
            {"reason": reason, "voided": [sample["id"] for sample in voided]},
        )
        return batch

    def _complete_batch(self, actor, entity, expected_version):
        batch, released = self.repository.complete_batch(
            entity["id"], actor.user_id, expected_version
        )
        for sample in released:
            self.audit.record(
                sample["id"],
                actor,
                "release_from_batch",
                sample["status"],
                sample["status"],
                {"batch_id": entity["id"]},
            )
        self.audit.record(
            entity["id"],
            actor,
            "complete",
            "submitted",
            "completed",
            {"released": [sample["id"] for sample in released]},
        )
        return batch

    def _report_result(self, actor, entity, data, expected_version):
        result = data.get("result")
        conclusion_at = data.get("conclusion_at") or utcnow()
        self.rules.validate_transition(
            actor, entity, "report_result", dict(data), self._lookup
        )
        sample, action, reopened = self.repository.report_result(
            entity["id"], result, conclusion_at, actor.user_id, expected_version
        )
        if action == "duplicate":
            self.audit.record(
                entity["id"],
                actor,
                "duplicate_result",
                sample["status"],
                sample["status"],
                {"result": result, "conclusion_at": conclusion_at},
            )
            return sample
        if action == "late":
            self.audit.record(
                entity["id"],
                actor,
                "late_result_ignored",
                sample["status"],
                sample["status"],
                {
                    "result": result,
                    "conclusion_at": conclusion_at,
                    "effective_conclusion_at": sample["data"].get("conclusion_at"),
                },
            )
            return sample
        self.audit.record(
            entity["id"],
            actor,
            "report_result",
            entity["status"],
            sample["status"],
            {"result": result, "conclusion_at": conclusion_at},
        )
        for case in reopened:
            self.audit.record(
                case["id"],
                actor,
                "reopen",
                case["status"],
                "reconsider",
                {
                    "sample_id": entity["id"],
                    "reason": "lab conclusion changed to %s" % result,
                },
            )
        return sample

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
