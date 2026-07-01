from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "social_twin.db"
PROMPT_KEYS = ("profile_text_analysis", "memory_update", "technique_decision", "reply_generation")


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _load_prompts(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def validate(expected: int, since: str = "") -> tuple[dict[str, Any], int]:
    failures: list[dict[str, Any]] = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        since_clause = ""
        params: list[Any] = []
        if since:
            since_clause = "and exists (select 1 from thread_messages sm where sm.thread_id = t.thread_id and sm.role = 'sent' and coalesce(sm.technique, '') != '' and sm.created_at >= ?)"
            params.append(since)
        rows = conn.execute(
            f"""
            select
                t.thread_id,
                t.platform_contact_id as contact_id,
                t.display_name,
                count(case when m.role = 'sent' and coalesce(m.technique, '') != '' then 1 end) as sent_count,
                max(case when m.role = 'sent' and coalesce(m.technique, '') != '' then m.created_at end) as last_sent_at,
                coalesce(pa.profile_text, '') as profile_text,
                coalesce(pa.last_llm_prompts_json, '{{}}') as prompts_json
            from threads t
            join thread_messages m on m.thread_id = t.thread_id
            left join profile_audits pa on pa.contact_id = t.platform_contact_id
            where t.platform = 'tantan'
              {since_clause}
            group by t.thread_id, t.platform_contact_id, t.display_name
            having sent_count > 0
            order by last_sent_at asc
            """,
            params,
        ).fetchall()

        sent_contacts = len(rows)
        for row in rows:
            contact_id = str(row["contact_id"])
            prompts = _load_prompts(str(row["prompts_json"]))
            profile_text = str(row["profile_text"] or "").strip()
            missing = [key for key in PROMPT_KEYS if not str((prompts.get(key) or {}).get("prompt", "")).strip()]
            times = {key: _parse_at((prompts.get(key) or {}).get("first_at") or (prompts.get(key) or {}).get("at")) for key in PROMPT_KEYS}
            reply_prompt = str((prompts.get("reply_generation") or {}).get("prompt", ""))
            rag_ok = (
                "策略案例，学习为什么这么回" in reply_prompt
                and "自然对话案例，学习真人语气和节奏" in reply_prompt
                and "无\n\n自然对话案例" not in reply_prompt
            )
            if not profile_text:
                failures.append({"contact_id": contact_id, "reason": "profile_text_empty"})
            if missing:
                failures.append({"contact_id": contact_id, "reason": "missing_llm_prompts", "missing": missing})
            if times["profile_text_analysis"] and times["memory_update"] and times["profile_text_analysis"] > times["memory_update"]:
                failures.append({"contact_id": contact_id, "reason": "profile_after_memory", "times": {k: str(v) for k, v in times.items()}})
            if not rag_ok:
                failures.append({"contact_id": contact_id, "reason": "rag_prompt_not_verified"})
            if "raw_evidence" in reply_prompt:
                failures.append({"contact_id": contact_id, "reason": "raw_evidence_in_reply_prompt"})
            if "profile上下文：" in reply_prompt or "联系人画像：" in reply_prompt:
                failures.append({"contact_id": contact_id, "reason": "duplicate_profile_sections_in_reply_prompt"})
            if reply_prompt.count("结构化画像：") != 1:
                failures.append({"contact_id": contact_id, "reason": "profile_section_count_invalid", "count": reply_prompt.count("结构化画像：")})
            if reply_prompt.count("about_me") > 1:
                failures.append({"contact_id": contact_id, "reason": "about_me_repeated_in_reply_prompt", "count": reply_prompt.count("about_me")})

        bumble_counts = dict(
            conn.execute(
                """
                select 'threads' key, count(*) value from threads where platform = 'bumble'
                union all
                select 'messages', count(*) from thread_messages where platform = 'bumble'
                union all
                select 'profiles', count(*) from contact_profile_fields where contact_id like 'bumble:%'
                """
            ).fetchall()
        )

    if sent_contacts < expected:
        failures.append({"reason": "sent_contacts_below_expected", "actual": sent_contacts, "expected": expected})

    report = {
        "expected_sent_contacts": expected,
        "since": since,
        "actual_sent_contacts": sent_contacts,
        "checked_contacts": [str(row["contact_id"]) for row in rows],
        "failures": failures,
        "bumble_counts": bumble_counts,
        "ok": not failures,
    }
    return report, 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=int, default=25)
    parser.add_argument("--since", default="")
    args = parser.parse_args()
    report, code = validate(args.expected, since=args.since)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
