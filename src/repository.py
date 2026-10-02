import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS batch_samples (
                    batch_id TEXT NOT NULL,
                    sample_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (batch_id, sample_id)
                );
                CREATE INDEX IF NOT EXISTS idx_batch_samples_batch
                    ON batch_samples(batch_id, state);
                CREATE TABLE IF NOT EXISTS active_batch_samples (
                    sample_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        results = []
        for entity in self.list_entities(kind=kind):
            if field == "id":
                match = entity["id"] == value
            else:
                actual = entity["data"].get(field)
                match = value in actual if isinstance(actual, list) else actual == value
            if match:
                results.append(entity)
        return results

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    # ------------------------------------------------------------------
    # 送检批次 (transport batch) transactional operations
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(value):
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @classmethod
    def _is_expired(cls, storage_until, now=None):
        deadline = cls._parse_ts(storage_until)
        if deadline is None:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current > deadline

    def _load_entity_tx(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        return self._entity_from_row(row)

    def _update_entity_tx(self, connection, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return current_version + 1

    def _memberships(self, connection, batch_id):
        rows = connection.execute(
            "SELECT sample_id, state FROM batch_samples WHERE batch_id = ? ORDER BY added_at, rowid",
            (batch_id,),
        ).fetchall()
        return [(row["sample_id"], row["state"]) for row in rows]

    def _set_membership_state(self, connection, batch_id, sample_id, state):
        connection.execute(
            "UPDATE batch_samples SET state = ? WHERE batch_id = ? AND sample_id = ?",
            (state, batch_id, sample_id),
        )

    def _release_sample_lock(self, connection, sample_id):
        connection.execute(
            "DELETE FROM active_batch_samples WHERE sample_id = ?", (sample_id,)
        )
        connection.execute(
            "DELETE FROM batch_samples WHERE sample_id = ?", (sample_id,)
        )

    def _void_sample_tx(self, connection, sample, reason):
        data = dict(sample["data"])
        data["void_reason"] = reason
        data["voided_at"] = utcnow()
        self._update_entity_tx(
            connection, sample["id"], sample["version"], "void", data
        )
        self._release_sample_lock(connection, sample["id"])

    def _sync_batch_lists(self, connection, batch):
        loaded = []
        queued = []
        for sample_id, state in self._memberships(connection, batch["id"]):
            if state == "loaded":
                loaded.append(sample_id)
            elif state == "queued":
                queued.append(sample_id)
        data = dict(batch["data"])
        data["sample_ids"] = loaded
        data["queue"] = queued
        return data

    def add_batch_sample(self, batch_id, sample_id, actor_id, expected_version=None):
        """Atomically attach a sample to a batch.

        Enforces: batch is open, sample is collected, sample is not expired,
        and a sample can only be attached to one unfinished batch (via the
        active_batch_samples lock). When the box is full the sample is queued.
        Only one of two concurrent inspectors adding the same sample succeeds.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._load_entity_tx(connection, batch_id)
            if batch["kind"] != "batch":
                raise ValidationError("not a batch: " + batch_id)
            if batch["status"] != "open":
                raise InvalidTransition("batch is not open for additions")
            sample = self._load_entity_tx(connection, sample_id)
            if sample["kind"] != "sample":
                raise ValidationError("not a sample: " + sample_id)
            if sample["status"] not in ("collected", "sealed"):
                raise ValidationError("sample must be collected before it can be batched")
            # Expired samples are voided on the spot instead of being batched.
            if self._is_expired(batch["data"].get("storage_until")):
                reason = "超过保存期限，无法入批"
                holder = connection.execute(
                    "SELECT batch_id FROM active_batch_samples WHERE sample_id = ?",
                    (sample_id,),
                ).fetchone()
                self._void_sample_tx(connection, sample, reason)
                if holder:
                    holder_batch = self._load_entity_tx(connection, holder["batch_id"])
                    synced = self._sync_batch_lists(connection, holder_batch)
                    self._update_entity_tx(
                        connection,
                        holder_batch["id"],
                        holder_batch["version"],
                        holder_batch["status"],
                        synced,
                    )
                connection.commit()
                return self.get_entity(batch_id), self.get_entity(sample_id), "voided"
            # Same sample cannot hang on two unfinished batches.
            lock = connection.execute(
                "SELECT batch_id FROM active_batch_samples WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
            if lock:
                raise ConflictError(
                    "sample already on unfinished batch: " + lock["batch_id"]
                )
            loaded_count = connection.execute(
                "SELECT COUNT(*) AS c FROM batch_samples "
                "WHERE batch_id = ? AND state IN ('loaded', 'queued')",
                (batch_id,),
            ).fetchone()["c"]
            capacity = int(batch["data"].get("capacity", 0))
            state = "loaded" if loaded_count < capacity else "queued"
            try:
                connection.execute(
                    "INSERT INTO batch_samples(batch_id, sample_id, state, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (batch_id, sample_id, state, utcnow()),
                )
                connection.execute(
                    "INSERT INTO active_batch_samples(sample_id, batch_id, updated_at) "
                    "VALUES (?, ?, ?)",
                    (sample_id, batch_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("sample already on unfinished batch")
            data = self._sync_batch_lists(connection, batch)
            self._update_entity_tx(
                connection, batch_id, expected_version, batch["status"], data
            )
            connection.commit()
            return self.get_entity(batch_id), self.get_entity(sample_id), state
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def submit_batch(self, batch_id, actor_id, expected_version=None):
        """Submit a batch to the lab: loaded samples go in transit."""
        connection = self._connect()
        shipped = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._load_entity_tx(connection, batch_id)
            if batch["kind"] != "batch":
                raise ValidationError("not a batch: " + batch_id)
            if batch["status"] != "open":
                raise InvalidTransition("batch is not open for submission")
            for sample_id, state in self._memberships(connection, batch_id):
                if state != "loaded":
                    continue
                sample = self._load_entity_tx(connection, sample_id)
                if sample["status"] in ("collected", "sealed"):
                    data = dict(sample["data"])
                    data["batch_id"] = batch_id
                    self._update_entity_tx(
                        connection, sample_id, sample["version"], "in_transit", data
                    )
                    shipped.append(self.get_entity(sample_id))
            self._update_entity_tx(
                connection, batch_id, expected_version, "submitted", batch["data"]
            )
            connection.commit()
            return self.get_entity(batch_id), shipped
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def return_batch(self, batch_id, reason, actor_id, expected_version=None):
        """Return a batch to pending entry (lab return / cold chain incident).

        The whole batch goes back to open. Loaded samples past the storage
        deadline are voided on the spot with the reason; freed box slots are
        filled from the queue. Remaining samples stay on the batch.
        """
        connection = self._connect()
        voided = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._load_entity_tx(connection, batch_id)
            if batch["kind"] != "batch":
                raise ValidationError("not a batch: " + batch_id)
            if batch["status"] != "submitted":
                raise InvalidTransition("batch is not submitted")
            expired = self._is_expired(batch["data"].get("storage_until"))
            loaded = [s for s, st in self._memberships(connection, batch_id) if st == "loaded"]
            queued = [s for s, st in self._memberships(connection, batch_id) if st == "queued"]
            freed = 0
            if expired:
                for sample_id in loaded:
                    sample = self._load_entity_tx(connection, sample_id)
                    self._void_sample_tx(
                        connection, sample, "退回时超过保存期限，样本作废：" + str(reason)
                    )
                    voided.append(self.get_entity(sample_id))
                    freed += 1
            # Promote queued samples into freed box slots.
            capacity = int(batch["data"].get("capacity", 0))
            occupied = len(loaded) - freed
            for sample_id in queued:
                if occupied >= capacity:
                    break
                self._set_membership_state(connection, batch_id, sample_id, "loaded")
                occupied += 1
            data = self._sync_batch_lists(connection, batch)
            self._update_entity_tx(
                connection, batch_id, expected_version, "open", data
            )
            connection.commit()
            return self.get_entity(batch_id), voided
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_batch(self, batch_id, actor_id, expected_version=None):
        """Complete a batch and release its sample locks."""
        connection = self._connect()
        released = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            batch = self._load_entity_tx(connection, batch_id)
            if batch["kind"] != "batch":
                raise ValidationError("not a batch: " + batch_id)
            if batch["status"] != "submitted":
                raise InvalidTransition("batch is not submitted")
            for sample_id, _state in self._memberships(connection, batch_id):
                self._release_sample_lock(connection, sample_id)
                released.append(self.get_entity(sample_id))
            self._update_entity_tx(
                connection, batch_id, expected_version, "completed", batch["data"]
            )
            connection.commit()
            return self.get_entity(batch_id), released
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def report_result(self, sample_id, result, conclusion_at, actor_id, expected_version=None):
        """Ingest a lab result with dedupe and out-of-order protection.

        Returns (sample, action, reopened_cases) where action is one of
        'duplicate', 'late' or 'updated'. A changed conclusion reopens any
        decided (closed/appeal) case for the sample.
        """
        connection = self._connect()
        reopened = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            sample = self._load_entity_tx(connection, sample_id)
            if sample["kind"] != "sample":
                raise ValidationError("not a sample: " + sample_id)
            if sample["status"] not in ("received", "analyzed", "adverse", "cleared"):
                raise InvalidTransition("sample has not been received/analyzed")
            data = dict(sample["data"])
            effective_result = data.get("lab_result")
            effective_at = data.get("conclusion_at")
            if effective_at is not None and self._parse_ts(effective_at) == self._parse_ts(conclusion_at):
                if effective_result == result:
                    connection.rollback()
                    return self.get_entity(sample_id), "duplicate", []
                raise ConflictError("conflicting result for the same conclusion time")
            if effective_at is not None and self._parse_ts(effective_at) > self._parse_ts(conclusion_at):
                connection.rollback()
                return self.get_entity(sample_id), "late", []
            changed = effective_result is not None and effective_result != result
            data["lab_result"] = result
            data["conclusion_at"] = conclusion_at
            data["result_reported_at"] = utcnow()
            new_status = "adverse" if result == "adverse" else "cleared"
            self._update_entity_tx(
                connection, sample_id, expected_version, new_status, data
            )
            if changed:
                rows = connection.execute(
                    "SELECT * FROM entities "
                    "WHERE kind = 'case' AND status IN ('closed', 'appeal')"
                ).fetchall()
                for row in rows:
                    case = self._entity_from_row(row)
                    if case["data"].get("sample_id") != sample_id:
                        continue
                    case_data = dict(case["data"])
                    case_data["reopen_reason"] = "lab conclusion changed to %s" % result
                    self._update_entity_tx(
                        connection, case["id"], case["version"], "reconsider", case_data
                    )
                    reopened.append(self.get_entity(case["id"]))
            connection.commit()
            return self.get_entity(sample_id), "updated", reopened
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_batch_samples(self, batch_id):
        with self._connect() as connection:
            return self._memberships(connection, batch_id)

    def find_active_batch_for_sample(self, sample_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT batch_id FROM active_batch_samples WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
        return row["batch_id"] if row else None

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
