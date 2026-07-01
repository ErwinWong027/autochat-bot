import os
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from social_twin.config import load_settings
from social_twin.bumble import (
    BumbleConnector,
    BumbleMessage,
    CONTACT_RECHECK_COOLDOWN_SECONDS,
    STAGE_CODES,
    bumble_contact_id,
    bumble_message_hash,
    extract_bumble_messages_from_html,
    extract_bumble_contacts_from_html,
    extract_bumble_profile_text,
    extract_bumble_profile_updates,
    extract_latest_incoming_text,
    extract_pending_incoming_group,
    is_turn_label,
    preview_matches_messages,
    profile_category,
)
from social_twin.android_base import AndroidBaseConnector, AndroidMessage, android_message_hash
from social_twin.android_apps import tantan as tantan_module
from social_twin.android_apps.tantan import TantanConnector
from social_twin.connectors import BrowserAgentConfig, BrowserAgentConnector, ManualConnector
from social_twin.knowledge import load_persona_dialogues, load_strategy_knowledge
from social_twin.memory import MemoryStore
from social_twin.profile import ProfileAnalyzer
from social_twin.service import DigitalTwinService, DraftRequest, ReplyGroupRequest
from social_twin.style import inspect_style, normalize_draft


class KnowledgeTests(unittest.TestCase):
    def test_strategy_coverage(self):
        _, samples, report = load_strategy_knowledge("all_chapters.json")
        self.assertEqual(report.chapters, 9)
        self.assertEqual(report.techniques, 9)
        self.assertEqual(report.total_a_replies, 178)
        self.assertEqual(report.annotated_strategy, 53)
        self.assertEqual(report.natural_dialogue, 125)
        self.assertEqual(len(samples), 178)
        self.assertEqual(report.indexed_samples, 178)
        self.assertIn("询问", report.technique_names)

    def test_every_technique_has_layered_samples(self):
        _, samples, report = load_strategy_knowledge("all_chapters.json")
        for technique in report.technique_names:
            annotated = [item for item in samples if item.technique == technique and item.sample_type == "annotated_strategy"]
            natural = [item for item in samples if item.technique == technique and item.sample_type == "natural_dialogue"]
            self.assertGreater(len(annotated), 0, technique)
            self.assertGreater(len(natural), 0, technique)

    def test_persona_dir_is_optional(self):
        samples = load_persona_dialogues("missing_persona_dir_for_test")
        self.assertEqual(samples, [])


