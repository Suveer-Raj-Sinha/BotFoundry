"""Saving and loading workflows, runs, node results and events."""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..rules.models import Rulebook
from ..workflow.definition import WorkflowDefinition
from ..workflow.events import TERMINAL_EVENTS, EventType, RunEvent
from ..workflow.runner import NodeRecord, Run
from .database import Database, StorageError
from .migrations import migrate

log = logging.getLogger(__name__)

_NODE_EVENTS = {EventType.NODE_STARTED, EventType.NODE_COMPLETED, EventType.NODE_FAILED, EventType.NODE_SKIPPED,
                EventType.APPROVAL_REQUESTED, EventType.APPROVAL_GRANTED, EventType.APPROVAL_DENIED}
_UNFINISHED = ("created", "validating", "running", "waiting_approval")


def _dumps(value: Any) -> str:
    # default=str: an unusual value in a node output becomes text instead of losing the whole run
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """The engine's persistent record. Memory is never the source of truth for finished work."""

    def __init__(self, db: Database, schema_version: int) -> None:
        self.db = db
        self.schema_version = schema_version

    @classmethod
    def open(cls, path: str | Path) -> "Storage":
        """Open (creating if needed) the database and bring its schema up to date."""
        db = Database(path)
        try:
            version = migrate(db)
        except BaseException:
            db.close()
            raise
        return cls(db, version)

    def close(self) -> None:
        self.db.close()

    # ---- workflows ------------------------------------------------------------------------
    def save_workflow(self, definition: WorkflowDefinition) -> dict[str, Any]:
        """Create or update. Invalid-but-well-formed workflows may be saved (work in progress)."""
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO workflows (id, name, definition, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, definition=excluded.definition,
                                                 updated_at=excluded.updated_at""",
                (definition.id, definition.name, definition.model_dump_json(), now, now))
        summary = self._workflow_summary(definition.id)
        assert summary is not None
        return summary

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        row = self.db.query_one("SELECT definition FROM workflows WHERE id=?", (workflow_id,))
        return WorkflowDefinition.model_validate_json(row["definition"]) if row else None

    def list_workflows(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM workflows ORDER BY updated_at DESC, id")
        return [s for r in rows if (s := self._workflow_summary(r["id"])) is not None]

    def delete_workflow(self, workflow_id: str) -> bool:
        """Deletes the saved workflow only. Past runs keep their own snapshot of it."""
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM workflows WHERE id=?", (workflow_id,)).rowcount > 0

    def _workflow_summary(self, workflow_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT id, name, definition, created_at, updated_at FROM workflows WHERE id=?",
                                (workflow_id,))
        if not row:
            return None
        definition = json.loads(row["definition"])
        return {"id": row["id"], "name": row["name"], "description": definition.get("description", ""),
                "node_count": len(definition.get("nodes", [])),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    # ---- rulebooks (immutable versions) ---------------------------------------------------
    def save_rulebook(self, rulebook: Rulebook, note: str | None = None) -> tuple[int, bool]:
        """Store a new version unless the content is identical to the latest one.
        Returns (version, created). Versions are never edited or deleted: a run can always be traced
        back to the exact rules it followed."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT version, document FROM rulebook_versions WHERE bot_id=? "
                               "ORDER BY version DESC LIMIT 1", (rulebook.bot_id,)).fetchone()
            if row and Rulebook.model_validate_json(row["document"]).canonical_json() == rulebook.canonical_json():
                return row["version"], False
            version = (row["version"] if row else 0) + 1
            conn.execute("INSERT INTO rulebook_versions (bot_id, version, name, document, note, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (rulebook.bot_id, version, rulebook.name, rulebook.model_dump_json(), note, _now()))
        return version, True

    def get_rulebook(self, bot_id: str, version: int | None = None) -> dict[str, Any] | None:
        """The latest version (or a specific one) as {rulebook, version, created_at, note}."""
        if version is None:
            row = self.db.query_one("SELECT * FROM rulebook_versions WHERE bot_id=? ORDER BY version DESC LIMIT 1", (bot_id,))
        else:
            row = self.db.query_one("SELECT * FROM rulebook_versions WHERE bot_id=? AND version=?", (bot_id, version))
        if not row:
            return None
        return {"rulebook": Rulebook.model_validate_json(row["document"]), "version": row["version"],
                "created_at": row["created_at"], "note": row["note"]}

    def list_rulebooks(self) -> list[dict[str, Any]]:
        rows = self.db.query("""SELECT r.bot_id, r.name, r.version, r.created_at FROM rulebook_versions r
                                WHERE r.version = (SELECT MAX(version) FROM rulebook_versions WHERE bot_id = r.bot_id)
                                ORDER BY r.bot_id""")
        return [{"bot_id": r["bot_id"], "name": r["name"], "version": r["version"], "created_at": r["created_at"]}
                for r in rows]

    def list_rulebook_versions(self, bot_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT version, name, note, created_at FROM rulebook_versions WHERE bot_id=? "
                             "ORDER BY version DESC", (bot_id,))
        return [{"version": r["version"], "name": r["name"], "note": r["note"], "created_at": r["created_at"]}
                for r in rows]

    # ---- runs -----------------------------------------------------------------------------
    def attach(self, run: Run) -> "RunPersister":
        """Start persisting a run (call before `execute`). Everything so far is written immediately."""
        return RunPersister(self, run)

    def list_runs(self, limit: int = 50, offset: int = 0, workflow_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if workflow_id is not None:
            sql += " WHERE workflow_id=?"
            params.append(workflow_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"  # rowid = insertion order: a stable tiebreak on coarse clocks
        params += [max(1, min(limit, 500)), max(0, offset)]
        return [self._run_summary(r) for r in self.db.query(sql, params)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not row:
            return None
        nodes = self.db.query("SELECT * FROM node_runs WHERE run_id=? ORDER BY rowid", (run_id,))
        result = self._run_summary(row)
        result["nodes"] = [{
            "node_id": n["node_id"], "type": n["node_type"], "status": n["status"],
            "outputs": _loads(n["outputs"]) or {}, "summary": n["summary"] or "", "error": _loads(n["error"]),
            "reason": n["reason"], "started_at": n["started_at"], "finished_at": n["finished_at"]} for n in nodes]
        return result

    def get_run_workflow(self, run_id: str) -> WorkflowDefinition | None:
        """The exact workflow definition this run executed."""
        row = self.db.query_one("SELECT workflow_snapshot FROM runs WHERE run_id=?", (run_id,))
        return WorkflowDefinition.model_validate_json(row["workflow_snapshot"]) if row else None

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 1000) -> list[RunEvent]:
        rows = self.db.query("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                             (run_id, max(0, after_id), max(1, min(limit, 5000))))
        return [RunEvent(id=r["id"], run_id=r["run_id"], type=EventType(r["type"]),
                         timestamp=datetime.fromisoformat(r["timestamp"]), node_id=r["node_id"],
                         payload=json.loads(r["payload"])) for r in rows]

    def get_approvals(self, run_id: str) -> list[dict[str, Any]]:
        """Every approval request of a run with its outcome, derived from the event log (the one record)."""
        rows = self.db.query("SELECT type, timestamp, node_id, payload FROM run_events WHERE run_id=? AND type IN (?, ?, ?) "
                             "ORDER BY id", (run_id, EventType.APPROVAL_REQUESTED.value, EventType.APPROVAL_GRANTED.value,
                                             EventType.APPROVAL_DENIED.value))
        approvals: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            approval_id = payload.get("approval_id")
            if row["type"] == EventType.APPROVAL_REQUESTED.value:
                approvals[approval_id] = {
                    "approval_id": approval_id, "run_id": run_id, "node_id": row["node_id"], "kind": payload.get("kind"),
                    "summary": payload.get("summary"), "details": payload.get("details", {}), "status": "pending",
                    "requested_at": row["timestamp"], "expires_at": payload.get("expires_at"),
                    "decided_at": None, "reason": None, "note": None}
            elif approval_id in approvals:
                approvals[approval_id].update(
                    status="granted" if row["type"] == EventType.APPROVAL_GRANTED.value else "denied",
                    decided_at=row["timestamp"], reason=payload.get("reason"), note=payload.get("note"))
        return list(approvals.values())

    @staticmethod
    def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {"run_id": row["run_id"], "workflow_id": row["workflow_id"], "workflow_name": row["workflow_name"],
                "status": row["status"], "error": _loads(row["error"]), "rulebook_version": row["rulebook_version"],
                "rulebook_bot_id": row["rulebook_bot_id"],
                "created_at": row["created_at"], "finished_at": row["finished_at"]}

    # ---- restart safety -------------------------------------------------------------------
    def mark_interrupted_runs(self) -> list[str]:
        """Call once at startup. A run still marked unfinished means the app closed or crashed
        while it was running. It is marked failed (`run.interrupted`) and NEVER resumed, so work
        is never executed twice. Returns the affected run ids."""
        rows = self.db.query(f"SELECT run_id FROM runs WHERE status IN ({','.join('?' * len(_UNFINISHED))})",
                             _UNFINISHED)
        interrupted: list[str] = []
        for row in rows:
            run_id, now = row["run_id"], _now()
            run_error = {"code": "run.interrupted", "details": {},
                         "message": "BotFoundry closed or crashed while this run was in progress. "
                                    "It was not resumed, so no work was repeated."}
            node_error = {"code": "node.interrupted", "details": {},
                          "message": "BotFoundry closed or crashed while this node was running."}
            with self.db.transaction() as conn:
                last = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM run_events WHERE run_id=?",
                                    (run_id,)).fetchone()["n"]
                running = [r["node_id"] for r in conn.execute(
                    "SELECT node_id FROM node_runs WHERE run_id=? AND status IN ('running', 'waiting_approval') "
                    "ORDER BY rowid", (run_id,))]
                conn.execute("UPDATE node_runs SET status='failed', error=?, finished_at=? "
                             "WHERE run_id=? AND status IN ('running', 'waiting_approval')", (_dumps(node_error), now, run_id))
                answered, open_approvals = set(), []
                for ev in conn.execute("SELECT id, type, node_id, payload FROM run_events WHERE run_id=? AND type IN (?, ?, ?) "
                                       "ORDER BY id", (run_id, EventType.APPROVAL_REQUESTED.value,
                                                       EventType.APPROVAL_GRANTED.value, EventType.APPROVAL_DENIED.value)):
                    approval_id = json.loads(ev["payload"]).get("approval_id")
                    if ev["type"] == EventType.APPROVAL_REQUESTED.value:
                        open_approvals.append((approval_id, ev["node_id"]))
                    else:
                        answered.add(approval_id)
                for approval_id, approval_node in open_approvals:
                    if approval_id not in answered:  # an unanswered question must not outlive the run
                        last += 1
                        conn.execute("INSERT INTO run_events (run_id, id, type, timestamp, node_id, payload) "
                                     "VALUES (?, ?, ?, ?, ?, ?)",
                                     (run_id, last, EventType.APPROVAL_DENIED.value, now, approval_node,
                                      _dumps({"approval_id": approval_id, "decided_by": "engine", "reason": "interrupted", "note": None})))
                conn.execute("UPDATE node_runs SET status='skipped', reason=?, finished_at=? "
                             "WHERE run_id=? AND status='pending'",
                             ("The run was interrupted before this node started.", now, run_id))
                conn.execute("UPDATE runs SET status='failed', error=?, finished_at=? WHERE run_id=?",
                             (_dumps(run_error), now, run_id))
                conn.execute("INSERT INTO run_events (run_id, id, type, timestamp, node_id, payload) "
                             "VALUES (?, ?, ?, ?, NULL, ?)",
                             (run_id, last + 1, EventType.RUN_FAILED.value, now,
                              _dumps({"error": run_error, "failed_node": running[0] if running else None,
                                      "duration_ms": None, "interrupted": True})))
            interrupted.append(run_id)
        return interrupted


class RunPersister:
    """Mirrors one run into the database as its events happen.

    A database problem never stops or breaks the run itself; it is logged and counted in `errors`
    so the caller can tell the user that history may be incomplete."""

    def __init__(self, storage: Storage, run: Run) -> None:
        self.storage = storage
        self.run = run
        self.errors: list[str] = []
        self._write_initial()
        run.events.subscribe(self._on_event)

    def _write_initial(self) -> None:
        run = self.run
        with self.storage.db.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, workflow_id, workflow_name, workflow_snapshot, status, error, "
                "rulebook_version, rulebook_bot_id, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.run_id, run.definition.id, run.definition.name, run.definition.model_dump_json(),
                 run.status.value, None, run.rulebook_version, run.rulebook_bot_id,
                 run.created_at.isoformat(), _iso(run.finished_at)))
            for record in run.nodes.values():
                self._upsert_node(conn, record)
            for event in run.events.all():  # events that happened before we subscribed (run_created)
                self._insert_event(conn, event)

    def _on_event(self, event: RunEvent) -> None:
        try:
            with self.storage.db.transaction() as conn:
                self._insert_event(conn, event)
                if event.type in TERMINAL_EVENTS:
                    for record in self.run.nodes.values():
                        self._upsert_node(conn, record)
                elif event.type in _NODE_EVENTS and event.node_id in self.run.nodes:
                    self._upsert_node(conn, self.run.nodes[event.node_id])
                conn.execute("UPDATE runs SET status=?, error=?, finished_at=? WHERE run_id=?",
                             (self.run.status.value, _dumps(self.run.error) if self.run.error else None,
                              _iso(self.run.finished_at), self.run.run_id))
        except Exception as exc:
            message = f"Could not save event {event.id} ({event.type.value}) of run {self.run.run_id}: {exc}"
            log.exception(message)
            self.errors.append(message)

    def _insert_event(self, conn: sqlite3.Connection, event: RunEvent) -> None:
        conn.execute("INSERT INTO run_events (run_id, id, type, timestamp, node_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                     (event.run_id, event.id, event.type.value, event.timestamp.isoformat(), event.node_id,
                      _dumps(event.payload)))

    def _upsert_node(self, conn: sqlite3.Connection, record: NodeRecord) -> None:
        conn.execute(
            """INSERT INTO node_runs (run_id, node_id, node_type, status, outputs, summary, error, reason,
                                      started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, node_id) DO UPDATE SET status=excluded.status, outputs=excluded.outputs,
                 summary=excluded.summary, error=excluded.error, reason=excluded.reason,
                 started_at=excluded.started_at, finished_at=excluded.finished_at""",
            (self.run.run_id, record.node_id, record.type, record.status.value, _dumps(record.outputs),
             record.summary, _dumps(record.error) if record.error else None, record.reason,
             _iso(record.started_at), _iso(record.finished_at)))
