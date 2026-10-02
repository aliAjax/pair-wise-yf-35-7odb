from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def parse_instant(value):
    """Parse an ISO-8601 datetime; naive values are treated as UTC."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("invalid datetime: %s" % (value,))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def effective_storage_deadline(sample_data, batch_data):
    """Earliest applicable storage deadline for a sample inside a batch."""
    candidates = []
    if batch_data.get("storage_deadline"):
        candidates.append(parse_instant(batch_data["storage_deadline"]))
    if sample_data and sample_data.get("storage_deadline"):
        candidates.append(parse_instant(sample_data["storage_deadline"]))
    if not candidates:
        raise ValidationError("storage deadline is not registered")
    return min(candidates)


def sample_storage_expired(sample_data, batch_data, now=None):
    reference = now or datetime.now(timezone.utc)
    return reference >= effective_storage_deadline(sample_data, batch_data)


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")
    if data.get("storage_deadline"):
        parse_instant(data["storage_deadline"])


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_batch(actor, data, lookup):
    capacity = data.get("capacity")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValidationError("capacity must be a positive integer")
    if not str(data.get("box_id", "")).strip():
        raise ValidationError("box_id is required")
    if not str(data.get("lab_slot", "")).strip():
        raise ValidationError("lab_slot is required")
    parse_instant(data.get("storage_deadline"))
    data.setdefault("sample_ids", [])
    data.setdefault("queued_sample_ids", [])
    data.setdefault("results", {})
    data.setdefault("processed_result_ids", [])


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


def _validate_case_reconfirm(actor, entity, data, lookup):
    if not entity["data"].get("needs_reconfirmation"):
        raise ValidationError("case does not require reconfirmation")
    outcome = data.get("outcome")
    if outcome not in ("upheld", "overturned"):
        raise ValidationError("outcome must be upheld or overturned")
    next_status = "dismissed" if outcome == "overturned" else entity["status"]
    return next_status, {
        "needs_reconfirmation": False,
        "reconfirmed_by": actor.user_id,
        "reconfirmation_outcome": outcome,
    }


def _validate_batch_add_sample(actor, entity, data, lookup):
    sample_id = data.get("sample_id")
    sample = _find_one(lookup, "sample", "id", sample_id)
    if not sample:
        raise ValidationError("sample not found: " + str(sample_id))
    if sample["status"] != "collected":
        raise ValidationError(
            "sample must be collected before batching (current status: %s)"
            % sample["status"]
        )
    box = entity["data"].get("sample_ids", [])
    queued = entity["data"].get("queued_sample_ids", [])
    if sample_id in box or sample_id in queued:
        raise ValidationError("sample is already attached to this batch")
    if sample_storage_expired(sample["data"], entity["data"]):
        raise ValidationError("storage deadline has passed; sample cannot enter the batch")


def _validate_batch_dispatch(actor, entity, data, lookup):
    if not entity["data"].get("sample_ids"):
        raise ValidationError("cannot dispatch an empty batch")


def _validate_batch_return(actor, entity, data, lookup):
    if entity["data"].get("results"):
        raise ValidationError("cannot return a batch after lab results are reported")


def _validate_batch_report_result(actor, entity, data, lookup):
    if data.get("result") not in ("adverse", "negative"):
        raise ValidationError("result must be adverse or negative")
    if data.get("sample_id") not in entity["data"].get("sample_ids", []):
        raise ValidationError("sample is not in this batch")
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValidationError("seq must be a non-negative integer")
    if not str(data.get("result_id", "")).strip():
        raise ValidationError("result_id is required")


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'batch': _validate_batch}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('case', 'reconfirm'): _validate_case_reconfirm, ('batch', 'add_sample'): _validate_batch_add_sample, ('batch', 'dispatch'): _validate_batch_dispatch, ('batch', 'lab_return'): _validate_batch_return, ('batch', 'cold_chain_breach'): _validate_batch_return, ('batch', 'report_result'): _validate_batch_report_result}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'batches': 'batch'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'batch': 'assembling'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received', 'analyzed'), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared'), 'revert_result': (('adverse', 'cleared'), 'analyzed'), 'void': (('scheduled', 'collected', 'sealed'), 'voided')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed'), 'reconfirm': (('open', 'suspended', 'hearing', 'closed', 'appeal'), None)}, 'batch': {'add_sample': (('assembling',), 'assembling'), 'dispatch': (('assembling',), 'dispatched'), 'receive': (('dispatched',), 'received'), 'lab_return': (('dispatched', 'received'), 'assembling'), 'cold_chain_breach': (('assembling', 'dispatched'), 'assembling'), 'report_result': (('received',), 'received'), 'close': (('assembling', 'received'), 'closed')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule'), 'batch': ('box_id', 'lab_slot', 'storage_deadline', 'capacity')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('sample', 'void'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('case', 'reconfirm'): ('outcome',), ('batch', 'add_sample'): ('sample_id',), ('batch', 'lab_return'): ('reason',), ('batch', 'cold_chain_breach'): ('reason',), ('batch', 'report_result'): ('sample_id', 'result', 'result_id', 'seq')}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'batch': ('admin', 'inspector')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'revert_result': ('admin', 'lab'), 'void': ('admin', 'inspector'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'reconfirm': ('admin', 'panel'), ('batch', 'add_sample'): ('admin', 'inspector'), ('batch', 'dispatch'): ('admin', 'inspector'), ('batch', 'receive'): ('admin', 'lab'), ('batch', 'lab_return'): ('admin', 'lab'), ('batch', 'cold_chain_breach'): ('admin', 'inspector', 'lab'), ('batch', 'report_result'): ('admin', 'lab'), ('batch', 'close'): ('admin', 'inspector')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = {}
        if custom:
            outcome = custom(actor, entity, data, lookup)
            if isinstance(outcome, tuple):
                next_status, extra = outcome
            elif outcome:
                extra = outcome
        if next_status is None:
            next_status = entity["status"]
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