class MemoryTests(unittest.TestCase):
    def test_contacts_do_not_share_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            c1 = store.get_or_create_conversation("alice", "manual")
            c2 = store.get_or_create_conversation("bob", "manual")
            store.add_message(c1, "alice", "manual", "user", "A消息")
            store.add_message(c2, "bob", "manual", "user", "B消息")
            store.add_message(c1, "alice", "manual", "draft", "草稿", technique="延词")
            self.assertNotEqual(c1, c2)
            self.assertEqual(store.recent_messages(c1)[0]["content"], "A消息")
            self.assertEqual(store.recent_messages(c2)[0]["content"], "B消息")
            self.assertEqual(store.recent_techniques(c1), ["延词"])

    def test_profile_updates_and_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            added = store.apply_profile_updates(
                "alice",
                [{"field": "hometown", "value": "杭州", "confidence": 0.7, "source": "message"}],
            )
            conflict = store.apply_profile_updates(
                "alice",
                [{"field": "hometown", "value": "上海", "confidence": 0.6, "source": "message"}],
            )
            profile = store.get_contact_profile("alice")
            self.assertEqual(added[0]["status"], "added")
            self.assertEqual(conflict[0]["status"], "conflict")
            self.assertEqual(profile["fields"]["hometown"]["value"], "杭州")

    def test_profile_updates_reject_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            updates = store.apply_profile_updates(
                "tantan:a",
                [
                    {"field": "job", "value": "品牌策划", "confidence": 0.8, "source": "message"},
                    {"field": "random_new_field", "value": "不该入库", "confidence": 0.9, "source": "message"},
                ],
            )
            profile = store.get_contact_profile("tantan:a")
            self.assertEqual([item["field"] for item in updates], ["job"])
            self.assertIn("job", profile["fields"])
            self.assertNotIn("random_new_field", profile["fields"])
            with self.assertRaises(Exception):
                with store._connect() as conn:
                    conn.execute(
                        """
                        insert into contact_profile_fields(contact_id, field, value, confidence, source, updated_at)
                        values ('tantan:a', 'random_new_field', '不该入库', 0.9, 'direct_sql', '2026-01-01')
                        """
                    )

    def test_thread_profile_updates_merge_into_contact_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("tantan", "tantan:a", "A")

            updates = store.apply_thread_profile_updates(
                thread_id,
                [
                    {"field": "location", "value": "上海", "confidence": 0.8, "source": "profile_text"},
                    {"field": "made_up", "value": "不该入库", "confidence": 0.9, "source": "profile_text"},
                ],
            )

            contact_profile = store.get_contact_profile("tantan:a")
            thread_profile = store.get_thread_profile(thread_id)
            with store._connect() as conn:
                thread_field_count = conn.execute("select count(*) from thread_profile_fields").fetchone()[0]
            self.assertEqual([item["field"] for item in updates], ["location"])
            self.assertEqual(contact_profile["fields"]["location"]["value"], "上海")
            self.assertEqual(thread_profile["fields"]["location"]["value"], "上海")
            self.assertEqual(thread_field_count, 0)

    def test_legacy_thread_profile_rows_migrate_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("tantan", "tantan:a", "A")
            with store._connect() as conn:
                conn.execute(
                    """
                    insert into thread_profile_fields(thread_id, field, value, confidence, source, updated_at)
                    values (?, 'interests_hobbies', '咖啡', 0.7, 'legacy', '2026-01-01')
                    """,
                    (thread_id,),
                )
                conn.execute(
                    """
                    insert into thread_profile_fields(thread_id, field, value, confidence, source, updated_at)
                    values (?, 'loose_field', '不该迁移', 0.9, 'legacy', '2026-01-01')
                    """,
                    (thread_id,),
                )

            store = MemoryStore(db_path)
            profile = store.get_contact_profile("tantan:a")
            with store._connect() as conn:
                thread_field_count = conn.execute("select count(*) from thread_profile_fields").fetchone()[0]

            self.assertEqual(profile["fields"]["interests_hobbies"]["value"], "咖啡")
            self.assertNotIn("loose_field", profile["fields"])
            self.assertEqual(thread_field_count, 0)

    def test_profile_low_coverage_writes_audit_row_with_full_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            store.apply_profile_updates(
                "tantan:a",
                [{"field": "name", "value": "A", "confidence": 0.9, "source": "profile_text"}],
            )

            audit = store.assess_profile_coverage("tantan:a", "A", "A\n主页写了很多但没抽出来")
            rows = store.profile_audits("open")

            self.assertEqual(audit["status"], "open")
            self.assertEqual(rows[0]["contact_id"], "tantan:a")
            self.assertEqual(rows[0]["display_name"], "A")
            self.assertIn("主页写了很多", rows[0]["profile_text"])
            self.assertIn("field_count_low", rows[0]["reasons_json"])

    def test_profile_audit_includes_recent_thread_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("tantan", "tantan:a", "A")
            store.sync_thread_messages(
                thread_id,
                "tantan",
                [
                    {"platform_message_id": "in-1", "role": "user", "content": "你好", "order_index": 0},
                    {"platform_message_id": "out-1", "role": "sent", "content": "hi", "order_index": 1},
                ],
            )
            store.apply_profile_updates(
                "tantan:a",
                [{"field": "name", "value": "A", "confidence": 0.9, "source": "profile_text"}],
            )

            store.assess_profile_coverage("tantan:a", "A", "profile raw")
            rows = store.profile_audits("open")
            recent = json.loads(rows[0]["recent_messages_json"])

            self.assertEqual([item["content"] for item in recent], ["你好", "hi"])
            self.assertEqual(recent[0]["thread_id"], thread_id)
            self.assertEqual(recent[0]["platform"], "tantan")

    def test_profile_audit_mainline_is_not_overwritten_by_later_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("tantan", "tantan:a", "A")
            store.sync_thread_messages(
                thread_id,
                "tantan",
                [{"platform_message_id": "in-1", "role": "user", "content": "第一条", "order_index": 0}],
            )
            store.apply_profile_updates(
                "tantan:a",
                [{"field": "name", "value": "A", "confidence": 0.9, "source": "profile_text"}],
            )

            store.assess_profile_coverage("tantan:a", "A", "初始 profile read")
            store.sync_thread_messages(
                thread_id,
                "tantan",
                [{"platform_message_id": "in-2", "role": "user", "content": "后续消息", "order_index": 1}],
            )
            store.apply_profile_updates(
                "tantan:a",
                [
                    {"field": "raw_evidence", "value": "后续消息不该倒修主线", "confidence": 0.9, "source": "message"}
                ],
            )
            store.assess_profile_coverage("tantan:a", "A2", "后续 profile text")

            rows = store.profile_audits()
            recent = json.loads(rows[0]["recent_messages_json"])

            self.assertEqual(rows[0]["display_name"], "A")
            self.assertEqual(rows[0]["profile_text"], "初始 profile read")
            self.assertEqual(rows[0]["status"], "open")
            self.assertIn("field_count_low", rows[0]["reasons_json"])
            self.assertEqual([item["content"] for item in recent], ["第一条", "后续消息"])

    def test_initial_profile_text_capture_is_completed_by_first_analysis_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)

            self.assertFalse(store.has_profile_audit_text("bumble:a"))
            store.record_initial_profile_text("bumble:a", "A", "DOM 原始 profile 全文")
            self.assertTrue(store.has_profile_audit_text("bumble:a"))
            store.apply_profile_updates(
                "bumble:a",
                [{"field": "name", "value": "A", "confidence": 0.9, "source": "bumble_profile"}],
            )
            store.assess_profile_coverage("bumble:a", "A", "分析时传入的同一段文本")
            first = store.profile_audits()[0]

            store.apply_profile_updates(
                "bumble:a",
                [
                    {
                        "field": "raw_evidence",
                        "value": "后续消息不能覆盖原文",
                        "confidence": 0.9,
                        "source": "message",
                    }
                ],
            )
            store.assess_profile_coverage("bumble:a", "A2", "后续 profile text")
            latest = store.profile_audits()[0]

            self.assertEqual(first["profile_text"], "DOM 原始 profile 全文")
            self.assertEqual(first["status"], "open")
            self.assertEqual(latest["display_name"], "A")
            self.assertEqual(latest["profile_text"], "DOM 原始 profile 全文")
            self.assertEqual(latest["status"], "open")
            self.assertIn("field_count_low", latest["reasons_json"])

    def test_existing_low_coverage_profiles_are_seeded_into_audit_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            store.upsert_contact("bumble:a", display_name="A")
            store.apply_profile_updates(
                "bumble:a",
                [{"field": "name", "value": "A", "confidence": 0.9, "source": "legacy"}],
            )

            store = MemoryStore(db_path)
            rows = store.profile_audits("open")

            self.assertEqual(rows[0]["contact_id"], "bumble:a")
            self.assertEqual(rows[0]["display_name"], "A")
            self.assertEqual(rows[0]["profile_text"], "")
            self.assertIn("legacy_profile_text_missing", rows[0]["reasons_json"])

    def test_draft_cache_and_recover_from_message_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            store.cache_draft("hash-1", "bumble:a", "你好", "回你好")
            self.assertEqual(store.get_cached_draft("hash-1"), "回你好")

            conversation_id = store.get_or_create_conversation("bumble:a", "bumble")
            store.add_message(conversation_id, "bumble:a", "bumble", "user", "旧消息", message_id="hash-2")
            store.add_message(conversation_id, "bumble:a", "bumble", "draft", "旧草稿", message_id="hash-2")
            self.assertEqual(store.recover_draft_for_message("hash-2", "bumble:a"), "旧草稿")

    def test_thread_memory_isolated_and_duplicate_text_keeps_distinct_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_a = store.get_or_create_thread("bumble", "bumble:a", "A")
            thread_a_again = store.get_or_create_thread("bumble", "bumble:a", "A2")
            thread_b = store.get_or_create_thread("bumble", "bumble:b", "B")
            self.assertEqual(thread_a, thread_a_again)
            self.assertNotEqual(thread_a, thread_b)

            inserted = store.sync_thread_messages(
                thread_a,
                "bumble",
                [
                    {"platform_message_id": "in:0:hh", "role": "user", "content": "hh", "order_index": 0},
                    {"platform_message_id": "in:1:hh", "role": "user", "content": "hh", "order_index": 1},
                    {"platform_message_id": "in:1:hh", "role": "user", "content": "hh", "order_index": 1},
                ],
            )
            self.assertEqual(len(inserted), 2)
            self.assertEqual(len(store.all_thread_messages(thread_a)), 2)
            self.assertEqual(store.all_thread_messages(thread_b), [])

            store.update_thread_memory(thread_a, {"working_summary": "A只属于A", "pinned_facts": {"city": "深圳"}})
            self.assertIn("A只属于A", store.get_thread_memory(thread_a)["working_summary"])
            self.assertEqual(store.get_thread_memory(thread_b)["working_summary"], "")

    def test_pending_group_survives_partial_draft_and_partial_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("bumble", "bumble:hedy", "Hedy")
            store.sync_thread_messages(
                thread_id,
                "bumble",
                [
                    {"platform_message_id": "out-1", "role": "sent", "content": "你看球吗", "order_index": 0},
                    {"platform_message_id": "in-1", "role": "user", "content": "世界杯", "order_index": 1},
                    {"platform_message_id": "in-2", "role": "user", "content": "你看嘛", "order_index": 2},
                ],
            )
            store.add_thread_message(thread_id, "bumble", "in-1", "draft", "世界杯赛程确实挺折磨")
            pending = store.pending_thread_messages(thread_id)
            self.assertEqual([row["content"] for row in pending], ["世界杯", "你看嘛"])

            store.mark_thread_sent(thread_id, "in-1")
            pending = store.pending_thread_messages(thread_id)
            self.assertEqual([row["content"] for row in pending], ["你看嘛"])

    def test_incremental_thread_sync_skips_existing_history_and_adds_only_new_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("bumble", "bumble:jin", "Jin")
            first = [
                {"role": "user", "content": "Eminem and Chopin?", "order_index": 0},
                {"role": "sent", "content": "复杂也是一种美", "order_index": 1},
                {"role": "user", "content": "那当然", "order_index": 2},
            ]
            inserted = store.sync_thread_messages_incremental(thread_id, "bumble", first)
            self.assertEqual(len(inserted), 3)

            shifted_same_history = [
                {"role": "user", "content": "Eminem and Chopin?", "order_index": 10},
                {"role": "sent", "content": "复杂也是一种美", "order_index": 11},
                {"role": "user", "content": "那当然", "order_index": 12},
            ]
            inserted = store.sync_thread_messages_incremental(thread_id, "bumble", shifted_same_history)
            self.assertEqual(inserted, [])
            self.assertEqual(len(store.all_thread_messages(thread_id)), 3)

            with_new_tail = shifted_same_history + [
                {"role": "user", "content": "嗯？", "order_index": 13},
            ]
            inserted = store.sync_thread_messages_incremental(thread_id, "bumble", with_new_tail)
            self.assertEqual([row["content"] for row in inserted], ["嗯？"])
            self.assertEqual(len(store.all_thread_messages(thread_id)), 4)

    def test_incremental_thread_sync_handles_partial_tail_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            thread_id = store.get_or_create_thread("tantan", "tantan:a", "A")
            store.sync_thread_messages_incremental(
                thread_id,
                "tantan",
                [
                    {"role": "user", "content": "第一条", "order_index": 0},
                    {"role": "sent", "content": "第一回", "order_index": 1},
                    {"role": "user", "content": "第二条", "order_index": 2},
                ],
            )
            inserted = store.sync_thread_messages_incremental(
                thread_id,
                "tantan",
                [
                    {"role": "user", "content": "第二条", "order_index": 0},
                    {"role": "user", "content": "第三条", "order_index": 1},
                ],
            )
            self.assertEqual([row["content"] for row in inserted], ["第三条"])
            self.assertEqual([row["content"] for row in store.pending_thread_messages(thread_id)], ["第二条", "第三条"])


