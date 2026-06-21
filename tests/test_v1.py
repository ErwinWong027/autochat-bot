import os
import tempfile
import unittest

from social_twin.config import load_settings
from social_twin.bumble import (
    BumbleConnector,
    BumbleMessage,
    STAGE_CODES,
    bumble_contact_id,
    bumble_message_hash,
    extract_bumble_messages_from_html,
    extract_bumble_contacts_from_html,
    extract_bumble_profile_updates,
    extract_latest_incoming_text,
    extract_pending_incoming_group,
    is_turn_label,
    profile_category,
)
from social_twin.connectors import BrowserAgentConfig, BrowserAgentConnector, ManualConnector
from social_twin.knowledge import load_persona_dialogues, load_strategy_knowledge
from social_twin.memory import MemoryStore
from social_twin.profile import ProfileAnalyzer
from social_twin.service import DraftRequest
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


class ProfileTests(unittest.TestCase):
    def test_rule_profile_extraction(self):
        analyzer = ProfileAnalyzer(load_settings(), None)
        updates = analyzer.extract_rules("我在杭州做金融，身高168cm，水瓶座，喜欢音乐和旅行", "message")
        fields = {item["field"]: item["value"] for item in updates}
        self.assertEqual(fields["height"], "168")
        self.assertIn("水瓶", fields["zodiac"])
        self.assertIn("音乐", fields["hobbies"])
        self.assertIn("旅行", fields["hobbies"])


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
        fields = {item["field"]: item["value"] for item in updates}
        self.assertEqual(fields["platform_name"], "Chan")
        self.assertEqual(fields["age"], "28")
        self.assertEqual(fields["height"], "165 cm")
        self.assertEqual(fields["zodiac"], "Taurus")
        self.assertEqual(fields["education"], "Undergraduate degree")
        self.assertEqual(fields["dating_intentions"], "Something casual")
        self.assertIn("Shenzhen", fields["location"])
        self.assertIn("The quickest way to my heart is", fields["profile_prompts"])
        self.assertIn("https://eu1.ecdn2.bumbcdn.com", fields["photo_urls"])
        self.assertEqual(profile_category("dating_intentions"), "关系意图")
        self.assertEqual(profile_category("photo_urls"), "照片信息")

    def test_bumble_display_name_can_be_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "memory.db")
            store = MemoryStore(db_path)
            store.upsert_contact("bumble:uid-1", display_name="Chan")
            store.apply_profile_updates(
                "bumble:uid-1",
                [{"field": "platform_name", "value": "Chan", "confidence": 0.95, "source": "bumble_contact"}],
            )
            profile = store.get_contact_profile("bumble:uid-1")
            self.assertEqual(profile["contact"]["display_name"], "Chan")
            self.assertEqual(profile["fields"]["platform_name"]["value"], "Chan")

    def test_bumble_reply_group_calls_create_draft_for_each_pending_message(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def create_draft(self, request):
                self.calls.append(request)
                return {"draft": f"回{len(self.calls)}"}

        service = FakeService()
        connector = BumbleConnector(service)
        pending = [
            BumbleMessage("in", "第一条", 0),
            BumbleMessage("in", "第二条", 1),
            BumbleMessage("in", "第三条", 2),
        ]
        replies = connector._create_reply_group("bumble:abc", pending)
        self.assertEqual([item["draft"] for item in replies], ["回1", "回2", "回3"])
        self.assertEqual([call.message for call in service.calls], ["第一条", "第二条", "第三条"])
        self.assertEqual(service.calls[0].contact_id, "bumble:abc")
        self.assertEqual(service.calls[0].message_id, bumble_message_hash("bumble:abc", "第一条"))
        duplicate = connector._create_reply_group("bumble:abc", [BumbleMessage("in", "第一条", 3)])
        self.assertEqual([item["draft"] for item in duplicate], ["回1"])
        self.assertEqual(len(service.calls), 3)

    def test_bumble_sent_message_is_skipped_but_cached_unsent_is_reused(self):
        class FakeService:
            def __init__(self, memory):
                self.memory = memory
                self.calls = []

            def create_draft(self, request):
                self.calls.append(request)
                return {"draft": f"回{len(self.calls)}"}

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(os.path.join(tmp, "memory.db"))
            service = FakeService(store)
            connector = BumbleConnector(service)
            first = connector._create_reply_group("bumble:abc", [BumbleMessage("in", "第一条", 0)])
            self.assertEqual(first[0]["draft"], "回1")
            self.assertEqual(store.load_sent_hashes(), set())
            again = connector._create_reply_group("bumble:abc", [BumbleMessage("in", "第一条", 1)])
            self.assertEqual(again[0]["draft"], "回1")
            self.assertEqual(len(service.calls), 1)

            input_box = type("FakeInput", (), {"fill": lambda self, text: None, "press": lambda self, key: None})()
            sent = connector._fill_or_send_reply_group(input_box, again, auto_send=True, contact_id="bumble:abc")
            self.assertEqual(sent, 1)
            skipped = connector._create_reply_group("bumble:abc", [BumbleMessage("in", "第一条", 2)])
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
            message_id = bumble_message_hash("bumble:abc", "旧消息")
            store.mark_sent(message_id, "bumble:abc")
            store.cache_draft(message_id, "bumble:abc", "旧消息", "旧草稿")

            connector = BumbleConnector(FakeService(store))
            replies = connector._create_reply_group("bumble:abc", [BumbleMessage("in", "旧消息", 0)])
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
