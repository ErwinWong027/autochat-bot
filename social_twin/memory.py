from __future__ import annotations

import sqlite3
import uuid
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .profile import PROFILE_FIELDS

PROFILE_FIELD_SET = set(PROFILE_FIELDS)


class MemoryStore:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True) if Path(sqlite_path).parent != Path(".") else None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists contacts (
                    contact_id text primary key,
                    display_name text,
                    identity text,
                    profile text,
                    relationship_stage text,
                    taboos text,
                    preferences text,
                    recent_emotion text,
                    interaction_frequency text,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists conversations (
                    conversation_id text primary key,
                    contact_id text not null,
                    channel text not null,
                    created_at text not null,
                    updated_at text not null
                );
                create table if not exists messages (
                    id integer primary key autoincrement,
                    conversation_id text not null,
                    contact_id text not null,
                    channel text not null,
                    message_id text,
                    role text not null,
                    content text not null,
                    technique text,
                    decision_reason text,
                    created_at text not null
                );
                create table if not exists contact_profile_fields (
                    contact_id text not null,
                    field text not null,
                    value text not null,
                    confidence real not null,
                    source text not null,
                    updated_at text not null,
                    primary key(contact_id, field)
                );
                create table if not exists profile_evidence (
                    id integer primary key autoincrement,
                    contact_id text not null,
                    field text not null,
                    value text not null,
                    confidence real not null,
                    source text not null,
                    evidence text,
                    status text not null,
                    created_at text not null
                );
                create index if not exists idx_messages_conversation on messages(conversation_id, id);
                create index if not exists idx_messages_contact on messages(contact_id, id);
                create index if not exists idx_profile_evidence_contact on profile_evidence(contact_id, id);
                create table if not exists profile_audits (
                    id integer primary key autoincrement,
                    contact_id text not null unique,
                    display_name text,
                    profile_text text not null,
                    field_count integer not null,
                    evidence_fields_json text not null,
                    reasons_json text not null,
                    recent_messages_json text not null default '[]',
                    status text not null,
                    created_at text not null,
                    updated_at text not null
                );
                create index if not exists idx_profile_audits_status on profile_audits(status, updated_at);
                create table if not exists sent_messages (
                    hash       text primary key,
                    contact_id text not null,
                    created_at text not null
                );
                create index if not exists idx_sent_messages_created on sent_messages(created_at);
                create table if not exists draft_cache (
                    message_hash text primary key,
                    contact_id   text not null,
                    incoming     text not null,
                    draft        text not null,
                    created_at   text not null,
                    updated_at   text not null
                );
                create index if not exists idx_draft_cache_contact on draft_cache(contact_id, updated_at);
                create table if not exists threads (
                    thread_id text primary key,
                    platform text not null,
                    platform_contact_id text not null,
                    display_name text,
                    status text not null default 'active',
                    created_at text not null,
                    updated_at text not null,
                    unique(platform, platform_contact_id)
                );
                create table if not exists thread_messages (
                    id integer primary key autoincrement,
                    thread_id text not null,
                    platform text not null,
                    platform_message_id text not null,
                    role text not null,
                    content text not null,
                    order_index integer,
                    occurred_at text,
                    technique text,
                    decision_reason text,
                    created_at text not null,
                    unique(thread_id, platform_message_id, role)
                );
                create index if not exists idx_thread_messages_thread on thread_messages(thread_id, id);
                create table if not exists thread_memory (
                    thread_id text primary key,
                    working_summary text not null default '',
                    pinned_facts_json text not null default '{}',
                    preferences_json text not null default '{}',
                    taboos_json text not null default '{}',
                    relationship_state text not null default '',
                    topic_history_json text not null default '[]',
                    reply_history_json text not null default '[]',
                    updated_at text not null
                );
                create table if not exists thread_profile_fields (
                    thread_id text not null,
                    field text not null,
                    value text not null,
                    confidence real not null,
                    source text not null,
                    updated_at text not null,
                    primary key(thread_id, field)
                );
                create table if not exists thread_profile_evidence (
                    id integer primary key autoincrement,
                    thread_id text not null,
                    field text not null,
                    value text not null,
                    confidence real not null,
                    source text not null,
                    evidence text,
                    status text not null,
                    created_at text not null
                );
                create table if not exists thread_pending_groups (
                    thread_id text not null,
                    group_hash text primary key,
                    message_ids_json text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null
                );
                """
            )
            self._ensure_contact_columns(conn)
            self._ensure_profile_audit_columns(conn)
            self._ensure_profile_field_constraints(conn)
            self._migrate_thread_profiles_to_contacts(conn)
            self._seed_existing_profile_audits(conn)

    def _ensure_contact_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("pragma table_info(contacts)").fetchall()}
        columns = {
            "identity": "text",
            "recent_emotion": "text",
            "interaction_frequency": "text",
        }
        for column, column_type in columns.items():
            if column not in existing:
                conn.execute(f"alter table contacts add column {column} {column_type}")

    def _ensure_profile_audit_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("pragma table_info(profile_audits)").fetchall()}
        if "recent_messages_json" not in existing:
            conn.execute("alter table profile_audits add column recent_messages_json text not null default '[]'")
        if "last_llm_prompt" not in existing:
            conn.execute("alter table profile_audits add column last_llm_prompt text not null default ''")
        if "last_decision_prompt" not in existing:
            conn.execute("alter table profile_audits add column last_decision_prompt text not null default ''")
        if "last_llm_prompts_json" not in existing:
            conn.execute("alter table profile_audits add column last_llm_prompts_json text not null default '{}'")

    def _ensure_profile_field_constraints(self, conn: sqlite3.Connection) -> None:
        allowed = ", ".join("'" + field.replace("'", "''") + "'" for field in PROFILE_FIELDS)
        conn.executescript(
            f"""
            drop trigger if exists trg_contact_profile_fields_valid_field_insert;
            drop trigger if exists trg_contact_profile_fields_valid_field_update;
            drop trigger if exists trg_profile_evidence_valid_field_insert;
            drop trigger if exists trg_profile_evidence_valid_field_update;
            create trigger trg_contact_profile_fields_valid_field_insert
            before insert on contact_profile_fields
            when new.field not in ({allowed})
            begin
                select raise(abort, 'invalid profile field');
            end;
            create trigger trg_contact_profile_fields_valid_field_update
            before update on contact_profile_fields
            when new.field not in ({allowed})
            begin
                select raise(abort, 'invalid profile field');
            end;
            create trigger trg_profile_evidence_valid_field_insert
            before insert on profile_evidence
            when new.field not in ({allowed})
            begin
                select raise(abort, 'invalid profile field');
            end;
            create trigger trg_profile_evidence_valid_field_update
            before update on profile_evidence
            when new.field not in ({allowed})
            begin
                select raise(abort, 'invalid profile field');
            end;
            """
        )

    # ------------------------------------------------------------------
    # Thread-centric memory API
    # ------------------------------------------------------------------

    def get_or_create_thread(self, platform: str, platform_contact_id: str, display_name: str = "") -> str:
        platform = (platform or "unknown").strip()
        platform_contact_id = (platform_contact_id or "").strip()
        if not platform_contact_id:
            raise ValueError("platform_contact_id is required")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                select thread_id from threads
                where platform = ? and platform_contact_id = ?
                """,
                (platform, platform_contact_id),
            ).fetchone()
            if row:
                thread_id = str(row["thread_id"])
                conn.execute(
                    """
                    update threads
                    set display_name = coalesce(nullif(?, ''), display_name),
                        updated_at = ?
                    where thread_id = ?
                    """,
                    (display_name, now, thread_id),
                )
                self._ensure_thread_memory(conn, thread_id, now)
                return thread_id
            thread_id = str(uuid.uuid4())
            conn.execute(
                """
                insert into threads(thread_id, platform, platform_contact_id, display_name, status, created_at, updated_at)
                values (?, ?, ?, ?, 'active', ?, ?)
                """,
                (thread_id, platform, platform_contact_id, display_name, now, now),
            )
            self._ensure_thread_memory(conn, thread_id, now)
            return thread_id

    def _ensure_thread_memory(self, conn: sqlite3.Connection, thread_id: str, now: str) -> None:
        conn.execute(
            """
            insert or ignore into thread_memory(
                thread_id, working_summary, pinned_facts_json, preferences_json, taboos_json,
                relationship_state, topic_history_json, reply_history_json, updated_at
            )
            values (?, '', '{}', '{}', '{}', '', '[]', '[]', ?)
            """,
            (thread_id, now),
        )

    def thread_for_contact(self, platform: str, platform_contact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from threads where platform = ? and platform_contact_id = ?",
                (platform, platform_contact_id),
            ).fetchone()
        return dict(row) if row else None

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select * from threads where thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def add_thread_message(
        self,
        thread_id: str,
        platform: str,
        platform_message_id: str,
        role: str,
        content: str,
        order_index: int | None = None,
        occurred_at: str = "",
        technique: str = "",
        decision_reason: str = "",
    ) -> bool:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    insert into thread_messages(
                        thread_id, platform, platform_message_id, role, content, order_index,
                        occurred_at, technique, decision_reason, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        platform,
                        platform_message_id,
                        role,
                        content,
                        order_index,
                        occurred_at,
                        technique,
                        decision_reason,
                        now,
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                inserted = False
            conn.execute("update threads set updated_at = ? where thread_id = ?", (now, thread_id))
        return inserted

    def sync_thread_messages(self, thread_id: str, platform: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        inserted: list[dict[str, Any]] = []
        for item in messages:
            ok = self.add_thread_message(
                thread_id=thread_id,
                platform=platform,
                platform_message_id=str(item.get("platform_message_id", "")),
                role=str(item.get("role", "")),
                content=str(item.get("content", "")),
                order_index=item.get("order_index"),
                occurred_at=str(item.get("occurred_at", "")),
                technique=str(item.get("technique", "")),
                decision_reason=str(item.get("decision_reason", "")),
            )
            if ok:
                inserted.append(dict(item))
        return inserted

    def sync_thread_messages_incremental(
        self,
        thread_id: str,
        platform: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Sync only messages after the stored thread tail.

        UI indices are volatile, so alignment uses role + normalized text. New rows
        receive stable ids based on thread, role, normalized text, and occurrence.
        """
        incoming = [self._canonical_thread_message(item) for item in messages]
        incoming = [item for item in incoming if item["role"] in ("user", "sent") and item["content"]]
        if not incoming:
            return []

        stored = [
            self._canonical_thread_message(item)
            for item in self.all_thread_messages(thread_id)
            if item.get("role") in ("user", "sent")
        ]
        stored_keys = [(item["role"], item["content"]) for item in stored]
        incoming_keys = [(item["role"], item["content"]) for item in incoming]
        overlap = self._tail_prefix_overlap(stored_keys, incoming_keys)
        new_items = incoming[overlap:]
        if not new_items:
            return []

        occurrence_counts: dict[tuple[str, str], int] = {}
        for item in stored:
            key = (item["role"], item["content"])
            occurrence_counts[key] = occurrence_counts.get(key, 0) + 1

        payload: list[dict[str, Any]] = []
        for item in new_items:
            key = (item["role"], item["content"])
            occurrence_counts[key] = occurrence_counts.get(key, 0) + 1
            payload.append(
                {
                    **item,
                    "platform_message_id": self.stable_thread_message_id(
                        thread_id,
                        item["role"],
                        item["content"],
                        occurrence_counts[key],
                    ),
                }
            )
        return self.sync_thread_messages(thread_id, platform, payload)

    def stable_thread_message_id(self, thread_id: str, role: str, normalized_text: str, occurrence: int) -> str:
        raw = f"{thread_id}:{role}:{normalized_text}:{occurrence}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _canonical_thread_message(self, item: dict[str, Any]) -> dict[str, Any]:
        role = str(item.get("role", ""))
        if role == "out":
            role = "sent"
        return {
            **item,
            "role": role,
            "content": self._normalize_message_text(str(item.get("content", ""))),
        }

    def _normalize_message_text(self, text: str) -> str:
        return " ".join((text or "").strip().split())

    def _tail_prefix_overlap(
        self,
        stored_keys: list[tuple[str, str]],
        incoming_keys: list[tuple[str, str]],
    ) -> int:
        max_len = min(len(stored_keys), len(incoming_keys))
        for size in range(max_len, 0, -1):
            if stored_keys[-size:] == incoming_keys[:size]:
                return size
        return 0

    def recent_thread_messages(self, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, thread_id, platform, platform_message_id, role, content, order_index,
                       occurred_at, technique, decision_reason, created_at
                from thread_messages
                where thread_id = ?
                order by id desc limit ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def all_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, thread_id, platform, platform_message_id, role, content, order_index,
                       occurred_at, technique, decision_reason, created_at
                from thread_messages
                where thread_id = ?
                order by id asc
                """,
                (thread_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        messages = self.all_thread_messages(thread_id)
        user_message_ids = {
            str(message["platform_message_id"])
            for message in messages
            if message["role"] == "user" and str(message["platform_message_id"])
        }
        replied_user_ids = {
            str(message["platform_message_id"])
            for message in messages
            if message["role"] in ("sent", "out")
            and str(message["platform_message_id"]) in user_message_ids
        }
        last_out = -1
        for index, message in enumerate(messages):
            message_id = str(message["platform_message_id"])
            is_actual_outgoing = message["role"] in ("out", "sent") and message_id not in user_message_ids
            if is_actual_outgoing:
                last_out = index
        return [
            message
            for message in messages[last_out + 1 :]
            if message["role"] == "user" and str(message["platform_message_id"]) not in replied_user_ids
        ]

    def unsent_thread_drafts(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, thread_id, platform, platform_message_id, role, content, order_index,
                       occurred_at, technique, decision_reason, created_at
                from thread_messages
                where thread_id = ? and role = 'draft'
                order by id asc
                """,
                (thread_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_thread_pending_group(
        self,
        thread_id: str,
        message_ids: list[str],
        status: str = "pending",
    ) -> str:
        clean_ids = [str(item) for item in message_ids if str(item)]
        group_hash = hashlib.sha256(f"{thread_id}:{'|'.join(clean_ids)}".encode("utf-8")).hexdigest()
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into thread_pending_groups(thread_id, group_hash, message_ids_json, status, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(group_hash) do update set
                    status = excluded.status,
                    message_ids_json = excluded.message_ids_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, group_hash, json.dumps(clean_ids, ensure_ascii=False), status, now, now),
            )
        return group_hash

    def thread_pending_group_status(self, group_hash: str, status: str) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "update thread_pending_groups set status = ?, updated_at = ? where group_hash = ?",
                (status, now, group_hash),
            )

    def thread_recent_techniques(self, thread_id: str, limit: int = 3) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select technique from thread_messages
                where thread_id = ? and role in ('draft', 'sent') and technique is not null and technique != ''
                order by id desc limit ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [str(row["technique"]) for row in rows]

    def get_thread_memory(self, thread_id: str) -> dict[str, Any]:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            thread = conn.execute("select 1 from threads where thread_id = ?", (thread_id,)).fetchone()
            if not thread:
                return {
                    "thread_id": thread_id,
                    "working_summary": "",
                    "pinned_facts": {},
                    "preferences": {},
                    "taboos": {},
                    "relationship_state": "",
                    "topic_history": [],
                    "reply_history": [],
                    "updated_at": "",
                }
            self._ensure_thread_memory(conn, thread_id, now)
            row = conn.execute("select * from thread_memory where thread_id = ?", (thread_id,)).fetchone()
        data = dict(row) if row else {"thread_id": thread_id}
        return {
            "thread_id": thread_id,
            "working_summary": data.get("working_summary", ""),
            "pinned_facts": json.loads(data.get("pinned_facts_json") or "{}"),
            "preferences": json.loads(data.get("preferences_json") or "{}"),
            "taboos": json.loads(data.get("taboos_json") or "{}"),
            "relationship_state": data.get("relationship_state", ""),
            "topic_history": json.loads(data.get("topic_history_json") or "[]"),
            "reply_history": json.loads(data.get("reply_history_json") or "[]"),
            "updated_at": data.get("updated_at", ""),
        }

    def update_thread_memory(self, thread_id: str, memory: dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into thread_memory(
                    thread_id, working_summary, pinned_facts_json, preferences_json, taboos_json,
                    relationship_state, topic_history_json, reply_history_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(thread_id) do update set
                    working_summary=excluded.working_summary,
                    pinned_facts_json=excluded.pinned_facts_json,
                    preferences_json=excluded.preferences_json,
                    taboos_json=excluded.taboos_json,
                    relationship_state=excluded.relationship_state,
                    topic_history_json=excluded.topic_history_json,
                    reply_history_json=excluded.reply_history_json,
                    updated_at=excluded.updated_at
                """,
                (
                    thread_id,
                    str(memory.get("working_summary", "")),
                    json.dumps(memory.get("pinned_facts", {}), ensure_ascii=False),
                    json.dumps(memory.get("preferences", {}), ensure_ascii=False),
                    json.dumps(memory.get("taboos", {}), ensure_ascii=False),
                    str(memory.get("relationship_state", "")),
                    json.dumps(memory.get("topic_history", []), ensure_ascii=False),
                    json.dumps(memory.get("reply_history", []), ensure_ascii=False),
                    now,
                ),
            )

    def mark_thread_sent(self, thread_id: str, platform_message_id: str) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update thread_messages
                set role = 'sent', created_at = ?
                where thread_id = ? and platform_message_id = ? and role = 'draft'
                """,
                (now, thread_id, platform_message_id),
            )

    def apply_thread_profile_updates(self, thread_id: str, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contact_id = self.profile_contact_id(thread_id)
        return self.apply_profile_updates(contact_id, updates)

    def profile_contact_id(self, contact_or_thread_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "select platform_contact_id from threads where thread_id = ?",
                (contact_or_thread_id,),
            ).fetchone()
        return str(row["platform_contact_id"]) if row else contact_or_thread_id

    def _migrate_thread_profiles_to_contacts(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select t.platform_contact_id as contact_id, f.field, f.value, f.confidence, f.source, f.updated_at
            from thread_profile_fields f
            join threads t on t.thread_id = f.thread_id
            """
        ).fetchall()
        now = datetime.now().isoformat()
        for row in rows:
            contact_id = str(row["contact_id"])
            field = str(row["field"])
            value = str(row["value"]).strip()
            if field not in PROFILE_FIELD_SET or not value:
                continue
            confidence = float(row["confidence"] or 0.5)
            existing = conn.execute(
                """
                select value, confidence from contact_profile_fields
                where contact_id = ? and field = ?
                """,
                (contact_id, field),
            ).fetchone()
            status = "added"
            should_write = True
            if existing:
                previous_value = str(existing["value"])
                existing_confidence = float(existing["confidence"])
                if previous_value == value:
                    status = "confirmed"
                elif confidence > existing_confidence:
                    status = "updated"
                else:
                    status = "conflict"
                    should_write = False
            if should_write:
                conn.execute(
                    """
                    insert into contact_profile_fields(contact_id, field, value, confidence, source, updated_at)
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(contact_id, field) do update set
                        value=excluded.value,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (contact_id, field, value, confidence, str(row["source"] or "thread_profile_migration"), now),
                )
            conn.execute(
                """
                insert into profile_evidence(contact_id, field, value, confidence, source, evidence, status, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_id,
                    field,
                    value,
                    confidence,
                    str(row["source"] or "thread_profile_migration"),
                    "migrated from thread_profile_fields",
                    status,
                    now,
                ),
            )
        conn.execute("delete from thread_profile_fields")
        conn.execute("delete from thread_profile_evidence")

    def _seed_existing_profile_audits(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select f.contact_id, c.display_name, f.field, f.value
            from contact_profile_fields f
            left join contacts c on c.contact_id = f.contact_id
            order by f.contact_id, f.field
            """
        ).fetchall()
        by_contact: dict[str, dict[str, Any]] = {}
        for row in rows:
            contact_id = str(row["contact_id"])
            item = by_contact.setdefault(
                contact_id,
                {
                    "display_name": str(row["display_name"] or ""),
                    "fields": {},
                },
            )
            item["fields"][str(row["field"])] = str(row["value"])
        now = datetime.now().isoformat()
        for contact_id, item in by_contact.items():
            existing = conn.execute(
                "select 1 from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
            if existing:
                continue
            fields = item["fields"]
            evidence_fields = [
                field
                for field in ("raw_evidence", "about_me", "profile_prompts")
                if str(fields.get(field, "")).strip()
            ]
            reasons: list[str] = []
            if len(fields) < 3:
                reasons.append("field_count_low")
            if not evidence_fields:
                reasons.append("missing_raw_evidence_about_me_profile_prompts")
                reasons.append("legacy_profile_text_missing")
            if not reasons:
                continue
            profile_text = "\n".join(str(fields[field]).strip() for field in evidence_fields if str(fields[field]).strip())
            conn.execute(
                """
                insert into profile_audits(
                    contact_id, display_name, profile_text, field_count,
                    evidence_fields_json, reasons_json, recent_messages_json, status, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    contact_id,
                    str(item["display_name"]),
                    profile_text,
                    len(fields),
                    json.dumps(evidence_fields, ensure_ascii=False),
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(self._recent_messages_for_contact(conn, contact_id), ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def get_thread_profile(self, thread_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            thread = conn.execute("select * from threads where thread_id = ?", (thread_id,)).fetchone()
        if not thread:
            return {"contact": {"thread_id": thread_id}, "fields": {}, "evidence": [], "memory": self.get_thread_memory(thread_id)}
        profile = self.get_contact_profile(str(thread["platform_contact_id"]))
        profile["thread"] = dict(thread)
        profile["memory"] = self.get_thread_memory(thread_id)
        return profile

    def upsert_contact(
        self,
        contact_id: str,
        display_name: str = "",
        identity: str = "",
        profile: str = "",
        relationship_stage: str = "",
        taboos: str = "",
        preferences: str = "",
        recent_emotion: str = "",
        interaction_frequency: str = "",
    ) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into contacts(contact_id, display_name, identity, profile, relationship_stage, taboos, preferences, recent_emotion, interaction_frequency, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(contact_id) do update set
                    display_name=coalesce(nullif(excluded.display_name, ''), contacts.display_name),
                    identity=coalesce(nullif(excluded.identity, ''), contacts.identity),
                    profile=coalesce(nullif(excluded.profile, ''), contacts.profile),
                    relationship_stage=coalesce(nullif(excluded.relationship_stage, ''), contacts.relationship_stage),
                    taboos=coalesce(nullif(excluded.taboos, ''), contacts.taboos),
                    preferences=coalesce(nullif(excluded.preferences, ''), contacts.preferences),
                    recent_emotion=coalesce(nullif(excluded.recent_emotion, ''), contacts.recent_emotion),
                    interaction_frequency=coalesce(nullif(excluded.interaction_frequency, ''), contacts.interaction_frequency),
                    updated_at=excluded.updated_at
                """,
                (
                    contact_id,
                    display_name,
                    identity,
                    profile,
                    relationship_stage,
                    taboos,
                    preferences,
                    recent_emotion,
                    interaction_frequency,
                    now,
                    now,
                ),
            )

    def get_or_create_conversation(self, contact_id: str, channel: str, conversation_id: str | None = None) -> str:
        now = datetime.now().isoformat()
        if conversation_id:
            with self._connect() as conn:
                conn.execute(
                    """
                    insert or ignore into conversations(conversation_id, contact_id, channel, created_at, updated_at)
                    values (?, ?, ?, ?, ?)
                    """,
                    (conversation_id, contact_id, channel, now, now),
                )
            return conversation_id

        with self._connect() as conn:
            row = conn.execute(
                """
                select conversation_id from conversations
                where contact_id = ? and channel = ?
                order by updated_at desc limit 1
                """,
                (contact_id, channel),
            ).fetchone()
            if row:
                return str(row["conversation_id"])
            new_id = str(uuid.uuid4())
            conn.execute(
                """
                insert into conversations(conversation_id, contact_id, channel, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                """,
                (new_id, contact_id, channel, now, now),
            )
            return new_id

    def add_message(
        self,
        conversation_id: str,
        contact_id: str,
        channel: str,
        role: str,
        content: str,
        message_id: str = "",
        technique: str = "",
        decision_reason: str = "",
    ) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            if message_id:
                existing = conn.execute(
                    """
                    select 1 from messages
                    where conversation_id = ? and contact_id = ? and channel = ? and role = ? and message_id = ?
                    limit 1
                    """,
                    (conversation_id, contact_id, channel, role, message_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        "update conversations set updated_at = ? where conversation_id = ?",
                        (now, conversation_id),
                    )
                    return
            conn.execute(
                """
                insert into messages(conversation_id, contact_id, channel, message_id, role, content, technique, decision_reason, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, contact_id, channel, message_id, role, content, technique, decision_reason, now),
            )
            conn.execute(
                "update conversations set updated_at = ? where conversation_id = ?",
                (now, conversation_id),
            )

    def recent_messages(self, conversation_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select role, content, technique, decision_reason, created_at
                from messages
                where conversation_id = ?
                order by id desc limit ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_techniques(self, conversation_id: str, limit: int = 3) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select technique from messages
                where conversation_id = ? and role in ('assistant', 'draft', 'sent') and technique is not null and technique != ''
                order by id desc limit ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [str(row["technique"]) for row in rows]

    def apply_profile_updates(self, contact_id: str, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now().isoformat()
        results: list[dict[str, Any]] = []
        with self._connect() as conn:
            for update in updates:
                field = str(update.get("field", "")).strip()
                value = str(update.get("value", "")).strip()
                if field not in PROFILE_FIELD_SET or not value:
                    continue
                confidence = float(update.get("confidence", 0.5))
                source = str(update.get("source", "unknown")).strip() or "unknown"
                evidence = str(update.get("evidence", "")).strip()
                existing = conn.execute(
                    """
                    select value, confidence from contact_profile_fields
                    where contact_id = ? and field = ?
                    """,
                    (contact_id, field),
                ).fetchone()
                status = "added"
                should_write = True
                previous_value = ""
                if existing:
                    previous_value = str(existing["value"])
                    existing_confidence = float(existing["confidence"])
                    if previous_value == value:
                        status = "confirmed"
                    elif confidence > existing_confidence:
                        status = "updated"
                    else:
                        status = "conflict"
                        should_write = False

                if should_write:
                    conn.execute(
                        """
                        insert into contact_profile_fields(contact_id, field, value, confidence, source, updated_at)
                        values (?, ?, ?, ?, ?, ?)
                        on conflict(contact_id, field) do update set
                            value=excluded.value,
                            confidence=excluded.confidence,
                            source=excluded.source,
                            updated_at=excluded.updated_at
                        """,
                        (contact_id, field, value, confidence, source, now),
                    )

                conn.execute(
                    """
                    insert into profile_evidence(contact_id, field, value, confidence, source, evidence, status, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (contact_id, field, value, confidence, source, evidence, status, now),
                )
                results.append(
                    {
                        "field": field,
                        "value": value,
                        "confidence": confidence,
                        "source": source,
                        "status": status,
                        "previous_value": previous_value,
                    }
                )
        return results

    def assess_profile_coverage(
        self,
        contact_id: str,
        display_name: str = "",
        profile_text: str = "",
        min_fields: int = 3,
    ) -> dict[str, Any]:
        profile = self.get_contact_profile(contact_id)
        fields = profile.get("fields", {})
        evidence_fields = [
            field
            for field in ("raw_evidence", "about_me", "profile_prompts")
            if str(fields.get(field, {}).get("value", "")).strip()
        ]
        reasons: list[str] = []
        if len(fields) < min_fields:
            reasons.append("field_count_low")
        if not evidence_fields:
            reasons.append("missing_raw_evidence_about_me_profile_prompts")
        if profile_text.strip() and len(fields) < min_fields:
            reasons.append("profile_text_available_but_structured_fields_low")

        status = "open" if reasons else "resolved"
        self.upsert_profile_audit(
            contact_id=contact_id,
            display_name=display_name,
            profile_text=profile_text,
            field_count=len(fields),
            evidence_fields=evidence_fields,
            reasons=reasons,
            status=status,
        )
        return {
            "contact_id": contact_id,
            "display_name": display_name,
            "field_count": len(fields),
            "evidence_fields": evidence_fields,
            "reasons": reasons,
            "status": status,
        }

    def upsert_profile_audit(
        self,
        contact_id: str,
        display_name: str,
        profile_text: str,
        field_count: int,
        evidence_fields: list[str],
        reasons: list[str],
        status: str,
    ) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            recent_messages = self._recent_messages_for_contact(conn, contact_id)
            existing = conn.execute(
                "select created_at, recent_messages_json from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            recent_messages_json = json.dumps(
                self._merge_recent_messages(
                    str(existing["recent_messages_json"] or "[]") if existing else "[]",
                    recent_messages,
                ),
                ensure_ascii=False,
            )
            conn.execute(
                """
                insert into profile_audits(
                    contact_id, display_name, profile_text, field_count,
                    evidence_fields_json, reasons_json, recent_messages_json, status, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(contact_id) do update set
                    display_name=case
                        when coalesce(trim(profile_audits.display_name), '') = ''
                        then excluded.display_name
                        else profile_audits.display_name
                    end,
                    profile_text=case
                        when coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.profile_text
                        else profile_audits.profile_text
                    end,
                    field_count=case
                        when profile_audits.status = 'profile_text_captured'
                            or coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.field_count
                        else profile_audits.field_count
                    end,
                    evidence_fields_json=case
                        when profile_audits.status = 'profile_text_captured'
                            or coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.evidence_fields_json
                        else profile_audits.evidence_fields_json
                    end,
                    reasons_json=case
                        when profile_audits.status = 'profile_text_captured'
                            or coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.reasons_json
                        else profile_audits.reasons_json
                    end,
                    status=case
                        when profile_audits.status = 'profile_text_captured'
                            or coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.status
                        else profile_audits.status
                    end,
                    recent_messages_json=excluded.recent_messages_json,
                    updated_at=excluded.updated_at
                """,
                (
                    contact_id,
                    display_name,
                    profile_text,
                    field_count,
                    json.dumps(evidence_fields, ensure_ascii=False),
                    json.dumps(reasons, ensure_ascii=False),
                    recent_messages_json,
                    status,
                    created_at,
                    now,
                ),
            )

    def record_initial_profile_text(self, contact_id: str, display_name: str, profile_text: str) -> None:
        profile_text = profile_text.strip()
        if not profile_text:
            return
        now = datetime.now().isoformat()
        with self._connect() as conn:
            recent_messages = self._recent_messages_for_contact(conn, contact_id)
            existing = conn.execute(
                "select created_at, recent_messages_json from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            recent_messages_json = json.dumps(
                self._merge_recent_messages(
                    str(existing["recent_messages_json"] or "[]") if existing else "[]",
                    recent_messages,
                ),
                ensure_ascii=False,
            )
            conn.execute(
                """
                insert into profile_audits(
                    contact_id, display_name, profile_text, field_count,
                    evidence_fields_json, reasons_json, recent_messages_json, status, created_at, updated_at
                )
                values (?, ?, ?, 0, '[]', '[]', ?, 'profile_text_captured', ?, ?)
                on conflict(contact_id) do update set
                    display_name=case
                        when coalesce(trim(profile_audits.display_name), '') = ''
                        then excluded.display_name
                        else profile_audits.display_name
                    end,
                    profile_text=case
                        when coalesce(trim(profile_audits.profile_text), '') = ''
                        then excluded.profile_text
                        else profile_audits.profile_text
                    end,
                    recent_messages_json=excluded.recent_messages_json,
                    updated_at=excluded.updated_at
                """,
                (contact_id, display_name, profile_text, recent_messages_json, created_at, now),
            )

    def store_llm_prompt(self, contact_id: str, key: str, prompt: str, model: str = "") -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "select last_llm_prompts_json from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
            prompts: dict = {}
            if row and row["last_llm_prompts_json"]:
                try:
                    prompts = json.loads(row["last_llm_prompts_json"])
                except Exception:
                    prompts = {}
            previous = prompts.get(key) if isinstance(prompts.get(key), dict) else {}
            first_at = str(previous.get("first_at") or previous.get("at") or now)
            prompts[key] = {"prompt": prompt, "model": model, "at": now, "first_at": first_at}
            prompts_json = json.dumps(prompts, ensure_ascii=False)
            conn.execute(
                """
                insert into profile_audits(contact_id, display_name, profile_text, field_count,
                    evidence_fields_json, reasons_json, status, last_llm_prompts_json, created_at, updated_at)
                values (?, '', '', 0, '[]', '[]', 'llm_prompt_only', ?, ?, ?)
                on conflict(contact_id) do update set
                    last_llm_prompts_json = excluded.last_llm_prompts_json,
                    updated_at = excluded.updated_at
                """,
                (contact_id, prompts_json, now, now),
            )

    def has_profile_audit_text(self, contact_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from profile_audits where contact_id = ? and coalesce(trim(profile_text), '') != '' limit 1",
                (contact_id,),
            ).fetchone()
        return row is not None

    def get_profile_text_for_contact(self, contact_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "select profile_text from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
        return str(row["profile_text"] or "").strip() if row else ""

    def get_last_llm_prompts_for_contact(self, contact_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "select last_llm_prompts_json from profile_audits where contact_id = ?",
                (contact_id,),
            ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["last_llm_prompts_json"] or "{}")
        except Exception:
            return {}

    def _recent_messages_for_contact(
        self,
        conn: sqlite3.Connection,
        contact_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        thread = conn.execute(
            """
            select thread_id, platform
            from threads
            where platform_contact_id = ?
            order by updated_at desc
            limit 1
            """,
            (contact_id,),
        ).fetchone()
        if not thread:
            return []
        rows = conn.execute(
            """
            select role, content, created_at, technique, decision_reason
            from thread_messages
            where thread_id = ?
            order by id desc
            limit ?
            """,
            (thread["thread_id"], limit),
        ).fetchall()
        result = []
        for row in reversed(rows):
            result.append(
                {
                    "thread_id": str(thread["thread_id"]),
                    "platform": str(thread["platform"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                    "technique": str(row["technique"] or ""),
                    "decision_reason": str(row["decision_reason"] or ""),
                }
            )
        return result

    def _merge_recent_messages(
        self,
        existing_json: str,
        new_messages: list[dict[str, Any]],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        try:
            existing = json.loads(existing_json) if existing_json else []
        except json.JSONDecodeError:
            existing = []
        if not isinstance(existing, list):
            existing = []
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for item in [*existing, *new_messages]:
            if not isinstance(item, dict):
                continue
            normalized = {
                "thread_id": str(item.get("thread_id", "")),
                "platform": str(item.get("platform", "")),
                "role": str(item.get("role", "")),
                "content": str(item.get("content", "")),
                "created_at": str(item.get("created_at", "")),
                "technique": str(item.get("technique", "")),
                "decision_reason": str(item.get("decision_reason", "")),
            }
            key = (
                normalized["thread_id"],
                normalized["role"],
                normalized["content"],
                normalized["created_at"],
                normalized["platform"],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
        return merged[-limit:]

    def backfill_profile_audit_recent_messages(self, limit: int = 20) -> int:
        with self._connect() as conn:
            rows = conn.execute("select contact_id from profile_audits").fetchall()
            count = 0
            for row in rows:
                contact_id = str(row["contact_id"])
                recent_messages = self._recent_messages_for_contact(conn, contact_id, limit=limit)
                existing = conn.execute(
                    "select recent_messages_json from profile_audits where contact_id = ?",
                    (contact_id,),
                ).fetchone()
                recent_messages_json = json.dumps(
                    self._merge_recent_messages(
                        str(existing["recent_messages_json"] or "[]") if existing else "[]",
                        recent_messages,
                    ),
                    ensure_ascii=False,
                )
                conn.execute(
                    """
                    update profile_audits
                    set recent_messages_json = ?, updated_at = ?
                    where contact_id = ?
                    """,
                    (recent_messages_json, datetime.now().isoformat(), contact_id),
                )
                count += 1
        return count

    def profile_audits(self, status: str = "") -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "select * from profile_audits where status = ? order by updated_at desc",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("select * from profile_audits order by updated_at desc").fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, hash: str, contact_id: str) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into sent_messages (hash, contact_id, created_at) values (?, ?, ?)",
                (hash, contact_id, now),
            )
            # 把对应的 draft 消息记录升级为 sent
            conn.execute(
                "update messages set role='sent' where message_id=? and contact_id=? and role='draft'",
                (hash, contact_id),
            )

    def cache_draft(self, message_hash: str, contact_id: str, incoming: str, draft: str) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into draft_cache(message_hash, contact_id, incoming, draft, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(message_hash) do update set
                    contact_id=excluded.contact_id,
                    incoming=excluded.incoming,
                    draft=excluded.draft,
                    updated_at=excluded.updated_at
                """,
                (message_hash, contact_id, incoming, draft, now, now),
            )

    def get_cached_draft(self, message_hash: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "select draft from draft_cache where message_hash = ?",
                (message_hash,),
            ).fetchone()
        return str(row["draft"]) if row else ""

    def recover_draft_for_message(self, message_hash: str, contact_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                select m2.content
                from messages m1
                join messages m2
                  on m2.conversation_id = m1.conversation_id
                 and m2.id > m1.id
                 and m2.role = 'draft'
                where m1.contact_id = ?
                  and m1.message_id = ?
                  and m1.role = 'user'
                order by m2.id asc
                limit 1
                """,
                (contact_id, message_hash),
            ).fetchone()
        return str(row["content"]) if row else ""

    def load_sent_hashes(self, since_days: int = 7) -> set[str]:
        cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "select hash from sent_messages where created_at >= ?",
                (cutoff,),
            ).fetchall()
        return {row["hash"] for row in rows}

    def contact_overview(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = conn.execute(
                """
                select
                    count(distinct thread_id) as contact_count,
                    count(*) as message_count,
                    sum(case when role in ('draft', 'sent', 'out') then 1 else 0 end) as reply_count
                from thread_messages
                """
            ).fetchone()
            sent = conn.execute("select count(*) as sent_count from thread_messages where role = 'sent'").fetchone()
        return {
            "contact_count": int(counts["contact_count"] or 0),
            "message_count": int(counts["message_count"] or 0),
            "reply_count": int(counts["reply_count"] or 0),
            "sent_count": int(sent["sent_count"] or 0),
        }

    def list_contacts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select
                    t.thread_id as contact_id,
                    t.thread_id,
                    t.platform,
                    t.platform_contact_id,
                    t.display_name,
                    tm.relationship_state,
                    tm.working_summary as profile,
                    tm.updated_at as memory_updated_at,
                    t.updated_at,
                    t.platform as channel,
                    coalesce(stats.message_count, 0) as message_count,
                    coalesce(stats.reply_count, 0) as reply_count,
                    stats.last_message,
                    stats.last_message_at
                from threads t
                left join thread_memory tm on tm.thread_id = t.thread_id
                left join (
                    select
                        m.thread_id,
                        count(*) as message_count,
                        sum(case when m.role in ('draft', 'sent', 'out') then 1 else 0 end) as reply_count,
                        (
                            select content from thread_messages lm
                            where lm.thread_id = m.thread_id
                            order by lm.id desc limit 1
                        ) as last_message,
                        max(m.created_at) as last_message_at
                    from thread_messages m
                    group by m.thread_id
                ) stats on stats.thread_id = t.thread_id
                order by stats.last_message_at desc, t.updated_at desc
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_contact_detail(self, contact_id: str) -> dict[str, Any]:
        thread_id = contact_id
        profile = self.get_thread_profile(thread_id)
        with self._connect() as conn:
            conversations = conn.execute(
                """
                select thread_id as conversation_id, platform as channel, platform_contact_id, created_at, updated_at
                from threads
                where thread_id = ?
                """,
                (thread_id,),
            ).fetchall()
            messages = conn.execute(
                """
                select id, thread_id as conversation_id, thread_id as contact_id, platform as channel,
                       platform_message_id as message_id, role, content, technique, decision_reason, created_at
                from thread_messages
                where thread_id = ?
                order by id asc
                """,
                (thread_id,),
            ).fetchall()
            pending = self.pending_thread_messages(thread_id)
            cached = conn.execute(
                """
                select platform_message_id as message_hash, content as draft, created_at, created_at as updated_at
                from thread_messages
                where thread_id = ? and role = 'draft'
                order by id desc limit 20
                """,
                (thread_id,),
            ).fetchall()
            thread_row = conn.execute(
                "select platform_contact_id from threads where thread_id = ?",
                (thread_id,),
            ).fetchone()
            platform_contact_id = thread_row["platform_contact_id"] if thread_row else thread_id
            audit = conn.execute(
                "select profile_text, recent_messages_json, last_llm_prompts_json from profile_audits where contact_id = ?",
                (platform_contact_id,),
            ).fetchone()
            if not audit:
                audit = conn.execute(
                    "select profile_text, recent_messages_json, last_llm_prompts_json from profile_audits where contact_id = ?",
                    (thread_id,),
                ).fetchone()
        return {
            **profile,
            "conversations": [dict(row) for row in conversations],
            "messages": [dict(row) for row in messages],
            "pending_group": pending,
            "draft_cache": [dict(row) for row in cached],
            "profile_text": audit["profile_text"] if audit else "",
            "recent_messages_json": audit["recent_messages_json"] if audit else "[]",
            "last_llm_prompts": json.loads(audit["last_llm_prompts_json"] or "{}") if audit else {},
        }

    def get_contact_profile(self, contact_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            contact = conn.execute(
                "select * from contacts where contact_id = ?",
                (contact_id,),
            ).fetchone()
            fields = conn.execute(
                """
                select field, value, confidence, source, updated_at
                from contact_profile_fields
                where contact_id = ?
                order by field
                """,
                (contact_id,),
            ).fetchall()
            evidence = conn.execute(
                """
                select field, value, confidence, source, evidence, status, created_at
                from profile_evidence
                where contact_id = ?
                order by id desc limit 50
                """,
                (contact_id,),
            ).fetchall()
        return {
            "contact": dict(contact) if contact else {"contact_id": contact_id},
            "fields": {row["field"]: dict(row) for row in fields},
            "evidence": [dict(row) for row in evidence],
        }