class StyleTests(unittest.TestCase):
    def test_style_rejects_ai_long_reply(self):
        result = inspect_style("我理解你的感受，听起来你今天真的承受了很多压力。")
        self.assertTrue(result.needs_rewrite)
        self.assertIn("ai_tone", result.issues)
        self.assertIn("too_long", result.issues)

    def test_normalize_short_draft(self):
        self.assertEqual(normalize_draft("先缓一口气。"), "先缓一口气")

    def test_30_round_style_regression(self):
        replies = [
            "先缓一口气",
            "嗯哼",
            "这就对了",
            "有点意思",
            "我记住了",
            "你还挺会",
        ] * 5
        issues = [inspect_style(reply).issues for reply in replies]
        self.assertTrue(all(not item for item in issues))


class ConfigTests(unittest.TestCase):
    def test_default_models_are_not_max(self):
        settings = load_settings()
        self.assertEqual(settings.decision_model, os.getenv("DECISION_MODEL", "qwen3.7-plus"))
        self.assertEqual(settings.reply_model, os.getenv("REPLY_MODEL", "qwen3.7-plus"))
        self.assertNotIn("max", settings.decision_model.lower())
        self.assertNotIn("max", settings.reply_model.lower())


class ServiceContextTests(unittest.TestCase):
    def test_format_context_keeps_last_20_messages(self):
        service = DigitalTwinService()
        messages = [
            {"role": "user" if index % 2 else "sent", "content": f"消息{index:02d}"}
            for index in range(1, 22)
        ]

        context = service._format_context(messages)

        self.assertNotIn("消息01", context)
        self.assertIn("消息02", context)
        self.assertIn("消息21", context)
        self.assertEqual(context.count(" | "), 19)

    def test_analyze_profile_stores_raw_evidence_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            settings = replace(load_settings(), sqlite_path=db_path, dashscope_api_key="")
            service = DigitalTwinService(settings)

            result = service.analyze_profile("tantan:a", text="主页只有一句很长的自我介绍")

            profile = service.memory.get_contact_profile("tantan:a")
            audits = service.memory.profile_audits("open")
            self.assertIn("raw_evidence", profile["fields"])
            self.assertIn("主页只有一句很长", profile["fields"]["raw_evidence"]["value"])
            self.assertEqual(result["profile_audit"]["status"], "open")
            self.assertEqual(audits[0]["contact_id"], "tantan:a")

    def test_reply_group_prompt_uses_compact_profile_once(self):
        class FakeLLM:
            def __init__(self):
                self.prompt = ""

            def chat(self, model, messages, temperature=0.0, max_tokens=0):
                self.prompt = messages[0]["content"]
                return '["滑雪徒步都不错"]'

        service = DigitalTwinService(load_settings())
        fake_llm = FakeLLM()
        service.llm = fake_llm
        long_about = "深圳互联网大厂做MKT，爱玩游戏。" * 8
        raw_evidence = "Molly- 身份证 4211 原始整页文本 巴厘岛 悉尼 " * 8

        replies = service._generate_reply_group(
            context="B: 滑雪然后偶尔去徒步",
            scenario="推进关系",
            relationship_state="持续互动",
            technique="延词",
            reason="测试",
            strategy_cases=[],
            natural_cases=[],
            persona_cases=[],
            request=ReplyGroupRequest(
                thread_id="thread-molly",
                contact_id="tantan:Molly-",
                platform="tantan",
                pending_messages=["滑雪然后偶尔去徒步"],
                message_ids=["msg-1"],
                contact_profile=f"about_me:{long_about};raw_evidence:{raw_evidence}",
                profile_context=f"about_me:{long_about};raw_evidence:{raw_evidence}",
            ),
            recent=[],
            structured_profile={
                "fields": {
                    "about_me": {"value": long_about, "confidence": 1.0},
                    "interests_hobbies": {"value": "滑雪、徒步、密室", "confidence": 1.0},
                    "raw_evidence": {"value": raw_evidence, "confidence": 1.0},
                }
            },
        )

        self.assertEqual(replies, ["滑雪徒步都不错"])
        self.assertNotIn("联系人画像：", fake_llm.prompt)
        self.assertNotIn("profile上下文：", fake_llm.prompt)
        self.assertNotIn("raw_evidence", fake_llm.prompt)
        self.assertNotIn("身份证 4211", fake_llm.prompt)
        self.assertEqual(fake_llm.prompt.count("about_me"), 1)


class ProfileTests(unittest.TestCase):
    def test_rule_profile_extraction(self):
        analyzer = ProfileAnalyzer(load_settings(), None)
        updates = analyzer.extract_rules("我在杭州做金融，身高168cm，水瓶座，喜欢音乐和旅行", "message")
        fields = {item["field"]: item["value"] for item in updates}
        self.assertEqual(fields["height"], "168")
        self.assertIn("水瓶", fields["zodiac"])
        self.assertIn("音乐", fields["interests_hobbies"])
        self.assertIn("旅行", fields["interests_hobbies"])

    def test_rule_profile_extraction_keeps_rich_personality_traits(self):
        analyzer = ProfileAnalyzer(load_settings(), None)
        updates = analyzer.extract_rules(
            "INFJ-A 恋爱脑但极其看重精神的brat，表面冷漠其实焦虑型回避依恋，高敏感，真诚至上，慕强的悲观务实家，也看星盘和算命",
            "bumble_profile",
        )
        fields = {item["field"]: item["value"] for item in updates}
        self.assertIn("INFJ-A", fields["personality_traits"])
        self.assertIn("brat", fields["personality_traits"])
        self.assertIn("焦虑型回避依恋", fields["personality_traits"])
        self.assertIn("高敏感", fields["personality_traits"])
        self.assertIn("慕强", fields["personality_traits"])
        self.assertIn("星盘", fields["interests_hobbies"])


class ReplyFitTests(unittest.TestCase):
    def test_non_question_incoming_cannot_answer_future_you_ne(self):
        service = DigitalTwinService(load_settings())
        self.assertTrue(service._is_premature_self_answer("大多时间宅家，有时跟朋友聚聚户外一下", "我周末基本在看盘和户外间切换"))
        self.assertFalse(service._is_premature_self_answer("你呢", "我周末基本在看盘和户外间切换"))

    def test_duplicate_recent_reply_is_detected(self):
        service = DigitalTwinService(load_settings())
        self.assertTrue(service._is_duplicate_reply("我周末基本在看盘和户外间切换", ["我周末基本在看盘和户外间切换"]))
        self.assertFalse(service._is_duplicate_reply("宅家户外切换挺舒服", ["我周末基本在看盘和户外间切换"]))


class ConnectorTests(unittest.TestCase):
    def test_manual_connector_requires_confirmation(self):
        class FakeService:
            def create_draft(self, request):
                return {"draft": "先缓一口气", "contact_id": request.contact_id}

        connector = ManualConnector(FakeService())
        result = connector.draft(DraftRequest(contact_id="alice", message="累"))
        self.assertEqual(result.draft, "先缓一口气")
        self.assertTrue(result.requires_human_confirmation)

    def test_browser_agent_missing_playwright_or_selector_returns_status(self):
        class FakeService:
            settings = type("Settings", (), {"auto_send_enabled": False, "browser_agent_target_url": ""})()

        connector = BrowserAgentConnector(FakeService())
        status = connector.status()
        self.assertFalse(status["running"])
        self.assertEqual(BrowserAgentConfig().poll_seconds, 5)


