from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


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
                create table if not exists sent_messages (
                    hash       text primary key,
                    contact_id text not null,
                    created_at text not null
                );
                create index if not exists idx_sent_messages_created on sent_messages(created_at);
                """
            )
            self._ensure_contact_columns(conn)

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
                where conversation_id = ? and role in ('assistant', 'draft') and technique is not null and technique != ''
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
                if not field or not value:
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

    def mark_sent(self, hash: str, contact_id: str) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "insert or ignore into sent_messages (hash, contact_id, created_at) values (?, ?, ?)",
                (hash, contact_id, now),
            )

    def load_sent_hashes(self, since_days: int = 7) -> set[str]:
        cutoff = (datetime.now() - timedelta(days=since_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "select hash from sent_messages where created_at >= ?",
                (cutoff,),
            ).fetchall()
        return {row["hash"] for row in rows}

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
