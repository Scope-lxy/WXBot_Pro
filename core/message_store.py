"""Account-scoped durable facts for chat events and reply delivery."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.account_storage import account_file
from core.memory_context_repair import build_tail_repair_plan


SCHEMA_VERSION = 4
DEFAULT_REPLY_TTL_SECONDS = 15 * 60

ACTION_STATES = {"pending", "inflight", "done", "uncertain", "cancelled", "stale", "expired"}
CHAT_TYPES = {"private", "group"}
REPLY_ECHO_KINDS = {"text", "voice", "file", "quote"}


class MessageStoreConflictError(RuntimeError):
    """Raised when an idempotency key is reused for different facts."""


class MessageStoreTransitionError(RuntimeError):
    """Raised when a persisted state transition is not legal."""


class MessageStoreSchemaError(RuntimeError):
    """Raised when the database schema cannot be trusted."""


def _required_text(value, name):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _required_chat_type(value):
    chat_type = _required_text(value, "chat_type")
    if chat_type not in CHAT_TYPES:
        raise ValueError(f"unsupported chat_type: {chat_type}")
    return chat_type


def _timestamp(value, name="timestamp"):
    if isinstance(value, datetime):
        value = value.timestamp()
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite timestamp")
    return result


def _now(value=None):
    return time.time() if value is None else _timestamp(value, "now")


def _json_object(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be a dict")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_value(event, name, default=None):
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


class MessageStore:
    """One SQLite fact store per WeChat account.

    Connections are short lived so one instance can safely be used by several
    producer threads. Every state-changing method starts an IMMEDIATE
    transaction; uniqueness and conversation-version checks therefore share
    the same commit boundary.
    """

    def __init__(self, base_dir, wx_id):
        self.path = Path(
            account_file(
                base_dir,
                wx_id,
                "message_store.sqlite3",
                create_parent=True,
            )
        )
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _initialize(self):
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2, 3, SCHEMA_VERSION}:
                raise MessageStoreSchemaError(
                    f"unsupported message store schema {version}; expected {SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    conversation TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 0),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (conversation, chat_type)
                );

                CREATE TABLE IF NOT EXISTS chat_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    conversation TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    original_content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    native_attr TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    native_hash TEXT NOT NULL,
                    native_hash_text TEXT NOT NULL,
                    native_time TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_batch TEXT NOT NULL,
                    source_order INTEGER,
                    identity_kind TEXT NOT NULL,
                    identity_value TEXT NOT NULL,
                    delivery_id TEXT UNIQUE,
                    received_at REAL NOT NULL,
                    stored_at REAL NOT NULL,
                    reply_expires_at REAL,
                    conversation_version INTEGER NOT NULL CHECK (conversation_version >= 0),
                    processing_state TEXT NOT NULL,
                    state_updated_at REAL NOT NULL,
                    history_visible INTEGER NOT NULL DEFAULT 1 CHECK (history_visible IN (0, 1)),
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chat_events_history
                    ON chat_events(conversation, chat_type, received_at, event_seq);
                CREATE INDEX IF NOT EXISTS idx_chat_events_pending
                    ON chat_events(processing_state, reply_expires_at, event_seq);

                CREATE TABLE IF NOT EXISTS reply_jobs (
                    turn_id TEXT PRIMARY KEY,
                    conversation TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    route_source TEXT NOT NULL DEFAULT '',
                    expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
                    expires_at REAL NOT NULL,
                    action_count INTEGER NOT NULL DEFAULT 0 CHECK (action_count >= 0),
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS reply_job_events (
                    turn_id TEXT NOT NULL REFERENCES reply_jobs(turn_id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL UNIQUE REFERENCES chat_events(event_id),
                    event_order INTEGER NOT NULL CHECK (event_order >= 0),
                    PRIMARY KEY (turn_id, event_id),
                    UNIQUE (turn_id, event_order)
                );

                CREATE TABLE IF NOT EXISTS delivery_actions (
                    action_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES reply_jobs(turn_id) ON DELETE CASCADE,
                    action_index INTEGER NOT NULL CHECK (action_index >= 0),
                    status TEXT NOT NULL,
                    claimed_at REAL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE (turn_id, action_index)
                );

                CREATE INDEX IF NOT EXISTS idx_delivery_actions_turn
                    ON delivery_actions(turn_id, action_index);

                CREATE TABLE IF NOT EXISTS reply_echo_expectations (
                    action_id TEXT PRIMARY KEY,
                    conversation TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confirmable INTEGER NOT NULL CHECK (confirmable IN (0, 1)),
                    message_types_json TEXT NOT NULL,
                    at TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'active', 'matched', 'complete')),
                    expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reply_echo_expiry
                    ON reply_echo_expectations(state, expires_at, updated_at);

                CREATE TABLE IF NOT EXISTS ui_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    conversation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                """
            )
            reply_job_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(reply_jobs)")
            }
            if "route_source" not in reply_job_columns:
                connection.execute(
                    "ALTER TABLE reply_jobs ADD COLUMN route_source TEXT NOT NULL DEFAULT ''"
                )
            chat_event_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(chat_events)")
            }
            if "reply_state" in chat_event_columns:
                connection.execute("ALTER TABLE chat_events DROP COLUMN reply_state")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise MessageStoreSchemaError(f"message store integrity check failed: {integrity}")
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _logical_event_id(event):
        conversation = _required_text(_event_value(event, "conversation"), "conversation")
        chat_type = _required_chat_type(_event_value(event, "chat_type", "private"))
        raw_native_id = _event_value(event, "native_id")
        value = "" if raw_native_id is None else str(raw_native_id).strip()
        if value:
            kind = "native_id"
        else:
            source = _required_text(_event_value(event, "source"), "source")
            source_batch = _required_text(_event_value(event, "source_batch"), "source_batch")
            source_order = _event_value(event, "source_order")
            if source_order is None:
                raise ValueError(
                    "source_order is required when no native message identity is available"
                )
            try:
                source_order = int(source_order)
            except (TypeError, ValueError) as exc:
                raise ValueError("source_order must be an integer") from exc
            kind = "observation"
            value = f"{source}\x1f{source_batch}\x1f{source_order}"
        identity = json.dumps(
            ["wxbot-event-v1", conversation, chat_type, kind, value],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"evt_{digest}", kind, value

    @staticmethod
    def _event_row(row):
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item["is_new"] = False
        return item

    @staticmethod
    def _ui_delivery_row(row):
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item["details"] = json.loads(item.pop("details_json"))
        return item

    @staticmethod
    def _reply_echo_row(row):
        item = dict(row)
        item["confirmable"] = bool(item["confirmable"])
        item["message_types"] = tuple(json.loads(item.pop("message_types_json")))
        confirmed_content = str(item.pop("confirmed_content", "") or "")
        item["content"] = confirmed_content or str(item.get("content", "") or "")
        return item

    @staticmethod
    def _job_event_ids(connection, turn_id):
        rows = connection.execute(
            """
            SELECT event_id FROM reply_job_events
            WHERE turn_id = ? ORDER BY event_order
            """,
            (turn_id,),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    @classmethod
    def _job_row(cls, connection, row):
        if row is None:
            return None
        item = dict(row)
        item["event_ids"] = cls._job_event_ids(connection, item["turn_id"])
        return item

    @staticmethod
    def _current_version(connection, conversation, chat_type):
        row = connection.execute(
            """
            SELECT version FROM conversation_state
            WHERE conversation = ? AND chat_type = ?
            """,
            (conversation, chat_type),
        ).fetchone()
        return int(row["version"]) if row else 0

    @classmethod
    def _advance_version(cls, connection, conversation, chat_type, now):
        current = cls._current_version(connection, conversation, chat_type)
        version = current + 1
        connection.execute(
            """
            INSERT INTO conversation_state(conversation, chat_type, version, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation, chat_type) DO UPDATE SET
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (conversation, chat_type, version, now),
        )
        return version

    @staticmethod
    def _same_event(row, values):
        compared = (
            "conversation",
            "chat_type",
            "direction",
            "sender",
            "message_type",
            "native_attr",
            "native_id",
            "identity_kind",
            "identity_value",
            "delivery_id",
        )
        if not all(row[key] == values[key] for key in compared):
            return False
        if row["content"] == values["content"]:
            return True
        original = str(row["original_content"] or "")
        return bool(
            original
            and original == values["original_content"]
            and row["content"] != original
            and values["content"] == values["original_content"]
        )

    def _record_event_locked(self, connection, values, *, advances_version):
        existing = connection.execute(
            "SELECT * FROM chat_events WHERE event_id = ?",
            (values["event_id"],),
        ).fetchone()
        if existing is not None:
            same_delivery = bool(
                values.get("delivery_id")
                and existing["delivery_id"] == values["delivery_id"]
                and existing["conversation"] == values["conversation"]
                and existing["chat_type"] == values["chat_type"]
                and existing["direction"] == "bot_echo"
                and values["direction"] == "bot_echo"
            )
            if same_delivery:
                return {
                    "event_id": values["event_id"],
                    "is_new": False,
                    "version": int(existing["conversation_version"]),
                }
            if not self._same_event(existing, values):
                raise MessageStoreConflictError(
                    f"event_id {values['event_id']} was reused for a different event"
                )
            return {
                "event_id": values["event_id"],
                "is_new": False,
                "version": int(existing["conversation_version"]),
            }

        version = self._current_version(
            connection,
            values["conversation"],
            values["chat_type"],
        )
        if advances_version:
            version = self._advance_version(
                connection,
                values["conversation"],
                values["chat_type"],
                values["stored_at"],
            )
        values = dict(values, conversation_version=version)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            connection.execute(
                f"INSERT INTO chat_events({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        except sqlite3.IntegrityError as exc:
            raise MessageStoreConflictError(str(exc)) from exc
        return {"event_id": values["event_id"], "is_new": True, "version": version}

    def _record_event(self, values, *, advances_version):
        with self._transaction() as connection:
            return self._record_event_locked(
                connection,
                values,
                advances_version=advances_version,
            )

    def record_inbound(self, event):
        """Record one normalized wxautox observation exactly once.

        ``friend`` and ``manual_self`` invalidate older replies. A recognized
        robot echo is history only and therefore does not advance the version.
        """

        with self._transaction() as connection:
            return self._record_inbound_locked(connection, event)

    @contextmanager
    def inbound_batch(self):
        """Yield a recorder whose observations share one transaction."""
        with self._transaction() as connection:
            yield lambda event: self._record_inbound_locked(connection, event)

    def _record_inbound_locked(self, connection, event):
        """Record one inbound observation on the caller-owned transaction."""

        direction = _required_text(_event_value(event, "direction"), "direction").lower()
        if direction not in {"friend", "manual_self", "bot_echo", "system", "unknown"}:
            raise ValueError(f"unsupported inbound direction: {direction}")
        conversation = _required_text(_event_value(event, "conversation"), "conversation")
        chat_type = _required_chat_type(_event_value(event, "chat_type", "private"))
        received_at = _timestamp(_event_value(event, "received_at"), "received_at")
        stored_at = _now(_event_value(event, "stored_at", None))
        related_delivery_id = str(
            _event_value(event, "related_delivery_id", "") or ""
        ).strip()
        if direction == "bot_echo" and related_delivery_id:
            event_id = self._delivery_event_id(related_delivery_id)
            version = self._current_version(connection, conversation, chat_type)
            existing = connection.execute(
                "SELECT * FROM chat_events WHERE delivery_id = ?",
                (related_delivery_id,),
            ).fetchone()
            if existing is not None and (
                existing["event_id"] != event_id
                or existing["conversation"] != conversation
                or existing["chat_type"] != chat_type
            ):
                raise MessageStoreConflictError(
                    f"delivery_id {related_delivery_id} belongs to a different event"
                )
            if existing is not None:
                callback_type = str(_event_value(event, "message_type", "text") or "text")
                callback_content = str(_event_value(event, "content", "") or "")
                callback_original = str(
                    _event_value(event, "original_content", "") or callback_content
                )
                preserve_voice_text = callback_type.lower() in {"voice", "audio"}
                if not callback_content:
                    callback_content = str(existing["content"] or "")
                if not callback_original:
                    callback_original = str(existing["original_content"] or callback_content)
                metadata = json.loads(existing["metadata_json"])
                callback_source = str(_event_value(event, "source", "") or "")
                if callback_source:
                    metadata["callback_source"] = callback_source
                connection.execute(
                    """
                    UPDATE chat_events SET
                        sender = ?,
                        content = ?,
                        original_content = ?,
                        message_type = ?,
                        native_attr = ?,
                        native_id = ?,
                        native_hash = ?,
                        native_hash_text = ?,
                        native_time = ?,
                        metadata_json = ?,
                        state_updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (
                        str(_event_value(event, "sender", "") or existing["sender"]),
                        existing["content"] if preserve_voice_text else callback_content,
                        existing["original_content"] if preserve_voice_text else callback_original,
                        callback_type,
                        str(_event_value(event, "native_attr", "self") or "self"),
                        str(_event_value(event, "native_id", "") or ""),
                        str(_event_value(event, "native_hash", "") or ""),
                        str(_event_value(event, "native_hash_text", "") or ""),
                        str(_event_value(event, "native_time", "") or ""),
                        _json_object(metadata),
                        stored_at,
                        related_delivery_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM reply_echo_expectations WHERE action_id = ?",
                    (related_delivery_id,),
                )
                return {"event_id": event_id, "is_new": False, "version": version}
            values = self._confirmed_outbound_values(
                related_delivery_id,
                conversation,
                str(_event_value(event, "content", "") or ""),
                received_at,
                str(_event_value(event, "sender", "") or "self"),
                chat_type,
                str(_event_value(event, "message_type", "text") or "text"),
                str(_event_value(event, "native_attr", "self") or "self"),
                {"callback_source": str(_event_value(event, "source", "") or "")},
                stored_at,
            )
            recorded = self._record_event_locked(
                connection,
                values,
                advances_version=False,
            )
            connection.execute(
                "DELETE FROM reply_echo_expectations WHERE action_id = ?",
                (related_delivery_id,),
            )
            return {
                "event_id": recorded["event_id"],
                "is_new": recorded["is_new"],
                "version": version,
            }

        event_id, identity_kind, identity_value = self._logical_event_id(event)
        expires_at = _event_value(event, "reply_expires_at", None)
        if direction == "friend":
            if expires_at is None:
                expires_at = received_at + DEFAULT_REPLY_TTL_SECONDS
            else:
                expires_at = _timestamp(expires_at, "reply_expires_at")
            processing_state = "pending"
        else:
            expires_at = None
            processing_state = "handled"

        source_order = _event_value(event, "source_order")
        if source_order is not None:
            try:
                source_order = int(source_order)
            except (TypeError, ValueError) as exc:
                raise ValueError("source_order must be an integer") from exc
        metadata = dict(_event_value(event, "metadata", None) or {})
        image_paths = [
            str(path or "").strip()
            for path in (_event_value(event, "image_paths", ()) or ())
            if str(path or "").strip()
        ]
        visual_notes = [
            str(note or "").strip()
            for note in (_event_value(event, "visual_notes", ()) or ())
        ]
        if image_paths:
            metadata["image_paths"] = image_paths
        if any(visual_notes):
            metadata["visual_notes"] = visual_notes
            metadata["visual_note"] = next(note for note in visual_notes if note)
        values = {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": chat_type,
            "direction": direction,
            "sender": str(_event_value(event, "sender", "") or ""),
            "content": str(_event_value(event, "content", "") or ""),
            "original_content": str(_event_value(event, "original_content", "") or ""),
            "message_type": str(_event_value(event, "message_type", "text") or "text"),
            "native_attr": str(_event_value(event, "native_attr", "") or ""),
            "native_id": str(_event_value(event, "native_id", "") or ""),
            "native_hash": str(_event_value(event, "native_hash", "") or ""),
            "native_hash_text": str(_event_value(event, "native_hash_text", "") or ""),
            "native_time": str(_event_value(event, "native_time", "") or ""),
            "source": str(_event_value(event, "source", "") or ""),
            "source_batch": str(_event_value(event, "source_batch", "") or ""),
            "source_order": source_order,
            "identity_kind": identity_kind,
            "identity_value": identity_value,
            "delivery_id": None,
            "received_at": received_at,
            "stored_at": stored_at,
            "reply_expires_at": expires_at,
            "processing_state": processing_state,
            "state_updated_at": stored_at,
            "history_visible": 1,
            "metadata_json": _json_object(metadata),
        }
        return self._record_event_locked(
            connection,
            values,
            advances_version=(
                direction == "manual_self"
                or (chat_type == "private" and direction == "friend")
            ),
        )

    def append_inbound_once(
        self,
        event_id,
        conversation,
        *,
        content,
        received_at,
        sender="",
        chat_type="private",
        message_type="text",
        message_attr="friend",
        original_content="",
        expires_at=None,
        metadata=None,
        now=None,
    ):
        """Lower-level append API for callers that already own a stable ID."""

        event_id = _required_text(event_id, "event_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        received_at = _timestamp(received_at, "received_at")
        stored_at = _now(now)
        expires_at = (
            received_at + DEFAULT_REPLY_TTL_SECONDS
            if expires_at is None
            else _timestamp(expires_at, "expires_at")
        )
        values = {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": chat_type,
            "direction": "friend",
            "sender": str(sender or ""),
            "content": str(content or ""),
            "original_content": str(original_content or ""),
            "message_type": str(message_type or "text"),
            "native_attr": str(message_attr or ""),
            "native_id": "",
            "native_hash": "",
            "native_hash_text": "",
            "native_time": "",
            "source": "explicit",
            "source_batch": "",
            "source_order": None,
            "identity_kind": "explicit",
            "identity_value": event_id,
            "delivery_id": None,
            "received_at": received_at,
            "stored_at": stored_at,
            "reply_expires_at": expires_at,
            "processing_state": "pending",
            "state_updated_at": stored_at,
            "metadata_json": _json_object(metadata),
        }
        return self._record_event(values, advances_version=True)

    @staticmethod
    def _delivery_event_id(delivery_id):
        delivery_id = _required_text(delivery_id, "delivery_id")
        identity = json.dumps(
            ["wxbot-delivery-v1", delivery_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "evt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def _confirmed_outbound_values(
        cls,
        delivery_id,
        conversation,
        content,
        sent_at,
        sender,
        chat_type,
        message_type,
        message_attr,
        metadata,
        stored_at,
    ):
        delivery_id = _required_text(delivery_id, "delivery_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        sent_at = _timestamp(sent_at, "sent_at")
        event_id = cls._delivery_event_id(delivery_id)
        return {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": chat_type,
            "direction": "bot_echo",
            "sender": str(sender or "self"),
            "content": str(content or ""),
            "original_content": str(content or ""),
            "message_type": str(message_type or "text"),
            "native_attr": str(message_attr or "self"),
            "native_id": "",
            "native_hash": "",
            "native_hash_text": "",
            "native_time": "",
            "source": "confirmed_delivery",
            "source_batch": "",
            "source_order": None,
            "identity_kind": "delivery_id",
            "identity_value": delivery_id,
            "delivery_id": delivery_id,
            "received_at": sent_at,
            "stored_at": stored_at,
            "reply_expires_at": None,
            "processing_state": "handled",
            "state_updated_at": stored_at,
            "metadata_json": _json_object(metadata),
        }

    def append_confirmed_outbound_once(
        self,
        delivery_id,
        conversation,
        *,
        content,
        sent_at,
        sender="self",
        chat_type="private",
        message_type="text",
        message_attr="self",
        metadata=None,
        now=None,
    ):
        """Append confirmed outbound history once, keyed by delivery ID."""

        values = self._confirmed_outbound_values(
            delivery_id,
            conversation,
            content,
            sent_at,
            sender,
            chat_type,
            message_type,
            message_attr,
            metadata,
            _now(now),
        )
        with self._transaction() as connection:
            result = self._record_event_locked(connection, values, advances_version=False)
            self._merge_confirmed_event(connection, delivery_id, values, metadata)
        return {"event_id": result["event_id"], "is_new": result["is_new"]}

    @staticmethod
    def _merge_confirmed_event(connection, delivery_id, values, metadata):
        row = connection.execute(
            "SELECT metadata_json FROM chat_events WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            return
        merged = json.loads(row["metadata_json"])
        if metadata:
            merged.update(dict(metadata))
        preserve_semantic_voice_text = str(values.get("message_type") or "").lower() in {
            "voice",
            "audio",
        }
        connection.execute(
            """
            UPDATE chat_events SET
                content = CASE WHEN ? THEN ? ELSE content END,
                original_content = CASE WHEN ? THEN ? ELSE original_content END,
                metadata_json = ?
            WHERE delivery_id = ?
            """,
            (
                preserve_semantic_voice_text,
                values["content"],
                preserve_semantic_voice_text,
                values["original_content"],
                _json_object(merged),
                delivery_id,
            ),
        )

    @staticmethod
    def _normalized_action_ids(action_ids):
        return list(dict.fromkeys(
            str(action_id or "").strip()
            for action_id in action_ids or ()
            if str(action_id or "").strip()
        ))

    def reserve_reply_echo(
        self,
        action_id,
        *,
        conversation,
        chat_type,
        kind,
        content,
        confirmable=True,
        message_types=(),
        at="",
        now=None,
    ):
        action_id = _required_text(action_id, "action_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        kind = _required_text(kind, "kind").lower()
        if kind not in REPLY_ECHO_KINDS:
            raise ValueError(f"unsupported reply echo kind: {kind}")
        content = _required_text(content, "content")
        message_types = tuple(dict.fromkeys(
            str(item or "").strip().lower()
            for item in message_types or ()
            if str(item or "").strip()
        ))
        at = str(at or "").strip().lstrip("@").strip()
        current = _now(now)
        immutable = (
            conversation,
            chat_type,
            kind,
            content,
            int(bool(confirmable)),
            json.dumps(message_types, ensure_ascii=False, separators=(",", ":")),
            at,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM reply_echo_expectations WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                stored = (
                    str(existing["conversation"]),
                    str(existing["chat_type"]),
                    str(existing["kind"]),
                    str(existing["content"]),
                    int(existing["confirmable"]),
                    str(existing["message_types_json"]),
                    str(existing["at"]),
                )
                if stored != immutable:
                    raise MessageStoreConflictError(
                        f"reply echo {action_id} was reused for different content"
                    )
                return False
            connection.execute(
                """
                INSERT INTO reply_echo_expectations(
                    action_id, conversation, chat_type, kind, content,
                    confirmable, message_types_json, at, state, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?)
                """,
                (action_id, *immutable, current, current),
            )
            return True

    def activate_reply_echoes(self, action_ids, *, expires_at, now=None):
        action_ids = self._normalized_action_ids(action_ids)
        if not action_ids:
            return 0
        expires_at = _timestamp(expires_at, "expires_at")
        current = _now(now)
        placeholders = ", ".join("?" for _ in action_ids)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE reply_echo_expectations SET
                    state = 'active', expires_at = ?, updated_at = ?
                WHERE action_id IN ({placeholders}) AND state = 'reserved'
                """,
                [expires_at, current, *action_ids],
            )
            return cursor.rowcount

    def complete_reply_echoes(self, action_ids, *, expires_at, now=None):
        action_ids = self._normalized_action_ids(action_ids)
        if not action_ids:
            return 0
        expires_at = _timestamp(expires_at, "expires_at")
        current = _now(now)
        placeholders = ", ".join("?" for _ in action_ids)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE reply_echo_expectations SET
                    state = CASE WHEN state = 'active' THEN 'complete' ELSE state END,
                    expires_at = CASE
                        WHEN state = 'active' OR expires_at IS NULL THEN ?
                        ELSE expires_at
                    END,
                    updated_at = ?
                WHERE action_id IN ({placeholders})
                  AND state IN ('active', 'complete')
                """,
                [expires_at, current, *action_ids],
            )
            return cursor.rowcount

    def mark_reply_echo_matched(self, action_id, *, expires_at, now=None):
        action_id = _required_text(action_id, "action_id")
        expires_at = _timestamp(expires_at, "expires_at")
        current = _now(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE reply_echo_expectations SET
                    state = 'matched',
                    expires_at = CASE WHEN state = 'active' THEN ? ELSE expires_at END,
                    updated_at = ?
                WHERE action_id = ? AND state IN ('active', 'matched', 'complete')
                """,
                (expires_at, current, action_id),
            )
            return cursor.rowcount == 1

    def discard_reply_echoes(self, action_ids):
        action_ids = self._normalized_action_ids(action_ids)
        if not action_ids:
            return 0
        placeholders = ", ".join("?" for _ in action_ids)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"DELETE FROM reply_echo_expectations WHERE action_id IN ({placeholders})",
                action_ids,
            )
            return cursor.rowcount

    def prune_reply_echoes(self, *, now=None):
        current = _now(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM reply_echo_expectations
                WHERE state IN ('matched', 'complete')
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (current,),
            )
            return cursor.rowcount

    def recover_reply_echoes(self, *, ttl, limit=512, max_age=DEFAULT_REPLY_TTL_SECONDS, now=None):
        current = _now(now)
        ttl = max(1.0, float(ttl))
        limit = max(1, int(limit))
        max_age = max(ttl, float(max_age))
        with self._transaction() as connection:
            connection.execute("DELETE FROM reply_echo_expectations WHERE state = 'reserved'")
            connection.execute(
                """
                DELETE FROM reply_echo_expectations
                WHERE state IN ('active', 'matched', 'complete')
                  AND (
                      expires_at IS NULL
                      OR expires_at <= ?
                      OR created_at <= ?
                  )
                """,
                (current, current - max_age),
            )
            rows = connection.execute(
                """
                SELECT expectation.*, event.content AS confirmed_content
                FROM reply_echo_expectations AS expectation
                LEFT JOIN chat_events AS event
                  ON event.delivery_id = expectation.action_id
                WHERE expectation.state IN ('active', 'matched', 'complete')
                  AND expectation.expires_at > ?
                ORDER BY expectation.created_at, expectation.action_id
                """,
                (current,),
            ).fetchall()
            if len(rows) > limit:
                dropped = [str(row["action_id"]) for row in rows[:-limit]]
                placeholders = ", ".join("?" for _ in dropped)
                connection.execute(
                    f"DELETE FROM reply_echo_expectations WHERE action_id IN ({placeholders})",
                    dropped,
                )
                rows = rows[-limit:]
            return [self._reply_echo_row(row) for row in rows]

    def reply_echo_expectations(self):
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT expectation.*, event.content AS confirmed_content
                FROM reply_echo_expectations AS expectation
                LEFT JOIN chat_events AS event
                  ON event.delivery_id = expectation.action_id
                ORDER BY expectation.created_at, expectation.action_id
                """
            ).fetchall()
            return [self._reply_echo_row(row) for row in rows]

    @staticmethod
    def _import_event_values(entry, stored_at):
        event_id = _required_text(_event_value(entry, "event_id"), "event_id")
        conversation = _required_text(_event_value(entry, "conversation"), "conversation")
        chat_type = _required_chat_type(_event_value(entry, "chat_type", "private"))
        received_at = _timestamp(_event_value(entry, "received_at"), "received_at")
        direction = _required_text(_event_value(entry, "direction"), "direction")
        return {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": chat_type,
            "direction": direction,
            "sender": str(_event_value(entry, "sender", "") or ""),
            "content": str(_event_value(entry, "content", "") or ""),
            "original_content": str(_event_value(entry, "original_content", "") or ""),
            "message_type": str(_event_value(entry, "message_type", "text") or "text"),
            "native_attr": str(_event_value(entry, "native_attr", "") or ""),
            "native_id": str(_event_value(entry, "native_id", "") or ""),
            "native_hash": str(_event_value(entry, "native_hash", "") or ""),
            "native_hash_text": str(_event_value(entry, "native_hash_text", "") or ""),
            "native_time": str(_event_value(entry, "native_time", "") or ""),
            "source": "history_import",
            "source_batch": "",
            "source_order": None,
            "identity_kind": "import",
            "identity_value": event_id,
            "delivery_id": None,
            "received_at": received_at,
            "stored_at": stored_at,
            "reply_expires_at": None,
            "processing_state": "handled",
            "state_updated_at": stored_at,
            "history_visible": 1,
            "metadata_json": _json_object(_event_value(entry, "metadata", None)),
        }

    def append_history(self, entries, *, now=None):
        """Append a complete history batch without changing reply versions."""

        entries = list(entries or [])
        current = _now(now)
        values = [self._import_event_values(entry, current) for entry in entries]
        with self._transaction() as connection:
            added = 0
            for item in values:
                result = self._record_event_locked(
                    connection,
                    item,
                    advances_version=False,
                )
                added += int(result["is_new"])
            return added

    @classmethod
    def _context_repair_values(
        cls,
        entry,
        *,
        conversation,
        chat_type,
        source_batch,
        source_order,
        received_at,
        stored_at,
    ):
        native_id = str(_event_value(entry, "message_id", "") or "").strip()
        native_attr = str(_event_value(entry, "attr", "") or "").strip().lower()
        direction = "manual_self" if native_attr == "self" else "friend"
        source = "wechat_context_repair"
        identity_input = {
            "conversation": conversation,
            "chat_type": chat_type,
            "native_id": native_id,
            "source": source,
            "source_batch": source_batch,
            "source_order": source_order,
        }
        event_id, identity_kind, identity_value = cls._logical_event_id(identity_input)
        content = str(_event_value(entry, "content", "") or "")
        image_paths = [
            str(path or "").strip()
            for path in (_event_value(entry, "image_paths", ()) or ())
            if str(path or "").strip()
        ]
        metadata = {"context_repair": True}
        if image_paths:
            metadata["image_paths"] = image_paths
        native_time = str(_event_value(entry, "time", "") or "").strip()
        if not native_time:
            metadata["time_inferred"] = True
        return {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": chat_type,
            "direction": direction,
            "sender": str(_event_value(entry, "sender", "") or ""),
            "content": content,
            "original_content": image_paths[0] if image_paths else content,
            "message_type": str(_event_value(entry, "type", "text") or "text"),
            "native_attr": native_attr,
            "native_id": native_id,
            "native_hash": str(_event_value(entry, "native_hash", "") or ""),
            "native_hash_text": str(_event_value(entry, "native_hash_text", "") or ""),
            "native_time": native_time,
            "source": source,
            "source_batch": source_batch,
            "source_order": source_order,
            "identity_kind": identity_kind,
            "identity_value": identity_value,
            "delivery_id": None,
            "received_at": received_at,
            "stored_at": stored_at,
            "reply_expires_at": None,
            "processing_state": "handled",
            "state_updated_at": stored_at,
            "history_visible": 1,
            "metadata_json": _json_object(metadata),
        }

    def reconcile_visible_tail(
        self,
        conversation,
        visible_tail,
        *,
        current_event_ids,
        chat_type,
        history_limit=50,
        now=None,
    ):
        """Atomically append the visible gap immediately before the current turn."""

        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        current_ids = list(dict.fromkeys(
            str(event_id or "").strip()
            for event_id in current_event_ids or ()
            if str(event_id or "").strip()
        ))
        if not current_ids:
            raise ValueError("current_event_ids is required")
        try:
            history_limit = max(1, int(history_limit))
        except (TypeError, ValueError) as exc:
            raise ValueError("history_limit must be an integer") from exc
        visible_tail = [entry for entry in visible_tail or [] if isinstance(entry, dict)]
        stored_at = _now(now)

        with self._transaction() as connection:
            current_placeholders = ", ".join("?" for _ in current_ids)
            current_rows = connection.execute(
                f"""
                SELECT event_id, conversation, chat_type, received_at
                FROM chat_events WHERE event_id IN ({current_placeholders})
                """,
                current_ids,
            ).fetchall()
            if len(current_rows) != len(current_ids):
                raise MessageStoreConflictError("one or more current context-repair events do not exist")
            if any(
                row["conversation"] != conversation or row["chat_type"] != chat_type
                for row in current_rows
            ):
                raise MessageStoreConflictError("current context-repair events belong to another conversation")
            boundary_at = min(float(row["received_at"]) for row in current_rows)

            local_rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM chat_events
                    WHERE conversation = ? AND chat_type = ?
                      AND history_visible = 1
                      AND received_at <= ?
                      AND event_id NOT IN ({current_placeholders})
                    ORDER BY received_at DESC, event_seq DESC
                    LIMIT ?
                )
                ORDER BY received_at, event_seq
                """,
                [conversation, chat_type, boundary_at, *current_ids, history_limit],
            ).fetchall()
            local_history = [dict(row) for row in local_rows]
            plan = build_tail_repair_plan(
                local_history,
                visible_tail,
                chat_type=chat_type,
            )

            deletion_row = connection.execute(
                """
                SELECT MAX(state_updated_at) AS deleted_at
                FROM chat_events
                WHERE conversation = ? AND chat_type = ? AND history_visible = 0
                """,
                (conversation, chat_type),
            ).fetchone()
            deletion_at = None if deletion_row is None else deletion_row["deleted_at"]
            deleted_boundary_skipped = 0
            selected = plan.messages_to_append
            has_post_delete_history = bool(
                deletion_at is not None
                and any(float(row["stored_at"]) > float(deletion_at) for row in local_rows)
            )
            if not plan.anchor_found and deletion_at is not None and not has_post_delete_history:
                deleted_boundary_skipped = len(selected)
                selected = []

            source_batch = hashlib.sha256("\x1f".join(current_ids).encode("utf-8")).hexdigest()
            event_ids = []
            added = 0
            visible_count = max(1, len(visible_tail))
            for entry in selected:
                source_order = int(entry.get("window_order", 0) or 0)
                distance = max(1, visible_count - source_order)
                values = self._context_repair_values(
                    entry,
                    conversation=conversation,
                    chat_type=chat_type,
                    source_batch=source_batch,
                    source_order=source_order,
                    received_at=boundary_at - distance / 1000.0,
                    stored_at=stored_at,
                )
                result = self._record_event_locked(connection, values, advances_version=False)
                event_ids.append(result["event_id"])
                added += int(result["is_new"])

            visible_events = {}
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                rows = connection.execute(
                    f"SELECT * FROM chat_events WHERE history_visible = 1 AND event_id IN ({placeholders})",
                    event_ids,
                ).fetchall()
                visible_events = {row["event_id"]: self._event_row(row) for row in rows}
            return {
                "added": added,
                "anchor_found": plan.anchor_found,
                "deleted_boundary_skipped": deleted_boundary_skipped,
                "events": [visible_events[event_id] for event_id in event_ids if event_id in visible_events],
            }

    def list_conversations(self, *, chat_type=None):
        parameters = []
        chat_type_sql = ""
        if chat_type is not None:
            chat_type_sql = " AND chat_type = ?"
            parameters.append(_required_chat_type(chat_type))
        with self._reader() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT conversation FROM chat_events
                WHERE history_visible = 1 {chat_type_sql}
                ORDER BY conversation
                """,
                parameters,
            ).fetchall()
            return [str(row["conversation"]) for row in rows]

    def attach_visual_notes(
        self,
        conversation,
        image_paths,
        visual_notes,
        *,
        chat_type=None,
    ):
        conversation = _required_text(conversation, "conversation")
        paths = [str(path or "").strip() for path in image_paths or []]
        paths = [path for path in paths if path]
        notes = [str(note or "").strip() for note in visual_notes or []]
        note_by_path = {
            path: notes[index]
            for index, path in enumerate(paths)
            if index < len(notes) and notes[index]
        }
        if not note_by_path:
            return False
        parameters = [conversation]
        chat_type_sql = ""
        if chat_type is not None:
            chat_type_sql = " AND chat_type = ?"
            parameters.append(_required_chat_type(chat_type))
        updated = False
        matched = set()
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT event_id, metadata_json FROM chat_events
                WHERE conversation = ? AND history_visible = 1 {chat_type_sql}
                ORDER BY received_at DESC, event_seq DESC
                """,
                parameters,
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                event_paths = [
                    str(path or "").strip()
                    for path in metadata.get("image_paths", [])
                    if str(path or "").strip()
                ]
                if not event_paths:
                    continue
                old_notes = list(metadata.get("visual_notes") or [])
                merged = []
                for index, path in enumerate(event_paths):
                    existing = str(old_notes[index] or "").strip() if index < len(old_notes) else ""
                    note = note_by_path.get(path, existing)
                    if path in note_by_path:
                        matched.add(path)
                    merged.append(note)
                primary = next((note for note in merged if note), "")
                if merged != old_notes or metadata.get("visual_note", "") != primary:
                    metadata["visual_notes"] = merged
                    metadata["visual_note"] = primary
                    connection.execute(
                        "UPDATE chat_events SET metadata_json = ? WHERE event_id = ?",
                        (_json_object(metadata), row["event_id"]),
                    )
                    updated = True
                if matched >= set(note_by_path):
                    break
        return updated

    def update_inbound_content(self, event_id, content, *, original_content=None, metadata=None, now=None):
        """Attach a later wxautox transcription or media enrichment to one fact."""

        event_id = _required_text(event_id, "event_id")
        current = _now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT direction, original_content, metadata_json FROM chat_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            if row["direction"] not in {"friend", "manual_self"}:
                raise MessageStoreTransitionError(f"event {event_id} cannot be enriched")
            merged_metadata = json.loads(row["metadata_json"])
            if metadata:
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be a dict")
                merged_metadata.update(metadata)
            connection.execute(
                """
                UPDATE chat_events SET
                    content = ?, original_content = ?, metadata_json = ?, state_updated_at = ?
                WHERE event_id = ?
                """,
                (
                    str(content or ""),
                    str(row["original_content"] if original_content is None else original_content),
                    _json_object(merged_metadata),
                    current,
                    event_id,
                ),
            )
            return True

    @staticmethod
    def _assert_no_active_jobs(connection, conversation=None, chat_type=None):
        clauses = ["status IN ('pending', 'generating', 'inflight')"]
        parameters = []
        if conversation is not None:
            clauses.append("conversation = ?")
            parameters.append(conversation)
        if chat_type is not None:
            clauses.append("chat_type = ?")
            parameters.append(chat_type)
        row = connection.execute(
            f"SELECT turn_id FROM reply_jobs WHERE {' AND '.join(clauses)} LIMIT 1",
            parameters,
        ).fetchone()
        if row is not None:
            raise MessageStoreTransitionError(
                f"active reply job {row['turn_id']} prevents conversation mutation"
            )

    def delete_conversation(self, conversation, *, chat_type=None, now=None):
        conversation = _required_text(conversation, "conversation")
        normalized_chat_type = None if chat_type is None else _required_chat_type(chat_type)
        current = _now(now)
        clauses = ["conversation = ?", "history_visible = 1"]
        parameters = [conversation]
        if normalized_chat_type is not None:
            clauses.append("chat_type = ?")
            parameters.append(normalized_chat_type)
        with self._transaction() as connection:
            self._assert_no_active_jobs(connection, conversation, normalized_chat_type)
            cursor = connection.execute(
                f"""
                UPDATE chat_events SET
                    history_visible = 0,
                    processing_state = CASE
                        WHEN processing_state = 'pending' THEN 'cancelled'
                        ELSE processing_state
                    END,
                    state_updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                [current, *parameters],
            )
            return cursor.rowcount

    def clear_history(self, *, chat_type=None, now=None):
        normalized_chat_type = None if chat_type is None else _required_chat_type(chat_type)
        current = _now(now)
        clauses = ["history_visible = 1"]
        parameters = []
        if normalized_chat_type is not None:
            clauses.append("chat_type = ?")
            parameters.append(normalized_chat_type)
        with self._transaction() as connection:
            self._assert_no_active_jobs(connection, chat_type=normalized_chat_type)
            cursor = connection.execute(
                f"""
                UPDATE chat_events SET
                    history_visible = 0,
                    processing_state = CASE
                        WHEN processing_state = 'pending' THEN 'cancelled'
                        ELSE processing_state
                    END,
                    state_updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                [current, *parameters],
            )
            return cursor.rowcount

    def conversation_version(self, conversation, *, chat_type="private"):
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        with self._reader() as connection:
            return self._current_version(connection, conversation, chat_type)

    def merge_conversations(self, old_conversation, new_conversation, *, chat_type="private"):
        old_conversation = _required_text(old_conversation, "old_conversation")
        new_conversation = _required_text(new_conversation, "new_conversation")
        chat_type = _required_chat_type(chat_type)
        if old_conversation == new_conversation:
            return {
                "changed": False,
                "events_changed": 0,
                "jobs_changed": 0,
                "echoes_changed": 0,
            }

        with self._transaction() as connection:
            self._assert_no_active_jobs(connection, old_conversation, chat_type)
            self._assert_no_active_jobs(connection, new_conversation, chat_type)

            state_rows = connection.execute(
                """
                SELECT conversation, version, updated_at
                FROM conversation_state
                WHERE chat_type = ? AND conversation IN (?, ?)
                """,
                (chat_type, old_conversation, new_conversation),
            ).fetchall()
            if state_rows:
                version = max(int(row["version"]) for row in state_rows)
                updated_at = max(float(row["updated_at"]) for row in state_rows)
                connection.execute(
                    "DELETE FROM conversation_state WHERE chat_type = ? AND conversation IN (?, ?)",
                    (chat_type, old_conversation, new_conversation),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_state(conversation, chat_type, version, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_conversation, chat_type, version, updated_at),
                )

            events = connection.execute(
                "UPDATE chat_events SET conversation = ? WHERE conversation = ? AND chat_type = ?",
                (new_conversation, old_conversation, chat_type),
            ).rowcount
            jobs = connection.execute(
                "UPDATE reply_jobs SET conversation = ? WHERE conversation = ? AND chat_type = ?",
                (new_conversation, old_conversation, chat_type),
            ).rowcount
            echoes = connection.execute(
                "UPDATE reply_echo_expectations SET conversation = ? WHERE conversation = ? AND chat_type = ?",
                (new_conversation, old_conversation, chat_type),
            ).rowcount
            return {
                "changed": bool(events or jobs or echoes or state_rows),
                "events_changed": events,
                "jobs_changed": jobs,
                "echoes_changed": echoes,
            }

    def history(
        self,
        conversation,
        limit,
        *,
        chat_type="private",
        exclude_event_ids=(),
        before_event_seq=None,
        before_event_ids=(),
    ):
        conversation = _required_text(conversation, "conversation")
        normalized_chat_type = None if chat_type is None else _required_chat_type(chat_type)
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit <= 0:
            return []
        excluded = list(dict.fromkeys(str(item or "").strip() for item in exclude_event_ids))
        excluded = [item for item in excluded if item]
        boundary_ids = list(dict.fromkeys(
            str(item or "").strip() for item in before_event_ids or ()
        ))
        boundary_ids = [item for item in boundary_ids if item]
        if before_event_seq is not None and boundary_ids:
            raise ValueError("before_event_seq and before_event_ids are mutually exclusive")
        if before_event_seq is not None:
            try:
                before_event_seq = int(before_event_seq)
            except (TypeError, ValueError) as exc:
                raise ValueError("before_event_seq must be an integer") from exc
            if before_event_seq < 0:
                raise ValueError("before_event_seq must not be negative")
        exclusion_sql = ""
        chat_type_sql = ""
        with self._reader() as connection:
            if boundary_ids:
                placeholders = ", ".join("?" for _ in boundary_ids)
                rows = connection.execute(
                    f"""
                    SELECT event_id, conversation, chat_type, event_seq
                    FROM chat_events WHERE event_id IN ({placeholders})
                    """,
                    boundary_ids,
                ).fetchall()
                if len(rows) != len(boundary_ids):
                    raise MessageStoreConflictError(
                        "one or more history boundary event IDs do not exist"
                    )
                for row in rows:
                    if row["conversation"] != conversation or (
                        normalized_chat_type is not None
                        and row["chat_type"] != normalized_chat_type
                    ):
                        raise MessageStoreConflictError(
                            f"event {row['event_id']} does not belong to the history conversation"
                        )
                before_event_seq = min(int(row["event_seq"]) for row in rows)

            parameters = [conversation]
            if normalized_chat_type is not None:
                chat_type_sql = " AND chat_type = ?"
                parameters.append(normalized_chat_type)
            if excluded:
                exclusion_sql = f" AND event_id NOT IN ({', '.join('?' for _ in excluded)})"
                parameters.extend(excluded)
            cutoff_sql = ""
            if before_event_seq is not None:
                cutoff_sql = " AND event_seq < ?"
                parameters.append(before_event_seq)
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM chat_events
                    WHERE conversation = ? {chat_type_sql}
                      AND history_visible = 1 {exclusion_sql} {cutoff_sql}
                    ORDER BY received_at DESC, event_seq DESC
                    LIMIT ?
                )
                ORDER BY received_at, event_seq
                """,
                parameters,
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def recent_image_events(self, conversation, *, chat_type, since, limit=9):
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        try:
            since = float(since)
        except (TypeError, ValueError) as exc:
            raise ValueError("since must be a timestamp") from exc
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit <= 0:
            return []
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM chat_events
                    WHERE conversation = ?
                      AND chat_type = ?
                      AND history_visible = 1
                      AND message_type = 'image'
                      AND received_at >= ?
                    ORDER BY received_at DESC, event_seq DESC
                    LIMIT ?
                )
                ORDER BY received_at, event_seq
                """,
                (conversation, chat_type, since, limit),
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def get_event(self, event_id):
        event_id = _required_text(event_id, "event_id")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM chat_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return self._event_row(row) if row else None

    def recover_pending_inbound(self, *, now=None, limit=1000, after_event_seq=0):
        """Expire stale unhandled inbound and return fresh ones in FIFO order."""

        current = _now(now)
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if limit <= 0:
            return []
        try:
            after_event_seq = max(0, int(after_event_seq))
        except (TypeError, ValueError) as exc:
            raise ValueError("after_event_seq must be an integer") from exc
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE chat_events SET
                    processing_state = 'expired',
                    state_updated_at = ?
                WHERE direction = 'friend'
                  AND processing_state = 'pending'
                  AND reply_expires_at <= ?
                """,
                (current, current),
            )
            rows = connection.execute(
                """
                SELECT * FROM chat_events
                WHERE direction = 'friend'
                  AND processing_state = 'pending'
                  AND reply_expires_at > ?
                  AND event_seq > ?
                ORDER BY event_seq
                LIMIT ?
                """,
                (current, after_event_seq, limit),
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def mark_inbound_events(self, event_ids, processing_state, *, now=None):
        """Atomically terminalize a complete set of inbound event IDs."""

        event_ids = list(dict.fromkeys(_required_text(item, "event_id") for item in event_ids))
        if not event_ids:
            raise ValueError("event_ids are required")
        processing_state = _required_text(processing_state, "processing_state")
        if processing_state not in {"handled", "cancelled", "expired"}:
            raise ValueError("processing_state must be handled, cancelled, or expired")
        placeholders = ", ".join("?" for _ in event_ids)
        current = _now(now)
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT event_id, direction, processing_state "
                f"FROM chat_events WHERE event_id IN ({placeholders})",
                event_ids,
            ).fetchall()
            if len(rows) != len(event_ids):
                raise MessageStoreConflictError("one or more inbound event IDs do not exist")
            for row in rows:
                if row["direction"] != "friend":
                    raise MessageStoreConflictError(f"event {row['event_id']} is not friend inbound")
                if row["processing_state"] not in {"pending", processing_state}:
                    raise MessageStoreTransitionError(
                        f"event {row['event_id']} is already {row['processing_state']}"
                    )
            connection.execute(
                f"""
                UPDATE chat_events SET
                    processing_state = ?, state_updated_at = ?
                WHERE event_id IN ({placeholders})
                """,
                [processing_state, current, *event_ids],
            )
            return sum(
                row["processing_state"] != processing_state
                for row in rows
            )

    @staticmethod
    def _normalized_event_ids(event_ids):
        result = list(dict.fromkeys(_required_text(item, "event_id") for item in event_ids))
        if not result:
            raise ValueError("event_ids are required")
        return result

    @classmethod
    def _assert_job_events(cls, connection, conversation, chat_type, event_ids):
        placeholders = ", ".join("?" for _ in event_ids)
        rows = connection.execute(
            f"""
            SELECT event_id, conversation, chat_type, direction, processing_state
            FROM chat_events WHERE event_id IN ({placeholders})
            """,
            event_ids,
        ).fetchall()
        if len(rows) != len(event_ids):
            raise MessageStoreConflictError("one or more reply event IDs do not exist")
        for row in rows:
            if (
                row["conversation"] != conversation
                or row["chat_type"] != chat_type
                or row["direction"] != "friend"
            ):
                raise MessageStoreConflictError(
                    f"event {row['event_id']} does not belong to the reply conversation"
                )
            if row["processing_state"] in {"cancelled", "expired"}:
                raise MessageStoreTransitionError(
                    f"event {row['event_id']} is already {row['processing_state']}"
                )

    @classmethod
    def _insert_reply_job(
        cls,
        connection,
        turn_id,
        conversation,
        chat_type,
        route_source,
        expected_version,
        expires_at,
        event_ids,
        current,
    ):
        existing = connection.execute(
            "SELECT * FROM reply_jobs WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if existing is not None:
            existing_ids = cls._job_event_ids(connection, turn_id)
            same = (
                existing["conversation"] == conversation
                and existing["chat_type"] == chat_type
                and (not route_source or existing["route_source"] == route_source)
                and int(existing["expected_version"]) == expected_version
                and float(existing["expires_at"]) == expires_at
                and existing_ids == event_ids
            )
            if not same:
                raise MessageStoreConflictError(
                    f"turn_id {turn_id} was reused for a different reply job"
                )
            return False

        cls._assert_job_events(connection, conversation, chat_type, event_ids)
        connection.execute(
            """
            INSERT INTO reply_jobs(
                turn_id, conversation, chat_type, route_source, expected_version, expires_at,
                action_count, status, created_at, updated_at, finished_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?, NULL, '')
            """,
            (
                turn_id,
                conversation,
                chat_type,
                route_source,
                expected_version,
                expires_at,
                current,
                current,
            ),
        )
        connection.executemany(
            "INSERT INTO reply_job_events(turn_id, event_id, event_order) VALUES (?, ?, ?)",
            [(turn_id, event_id, index) for index, event_id in enumerate(event_ids)],
        )
        placeholders = ", ".join("?" for _ in event_ids)
        connection.execute(
            f"""
            UPDATE chat_events SET
                processing_state = 'handled',
                state_updated_at = ?
            WHERE event_id IN ({placeholders})
            """,
            [current, *event_ids],
        )
        return True

    def create_reply_job(
        self,
        turn_id,
        *,
        conversation,
        expected_version,
        expires_at,
        event_ids,
        chat_type="private",
        route_source="",
        now=None,
    ):
        turn_id = _required_text(turn_id, "turn_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        route_source = str(route_source or "").strip()
        expected_version = int(expected_version)
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        expires_at = _timestamp(expires_at, "expires_at")
        event_ids = self._normalized_event_ids(event_ids)
        current = _now(now)
        with self._transaction() as connection:
            return self._insert_reply_job(
                connection,
                turn_id,
                conversation,
                chat_type,
                route_source,
                expected_version,
                expires_at,
                event_ids,
                current,
            )

    def mark_reply_job_generating(self, turn_id, *, now=None):
        turn_id = _required_text(turn_id, "turn_id")
        current = _now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM reply_jobs WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return "cancelled"
            if row["status"] not in {"pending", "generating"} or int(row["action_count"]) != 0:
                raise MessageStoreTransitionError(f"reply job {turn_id} cannot start generating")
            if float(row["expires_at"]) <= current:
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "expired",
                    "reply TTL expired before generation",
                    current,
                )
                return "expired"
            current_version = self._current_version(
                connection,
                str(row["conversation"]),
                str(row["chat_type"]),
            )
            if current_version != int(row["expected_version"]):
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "stale",
                    "conversation version changed before generation",
                    current,
                )
                return "stale"
            if row["status"] == "generating":
                return "generating"
            connection.execute(
                "UPDATE reply_jobs SET status = 'generating', updated_at = ? WHERE turn_id = ?",
                (current, turn_id),
            )
            return "generating"

    @staticmethod
    def _prepare_actions(connection, turn_id, action_count, current):
        row = connection.execute(
            "SELECT status, action_count FROM reply_jobs WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise MessageStoreConflictError(f"reply job {turn_id} does not exist")
        existing_count = int(row["action_count"])
        existing_actions = connection.execute(
            "SELECT action_id, action_index FROM delivery_actions WHERE turn_id = ? ORDER BY action_index",
            (turn_id,),
        ).fetchall()
        if existing_actions:
            expected = [(f"{turn_id}:{index}", index) for index in range(action_count)]
            actual = [(item["action_id"], int(item["action_index"])) for item in existing_actions]
            if existing_count != action_count or actual != expected:
                raise MessageStoreConflictError(
                    f"reply job {turn_id} already has different delivery actions"
                )
            return [item[0] for item in expected], False
        if existing_count not in {0, action_count}:
            raise MessageStoreConflictError(
                f"reply job {turn_id} already has action_count={existing_count}"
            )
        if row["status"] not in {"pending", "generating"}:
            raise MessageStoreTransitionError(
                f"reply job {turn_id} is already {row['status']}"
            )
        actions = [(f"{turn_id}:{index}", turn_id, index, "pending") for index in range(action_count)]
        connection.executemany(
            """
            INSERT INTO delivery_actions(action_id, turn_id, action_index, status)
            VALUES (?, ?, ?, ?)
            """,
            actions,
        )
        connection.execute(
            """
            UPDATE reply_jobs SET
                action_count = ?, status = 'pending', updated_at = ?, error = ''
            WHERE turn_id = ?
            """,
            (action_count, current, turn_id),
        )
        return [item[0] for item in actions], True

    def prepare_delivery_actions(self, turn_id, action_count, *, now=None):
        turn_id = _required_text(turn_id, "turn_id")
        try:
            action_count = int(action_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("action_count must be a positive integer") from exc
        if action_count <= 0:
            raise ValueError("action_count must be a positive integer")
        current = _now(now)
        with self._transaction() as connection:
            actions, _created = self._prepare_actions(connection, turn_id, action_count, current)
            return actions

    def register_reply_turn(
        self,
        turn_id,
        *,
        conversation,
        expected_version,
        expires_at,
        event_ids,
        action_count,
        chat_type="private",
        route_source="",
        now=None,
    ):
        """Atomically register a generated reply and all of its bubbles."""

        turn_id = _required_text(turn_id, "turn_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        route_source = str(route_source or "").strip()
        expected_version = int(expected_version)
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        expires_at = _timestamp(expires_at, "expires_at")
        event_ids = self._normalized_event_ids(event_ids)
        try:
            action_count = int(action_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("action_count must be a positive integer") from exc
        if action_count <= 0:
            raise ValueError("action_count must be a positive integer")
        current = _now(now)
        with self._transaction() as connection:
            job_created = self._insert_reply_job(
                connection,
                turn_id,
                conversation,
                chat_type,
                route_source,
                expected_version,
                expires_at,
                event_ids,
                current,
            )
            _actions, actions_created = self._prepare_actions(
                connection,
                turn_id,
                action_count,
                current,
            )
            return job_created or actions_created

    @classmethod
    def _cancel_pending_locked(cls, connection, turn_id, status, error, current):
        if status not in {"cancelled", "stale", "expired"}:
            raise ValueError("status must be cancelled, stale, or expired")
        cursor = connection.execute(
            """
            UPDATE delivery_actions SET status = ?, finished_at = ?, error = ?
            WHERE turn_id = ? AND status = 'pending'
            """,
            (status, current, str(error or ""), turn_id),
        )
        inflight = connection.execute(
            """
            SELECT 1 FROM delivery_actions
            WHERE turn_id = ? AND status = 'inflight' LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        if inflight is None:
            updated = connection.execute(
                """
                UPDATE reply_jobs SET
                    status = ?, updated_at = ?, finished_at = ?, error = ?
                WHERE turn_id = ? AND status NOT IN ('done', 'uncertain')
                """,
                (status, current, current, str(error or ""), turn_id),
            )
        return cursor.rowcount

    def conditional_claim(
        self,
        action_id,
        *,
        conversation,
        expected_version,
        expires_at,
        now=None,
    ):
        """Claim one bubble iff identity, order, version and TTL still match."""

        action_id = _required_text(action_id, "action_id")
        conversation = _required_text(conversation, "conversation")
        expected_version = int(expected_version)
        expires_at = _timestamp(expires_at, "expires_at")
        current = _now(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, j.conversation, j.chat_type, j.expected_version, j.expires_at,
                       j.status AS job_status
                FROM delivery_actions AS a
                JOIN reply_jobs AS j ON j.turn_id = a.turn_id
                WHERE a.action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                return "cancelled"
            status = str(row["status"])
            if status == "inflight":
                return "blocked"
            if status != "pending":
                return status
            turn_id = str(row["turn_id"])
            if (
                row["conversation"] != conversation
                or int(row["expected_version"]) != expected_version
                or float(row["expires_at"]) != expires_at
            ):
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "cancelled",
                    "delivery claim metadata mismatch",
                    current,
                )
                return "cancelled"
            if current >= expires_at:
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "expired",
                    "reply TTL expired before delivery claim",
                    current,
                )
                return "expired"
            current_version = self._current_version(
                connection,
                conversation,
                str(row["chat_type"]),
            )
            if current_version != expected_version:
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "stale",
                    "conversation version changed before delivery claim",
                    current,
                )
                return "stale"
            inflight = connection.execute(
                """
                SELECT 1 FROM delivery_actions
                WHERE turn_id = ? AND status = 'inflight' LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
            if inflight is not None:
                return "blocked"
            previous = connection.execute(
                """
                SELECT status FROM delivery_actions
                WHERE turn_id = ? AND action_index < ?
                ORDER BY action_index DESC LIMIT 1
                """,
                (turn_id, int(row["action_index"])),
            ).fetchone()
            if previous is not None and previous["status"] != "done":
                previous_status = str(previous["status"])
                return previous_status if previous_status in ACTION_STATES - {"pending", "inflight"} else "blocked"
            cursor = connection.execute(
                """
                UPDATE delivery_actions SET status = 'inflight', claimed_at = ?, error = ''
                WHERE action_id = ? AND status = 'pending'
                """,
                (current, action_id),
            )
            if cursor.rowcount != 1:
                return "blocked"
            connection.execute(
                """
                UPDATE reply_jobs SET status = 'inflight', updated_at = ?, error = ''
                WHERE turn_id = ?
                """,
                (current, turn_id),
            )
            return "claimed"

    @classmethod
    def _finish_action_locked(cls, connection, action_id, status, error, current):
        row = connection.execute(
            "SELECT turn_id, status FROM delivery_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return False
        current_status = str(row["status"])
        if current_status == status:
            return False
        if current_status != "inflight" and not (
            current_status == "uncertain" and status == "done"
        ):
            raise MessageStoreTransitionError(
                f"delivery action {action_id} is {current_status}, not inflight"
            )
        turn_id = str(row["turn_id"])
        connection.execute(
            """
            UPDATE delivery_actions SET status = ?, finished_at = ?, error = ?
            WHERE action_id = ?
            """,
            (status, current, str(error or ""), action_id),
        )
        if status in {"uncertain", "cancelled", "stale", "expired"}:
            remainder_status = "cancelled" if status == "uncertain" else status
            remainder_error = (
                "earlier delivery result is uncertain"
                if status == "uncertain"
                else str(error or "earlier delivery was not started")
            )
            connection.execute(
                """
                UPDATE delivery_actions SET
                    status = ?, finished_at = ?, error = ?
                WHERE turn_id = ? AND status = 'pending'
                """,
                (remainder_status, current, remainder_error, turn_id),
            )
            connection.execute(
                """
                UPDATE reply_jobs SET
                    status = ?, updated_at = ?, finished_at = ?, error = ?
                WHERE turn_id = ?
                """,
                (status, current, current, str(error or ""), turn_id),
            )
            return True

        remaining = connection.execute(
            """
            SELECT status FROM delivery_actions
            WHERE turn_id = ? AND status != 'done'
            ORDER BY action_index LIMIT 1
            """,
            (turn_id,),
        ).fetchone()
        if remaining is None:
            job_status, finished_at = "done", current
        elif remaining["status"] == "pending":
            job_status, finished_at = "pending", None
        else:
            job_status = str(remaining["status"])
            finished_at = current
        connection.execute(
            """
            UPDATE reply_jobs SET
                status = ?, updated_at = ?, finished_at = ?, error = ?
            WHERE turn_id = ?
            """,
            (job_status, current, finished_at, str(error or ""), turn_id),
        )
        return True

    def confirm_outbound(
        self,
        action_id,
        conversation,
        *,
        content,
        sent_at,
        sender="self",
        chat_type="private",
        message_type="text",
        message_attr="self",
        metadata=None,
        now=None,
    ):
        """Atomically append confirmed history and complete one claimed bubble."""

        action_id = _required_text(action_id, "action_id")
        conversation = _required_text(conversation, "conversation")
        chat_type = _required_chat_type(chat_type)
        current = _now(now)
        values = self._confirmed_outbound_values(
            action_id,
            conversation,
            content,
            sent_at,
            sender,
            chat_type,
            message_type,
            message_attr,
            metadata,
            current,
        )
        with self._transaction() as connection:
            action = connection.execute(
                """
                SELECT a.status, j.conversation, j.chat_type
                FROM delivery_actions AS a
                JOIN reply_jobs AS j ON j.turn_id = a.turn_id
                WHERE a.action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if action is None:
                raise MessageStoreConflictError(f"delivery action {action_id} does not exist")
            if action["conversation"] != conversation or action["chat_type"] != chat_type:
                raise MessageStoreConflictError(
                    f"delivery action {action_id} belongs to a different conversation"
                )
            if action["status"] not in {"inflight", "done", "uncertain"}:
                raise MessageStoreTransitionError(
                    f"delivery action {action_id} is {action['status']}, not confirmable"
                )
            event = self._record_event_locked(connection, values, advances_version=False)
            self._merge_confirmed_event(connection, action_id, values, metadata)
            finished = self._finish_action_locked(
                connection,
                action_id,
                "done",
                "",
                current,
            )
            return {
                "event_id": event["event_id"],
                "is_new": event["is_new"],
                "action_finished": finished,
            }

    def finish(self, action_id, status="uncertain", error="", *, now=None):
        """Finish a claimed action without recording confirmed outbound history."""

        action_id = _required_text(action_id, "action_id")
        status = _required_text(status, "status")
        if status not in {"uncertain", "cancelled", "stale", "expired"}:
            raise ValueError("status must be uncertain, cancelled, stale, or expired")
        current = _now(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM delivery_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                return ""
            current_status = str(row["status"])
            if current_status in {"done", status}:
                return current_status
            self._finish_action_locked(
                connection,
                action_id,
                status,
                error,
                current,
            )
            return status

    def cancel_pending(self, turn_id, status="cancelled", error="", *, now=None):
        turn_id = _required_text(turn_id, "turn_id")
        current = _now(now)
        with self._transaction() as connection:
            return self._cancel_pending_locked(
                connection,
                turn_id,
                _required_text(status, "status"),
                error,
                current,
            )

    def get_reply_job(self, turn_id):
        turn_id = _required_text(turn_id, "turn_id")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM reply_jobs WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            return self._job_row(connection, row)

    def delivery_actions(self, turn_id):
        turn_id = _required_text(turn_id, "turn_id")
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_actions
                WHERE turn_id = ? ORDER BY action_index
                """,
                (turn_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def delivery_action(self, action_id):
        action_id = _required_text(action_id, "action_id")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            return dict(row) if row else None

    def delivery_action_status(self, action_id):
        action = self.delivery_action(action_id)
        return str(action["status"]) if action else ""

    def recover_startup(self, *, now=None):
        """Apply crash rules and return only jobs that are safe to regenerate."""

        current = _now(now)
        replay_jobs = []
        expired_job_ids = []
        uncertain_action_ids = []
        cancelled_job_ids = []
        with self._transaction() as connection:
            inflight = connection.execute(
                "SELECT action_id, turn_id FROM delivery_actions WHERE status = 'inflight'"
            ).fetchall()
            for action in inflight:
                action_id = str(action["action_id"])
                turn_id = str(action["turn_id"])
                connection.execute(
                    """
                    UPDATE delivery_actions SET
                        status = 'uncertain', finished_at = ?,
                        error = 'process exited after delivery claim'
                    WHERE action_id = ?
                    """,
                    (current, action_id),
                )
                connection.execute(
                    """
                    UPDATE delivery_actions SET
                        status = 'cancelled', finished_at = ?,
                        error = 'earlier delivery result is uncertain'
                    WHERE turn_id = ? AND status = 'pending'
                    """,
                    (current, turn_id),
                )
                connection.execute(
                    """
                    UPDATE reply_jobs SET
                        status = 'uncertain', updated_at = ?, finished_at = ?,
                        error = 'process exited after delivery claim'
                    WHERE turn_id = ?
                    """,
                    (current, current, turn_id),
                )
                uncertain_action_ids.append(action_id)

            jobs = connection.execute(
                """
                SELECT * FROM reply_jobs
                WHERE status IN ('pending', 'generating')
                ORDER BY created_at, turn_id
                """
            ).fetchall()
            for job in jobs:
                turn_id = str(job["turn_id"])
                if float(job["expires_at"]) <= current:
                    self._cancel_pending_locked(
                        connection,
                        turn_id,
                        "expired",
                        "reply TTL expired before startup recovery",
                        current,
                    )
                    expired_job_ids.append(turn_id)
                    continue
                delivered = connection.execute(
                    """
                    SELECT 1 FROM delivery_actions
                    WHERE turn_id = ? AND status = 'done' LIMIT 1
                    """,
                    (turn_id,),
                ).fetchone()
                if delivered is not None:
                    self._cancel_pending_locked(
                        connection,
                        turn_id,
                        "cancelled",
                        "remaining bubbles cannot be reconstructed after restart",
                        current,
                    )
                    cancelled_job_ids.append(turn_id)
                    continue
                connection.execute(
                    "DELETE FROM delivery_actions WHERE turn_id = ? AND status = 'pending'",
                    (turn_id,),
                )
                connection.execute(
                    """
                    UPDATE reply_jobs SET
                        action_count = 0, status = 'pending', updated_at = ?,
                        finished_at = NULL, error = ''
                    WHERE turn_id = ?
                    """,
                    (current, turn_id),
                )
                refreshed = connection.execute(
                    "SELECT * FROM reply_jobs WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                replay_jobs.append(self._job_row(connection, refreshed))
        return {
            "replay_jobs": replay_jobs,
            "expired_job_ids": expired_job_ids,
            "uncertain_action_ids": uncertain_action_ids,
            "cancelled_job_ids": cancelled_job_ids,
        }

    def cancel_unclaimed_on_shutdown(self, *, now=None):
        """Cancel work that never crossed the non-idempotent delivery boundary."""

        current = _now(now)
        cancelled_job_ids = []
        with self._transaction() as connection:
            jobs = connection.execute(
                """
                SELECT turn_id FROM reply_jobs
                WHERE status IN ('pending', 'generating')
                ORDER BY created_at, turn_id
                """
            ).fetchall()
            for job in jobs:
                turn_id = str(job["turn_id"])
                self._cancel_pending_locked(
                    connection,
                    turn_id,
                    "cancelled",
                    "clean shutdown before delivery claim",
                    current,
                )
                connection.execute(
                    """
                    UPDATE reply_jobs SET status = 'cancelled_shutdown'
                    WHERE turn_id = ? AND status = 'cancelled'
                    """,
                    (turn_id,),
                )
                cancelled_job_ids.append(turn_id)
            cursor = connection.execute(
                """
                UPDATE chat_events SET
                    processing_state = 'cancelled', state_updated_at = ?
                WHERE direction = 'friend' AND processing_state = 'pending'
                """,
                (current,),
            )
        return {
            "cancelled_job_ids": cancelled_job_ids,
            "cancelled_pending_events": cursor.rowcount,
        }

    def begin_ui_delivery(self, delivery_id, kind, payload, *, now=None):
        delivery_id = _required_text(delivery_id, "delivery_id")
        current = _now(now)
        payload = payload if isinstance(payload, dict) else {}
        metadata = {
            key: payload[key]
            for key in ("request_id", "run_id", "batch_id", "contact_key", "targets")
            if key in payload
        }
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO ui_deliveries(
                        delivery_id, kind, conversation, status, started_at,
                        finished_at, error, metadata_json, details_json
                    ) VALUES (?, ?, ?, 'inflight', ?, NULL, '', ?, '{}')
                    """,
                    (
                        delivery_id,
                        str(kind or ""),
                        str(payload.get("conversation") or ""),
                        current,
                        _json_object(metadata),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def finish_ui_delivery(self, delivery_id, status, error="", details=None, *, now=None):
        delivery_id = _required_text(delivery_id, "delivery_id")
        current = _now(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE ui_deliveries SET
                    status = ?, finished_at = ?, error = ?, details_json = ?
                WHERE delivery_id = ? AND status = 'inflight'
                """,
                (str(status or ""), current, str(error or ""), _json_object(details), delivery_id),
            )
            return cursor.rowcount == 1

    def freeze_interrupted_ui_deliveries(self, *, now=None):
        current = _now(now)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ui_deliveries WHERE status = 'inflight' ORDER BY started_at"
            ).fetchall()
            connection.execute(
                """
                UPDATE ui_deliveries SET
                    status = 'uncertain', finished_at = ?,
                    error = 'process exited after UI delivery started'
                WHERE status = 'inflight'
                """,
                (current,),
            )
            recovered = []
            for row in rows:
                item = dict(row)
                item.update(
                    status="uncertain",
                    finished_at=current,
                    error="process exited after UI delivery started",
                )
                recovered.append(self._ui_delivery_row(item))
            return recovered

    def ui_delivery_records(self):
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM ui_deliveries ORDER BY started_at, delivery_id"
            ).fetchall()
            return [self._ui_delivery_row(row) for row in rows]


class SQLiteUIDeliveryJournal:
    """UI-owner journal adapter backed by the account MessageStore."""

    def __init__(self, store):
        self.store = store

    def begin(self, delivery_id, kind, payload):
        return self.store.begin_ui_delivery(delivery_id, kind, payload)

    def finish(self, delivery_id, status, error="", details=None):
        return self.store.finish_ui_delivery(delivery_id, status, error, details)

    def freeze_interrupted(self):
        return self.store.freeze_interrupted_ui_deliveries()

    def records(self):
        return self.store.ui_delivery_records()