class BumbleTests(unittest.TestCase):
    def test_bumble_contact_parser_reads_turn_contacts(self):
        html = """
        <div>
          <div class="contact" data-qa-role="contact" data-qa-uid="uid-1" data-qa-name="Chan">
            <div class="contact__notifications"><div class="contact__move-label"><span>轮到您了</span></div></div>
            <div class="contact__message"><span>下周还会不会下雨hhh</span></div>
            <img class="avatar__image" src="x">
          </div>
          <div class="contact" data-qa-role="contact" data-qa-uid="uid-2" data-qa-name="Yim">
            <div class="contact__notifications"></div>
            <div class="contact__message"><span>Hey what's up</span></div>
          </div>
        </div>
        """
        contacts = extract_bumble_contacts_from_html(html)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].uid, "uid-1")
        self.assertEqual(contacts[0].contact_id, "bumble:uid-1")
        self.assertEqual(contacts[0].name, "Chan")
        self.assertEqual(contacts[0].preview, "下周还会不会下雨hhh")

    def test_bumble_latest_incoming_ignores_last_outgoing(self):
        html = """
        <div class="messages-list__conversation">
          <div class="message message--in"><div class="message-bubble__text"><span>坂田那个到期啦？</span></div></div>
          <div class="message message--out"><div class="message-bubble__text"><span>对</span></div></div>
          <div class="message message--last message--in"><div class="message-bubble__text"><span>下周还会不会下雨hhh</span></div></div>
          <div class="message message--last message--out"><div class="message-bubble__text"><span>我看看</span></div></div>
        </div>
        """
        self.assertEqual(extract_latest_incoming_text(html), "下周还会不会下雨hhh")
        messages = extract_bumble_messages_from_html(html)
        self.assertEqual([item.role for item in messages], ["in", "out", "in", "out"])

    def test_bumble_pending_group_after_last_out(self):
        messages = [
            BumbleMessage("in", "旧消息", 0),
            BumbleMessage("out", "旧回复", 1),
            BumbleMessage("in", "第一条", 2),
            BumbleMessage("in", "第二条", 3),
            BumbleMessage("in", "第三条", 4),
        ]
        pending = extract_pending_incoming_group(messages)
        self.assertEqual([item.text for item in pending], ["第一条", "第二条", "第三条"])

    def test_bumble_pending_group_empty_when_last_message_is_out(self):
        messages = [
            BumbleMessage("in", "第一条", 0),
            BumbleMessage("out", "回复", 1),
        ]
        self.assertEqual(extract_pending_incoming_group(messages), [])

    def test_bumble_pending_group_uses_all_incoming_without_outgoing(self):
        messages = [
            BumbleMessage("in", "第一条", 0),
            BumbleMessage("in", "第二条", 1),
        ]
        pending = extract_pending_incoming_group(messages)
        self.assertEqual([item.text for item in pending], ["第一条", "第二条"])

    def test_bumble_preview_must_match_visible_messages(self):
        messages = [
            BumbleMessage("in", "旧消息", 0),
            BumbleMessage("out", "旧回复", 1),
            BumbleMessage("in", "你呢", 2),
        ]
        self.assertTrue(preview_matches_messages("你呢", messages))
        self.assertFalse(preview_matches_messages("学得好烂哈哈哈，最近在试课，想换教练", messages))

    def test_bumble_existing_thread_skips_full_history_load(self):
        class FakeMemory:
            def __init__(self, rows):
                self.rows = rows

            def all_thread_messages(self, thread_id):
                return self.rows

            def pending_thread_messages(self, thread_id):
                return []

        class FakeService:
            def __init__(self, rows):
                self.memory = FakeMemory(rows)

        connector = BumbleConnector(FakeService([{"role": "user", "content": "旧消息"}]))
        calls = {"full": 0}
        connector._assert_selected_contact = lambda *args, **kwargs: True
        connector._load_full_conversation_history = lambda page: calls.__setitem__("full", calls["full"] + 1)
        connector._stable_visible_messages = lambda page: [BumbleMessage("in", "新消息", 0)]
        connector._sync_visible_messages = lambda *args, **kwargs: []
        connector._pending_incoming_group(object(), "thread-old", "bumble:old")
        self.assertEqual(calls["full"], 0)

        fresh = BumbleConnector(FakeService([]))
        fresh._assert_selected_contact = lambda *args, **kwargs: True
        fresh._load_full_conversation_history = lambda page: calls.__setitem__("full", calls["full"] + 1)
        fresh._stable_visible_messages = lambda page: []
        fresh._sync_visible_messages = lambda *args, **kwargs: []
        fresh._pending_incoming_group(object(), "thread-new", "bumble:new")
        self.assertEqual(calls["full"], 1)

    def test_tantan_existing_thread_skips_full_history_load(self):
        class FakeMemory:
            def __init__(self, rows):
                self.rows = rows

            def all_thread_messages(self, thread_id):
                return self.rows

        class FakeService:
            def __init__(self, rows):
                self.memory = FakeMemory(rows)

        class FakeElement:
            def exists(self, timeout=0):
                return True

            class Fling:
                def toEnd(self, max_swipes=0):
                    return None

            fling = Fling()

        class FakeDevice:
            def __call__(self, **kwargs):
                return FakeElement()

        connector = TantanConnector(FakeService([{"role": "user", "content": "旧消息"}]))
        calls = {"full": 0, "screen": 0}
        connector._read_conversation_full = lambda device: calls.__setitem__("full", calls["full"] + 1) or []
        connector._parse_screen_messages = lambda device: calls.__setitem__("screen", calls["screen"] + 1) or []
        connector._read_conversation(FakeDevice(), "tantan:old", "thread-old")
        self.assertEqual(calls["full"], 0)
        self.assertEqual(calls["screen"], 1)

        fresh = TantanConnector(FakeService([]))
        fresh._read_conversation_full = lambda device: calls.__setitem__("full", calls["full"] + 1) or []
        fresh._parse_screen_messages = lambda device: calls.__setitem__("screen", calls["screen"] + 1) or []
        fresh._read_conversation(FakeDevice(), "tantan:new", "thread-new")
        self.assertEqual(calls["full"], 1)
        self.assertEqual(calls["screen"], 1)

    def test_bumble_reply_group_combines_pending_into_one_draft(self):
        class FakeMemory:
            def __init__(self):
                self.cached = {}

            def load_sent_hashes(self, since_days=7):
                return set()

            def get_contact_profile(self, contact_id):
                return {"fields": {}}

            def get_cached_draft(self, message_hash):
                return ""

            def recover_draft_for_message(self, message_hash, contact_id):
                return ""

            def cache_draft(self, message_hash, contact_id, incoming, draft):
                self.cached[message_hash] = (contact_id, incoming, draft)

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()
                self.calls = []

            def create_reply_group(self, request, progress_callback=None):
                self.calls.append(request)
                return {
                    "reply_group": [
                        {"incoming": incoming, "draft": f"合并回复{index + 1}", "message_id": request.message_ids[index]}
                        for index, incoming in enumerate(request.pending_messages)
                    ]
                }

        service = FakeService()
        connector = BumbleConnector(service)
        pending = [
            BumbleMessage("in", "第一条", 0),
            BumbleMessage("in", "第二条", 1),
            BumbleMessage("in", "第三条", 2),
        ]
        reply_group = connector._create_reply_group("thread-test", "bumble:test", pending)
        self.assertEqual(len(reply_group), 3)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0].pending_messages, ["第一条", "第二条", "第三条"])
        self.assertEqual([item["incoming"] for item in reply_group], ["第一条", "第二条", "第三条"])
        self.assertEqual([item["draft"] for item in reply_group], ["合并回复1", "合并回复2", "合并回复3"])

    def test_bumble_profile_parser_extracts_categorized_fields(self):
        html = """
        <div class="profile__entry">
          <img class="profile__photo" src="//eu1.ecdn2.bumbcdn.com/p51/hidden?x=1" alt="">
          <div class="profile__name" title="Chan"><div>Chan</div></div>
          <div class="profile__age"><div><span class="comma">,</span> 28</div></div>
          <div class="profile__about"><div>友好交流 舞蹈以前很爱 目前待业聊的来的可以约饭哦</div></div>
          <ul class="profile__badges">
            <li><div class="pill"><div class="pill__title"><div>165 cm</div></div></div></li>
            <li><div class="pill"><div class="pill__title"><div>Taurus</div></div></div></li>
            <li><div class="pill"><div class="pill__title"><div>Undergraduate degree</div></div></div></li>
            <li><div class="pill"><div class="pill__title"><div>Something casual</div></div></div></li>
          </ul>
          <div class="profile-answer__title"><h3>The quickest way to my heart is</h3></div>
          <div class="profile-answer__text"><p>有领导力 懂得尊重</p></div>
          <section class="location-widget">
            <div class="location-widget__town"><span>Shenzhen</span></div>
            <div class="location-widget__pill"><div class="pill__title"><div>🇨🇳 From Shenzhen, Guangdong</div></div></div>
          </section>
        </div>
        """
        updates = extract_bumble_profile_updates(html)
        profile_text = extract_bumble_profile_text(html)
        fields = {item["field"]: item["value"] for item in updates}
        self.assertEqual(fields["name"], "Chan")
        self.assertEqual(fields["age"], "28")
        self.assertEqual(fields["height"], "165 cm")
        self.assertEqual(fields["zodiac"], "Taurus")
        self.assertEqual(fields["education"], "Undergraduate degree")
        self.assertIn("Shenzhen", fields["location"])
        self.assertIn("The quickest way to my heart is", fields["profile_prompts"])
        self.assertIn("Something casual", fields["raw_evidence"])
        self.assertIn("https://eu1.ecdn2.bumbcdn.com", fields["raw_evidence"])
        self.assertIn("友好交流", profile_text)
        self.assertIn("友好交流", fields["raw_evidence"])
        self.assertEqual(profile_category("about_me"), "关于我")
        self.assertEqual(profile_category("raw_evidence"), "原始证据")

    def test_bumble_captures_raw_profile_text_before_profile_field_analysis(self):
        class FakeMemory:
            def __init__(self):
                self.calls = []

            def load_sent_hashes(self, since_days=7):
                return set()

            def get_contact_profile(self, contact_id):
                return {"fields": {}}

            def has_profile_audit_text(self, contact_id):
                return False

            def apply_profile_updates(self, contact_id, updates):
                source = updates[0]["source"] if updates else ""
                self.calls.append(("apply", source))
                return updates

            def record_initial_profile_text(self, contact_id, display_name, profile_text):
                self.calls.append(("capture", profile_text))

            def assess_profile_coverage(self, contact_id, display_name="", profile_text="", min_fields=3):
                self.calls.append(("assess", profile_text))
                return {"status": "open"}

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()

        html = """
        <div class="profile__entry">
          <div class="profile__name" title="Chan"><div>Chan</div></div>
          <div class="profile__about"><div>DOM 原始 profile 全文</div></div>
        </div>
        """
        service = FakeService()
        connector = BumbleConnector(service)
        connector._profile_html = lambda page: html

        connector._ensure_profile(object(), "thread-1", "bumble:uid-1", "Chan", refresh=True)

        calls = service.memory.calls
        capture_index = next(index for index, call in enumerate(calls) if call[0] == "capture")
        profile_apply_index = next(index for index, call in enumerate(calls) if call == ("apply", "bumble_profile"))
        assess_index = next(index for index, call in enumerate(calls) if call[0] == "assess")
        self.assertLess(capture_index, profile_apply_index)
        self.assertLess(capture_index, assess_index)
        self.assertIn("DOM 原始 profile 全文", calls[capture_index][1])

    def test_bumble_name_only_profile_does_not_skip_dom_profile_read(self):
        class FakeMemory:
            def __init__(self, has_audit_text=False):
                self.calls = []
                self.has_audit_text = has_audit_text

            def load_sent_hashes(self, since_days=7):
                return set()

            def get_contact_profile(self, contact_id):
                return {
                    "fields": {
                        "name": {
                            "field": "name",
                            "value": "Autumn",
                            "confidence": 0.95,
                            "source": "bumble_contact",
                        }
                    }
                }

            def has_profile_audit_text(self, contact_id):
                return self.has_audit_text

            def apply_profile_updates(self, contact_id, updates):
                source = updates[0]["source"] if updates else ""
                self.calls.append(("apply", source))
                return updates

            def record_initial_profile_text(self, contact_id, display_name, profile_text):
                self.calls.append(("capture", profile_text))

            def assess_profile_coverage(self, contact_id, display_name="", profile_text="", min_fields=3):
                self.calls.append(("assess", profile_text))
                return {"status": "open"}

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()

        html = """
        <div class="profile__entry">
          <div class="profile__name" title="Autumn"><div>Autumn</div></div>
          <div class="profile__about"><div>喜欢网球和户外</div></div>
        </div>
        """
        service = FakeService()
        connector = BumbleConnector(service)
        calls = {"profile_html": 0}

        def fake_profile_html(page):
            calls["profile_html"] += 1
            return html

        connector._profile_html = fake_profile_html

        connector._ensure_profile(object(), "thread-1", "bumble:uid-1", "Autumn", refresh=False)

        self.assertEqual(calls["profile_html"], 1)
        capture = next(call[1] for call in service.memory.calls if call[0] == "capture")
        self.assertIn("Autumn", capture)
        self.assertIn("喜欢网球和户外", capture)

    def test_bumble_profile_skips_when_audit_text_exists(self):
        class FakeMemory:
            def load_sent_hashes(self, since_days=7):
                return set()

            def get_contact_profile(self, contact_id):
                return {"fields": {}}

            def has_profile_audit_text(self, contact_id):
                return True

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()

        connector = BumbleConnector(FakeService())
        calls = {"profile_html": 0}
        connector._profile_html = lambda page: calls.__setitem__("profile_html", calls["profile_html"] + 1) or ""

        result = connector._ensure_profile(object(), "thread-1", "bumble:uid-1", "Autumn", refresh=False)

        self.assertEqual(result, [])
        self.assertEqual(calls["profile_html"], 0)
        self.assertEqual(connector.status()["stage"], "PROFILE_SKIPPED")

    def test_bumble_display_name_can_be_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            store.upsert_contact("bumble:uid-1", display_name="Chan")
            store.apply_profile_updates(
                "bumble:uid-1",
                [{"field": "name", "value": "Chan", "confidence": 0.95, "source": "bumble_contact"}],
            )
            profile = store.get_contact_profile("bumble:uid-1")
            self.assertEqual(profile["contact"]["display_name"], "Chan")
            self.assertEqual(profile["fields"]["name"]["value"], "Chan")

    def test_bumble_reply_group_uses_full_pending_context_for_each_incoming(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def create_reply_group(self, request, progress_callback=None):
                self.calls.append(request)
                return {
                    "reply_group": [
                        {"incoming": incoming, "draft": f"回{index + 1}", "message_id": request.message_ids[index]}
                        for index, incoming in enumerate(request.pending_messages)
                    ]
                }

        service = FakeService()
        connector = BumbleConnector(service)
        pending = [
            BumbleMessage("in", "第一条", 0),
            BumbleMessage("in", "第二条", 1),
            BumbleMessage("in", "第三条", 2),
        ]
        replies = connector._create_reply_group("thread-abc", "bumble:abc", pending)
        self.assertEqual([item["draft"] for item in replies], ["回1", "回2", "回3"])
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0].pending_messages, ["第一条", "第二条", "第三条"])
        self.assertIn("第一条", service.calls[0].pending_group_context)
        self.assertIn("第二条", service.calls[0].pending_group_context)
        self.assertIn("第三条", service.calls[0].pending_group_context)
        self.assertEqual(service.calls[0].contact_id, "bumble:abc")
        self.assertEqual(service.calls[0].message_ids[0], bumble_message_hash("bumble:abc", "in:0:第一条"))
        duplicate = connector._create_reply_group("thread-abc", "bumble:abc", pending)
        self.assertEqual([item["draft"] for item in duplicate], ["回1", "回2", "回3"])
        self.assertEqual(len(service.calls), 1)

    def test_android_reply_group_uses_full_pending_context_for_each_incoming(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def create_reply_group(self, request):
                self.calls.append(request)
                return {
                    "reply_group": [
                        {"incoming": incoming, "draft": f"回{index + 1}", "message_id": request.message_ids[index]}
                        for index, incoming in enumerate(request.pending_messages)
                    ]
                }

        class FakeAndroidConnector(AndroidBaseConnector):
            app_name = "tantan"
            channel = "tantan"

        service = FakeService()
        connector = FakeAndroidConnector(service)
        pending = [
            AndroidMessage("in", "第一条", 0),
            AndroidMessage("in", "第二条", 1),
            AndroidMessage("in", "第三条", 2),
        ]
        replies = connector._create_reply_group("thread-abc", "tantan:abc", pending)
        self.assertEqual([item["draft"] for item in replies], ["回1", "回2", "回3"])
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0].pending_messages, ["第一条", "第二条", "第三条"])
        self.assertIn("第一条", service.calls[0].pending_group_context)
        self.assertIn("第二条", service.calls[0].pending_group_context)
        self.assertIn("第三条", service.calls[0].pending_group_context)
        self.assertEqual(service.calls[0].contact_id, "tantan:abc")
        self.assertEqual(service.calls[0].message_ids[0], android_message_hash("tantan:abc", "in:0:第一条"))
        duplicate = connector._create_reply_group("thread-abc", "tantan:abc", pending)
        self.assertEqual([item["draft"] for item in duplicate], ["回1", "回2", "回3"])
        self.assertEqual(len(service.calls), 1)

    def test_reply_group_length_mismatch_stops_before_send(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def create_reply_group(self, request, progress_callback=None):
                self.calls.append(request)
                return {"reply_group": [{"incoming": "第一条", "draft": "只回一条", "message_id": request.message_ids[0]}]}

        bumble = BumbleConnector(FakeService())
        bumble_result = bumble._create_reply_group(
            "thread-abc",
            "bumble:abc",
            [BumbleMessage("in", "第一条", 0), BumbleMessage("in", "第二条", 1), BumbleMessage("in", "第三条", 2)],
        )
        self.assertEqual(bumble_result, [])
        self.assertEqual(bumble.status()["stage"], "DRAFT_FAILED")

        class FakeAndroidConnector(AndroidBaseConnector):
            app_name = "tantan"
            channel = "tantan"

        android = FakeAndroidConnector(FakeService())
        android_result = android._create_reply_group(
            "thread-abc",
            "tantan:abc",
            [AndroidMessage("in", "第一条", 0), AndroidMessage("in", "第二条", 1), AndroidMessage("in", "第三条", 2)],
        )
        self.assertEqual(android_result, [])
        self.assertEqual(android.status()["stage"], "DRAFT_FAILED")

    def test_tantan_open_conversation_never_uses_notification_fallback(self):
        class FakeElement:
            def __init__(self, exists=False):
                self._exists = exists

            def exists(self, timeout=0):
                return self._exists

            def click(self):
                raise AssertionError("missing contact should not be clicked")

            def click_exists(self, timeout=0):
                return self._exists

        class FakeDevice:
            info = {"displayWidth": 720, "displayHeight": 1600}

            def __init__(self):
                self.swipes = []

            def __call__(self, **kwargs):
                if kwargs.get("resourceId") == tantan_module._CHAT_LIST_ID:
                    return FakeElement(True)
                return FakeElement(False)

            def swipe(self, *args, **kwargs):
                self.swipes.append((args, kwargs))

            def press(self, key):
                raise AssertionError("message list is already visible")

            def open_notification(self):
                raise AssertionError("Tantan should not open notification panel")

        connector = TantanConnector(type("FakeService", (), {})())
        device = FakeDevice()
        with patch.object(tantan_module.time, "sleep", lambda _: None):
            opened = connector._open_conversation(device, "tantan:missing", "missing")

        self.assertFalse(opened)
        self.assertGreater(len(device.swipes), 0)

    def test_tantan_find_unread_returns_chat_list_to_top(self):
        xml = f"""
        <hierarchy>
          <node resource-id="{tantan_module._CONV_ITEM_ROOT}" bounds="[0,200][720,360]">
            <node resource-id="{tantan_module._CONTACT_NAME_ID}" text="Alice" />
            <node resource-id="{tantan_module._LAST_MSG_ID}" text="hello" />
            <node resource-id="{tantan_module._RED_DOT_ID}" text="" />
          </node>
        </hierarchy>
        """

        class FakeElement:
            def exists(self, timeout=0):
                return True

        class FakeDevice:
            info = {"displayWidth": 720, "displayHeight": 1600}

            def __init__(self):
                self.swipes = []

            def __call__(self, **kwargs):
                return FakeElement()

            def dump_hierarchy(self):
                return xml

            def swipe(self, x1, y1, x2, y2, **kwargs):
                self.swipes.append((x1, y1, x2, y2, kwargs))

        connector = TantanConnector(type("FakeService", (), {})())
        device = FakeDevice()
        with patch.object(tantan_module.time, "sleep", lambda _: None):
            contacts = connector._find_unread_contacts(device)

        self.assertEqual(contacts, [{"contact_id": "Alice", "name": "Alice", "preview": "hello", "bounds": "[0,200][720,360]"}])
        self.assertGreaterEqual(len(device.swipes), 2)
        for _, y1, _, y2, _ in device.swipes[-2:]:
            self.assertLess(y1, y2)

    def test_tantan_system_preview_with_red_dot_is_processable(self):
        xml = f"""
        <hierarchy>
          <node resource-id="{tantan_module._CONV_ITEM_ROOT}" bounds="[0,200][720,360]">
            <node resource-id="{tantan_module._CONTACT_NAME_ID}" text="Scarlett" />
            <node resource-id="{tantan_module._LAST_MSG_ID}" text="hi，我们可以聊天啦！" />
            <node resource-id="{tantan_module._RED_DOT_ID}" text="" />
          </node>
        </hierarchy>
        """

        class FakeElement:
            def exists(self, timeout=0):
                return True

        class FakeDevice:
            info = {"displayWidth": 720, "displayHeight": 1600}

            def __init__(self):
                self.swipes = []

            def __call__(self, **kwargs):
                return FakeElement()

            def dump_hierarchy(self):
                return xml

            def swipe(self, x1, y1, x2, y2, **kwargs):
                self.swipes.append((x1, y1, x2, y2, kwargs))

        connector = TantanConnector(type("FakeService", (), {})())
        with patch.object(tantan_module.time, "sleep", lambda _: None):
            contacts = connector._find_unread_contacts(FakeDevice())

        self.assertEqual(
            contacts,
            [{"contact_id": "Scarlett", "name": "Scarlett", "preview": "hi，我们可以聊天啦！", "bounds": "[0,200][720,360]"}],
        )

    def test_tantan_rejects_conversation_name_mismatch(self):
        xml = f"""
        <hierarchy>
          <node resource-id="{tantan_module._MSG_LIST_ID}" />
          <node resource-id="{tantan_module._PROFILE_ENTRY_ID}">
            <node text="Bob" />
          </node>
        </hierarchy>
        """

        class FakeElement:
            def __init__(self, exists=True):
                self._exists = exists

            def exists(self, timeout=0):
                return self._exists

        class FakeDevice:
            def __call__(self, **kwargs):
                return FakeElement(True)

            def dump_hierarchy(self):
                return xml

        connector = TantanConnector(type("FakeService", (), {})())
        self.assertFalse(connector._conversation_opened_for(FakeDevice(), "tantan:Alice", "Alice"))

    def test_tantan_profile_text_requires_profile_container(self):
        xml = f"""
        <hierarchy>
          <node package="{tantan_module._PKG}" text="小胖不胖" />
          <node package="{tantan_module._PKG}" text="06/23" />
          <node package="{tantan_module._PKG}" text="hi，我们可以聊天啦！" />
        </hierarchy>
        """

        class FakeDevice:
            def dump_hierarchy(self):
                return xml

        connector = TantanConnector(type("FakeService", (), {})())
        self.assertEqual(connector._extract_screen_text(FakeDevice()), "")

    def test_tantan_profile_text_rejects_chat_list_like_content(self):
        text = "\n".join(
            [
                "小胖不胖",
                "星期六",
                "这么晚还不睡是在等我消息吧",
                "111",
                "我还在上班哈哈哈",
                "NNina",
                "06/23",
                "hi，我们可以聊天啦！",
                "木糖醇",
                "06/22",
                "加一",
                "昵称",
                "小狗麦当当",
                "04/18",
                "哈哈哈",
                "系统通知",
                "04/17",
                "晚上好",
                "Kiraaa",
                "03/28",
            ]
        )
        connector = TantanConnector(type("FakeService", (), {})())
        self.assertTrue(connector._profile_text_looks_like_chat_list(text))

    def test_tantan_existing_drafts_are_not_auto_sent(self):
        connector = TantanConnector(type("FakeService", (), {})())
        self.assertEqual(connector._unsent_draft_reply_group("thread-any"), [])

    def test_tantan_empty_message_read_sends_hiii(self):
        class FakeMemory:
            def __init__(self):
                self.sent = []

            def get_or_create_thread(self, platform, contact_id, name):
                return "thread-empty"

            def add_thread_message(self, thread_id, platform, platform_message_id, role, content, **kwargs):
                self.sent.append(
                    {
                        "thread_id": thread_id,
                        "platform": platform,
                        "platform_message_id": platform_message_id,
                        "role": role,
                        "content": content,
                        **kwargs,
                    }
                )
                return True

            def pending_thread_messages(self, thread_id):
                raise AssertionError("empty message fallback should stop before pending lookup")

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()

        service = FakeService()
        connector = TantanConnector(service)
        sent_texts = []
        connector._open_conversation = lambda device, contact_id, name, open_bounds=None: True
        connector._is_current_contact = lambda device, contact_id, name: True
        connector._fetch_profile_if_needed = lambda device, contact_id: True
        connector._read_conversation = lambda device, contact_id, thread_id="": []
        connector._sync_thread_messages = lambda thread_id, contact_id, messages: []
        connector._send_reply = lambda device, text: sent_texts.append(text) or True

        ok = connector._process_contact(object(), "tantan:empty", "Empty", auto_send=True)

        self.assertTrue(ok)
        self.assertEqual(sent_texts, ["hiii"])
        self.assertEqual(connector.status()["stage"], "SENT")
        self.assertEqual(connector.status()["sent_count"], 1)
        self.assertEqual(connector.status()["contacts"]["tantan:empty"]["stage"], "SENT")
        self.assertEqual(service.memory.sent[0]["role"], "sent")
        self.assertEqual(service.memory.sent[0]["content"], "hiii")
        self.assertEqual(service.memory.sent[0]["technique"], "empty_thread_fallback")

    def test_tantan_system_preview_does_not_block_empty_hiii(self):
        class FakeMemory:
            def __init__(self):
                self.sent = []

            def get_or_create_thread(self, platform, contact_id, name):
                return "thread-empty"

            def add_thread_message(self, thread_id, platform, platform_message_id, role, content, **kwargs):
                self.sent.append({"role": role, "content": content, **kwargs})
                return True

            def pending_thread_messages(self, thread_id):
                raise AssertionError("empty message fallback should stop before pending lookup")

        class FakeService:
            def __init__(self):
                self.memory = FakeMemory()

        service = FakeService()
        connector = TantanConnector(service)
        sent_texts = []
        connector._open_conversation = lambda device, contact_id, name, open_bounds=None: True
        connector._is_current_contact = lambda device, contact_id, name: True
        connector._fetch_profile_if_needed = lambda device, contact_id: True
        connector._read_conversation = lambda device, contact_id, thread_id="": []
        connector._sync_thread_messages = lambda thread_id, contact_id, messages: []
        connector._send_reply = lambda device, text: sent_texts.append(text) or True

        ok = connector._process_contact(
            object(),
            "tantan:empty",
            "Empty",
            auto_send=True,
            list_preview="hi，我们可以聊天啦！",
        )

        self.assertTrue(ok)
        self.assertEqual(sent_texts, ["hiii"])
        self.assertEqual(service.memory.sent[0]["content"], "hiii")
        self.assertEqual(service.memory.sent[0]["technique"], "empty_thread_fallback")

    def test_bumble_process_contact_skips_before_draft_when_binding_fails(self):
        class FakeService:
            def __init__(self):
                self.memory = type("FakeMemory", (), {"upsert_contact": lambda *args, **kwargs: None})()
                self.calls = []

            def create_draft(self, request):
                self.calls.append(request)
                return {"draft": "不应该生成"}

        service = FakeService()
        connector = BumbleConnector(service)
        connector._open_contact_and_wait_bound = lambda *args, **kwargs: False
        connector._create_reply_group = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("draft should not run"))

        completed = connector._process_contact(
            page=object(),
            contact=object(),
            contact_id="bumble:autumn",
            name="Autumn",
            preview="你呢",
            refresh_profile=False,
            auto_send=False,
        )
        self.assertTrue(completed)
        self.assertEqual(service.calls, [])
        self.assertEqual(connector.status()["contacts"]["bumble:autumn"]["stage"], "CONTACT_MISMATCH")

    def test_bumble_fill_drops_draft_when_binding_fails(self):
        class FakeInput:
            def __init__(self):
                self.actions = []

            def fill(self, text):
                self.actions.append(("fill", text))

            def press(self, key):
                self.actions.append(("press", key))

        connector = BumbleConnector(type("FakeService", (), {})())
        connector._wait_for_contact_binding = lambda *args, **kwargs: (
            False,
            {"expected_preview": "你呢", "incoming": ["世界杯", "你看嘛"]},
        )
        input_box = FakeInput()
        sent = connector._fill_or_send_reply_group(
            input_box,
            [{"draft": "不应该填入", "incoming": "你呢", "message_id": "hash"}],
            auto_send=False,
            contact_id="bumble:autumn",
            page=object(),
            expected_preview="你呢",
        )
        self.assertEqual(sent, 0)
        self.assertEqual(input_box.actions, [])

    def test_bumble_sent_message_is_skipped_but_cached_unsent_is_reused(self):
        class FakeService:
            def __init__(self, memory):
                self.memory = memory
                self.calls = []

            def create_reply_group(self, request, progress_callback=None):
                self.calls.append(request)
                return {
                    "reply_group": [
                        {
                            "incoming": request.pending_messages[0],
                            "draft": f"回{len(self.calls)}",
                            "message_id": request.message_ids[0],
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(os.path.join(tmp, "memory.db"))
            service = FakeService(store)
            connector = BumbleConnector(service)
            first = connector._create_reply_group("thread-abc", "bumble:abc", [BumbleMessage("in", "第一条", 0)])
            self.assertEqual(first[0]["draft"], "回1")
            self.assertEqual(store.load_sent_hashes(), set())
            again = connector._create_reply_group("thread-abc", "bumble:abc", [BumbleMessage("in", "第一条", 0)])
            self.assertEqual(again[0]["draft"], "回1")
            self.assertEqual(len(service.calls), 1)

            input_box = type("FakeInput", (), {"fill": lambda self, text: None, "press": lambda self, key: None})()
            sent = connector._fill_or_send_reply_group(input_box, again, auto_send=True, contact_id="bumble:abc")
            self.assertEqual(sent, 1)
            skipped = connector._create_reply_group("thread-abc", "bumble:abc", [BumbleMessage("in", "第一条", 0)])
            self.assertEqual(skipped, [])
            self.assertEqual(connector.status()["skipped_duplicate_count"], 1)

    def test_bumble_stale_sent_marker_does_not_hide_pending_cached_draft(self):
        class FakeService:
            def __init__(self, memory):
                self.memory = memory
                self.calls = []

            def create_draft(self, request):
                self.calls.append(request)
                return {"draft": "新草稿"}

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(os.path.join(tmp, "memory.db"))
            message_id = bumble_message_hash("bumble:abc", "in:0:旧消息")
            store.mark_sent(message_id, "bumble:abc")
            store.cache_draft(message_id, "bumble:abc", "旧消息", "旧草稿")

            connector = BumbleConnector(FakeService(store))
            replies = connector._create_reply_group("thread-abc", "bumble:abc", [BumbleMessage("in", "旧消息", 0)])
            self.assertEqual([item["draft"] for item in replies], ["旧草稿"])
            self.assertEqual(connector.service.calls, [])

    def test_bumble_fill_or_send_uses_enter_not_send_button(self):
        class FakeInput:
            def __init__(self):
                self.actions = []

            def fill(self, text):
                self.actions.append(("fill", text))

            def press(self, key):
                self.actions.append(("press", key))

        connector = BumbleConnector(type("FakeService", (), {})())
        replies = [{"draft": "一"}, {"draft": "二"}]
        input_box = FakeInput()
        sent = connector._fill_or_send_reply_group(input_box, replies, auto_send=True)
        self.assertEqual(sent, 2)
        self.assertEqual(input_box.actions, [("fill", "一"), ("press", "Enter"), ("fill", "二"), ("press", "Enter")])

        input_box = FakeInput()
        sent = connector._fill_or_send_reply_group(input_box, replies, auto_send=False)
        self.assertEqual(sent, 0)
        self.assertEqual(input_box.actions, [("fill", "一")])

    def test_bumble_action_contact_candidates_returns_all_turn_contacts(self):
        class FakeText:
            def __init__(self, text):
                self.text = text

            def inner_text(self, timeout=1000):
                return self.text

        class FakeItem:
            def __init__(self, uid, name, move_text):
                self.attrs = {"data-qa-uid": uid, "data-qa-name": name}
                self.move_text = move_text

            def locator(self, selector):
                return FakeText(self.move_text)

            def get_attribute(self, name):
                return self.attrs.get(name, "")

        class FakeLocator:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class FakePage:
            def __init__(self, items):
                self.items = items

            def locator(self, selector):
                return FakeLocator(self.items)

        connector = BumbleConnector(type("FakeService", (), {})())
        page = FakePage(
            [
                FakeItem("a", "A", "Your move"),
                FakeItem("b", "B", "Conversation expired"),
                FakeItem("c", "C", "轮到您了"),
            ]
        )
        candidates = connector._action_contact_candidates(page)
        self.assertEqual([item["uid"] for item in candidates], ["a", "c"])

    def test_bumble_contact_recheck_cooldown_skips_within_ten_minutes(self):
        connector = BumbleConnector(type("FakeService", (), {})())
        connector._contact_processed_at["bumble:a"] = 1000

        skipped = connector._is_contact_in_recheck_cooldown(
            "bumble:a",
            "A",
            now=1000 + CONTACT_RECHECK_COOLDOWN_SECONDS - 1,
        )

        self.assertTrue(skipped)
        self.assertEqual(connector.status()["logs"][-1]["message"], "联系人刚处理过，10分钟冷却内跳过")
        self.assertEqual(connector.status()["logs"][-1]["data"]["contact_id"], "bumble:a")

    def test_bumble_contact_recheck_cooldown_allows_after_ten_minutes(self):
        connector = BumbleConnector(type("FakeService", (), {})())
        connector._contact_processed_at["bumble:a"] = 1000

        skipped = connector._is_contact_in_recheck_cooldown(
            "bumble:a",
            "A",
            now=1000 + CONTACT_RECHECK_COOLDOWN_SECONDS,
        )

        self.assertFalse(skipped)
        self.assertEqual(connector.status()["logs"], [])

    def test_bumble_next_candidate_skips_cooldown_and_continues(self):
        class FakeItem:
            def __init__(self, uid, name):
                self.attrs = {"data-qa-uid": uid, "data-qa-name": name}

            def get_attribute(self, name):
                return self.attrs.get(name, "")

        class FakeLocator:
            def __init__(self, items):
                self.items = items

            def nth(self, index):
                return self.items[index]

        class FakePage:
            def __init__(self, items):
                self.items = items

            def locator(self, selector):
                return FakeLocator(self.items)

        connector = BumbleConnector(type("FakeService", (), {})())
        connector._contact_processed_at["bumble:a"] = 1000
        page = FakePage([FakeItem("a", "A"), FakeItem("c", "C"), FakeItem("d", "D")])
        candidates = [
            {"index": 0, "uid": "a", "name": "A", "preview": "A again"},
            {"index": 1, "uid": "c", "name": "C", "preview": "C new"},
            {"index": 2, "uid": "d", "name": "D", "preview": "D new"},
        ]

        selected = connector._next_processable_candidate(page, candidates, now=1001)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["contact_id"], "bumble:c")
        self.assertEqual(selected["preview"], "C new")

    def test_bumble_contact_id_and_hash_are_stable(self):
        self.assertEqual(bumble_contact_id("abc"), "bumble:abc")
        self.assertEqual(bumble_contact_id("bumble:abc"), "bumble:abc")
        self.assertEqual(
            bumble_message_hash("bumble:abc", "hello"),
            bumble_message_hash("bumble:abc", "hello"),
        )

    def test_bumble_turn_label_supports_common_locales(self):
        self.assertTrue(is_turn_label("轮到您了"))
        self.assertTrue(is_turn_label("Your move"))
        self.assertTrue(is_turn_label("It's your move"))
        self.assertTrue(is_turn_label("your turn"))
        self.assertFalse(is_turn_label("Conversation expired"))

    def test_bumble_status_logs_stage_and_keeps_recent_100(self):
        connector = BumbleConnector(type("FakeService", (), {})())
        connector._set_stage("SCANNING_CONTACTS", "扫描联系人", data={"contact_count": 2})
        status = connector.status()
        self.assertEqual(status["stage"], "SCANNING_CONTACTS")
        self.assertEqual(status["status_code"], STAGE_CODES["SCANNING_CONTACTS"])
        self.assertEqual(status["logs"][-1]["message"], "扫描联系人")
        self.assertEqual(status["logs"][-1]["data"]["contact_count"], 2)

        for index in range(105):
            connector._log("IDLE", f"log-{index}")
        status = connector.status()
        self.assertEqual(len(status["logs"]), 100)
        self.assertEqual(status["logs"][0]["message"], "log-5")

    def test_bumble_error_stage_sets_last_error(self):
        connector = BumbleConnector(type("FakeService", (), {})())
        connector._set_stage("ERROR", "失败", ok=False)
        status = connector.status()
        self.assertEqual(status["stage"], "ERROR")
        self.assertEqual(status["status_code"], STAGE_CODES["ERROR"])
        self.assertEqual(status["last_error"], "失败")

    def test_bumble_contact_status_is_isolated_by_contact_id(self):
        connector = BumbleConnector(type("FakeService", (), {})())
        connector._update_contact_status("bumble:a", name="A", pending_group_count=1)
        connector._update_contact_status("bumble:b", name="B", pending_group_count=2)
        connector._update_contact_status("bumble:a", stage="DRAFTED")
        contacts = connector.status()["contacts"]
        self.assertEqual(contacts["bumble:a"]["name"], "A")
        self.assertEqual(contacts["bumble:a"]["pending_group_count"], 1)
        self.assertEqual(contacts["bumble:a"]["stage"], "DRAFTED")
        self.assertEqual(contacts["bumble:b"]["name"], "B")
        self.assertEqual(contacts["bumble:b"]["pending_group_count"], 2)
        self.assertNotIn("stage", contacts["bumble:b"])


if __name__ == "__main__":
    unittest.main()
