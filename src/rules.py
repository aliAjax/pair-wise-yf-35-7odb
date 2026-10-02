from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _parse_iso(value, field):
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise ValidationError("%s must be a valid ISO timestamp" % field)


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


def _validate_batch(actor, data, lookup):
    if not str(data.get("box_id", "")).strip():
        raise ValidationError("box_id is required")
    if not str(data.get("lab_slot", "")).strip():
        raise ValidationError("lab_slot is required")
    storage_until = data.get("storage_until")
    if not storage_until:
        raise ValidationError("storage_until is required")
    _parse_iso(storage_until, "storage_until")
    capacity = data.get("capacity")
    if isinstance(capacity, str) and capacity.strip().isdigit():
        capacity = int(capacity.strip())
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValidationError("capacity must be a positive integer")
    data["capacity"] = capacity
    return dict(data)


def _validate_batch_add_sample(actor, entity, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("sample not found")
    if sample["status"] not in ("collected", "sealed"):
        raise ValidationError("sample must be collected before it can be batched")
    return {}


def _validate_batch_reason(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("reason is required")
    return {}


def _validate_sample_ship(actor, entity, data, lookup):
    for field in ("sample_ids", "queue"):
        batched = lookup("batch", field, entity["id"]) or []
        if any(b["status"] in ("open", "submitted") for b in batched):
            raise ValidationError("sample is already allocated to an unfinished batch")
    return {}


def _validate_report_result(actor, entity, data, lookup):
    result = data.get("result")
    if result not in ("adverse", "cleared"):
        raise ValidationError("result must be adverse or cleared")
    conclusion_at = data.get("conclusion_at")
    if conclusion_at is not None:
        _parse_iso(conclusion_at, "conclusion_at")
    return {}


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'case': _validate_case,
    'batch': _validate_batch,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('sample', 'ship'): _validate_sample_ship,
    ('sample', 'report_result'): _validate_report_result,
    ('case', 'decide'): _validate_case_decision,
    ('case', 'resolve_appeal'): _validate_case_decision,
    ('batch', 'add_sample'): _validate_batch_add_sample,
    ('batch', 'return_batch'): _validate_batch_reason,
    ('batch', 'cold_chain_abnormal'): _validate_batch_reason,
}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'batches': 'batch'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'batch': 'open'}
    TRANSITIONS = {
        'athlete': {'retire': (('active',), 'retired')},
        'sample': {
            'collect': (('scheduled',), 'collected'),
            'seal': (('collected',), 'sealed'),
            'ship': (('sealed',), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'analyze': (('received',), 'analyzed'),
            'report_adverse': (('analyzed',), 'adverse'),
            'report_result': (('received', 'analyzed', 'adverse', 'cleared'), None),
            'clear': (('analyzed',), 'cleared'),
        },
        'case': {
            'provisional_suspend': (('open',), 'suspended'),
            'schedule_hearing': (('suspended',), 'hearing'),
            'decide': (('hearing', 'reconsider'), 'closed'),
            'appeal': (('closed',), 'appeal'),
            'reopen': (('closed', 'appeal'), 'reconsider'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
        'batch': {
            'add_sample': (('open',), 'open'),
            'submit': (('open',), 'submitted'),
            'return_batch': (('submitted',), 'open'),
            'cold_chain_abnormal': (('submitted',), 'open'),
            'complete': (('submitted',), 'completed'),
        },
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule'),
        'batch': ('box_id', 'lab_slot', 'storage_until', 'capacity'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'report_result'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('batch', 'add_sample'): ('sample_id',),
        ('batch', 'return_batch'): ('reason',),
        ('batch', 'cold_chain_abnormal'): ('reason',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'case': ('admin', 'panel'),
        'batch': ('admin', 'inspector'),
    }
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'report_result': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'provisional_suspend': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'reopen': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        'add_sample': ('admin', 'inspector'),
        'submit': ('admin', 'inspector'),
        'return_batch': ('admin', 'lab'),
        'cold_chain_abnormal': ('admin', 'lab', 'inspector'),
        'complete': ('admin', 'lab'),
    }

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
        extra = custom(actor, entity, data, lookup) if custom else {}
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
