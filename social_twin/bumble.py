from __future__ import annotations

import json
import hashlib
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .profile import ProfileAnalyzer
from .service import DigitalTwinService, ReplyGroupRequest


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
CONTACT_SELECTOR = '[data-qa-role="contact"]'
ACTION_CONTACT_SELECTOR = '[data-qa-role="contact"]:has(.contact__move-label)'
INCOMING_MESSAGE_SELECTOR = ".messages-list__conversation .message.message--in .message-bubble__text"
INPUT_SELECTOR = '[data-qa-role="chat-input"] textarea.textarea__input'
SEND_SELECTOR = "button.message-field__send"
PROFILE_SELECTOR = ".profile__entry"

STAGE_CODES = {
    "INIT": 0,
    "LAUNCHING_BROWSER": 90,
    "OPENING_BUMBLE": 100,
    "PAGE_READY": 110,
    "SCANNING_CONTACTS": 200,
    "NO_CONTACT": 204,
    "CONTACT_FOUND": 210,
    "CONTACT_OPENED": 220,
    "CONTACT_MISMATCH": 221,
    "PROFILE_CHECKING": 300,
    "PROFILE_SKIPPED": 304,
    "PROFILE_READ": 310,
    "PROFILE_READ_FAILED": 399,
    "MESSAGES_READ": 400,
    "PENDING_GROUP_FOUND": 410,
    "NO_PENDING_GROUP": 411,
    "DRAFTING": 500,
    "DRAFT_ITEM_START": 501,
    "DRAFT_CACHED": 502,
    "DRAFT_FAILED": 509,
    "DRAFTED": 510,
    "SKIPPED_ALREADY_SENT": 520,
    "NO_UNSENT_DRAFT": 521,
    "FILLED_TEXTAREA": 600,
    "SENT_BY_ENTER": 610,
    "SEND_NOT_CONFIRMED": 611,
    "IDLE": 700,
    "STOPPING": 799,
    "STOPPED": 800,
    "ERROR": 900,
}


ZODIAC_VALUES = {
    "Aries",
    "Taurus",
    "Gemini",
    "Cancer",
    "Leo",
    "Virgo",
    "Libra",
    "Scorpio",
    "Sagittarius",
    "Capricorn",
    "Aquarius",
    "Pisces",
}
GENDER_VALUES = {"Woman", "Man", "Nonbinary"}
EXERCISE_VALUES = {"Active", "Sometimes", "Almost never"}
DRINKING_VALUES = {"Socially", "Never", "Frequently", "Sober"}
EDUCATION_MARKERS = ("degree", "school", "college", "university", "本科", "硕士", "博士", "Undergraduate")
INTENTION_VALUES = {"Something casual", "Relationship", "Marriage", "Don't know yet", "Intimacy, without commitment"}
FAMILY_VALUES = {"Not sure yet", "Want someday", "Don't want", "Have and want more", "Have and don't want more"}
RELIGION_VALUES = {"Atheist", "Agnostic", "Buddhist", "Christian", "Hindu", "Jewish", "Muslim", "Spiritual"}
POLITICS_VALUES = {"Apolitical", "Moderate", "Liberal", "Conservative"}
TURN_LABEL_KEYWORDS = ("轮到您了", "your move", "your turn", "it's your move", "it’s your move")
CONTACT_RECHECK_COOLDOWN_SECONDS = 10 * 60


@dataclass(frozen=True)
class BumbleContact:
    uid: str
    name: str
    preview: str
    needs_reply: bool

    @property
    def contact_id(self) -> str:
        return bumble_contact_id(self.uid)


@dataclass(frozen=True)
class BumbleMessage:
    role: str
    text: str
    index: int


@dataclass(frozen=True)
class BumbleConfig:
    target_url: str = ""
    auto_send_enabled: bool | None = None
    poll_seconds: int = 5
    refresh_profile: bool = False


def profile_category(field: str) -> str:
    categories = {
        "name": "名字",
        "age": "基础资料",
        "height": "基础资料",
        "education": "基础资料",
        "job": "基础资料",
        "company": "基础资料",
        "school": "基础资料",
        "zodiac": "基础资料",
        "location": "基础资料",
        "hometown": "基础资料",
        "about_me": "关于我",
        "personality_traits": "性格特征",
        "interests_hobbies": "兴趣爱好",
        "profile_prompts": "主页问答",
        "compatibility_points": "契合点",
        "raw_evidence": "原始证据",
    }
    return categories.get(field, "其他")


def categorized_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**item, "category": profile_category(str(item.get("field", "")))} for item in updates]


def bumble_contact_id(uid: str) -> str:
    clean_uid = (uid or "bumble_unknown").strip()
    return clean_uid if clean_uid.startswith("bumble:") else f"bumble:{clean_uid}"


def bumble_message_hash(contact_id: str, incoming_text: str) -> str:
    raw = f"{contact_id}:{incoming_text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def bumble_ui_message_hash(contact_id: str, message: BumbleMessage) -> str:
    raw = f"{message.role}:{message.index}:{message.text}"
    return bumble_message_hash(contact_id, raw)


def bumble_message_key(message: BumbleMessage) -> tuple[str, str]:
    role = "user" if message.role == "in" else "sent"
    return role, " ".join((message.text or "").strip().split())


def is_turn_label(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    return any(keyword.lower() in normalized for keyword in TURN_LABEL_KEYWORDS)


def normalize_preview_text(text: str) -> str:
    return " ".join((text or "").split())


def preview_matches_messages(preview: str, messages: list[BumbleMessage]) -> bool:
    expected = normalize_preview_text(preview)
    if not expected or is_turn_label(expected):
        return True
    visible = [normalize_preview_text(message.text) for message in messages if message.role in ("in", "out")]
    if not visible:
        return False
    latest = visible[-1]
    return expected in latest or latest in expected


class _ContactListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.contacts: list[BumbleContact] = []
        self.current: dict[str, Any] | None = None
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        class_names = set(attr.get("class", "").split())
        if self.current is None and tag == "div" and attr.get("data-qa-role") == "contact":
            self.current = {
                "uid": attr.get("data-qa-uid", ""),
                "name": attr.get("data-qa-name", ""),
                "preview": [],
                "move": [],
            }
            self.stack.append("contact")
            return
        if self.current is None:
            return
        if tag in VOID_TAGS:
            return
        marker = ""
        if "contact__move-label" in class_names:
            marker = "move"
        elif "contact__message" in class_names:
            marker = "preview"
        self.stack.append(marker)

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        text = " ".join(data.split())
        if not text:
            return
        if "move" in self.stack:
            self.current["move"].append(text)
        if "preview" in self.stack:
            self.current["preview"].append(text)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or not self.stack:
            return
        marker = self.stack.pop()
        if marker == "contact":
            move_text = " ".join(self.current["move"])
            self.contacts.append(
                BumbleContact(
                    uid=str(self.current["uid"]),
                    name=str(self.current["name"]),
                    preview=" ".join(self.current["preview"]).strip(),
                    needs_reply=is_turn_label(move_text),
                )
            )
            self.current = None


class _MessageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[BumbleMessage] = []
        self.current_role = ""
        self.capture_text = False
        self.text_parts: list[str] = []
        self.text_depth = 0
        self.index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        if self.capture_text:
            self.text_depth += 1
        if tag == "div" and "message" in classes:
            if "message--in" in classes:
                self.current_role = "in"
            elif "message--out" in classes:
                self.current_role = "out"
        if self.current_role and "message-bubble__text" in classes:
            self.capture_text = True
            self.text_parts = []
            self.text_depth = 1

    def handle_data(self, data: str) -> None:
        if self.capture_text:
            text = " ".join(data.split())
            if text:
                self.text_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if not self.capture_text:
            return
        self.text_depth -= 1
        if self.text_depth <= 0:
            text = " ".join(self.text_parts).strip()
            if text:
                self.messages.append(BumbleMessage(role=self.current_role, text=text, index=self.index))
                self.index += 1
            self.capture_text = False
            self.text_parts = []
            self.current_role = ""
            self.text_depth = 0


class _ProfileParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.name = ""
        self.age = ""
        self.about: list[str] = []
        self.badges: list[str] = []
        self.photos: list[str] = []
        self.location: list[str] = []
        self.prompt_title = ""
        self.prompts: list[dict[str, str]] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        marker = ""
        if tag == "img" and "profile__photo" in classes and attr.get("src"):
            self.photos.append(_normalize_image_url(attr["src"]))
        if tag in VOID_TAGS:
            return
        if "profile__name" in classes:
            self.name = attr.get("title", self.name)
            marker = "name"
        elif "profile__age" in classes:
            marker = "age"
        elif "profile__about" in classes:
            marker = "about"
        elif "pill__title" in classes:
            marker = "badge"
        elif "location-widget" in classes or any(item.startswith("location-widget__") for item in classes):
            marker = "location"
        elif "profile-answer__title" in classes:
            marker = "prompt_title"
        elif "profile-answer__text" in classes:
            marker = "prompt_text"
        self.stack.append(marker)

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if "name" in self.stack and not self.name:
            self.name = text
        if "age" in self.stack:
            self.age = text.replace(",", "").strip()
        if "about" in self.stack:
            self.about.append(text)
        if "badge" in self.stack:
            self.badges.append(text)
        if "location" in self.stack:
            self.location.append(text)
        if "prompt_title" in self.stack:
            self.prompt_title = text
        if "prompt_text" in self.stack and self.prompt_title:
            self.prompts.append({"title": self.prompt_title, "answer": text})
            self.prompt_title = ""

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            self.stack.pop()


def extract_bumble_contacts_from_html(html: str) -> list[BumbleContact]:
    parser = _ContactListParser()
    parser.feed(html or "")
    return [contact for contact in parser.contacts if contact.needs_reply]


def extract_latest_incoming_text(html: str) -> str:
    messages = extract_bumble_messages_from_html(html)
    incoming = [message.text for message in messages if message.role == "in"]
    return incoming[-1] if incoming else ""


def extract_bumble_messages_from_html(html: str) -> list[BumbleMessage]:
    parser = _MessageParser()
    parser.feed(html or "")
    return parser.messages


def extract_pending_incoming_group(messages: list[BumbleMessage]) -> list[BumbleMessage]:
    last_out_index = -1
    for index, message in enumerate(messages):
        if message.role == "out":
            last_out_index = index
    if last_out_index == -1:
        return [message for message in messages if message.role == "in"]
    return [message for message in messages[last_out_index + 1 :] if message.role == "in"]


def extract_bumble_profile_updates(html: str, source: str = "bumble_profile") -> list[dict[str, Any]]:
    parser = _ProfileParser()
    parser.feed(html or "")
    updates: list[dict[str, Any]] = []
    if parser.name:
        updates.append(_update("name", parser.name, 0.95, source, parser.name))
    if parser.age:
        updates.append(_update("age", parser.age, 0.9, source, parser.age))
    about = "\n".join(parser.about).strip()
    if about:
        updates.append(_update("about_me", about, 0.86, source, about))
        updates.extend(ProfileAnalyzer.extract_rules(ProfileAnalyzer.__new__(ProfileAnalyzer), about, source=source))
    raw_evidence: list[str] = []
    for badge in dict.fromkeys(item.strip() for item in parser.badges if item.strip()):
        classified = _classify_badge(badge)
        if classified:
            field, confidence = classified
            updates.append(_update(field, badge, confidence, source, badge))
        else:
            raw_evidence.append(badge)
    location = "；".join(dict.fromkeys(item.strip() for item in parser.location if item.strip()))
    if location:
        updates.append(_update("location", location, 0.82, source, location))
        hometown = _extract_from_location(location)
        if hometown:
            updates.append(_update("hometown", hometown, 0.78, source, location))
    if parser.prompts:
        prompt_text = json.dumps(parser.prompts, ensure_ascii=False)
        updates.append(_update("profile_prompts", prompt_text, 0.9, source, prompt_text))
    if parser.photos:
        raw_evidence.extend(list(dict.fromkeys(parser.photos)))
    profile_text = _profile_text_from_parser(parser)
    if profile_text:
        raw_evidence.insert(0, profile_text)
    if raw_evidence:
        raw = "；".join(dict.fromkeys(raw_evidence))
        updates.append(_update("raw_evidence", raw, 0.55, source, raw))
    return categorized_updates(_dedupe_updates(updates))


def extract_bumble_profile_text(html: str) -> str:
    parser = _ProfileParser()
    parser.feed(html or "")
    return _profile_text_from_parser(parser)


def _profile_text_from_parser(parser: _ProfileParser) -> str:
    lines: list[str] = []
    if parser.name:
        lines.append(f"name: {parser.name}")
    if parser.age:
        lines.append(f"age: {parser.age}")
    lines.extend(parser.about)
    lines.extend(parser.badges)
    lines.extend(parser.location)
    for prompt in parser.prompts:
        title = str(prompt.get("title", "")).strip()
        answer = str(prompt.get("answer", "")).strip()
        if title or answer:
            lines.append(f"{title}: {answer}".strip(": "))
    return "\n".join(dict.fromkeys(line.strip() for line in lines if line.strip()))


def _classify_badge(value: str) -> tuple[str, float] | None:
    if value.endswith("cm") or value.endswith("厘米"):
        return "height", 0.94
    if value in ZODIAC_VALUES:
        return "zodiac", 0.9
    if any(marker.lower() in value.lower() for marker in EDUCATION_MARKERS):
        return "education", 0.86
    return None


def _extract_from_location(text: str) -> str:
    markers = ("From ", "来自")
    for marker in markers:
        if marker in text:
            return text.split(marker, 1)[1].split("；", 1)[0].strip()
    return ""


def _normalize_image_url(src: str) -> str:
    if src.startswith("//"):
        return f"https:{src}"
    return src


def _update(field: str, value: str, confidence: float, source: str, evidence: str) -> dict[str, Any]:
    return {
        "field": field,
        "value": value.strip(),
        "confidence": confidence,
        "source": source,
        "evidence": evidence.strip(),
    }


def _dedupe_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result = []
    for update in updates:
        key = (str(update.get("field", "")), str(update.get("value", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(update)
    return result


class BumbleConnector:
    name = "bumble"

    def __init__(self, service: DigitalTwinService):
        self.service = service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        memory = getattr(self.service, "memory", None)
        if memory is not None and hasattr(memory, "load_sent_hashes"):
            self._sent_messages: set[str] = memory.load_sent_hashes(since_days=7)
        else:
            self._sent_messages = set()
        self._draft_cache: dict[str, str] = {}
        self._sent_message_at: dict[str, float] = {}
        self._contact_processed_at: dict[str, float] = {}
        self._status: dict[str, Any] = self._initial_status()

    def _initial_status(self) -> dict[str, Any]:
        return {
            "running": False,
            "stage": "INIT",
            "status_code": STAGE_CODES["INIT"],
            "last_error": "",
            "last_contact_id": "",
            "last_contact_name": "",
            "last_message": "",
            "last_draft": "",
            "profile_updates": [],
            "pending_group_count": 0,
            "reply_count": 0,
            "last_reply_group": [],
            "skipped_duplicate_count": 0,
            "sent_count": 0,
            "draft_count": 0,
            "contact_count": 0,
            "message_count": 0,
            "last_out_index": -1,
            "contacts": {},
            "logs": [],
        }

    def run(self, config: BumbleConfig | None = None) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._status = self._initial_status()
        self._status["running"] = True
        self._set_stage("INIT", "Bumble Agent 启动")
        self._thread = threading.Thread(target=self._loop, args=(config or BumbleConfig(),), daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._status.get("running"):
            self._set_stage("STOPPING", "收到停止指令，等待当前安全点停止")
        else:
            self._set_stage("STOPPED", "收到停止指令")
        return self.status()

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _set_stage(self, stage: str, message: str, ok: bool = True, data: dict[str, Any] | None = None) -> None:
        self._status["stage"] = stage
        self._status["status_code"] = STAGE_CODES.get(stage, -1)
        if not ok:
            self._status["last_error"] = message
        self._log(stage, message, ok=ok, data=data or {})

    def _log(self, stage: str, message: str, ok: bool = True, data: dict[str, Any] | None = None) -> None:
        logs = list(self._status.get("logs", []))
        logs.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "stage": stage,
                "status_code": STAGE_CODES.get(stage, -1),
                "ok": ok,
                "message": message,
                "data": data or {},
            }
        )
        self._status["logs"] = logs[-100:]

    def _update_contact_status(self, contact_id: str, **updates: Any) -> None:
        contacts = dict(self._status.get("contacts", {}))
        current = dict(contacts.get(contact_id, {}))
        current.update(updates)
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        contacts[contact_id] = current
        self._status["contacts"] = contacts

    def _fill_progress(self, input_box, text: str) -> None:
        if input_box is None:
            return
        try:
            input_box.fill(text)
        except Exception:
            pass

    def _loop(self, config: BumbleConfig) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._status.update({"running": False, "last_error": f"playwright不可用：{exc}"})
            return

        request_auto_send = True if config.auto_send_enabled is None else config.auto_send_enabled
        auto_send = self.service.settings.auto_send_enabled and request_auto_send
        target_url = config.target_url or self.service.settings.bumble_target_url
        poll_seconds = config.poll_seconds or self.service.settings.bumble_poll_seconds
        user_data_dir = self.service.settings.bumble_user_data_dir
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                self._set_stage(
                    "LAUNCHING_BROWSER",
                    "启动 Bumble 专用 Chromium",
                    data={"user_data_dir": user_data_dir},
                )
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=False,
                    timeout=30000,
                )
                page = browser.pages[0] if browser.pages else browser.new_page()
                self._set_stage(
                    "OPENING_BUMBLE",
                    "打开 Bumble Web",
                    data={"target_url": target_url, "user_data_dir": user_data_dir},
                )
                page.goto(target_url)
                self._set_stage("PAGE_READY", "Bumble 页面已打开", data={"target_url": target_url})
                while not self._stop.is_set():
                    self._set_stage("SCANNING_CONTACTS", "扫描轮到您了联系人")
                    candidates = self._action_contact_candidates(page)
                    if not candidates:
                        self._set_stage("NO_CONTACT", "未找到轮到您了联系人", data={"contact_count": self._status["contact_count"]})
                        time.sleep(poll_seconds)
                        continue
                    selected = self._next_processable_candidate(page, candidates)
                    if selected is not None:
                        completed = self._process_contact(
                            page,
                            selected["contact"],
                            selected["contact_id"],
                            selected["name"],
                            selected["preview"],
                            config.refresh_profile,
                            auto_send,
                        )
                        if completed:
                            self._contact_processed_at[selected["contact_id"]] = time.time()
                        else:
                            self._stop.set()
                    if selected is None:
                        time.sleep(poll_seconds)
                        continue
                    time.sleep(poll_seconds)
                browser.close()
        except Exception as exc:
            self._set_stage("ERROR", str(exc), ok=False, data={"traceback": traceback.format_exc()})
        finally:
            self._status["running"] = False
            if self._status.get("stage") != "ERROR":
                self._set_stage("STOPPED", "Bumble Agent 已停止")

    def _process_contact(
        self,
        page,
        contact,
        contact_id: str,
        name: str,
        preview: str,
        refresh_profile: bool,
        auto_send: bool,
    ) -> bool:
        self._status.update({"last_contact_id": contact_id, "last_contact_name": name})
        self._update_contact_status(contact_id, name=name, stage="CONTACT_FOUND")
        self._set_stage("CONTACT_FOUND", "找到待回复联系人", data={"contact_id": contact_id, "name": name})
        if not self._open_contact_and_wait_bound(page, contact, contact_id, name, preview):
            self._update_contact_status(contact_id, stage="CONTACT_MISMATCH")
            return True
        self._update_contact_status(contact_id, stage="CONTACT_OPENED")
        self._set_stage("CONTACT_OPENED", "已打开联系人会话", data={"contact_id": contact_id, "name": name})
        thread_id = self.service.memory.get_or_create_thread("bumble", contact_id, name)
        self._update_contact_status(contact_id, thread_id=thread_id)
        input_box = self._visible_input_box(page)
        self._fill_progress(input_box, "正在读取 profile 生成画像中，请等待……")
        profile_updates = self._ensure_profile(page, thread_id, contact_id, name, refresh_profile)
        if not self._assert_selected_contact(page, contact_id, "读取消息前联系人已切换，已停止 Agent"):
            return False
        self._fill_progress(input_box, "正在分析对话中，请等待……")
        pending_group = self._pending_incoming_group(page, thread_id, contact_id, preview)
        if self._status.get("stage") == "CONTACT_MISMATCH":
            self._update_contact_status(contact_id, stage="CONTACT_MISMATCH")
            return True
        self._status["pending_group_count"] = len(pending_group)
        self._status["reply_count"] = 0
        self._update_contact_status(
            contact_id,
            message_count=self._status["message_count"],
            last_out_index=self._status["last_out_index"],
            pending_group_count=len(pending_group),
            pending_group=[item.text for item in pending_group],
        )
        if not pending_group:
            if auto_send:
                reply_group = self._unsent_draft_reply_group(thread_id)
                if reply_group:
                    self._status["reply_count"] = len(reply_group)
                    self._update_contact_status(contact_id, stage="SENDING_EXISTING_DRAFTS", last_reply_group=reply_group)
                    self._set_stage(
                        "SENDING_EXISTING_DRAFTS",
                        f"发现 {len(reply_group)} 条已留痕未发送草稿，自动逐条发送",
                        data={"reply_count": len(reply_group)},
                    )
                    sent = self._fill_or_send_reply_group(input_box, reply_group, True, contact_id, page, preview, thread_id)
                    self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
                    if sent == len(reply_group):
                        self._update_contact_status(contact_id, stage="SENT_BY_ENTER", sent_count=sent)
                        self._set_stage("SENT_BY_ENTER", f"已按 Enter 发送 {sent} 条")
                        return True
                    self._update_contact_status(contact_id, stage="SEND_NOT_CONFIRMED", sent_count=sent)
                    self._set_stage(
                        "SEND_NOT_CONFIRMED",
                        "已有草稿发送未确认，已停止 Agent",
                        ok=False,
                        data={"contact_id": contact_id, "sent_count": sent, "draft_count": len(reply_group)},
                    )
                    return False
            self._update_contact_status(contact_id, stage="NO_PENDING_GROUP")
            self._set_stage(
                "NO_PENDING_GROUP",
                "没有待回复消息组",
                data={
                    "message_count": self._status["message_count"],
                    "last_out_index": self._status["last_out_index"],
                },
            )
            return True
        self._set_stage(
            "PENDING_GROUP_FOUND",
            f"找到 {len(pending_group)} 条待回复 incoming",
            data={"pending_group": [item.text for item in pending_group]},
        )
        self._update_contact_status(contact_id, stage="DRAFTING")
        self._set_stage(
            "DRAFTING",
            "开始按 pending_group 合并生成草稿",
            data={"pending_group_count": len(pending_group), "reply_count": len(pending_group)},
        )
        reply_group = self._create_reply_group(thread_id, contact_id, pending_group, input_box)
        if self._stop.is_set():
            self._update_contact_status(contact_id, stage="STOPPING")
            self._set_stage("STOPPING", "停止指令已生效，跳过发送")
            return False
        if not reply_group:
            self._update_contact_status(contact_id, stage="NO_UNSENT_DRAFT")
            self._set_stage("NO_UNSENT_DRAFT", "没有未发送草稿")
            return True
        self._update_contact_status(contact_id, stage="DRAFTED", last_reply_group=reply_group)
        self._status["reply_count"] = len(reply_group)
        self._set_stage("DRAFTED", f"准备 {len(reply_group)} 条草稿")
        self._status.update(
            {
                "last_contact_id": contact_id,
                "last_contact_name": name,
                "last_message": reply_group[-1]["incoming"],
                "last_draft": reply_group[-1]["draft"],
                "last_reply_group": reply_group,
                "profile_updates": profile_updates,
                "draft_count": int(self._status.get("draft_count", 0)) + len(reply_group),
            }
        )
        sent = self._fill_or_send_reply_group(input_box, reply_group, auto_send, contact_id, page, preview, thread_id)
        if self._status.get("stage") == "CONTACT_MISMATCH":
            self._update_contact_status(contact_id, stage="CONTACT_MISMATCH", sent_count=0)
            return True
        self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
        if sent:
            self._update_contact_status(contact_id, stage="SENT_BY_ENTER", sent_count=sent)
            self._set_stage("SENT_BY_ENTER", f"已按 Enter 发送 {sent} 条")
            if auto_send and sent < len(reply_group):
                self._update_contact_status(contact_id, stage="SEND_NOT_CONFIRMED", sent_count=sent)
                self._set_stage(
                    "SEND_NOT_CONFIRMED",
                    "发送未确认，已停止 Agent，避免继续处理其他联系人",
                    ok=False,
                    data={"contact_id": contact_id, "sent_count": sent, "draft_count": len(reply_group)},
                )
                return False
        else:
            if auto_send:
                self._update_contact_status(contact_id, stage="SEND_NOT_CONFIRMED", sent_count=0)
                self._set_stage(
                    "SEND_NOT_CONFIRMED",
                    "发送未确认，已停止 Agent，避免继续处理其他联系人",
                    ok=False,
                    data={"contact_id": contact_id, "sent_count": 0, "draft_count": len(reply_group)},
                )
                return False
            self._update_contact_status(contact_id, stage="FILLED_TEXTAREA", sent_count=0)
            self._set_stage("FILLED_TEXTAREA", "自动发送关闭，只填入第一条草稿")
        return True

    def _next_processable_candidate(self, page, candidates: list[dict[str, Any]], now: float | None = None) -> dict[str, Any] | None:
        for candidate in candidates:
            if self._stop.is_set():
                return None
            contact = page.locator(ACTION_CONTACT_SELECTOR).nth(candidate["index"])
            uid = candidate["uid"] or contact.get_attribute("data-qa-uid") or "bumble_unknown"
            contact_id = bumble_contact_id(uid)
            name = candidate["name"] or contact.get_attribute("data-qa-name") or uid
            if self._is_contact_in_recheck_cooldown(contact_id, name, now=now):
                continue
            return {
                "contact": contact,
                "contact_id": contact_id,
                "name": name,
                "preview": candidate.get("preview", ""),
            }
        return None

    def _is_contact_in_recheck_cooldown(self, contact_id: str, name: str = "", now: float | None = None) -> bool:
        checked_at = time.time() if now is None else now
        last_processed = self._contact_processed_at.get(contact_id, 0)
        if not last_processed:
            return False
        elapsed = checked_at - last_processed
        if elapsed >= CONTACT_RECHECK_COOLDOWN_SECONDS:
            return False
        remaining = max(0, int(CONTACT_RECHECK_COOLDOWN_SECONDS - elapsed))
        self._log(
            "SCANNING_CONTACTS",
            "联系人刚处理过，10分钟冷却内跳过",
            data={"contact_id": contact_id, "name": name, "cooldown_remaining_seconds": remaining},
        )
        return True

    def _action_contact_candidates(self, page) -> list[dict[str, Any]]:
        contacts = page.locator(ACTION_CONTACT_SELECTOR)
        total_contacts = contacts.count()
        candidates = []
        for index in range(total_contacts):
            item = contacts.nth(index)
            try:
                move_text = item.locator(".contact__move-label").inner_text(timeout=1000)
            except Exception:
                continue
            candidates.append(
                {
                    "index": index,
                    "uid": item.get_attribute("data-qa-uid") or "",
                    "name": item.get_attribute("data-qa-name") or "",
                    "preview": self._contact_preview(item),
                    "move_text": move_text,
                }
            )
        matches = [candidate for candidate in candidates if is_turn_label(candidate["move_text"])]
        self._status["contact_count"] = len(matches)
        if matches:
            self._log(
                "SCANNING_CONTACTS",
                f"命中 {len(matches)} 个待回复联系人",
                data={"total_contacts": total_contacts, "candidates": matches[:20]},
            )
            return matches
        self._log(
            "SCANNING_CONTACTS",
            "候选联系人未命中文本",
            data={"total_contacts": total_contacts, "candidates": candidates[:20]},
        )
        return []

    def _first_action_contact(self, page):
        candidates = self._action_contact_candidates(page)
        if not candidates:
            return None
        return page.locator(ACTION_CONTACT_SELECTOR).nth(candidates[0]["index"])

    def _open_contact(self, contact, force: bool = False) -> None:
        classes = contact.get_attribute("class") or ""
        if "is-selected" in classes and not force:
            self._log("CONTACT_FOUND", "联系人已选中，跳过点击")
            return
        try:
            contact.click(timeout=3000)
            return
        except Exception as exc:
            self._log("CONTACT_FOUND", "普通点击失败，改用 JS click", ok=False, data={"error": str(exc)})
        contact.evaluate("element => element.click()")

    def _open_contact_and_wait_bound(
        self,
        page,
        contact,
        contact_id: str,
        name: str,
        preview: str,
        attempts: int = 3,
    ) -> bool:
        last_data: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            self._open_contact(contact, force=attempt > 1)
            ok, data = self._wait_for_contact_binding(page, contact_id, name, preview)
            if ok:
                return True
            last_data = data
            self._log(
                "CONTACT_MISMATCH",
                "联系人绑定校验失败，重试点击",
                ok=False,
                data={**data, "attempt": attempt, "max_attempts": attempts},
            )
            time.sleep(0.5)
        self._set_stage(
            "CONTACT_MISMATCH",
            "联系人绑定校验失败，跳过该联系人",
            ok=False,
            data={**last_data, "expected_contact_id": contact_id, "expected_name": name, "expected_preview": preview},
        )
        return False

    def _wait_for_contact_binding(
        self,
        page,
        contact_id: str,
        name: str,
        preview: str,
        timeout_seconds: float = 6.0,
    ) -> tuple[bool, dict[str, Any]]:
        deadline = time.time() + timeout_seconds
        last_data: dict[str, Any] = {}
        while time.time() < deadline:
            selected_uid = self._selected_contact_uid(page)
            selected_contact_id = bumble_contact_id(selected_uid)
            messages = self._stable_visible_messages(page, timeout_seconds=1.0)
            header_text = self._visible_chat_header_text(page)
            incoming = [message.text for message in messages if message.role == "in"]
            selected_ok = bool(selected_uid) and selected_contact_id == contact_id
            preview_ok = preview_matches_messages(preview, messages)
            header_ok = self._header_matches_name(header_text, name)
            last_data = {
                "expected_contact_id": contact_id,
                "actual_contact_id": selected_contact_id,
                "expected_name": name,
                "header_text": header_text,
                "expected_preview": preview,
                "incoming": incoming[-5:],
                "selected_ok": selected_ok,
                "header_ok": header_ok,
                "preview_ok": preview_ok,
            }
            if selected_ok and preview_ok and header_ok:
                return True, last_data
            time.sleep(0.25)
        return False, last_data

    def _visible_chat_header_text(self, page) -> str:
        selectors = [
            ".message-list-header:visible",
            ".chat-header:visible",
            ".messages-header:visible",
            '[data-qa-role="chat-header"]:visible',
            '[data-qa-role="conversation-header"]:visible',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    text = locator.first.inner_text(timeout=500).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    def _header_matches_name(self, header_text: str, name: str) -> bool:
        header = normalize_preview_text(header_text).lower()
        expected = normalize_preview_text(name).lower()
        if not expected or not header:
            return True
        return expected in header

    def _selected_contact_uid(self, page) -> str:
        try:
            selected = page.locator(f"{CONTACT_SELECTOR}.is-selected").first
            if selected.count() > 0:
                return selected.get_attribute("data-qa-uid") or ""
        except Exception:
            return ""
        return ""

    def _assert_selected_contact(self, page, contact_id: str, message: str) -> bool:
        selected_uid = self._selected_contact_uid(page)
        actual_contact_id = bumble_contact_id(selected_uid)
        if selected_uid and actual_contact_id == contact_id:
            return True
        self._set_stage(
            "CONTACT_MISMATCH",
            message,
            ok=False,
            data={"expected_contact_id": contact_id, "actual_contact_id": actual_contact_id},
        )
        return False

    def _wait_for_selected_contact_uid(self, page, expected_contact_id: str, timeout_seconds: float = 3.0) -> str:
        deadline = time.time() + timeout_seconds
        selected_uid = ""
        while time.time() < deadline:
            selected_uid = self._selected_contact_uid(page)
            if selected_uid and bumble_contact_id(selected_uid) == expected_contact_id:
                return selected_uid
            time.sleep(0.2)
        return selected_uid

    def _latest_incoming(self, page) -> str:
        messages = page.locator(INCOMING_MESSAGE_SELECTOR)
        if messages.count() == 0:
            return ""
        return messages.nth(messages.count() - 1).inner_text().strip()

    def _pending_incoming_group(self, page, thread_id: str, contact_id: str, expected_preview: str = "") -> list[BumbleMessage]:
        if not self._assert_selected_contact(page, contact_id, "读取消息时联系人已切换，已停止 Agent"):
            return []
        if self._thread_has_existing_messages(thread_id):
            self._log("MESSAGES_READ", "已有历史消息，跳过全量加载", data={"thread_id": thread_id})
        else:
            self._load_full_conversation_history(page)
        messages = self._stable_visible_messages(page)
        if expected_preview and not preview_matches_messages(expected_preview, messages):
            self._set_stage(
                "CONTACT_MISMATCH",
                "消息区最新内容和联系人列表预览不一致，已停止 Agent",
                ok=False,
                data={
                    "contact_id": contact_id,
                    "expected_preview": expected_preview,
                    "incoming": [message.text for message in messages if message.role == "in"][-5:],
                },
            )
            return []
        self._status["message_count"] = len(messages)
        self._status["last_out_index"] = max((item.index for item in messages if item.role == "out"), default=-1)
        inserted = self._sync_visible_messages(thread_id, contact_id, messages)
        if inserted:
            try:
                self.service.update_thread_memory_from_messages(thread_id, inserted)
                self._set_stage("MEMORY_UPDATED", "长期 memory 已更新", data={"thread_id": thread_id, "new_message_count": len(inserted)})
            except Exception as exc:
                self._set_stage("MEMORY_UPDATE_FAILED", f"长期 memory 更新失败：{exc}", ok=False, data={"thread_id": thread_id})
        self._set_stage(
            "MESSAGES_READ",
            f"读取到 {len(messages)} 条消息",
            data={"last_out_index": self._status["last_out_index"]},
        )
        pending = self.service.memory.pending_thread_messages(thread_id)
        if pending and hasattr(self.service.memory, "upsert_thread_pending_group"):
            group_hash = self.service.memory.upsert_thread_pending_group(
                thread_id,
                [str(item["platform_message_id"]) for item in pending],
                "pending",
            )
            self._status["thread_pending_group_hash"] = group_hash
        pending_by_key: dict[tuple[str, str], list[BumbleMessage]] = {}
        for message in messages:
            pending_by_key.setdefault(bumble_message_key(message), []).append(message)
        result: list[BumbleMessage] = []
        for item in pending:
            key = ("user", " ".join(str(item.get("content", "")).strip().split()))
            bucket = pending_by_key.get(key, [])
            if bucket:
                result.append(bucket.pop(0))
        return result

    def _thread_has_existing_messages(self, thread_id: str) -> bool:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "all_thread_messages"):
            return False
        try:
            return bool(memory.all_thread_messages(thread_id))
        except Exception:
            return False

    def _unsent_draft_reply_group(self, thread_id: str) -> list[dict[str, str]]:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "unsent_thread_drafts"):
            return []
        rows = memory.unsent_thread_drafts(thread_id)
        return [
            {
                "incoming": "",
                "draft": str(row.get("content", "")),
                "message_id": str(row.get("platform_message_id", "")),
                "technique": str(row.get("technique", "")),
                "reason": str(row.get("decision_reason", "")),
                "trace": "existing_draft",
            }
            for row in rows
            if str(row.get("content", "")).strip()
        ]

    def _sync_visible_messages(self, thread_id: str, contact_id: str, messages: list[BumbleMessage]) -> list[dict[str, Any]]:
        memory = getattr(self.service, "memory", None)
        if memory is None:
            return []
        payload: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "in":
                role = "user"
            elif message.role == "out":
                role = "sent"
            else:
                continue
            payload.append(
                {
                    "platform_message_id": "",
                    "role": role,
                    "content": message.text,
                    "order_index": message.index,
                }
            )
        return memory.sync_thread_messages_incremental(
            thread_id=thread_id,
            platform="bumble",
            messages=payload,
        )

    def _load_full_conversation_history(self, page, max_scrolls: int = 30) -> None:
        container = self._visible_message_container(page)
        if container is None:
            return
        last_signature = ""
        stable_rounds = 0
        for _ in range(max_scrolls):
            try:
                state = container.evaluate(
                    """node => {
                        let scrollable = node.parentElement;
                        while (scrollable && !(scrollable.scrollHeight > scrollable.clientHeight + 20)) {
                            scrollable = scrollable.parentElement;
                        }
                        if (!scrollable) {
                            return { ok: false, messageCount: node.querySelectorAll('.message').length };
                        }
                        scrollable.scrollTop = 0;
                        return {
                            ok: true,
                            scrollTop: scrollable.scrollTop,
                            scrollHeight: scrollable.scrollHeight,
                            clientHeight: scrollable.clientHeight,
                            messageCount: node.querySelectorAll('.message').length,
                            firstText: (node.querySelector('.message .message-bubble__text') || {}).innerText || '',
                        };
                    }"""
                )
            except Exception as exc:
                self._log("MESSAGES_READ", "加载历史消息失败", ok=False, data={"error": str(exc)})
                return
            signature = f"{state.get('messageCount')}:{state.get('firstText')}:{state.get('scrollTop')}:{state.get('scrollHeight')}"
            if signature == last_signature:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_signature = signature
            if stable_rounds >= 2:
                break
            time.sleep(0.35)
        try:
            container.evaluate(
                """node => {
                    let scrollable = node.parentElement;
                    while (scrollable && !(scrollable.scrollHeight > scrollable.clientHeight + 20)) {
                        scrollable = scrollable.parentElement;
                    }
                    if (scrollable) scrollable.scrollTop = scrollable.scrollHeight;
                }"""
            )
            time.sleep(0.35)
        except Exception:
            pass

    def _stable_visible_messages(self, page, timeout_seconds: float = 3.0) -> list[BumbleMessage]:
        deadline = time.time() + timeout_seconds
        last_signature: list[tuple[str, str]] | None = None
        last_messages: list[BumbleMessage] = []
        while time.time() < deadline:
            messages = self._visible_messages(page)
            signature = [(message.role, message.text) for message in messages]
            if signature and signature == last_signature:
                return messages
            last_signature = signature
            last_messages = messages
            time.sleep(0.25)
        return last_messages

    def _visible_messages(self, page) -> list[BumbleMessage]:
        container = self._visible_message_container(page)
        if container is None:
            return []
        try:
            rows = container.locator(".message").evaluate_all(
                """nodes => nodes
                    .filter(node => {
                        const style = window.getComputedStyle(node);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    })
                    .map((node, index) => {
                        const textNode = node.querySelector('.message-bubble__text');
                        return {
                            index,
                            classes: node.getAttribute('class') || '',
                            text: textNode ? textNode.innerText.trim() : '',
                        };
                    })"""
            )
        except Exception as exc:
            self._log("MESSAGES_READ", "读取消息 DOM 失败", ok=False, data={"error": str(exc)})
            return []
        messages = []
        for row in rows:
            classes = str(row.get("classes", ""))
            role = ""
            if "message--in" in classes:
                role = "in"
            elif "message--out" in classes:
                role = "out"
            if not role:
                continue
            text = str(row.get("text", "")).strip()
            if text:
                messages.append(BumbleMessage(role=role, text=text, index=int(row.get("index", len(messages)))))
        return messages

    def _visible_message_container(self, page):
        containers = page.locator(".messages-list__conversation:visible")
        count = containers.count()
        if count == 1:
            return containers.first
        if count > 1:
            self._set_stage(
                "CONTACT_MISMATCH",
                "发现多个可见消息区，已停止 Agent",
                ok=False,
                data={"visible_message_container_count": count},
            )
            return None
        self._set_stage("MESSAGES_READ", "未找到可见消息区", ok=False)
        return None

    def _visible_input_box(self, page):
        visible = page.locator(f"{INPUT_SELECTOR}:visible")
        if visible.count() > 0:
            return visible.first
        return page.locator(INPUT_SELECTOR).first

    def _contact_preview(self, contact) -> str:
        try:
            return contact.locator(".contact__message").inner_text(timeout=1000).strip()
        except Exception:
            return ""

    def _create_reply_group(
        self, thread_id: str, contact_id: str, pending_group: list[BumbleMessage], input_box=None
    ) -> list[dict[str, str]]:
        memory = getattr(self.service, "memory", None)
        profile = memory.get_contact_profile(contact_id) if memory is not None and hasattr(memory, "get_contact_profile") else {}
        thread_memory = memory.get_thread_memory(thread_id) if memory is not None and hasattr(memory, "get_thread_memory") else {}
        profile_text = "；".join(
            f"{key}:{value.get('value')}"
            for key, value in profile.get("fields", {}).items()
            if value.get("value")
        )
        memory_context = self.service._format_thread_memory(thread_memory) if hasattr(self.service, "_format_thread_memory") else ""
        total = len(pending_group)
        pending_message_ids = self._pending_message_ids_for_group(thread_id, contact_id, pending_group)
        if memory is not None and hasattr(memory, "upsert_thread_pending_group"):
            self._status["thread_pending_group_hash"] = memory.upsert_thread_pending_group(
                thread_id,
                pending_message_ids,
                "drafting",
            )
        self._fill_progress(input_box, f"正在为 {total} 条消息生成 {total} 条回复，请等待……")
        active_pending: list[BumbleMessage] = []
        active_message_ids: list[str] = []
        for index, incoming in enumerate(pending_group):
            message_key = pending_message_ids[index] if index < len(pending_message_ids) else self._stable_message_id_for_pending(thread_id, contact_id, incoming)
            if self._recently_sent(message_key):
                self._status["skipped_duplicate_count"] = int(self._status.get("skipped_duplicate_count", 0)) + 1
                self._set_stage(
                    "SKIPPED_ALREADY_SENT",
                    "消息已经发送过，跳过",
                    data={"contact_id": contact_id, "incoming": incoming.text, "message_id": message_key},
                )
                continue
            active_pending.append(incoming)
            active_message_ids.append(message_key)
        if not active_pending:
            return []

        if len(active_pending) == 1:
            incoming = active_pending[0]
            message_key = active_message_ids[0]
            cached = self._cached_or_recovered_draft(
                memory, message_key, contact_id, incoming.text, legacy_message_key=bumble_ui_message_hash(contact_id, incoming)
            )
            if cached:
                self._set_stage(
                    "DRAFT_CACHED",
                    "使用已缓存草稿",
                    data={"contact_id": contact_id, "incoming": incoming.text, "draft": cached},
                )
                return [{"incoming": incoming.text, "draft": cached, "message_id": message_key}]

        group_cache_key = self._reply_group_cache_key(thread_id, active_message_ids)
        cached_group = self._cached_reply_group(memory, group_cache_key)
        if len(cached_group) == len(active_pending):
            return cached_group

        try:
            self._set_stage(
                "DRAFT_ITEM_START",
                f"开始按语义群生成 {len(active_pending)} 条草稿",
                data={"contact_id": contact_id, "message_ids": active_message_ids, "pending_group": [item.text for item in active_pending]},
            )
            request = ReplyGroupRequest(
                thread_id=thread_id,
                contact_id=contact_id,
                platform="bumble",
                pending_messages=[item.text for item in active_pending],
                message_ids=active_message_ids,
                pending_group_context="\n".join(f"{index + 1}. {message.text}" for index, message in enumerate(active_pending)),
                memory_context=memory_context,
                profile_context=profile_text,
                contact_identity="Bumble联系人",
                contact_profile=profile_text,
            )
            progress = self._reply_progress(input_box, len(active_pending), len(active_pending))
            try:
                payload = self.service.create_reply_group(request, progress_callback=progress)
            except TypeError as exc:
                if "progress_callback" not in str(exc):
                    raise
                payload = self.service.create_reply_group(request)
        except Exception as exc:
            self._set_stage(
                "DRAFT_FAILED",
                f"草稿生成失败：{exc}",
                ok=False,
                data={"contact_id": contact_id, "pending_group": [item.text for item in active_pending]},
            )
            return []
        try:
            reply_group = self._normalize_service_reply_group(payload, active_pending, active_message_ids)
        except Exception as exc:
            self._set_stage(
                "DRAFT_FAILED",
                f"草稿生成失败：{exc}",
                ok=False,
                data={"contact_id": contact_id, "pending_group": [item.text for item in active_pending]},
            )
            return []
        self._cache_reply_group(memory, group_cache_key, contact_id, active_pending, reply_group)
        if len(active_pending) == 1 and reply_group:
            self._draft_cache[active_message_ids[0]] = reply_group[0]["draft"]
            if memory is not None and hasattr(memory, "cache_draft"):
                memory.cache_draft(active_message_ids[0], contact_id, active_pending[0].text, reply_group[0]["draft"])
        self._set_stage(
            "DRAFT_CACHED",
            f"语义群 {len(reply_group)}/{len(active_pending)} 条草稿生成并缓存",
            data={"contact_id": contact_id, "reply_group": reply_group},
        )
        if reply_group and memory is not None and hasattr(memory, "upsert_thread_pending_group"):
            self._status["thread_pending_group_hash"] = memory.upsert_thread_pending_group(
                thread_id,
                [str(item["message_id"]) for item in reply_group],
                "drafted",
            )
        return reply_group

    def _reply_group_cache_key(self, thread_id: str, message_ids: list[str]) -> str:
        raw = f"{thread_id}:{'|'.join(message_ids)}".encode("utf-8")
        return f"group:{hashlib.sha256(raw).hexdigest()}"

    def _cached_reply_group(self, memory, group_cache_key: str) -> list[dict[str, str]]:
        cached = self._draft_cache.get(group_cache_key, "")
        if not cached and memory is not None and hasattr(memory, "get_cached_draft"):
            cached = memory.get_cached_draft(group_cache_key)
        if not cached:
            return []
        try:
            parsed = json.loads(cached)
        except Exception:
            return []
        if not isinstance(parsed, list):
            return []
        reply_group = [
            {"incoming": str(item.get("incoming", "")), "draft": str(item.get("draft", "")), "message_id": str(item.get("message_id", ""))}
            for item in parsed
            if isinstance(item, dict) and str(item.get("draft", "")).strip()
        ]
        if reply_group:
            self._set_stage("DRAFT_CACHED", "使用语义群缓存草稿", data={"reply_group": reply_group})
        return reply_group

    def _cache_reply_group(
        self,
        memory,
        group_cache_key: str,
        contact_id: str,
        pending_group: list[BumbleMessage],
        reply_group: list[dict[str, str]],
    ) -> None:
        if not reply_group:
            return
        raw = json.dumps(reply_group, ensure_ascii=False)
        self._draft_cache[group_cache_key] = raw
        if memory is not None and hasattr(memory, "cache_draft"):
            incoming = "\n".join(item.text for item in pending_group)
            memory.cache_draft(group_cache_key, contact_id, incoming, raw)

    def _normalize_service_reply_group(
        self,
        payload: dict[str, Any],
        pending_group: list[BumbleMessage],
        message_ids: list[str],
    ) -> list[dict[str, str]]:
        rows = payload.get("reply_group", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or len(rows) != len(pending_group):
            raise ValueError("reply_group length mismatch")
        reply_group = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError("reply_group item must be object")
            draft = str(row.get("draft", "")).strip()
            if not draft:
                raise ValueError("reply_group draft is empty")
            reply_group.append(
                {
                    "incoming": pending_group[index].text,
                    "draft": draft,
                    "message_id": message_ids[index],
                    "technique": str(row.get("technique", "")),
                    "reason": str(row.get("reason", "")),
                }
            )
        return reply_group

    def _pending_message_ids_for_group(self, thread_id: str, contact_id: str, pending_group: list[BumbleMessage]) -> list[str]:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "pending_thread_messages"):
            return [self._stable_message_id_for_pending(thread_id, contact_id, item) for item in pending_group]
        rows = list(memory.pending_thread_messages(thread_id))
        ids: list[str] = []
        for message in pending_group:
            normalized = " ".join((message.text or "").strip().split())
            matched_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row.get("role") == "user"
                    and " ".join(str(row.get("content", "")).strip().split()) == normalized
                ),
                -1,
            )
            if matched_index >= 0:
                row = rows.pop(matched_index)
                ids.append(str(row.get("platform_message_id", "")))
            else:
                ids.append(self._stable_message_id_for_pending(thread_id, contact_id, message))
        return ids

    def _stable_message_id_for_pending(self, thread_id: str, contact_id: str, incoming: BumbleMessage) -> str:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "stable_thread_message_id"):
            return bumble_ui_message_hash(contact_id, incoming)
        normalized = " ".join((incoming.text or "").strip().split())
        occurrence = 0
        if hasattr(memory, "all_thread_messages"):
            for row in memory.all_thread_messages(thread_id):
                if row.get("role") != "user":
                    continue
                if " ".join(str(row.get("content", "")).strip().split()) == normalized:
                    occurrence += 1
        return memory.stable_thread_message_id(thread_id, "user", normalized, max(occurrence, 1))

    def _cached_or_recovered_draft(
        self,
        memory,
        message_key: str,
        contact_id: str,
        incoming: str,
        legacy_message_key: str = "",
    ) -> str:
        if message_key in self._draft_cache:
            return self._draft_cache[message_key]
        if memory is None:
            return ""
        draft = memory.get_cached_draft(message_key) if hasattr(memory, "get_cached_draft") else ""
        if draft:
            self._draft_cache[message_key] = draft
            return draft
        draft = memory.recover_draft_for_message(message_key, contact_id) if hasattr(memory, "recover_draft_for_message") else ""
        if not draft and legacy_message_key and legacy_message_key != message_key:
            draft = memory.get_cached_draft(legacy_message_key) if hasattr(memory, "get_cached_draft") else ""
            if not draft and hasattr(memory, "recover_draft_for_message"):
                draft = memory.recover_draft_for_message(legacy_message_key, contact_id)
        if draft and hasattr(memory, "cache_draft"):
            memory.cache_draft(message_key, contact_id, incoming, draft)
        if draft:
            self._draft_cache[message_key] = draft
        return draft

    def _recently_sent(self, message_key: str) -> bool:
        sent_at = self._sent_message_at.get(message_key, 0)
        return message_key in self._sent_messages and time.time() - sent_at < 120

    def _reply_progress(self, input_box, pending_count: int, reply_count: int):
        def progress(stage: str) -> None:
            if stage == "正在数据检索 RAG 中，请等待……":
                self._fill_progress(input_box, stage)
                return
            if stage == "正在分析对话中，请等待……":
                self._fill_progress(input_box, stage)
                return
            self._fill_progress(input_box, f"正在为 {pending_count} 条消息合并生成 {reply_count} 条回复，请等待……")

        return progress

    def _fill_or_send_reply_group(
        self,
        input_box,
        reply_group: list[dict[str, str]],
        auto_send: bool,
        contact_id: str = "",
        page=None,
        expected_preview: str = "",
        thread_id: str = "",
    ) -> int:
        if not reply_group:
            return 0
        if page is not None and contact_id:
            ok, data = self._wait_for_contact_binding(page, contact_id, "", expected_preview, timeout_seconds=2.0)
            if not ok:
                self._set_stage(
                    "CONTACT_MISMATCH",
                    "填入或发送前联系人绑定校验失败，丢弃草稿",
                    ok=False,
                    data=data,
                )
                memory = getattr(self.service, "memory", None)
                group_hash = self._status.get("thread_pending_group_hash", "")
                if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
                    memory.thread_pending_group_status(str(group_hash), "discarded")
                return 0
        if not auto_send:
            input_box.fill(reply_group[0]["draft"])
            return 0
        sent = 0
        memory = getattr(self.service, "memory", None)
        for item in reply_group:
            if self._stop.is_set():
                self._set_stage("STOPPING", "停止指令已生效，停止发送剩余草稿")
                break
            if page is not None and contact_id and not self._assert_selected_contact(page, contact_id, "发送前联系人已切换，已停止 Agent"):
                break
            before_out_count = self._outgoing_count(page)
            input_box.fill(item["draft"])
            input_box.press("Enter")
            if page is not None and not self._wait_for_outgoing(page, before_out_count, item["draft"]):
                self._set_stage(
                    "SEND_NOT_CONFIRMED",
                    "按 Enter 后未确认新 outgoing，停止发送",
                    ok=False,
                    data={"contact_id": contact_id, "draft": item["draft"], "message_id": item.get("message_id", "")},
                )
                group_hash = self._status.get("thread_pending_group_hash", "")
                if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
                    memory.thread_pending_group_status(str(group_hash), "discarded")
                break
            sent += 1
            message_key = item.get("message_id", "")
            if message_key:
                self._sent_messages.add(message_key)
                self._sent_message_at[message_key] = time.time()
                if memory is not None:
                    if thread_id and hasattr(memory, "mark_thread_sent"):
                        memory.mark_thread_sent(thread_id, message_key)
                    else:
                        memory.mark_sent(message_key, contact_id)
            time.sleep(0.4)
        group_hash = self._status.get("thread_pending_group_hash", "")
        if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
            memory.thread_pending_group_status(str(group_hash), "sent" if sent == len(reply_group) else "discarded")
        return sent

    def _outgoing_count(self, page) -> int:
        if page is None:
            return 0
        try:
            return page.locator(".messages-list__conversation .message.message--out").count()
        except Exception:
            return 0

    def _latest_outgoing_text(self, page) -> str:
        try:
            outgoing = page.locator(".messages-list__conversation .message.message--out .message-bubble__text")
            if outgoing.count() == 0:
                return ""
            return outgoing.nth(outgoing.count() - 1).inner_text(timeout=1000).strip()
        except Exception:
            return ""

    def _wait_for_outgoing(self, page, before_out_count: int, draft: str, timeout_seconds: float = 8.0) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._outgoing_count(page) > before_out_count:
                latest = self._latest_outgoing_text(page)
                if not latest or latest == draft or draft in latest:
                    return True
            time.sleep(0.25)
        return False

    def _ensure_profile(self, page, thread_id: str, contact_id: str, name: str, refresh: bool) -> list[dict[str, Any]]:
        self._update_contact_status(contact_id, profile_stage="PROFILE_CHECKING")
        self._set_stage("PROFILE_CHECKING", "检查 Bumble 画像", data={"contact_id": contact_id})
        profile = self.service.memory.get_contact_profile(contact_id)
        has_bumble_profile = self.service.memory.has_profile_audit_text(contact_id)
        if has_bumble_profile and not refresh:
            self._update_contact_status(contact_id, profile_stage="PROFILE_SKIPPED", profile_updates_count=0)
            self._set_stage("PROFILE_SKIPPED", "已有 Bumble 画像，跳过读取", data={"contact_id": contact_id})
            return []
        applied = self.service.memory.apply_profile_updates(
            contact_id,
            categorized_updates([_update("name", name, 0.95, "bumble_contact", name)]),
        )
        html = self._profile_html(page)
        if not html:
            self._update_contact_status(contact_id, profile_stage="PROFILE_READ_FAILED", profile_updates_count=len(applied))
            self._set_stage("PROFILE_READ_FAILED", "未读取到 Bumble profile DOM", ok=False, data={"contact_id": contact_id})
            return applied
        profile_text = extract_bumble_profile_text(html)
        self.service.memory.record_initial_profile_text(contact_id, name, profile_text)
        updates = extract_bumble_profile_updates(html, source="bumble_profile")
        profile_updates = applied + self.service.memory.apply_profile_updates(contact_id, updates)
        audit = self.service.memory.assess_profile_coverage(contact_id, name, profile_text)
        self._update_contact_status(contact_id, profile_stage="PROFILE_READ", profile_updates_count=len(profile_updates))
        self._set_stage(
            "PROFILE_READ",
            f"读取并更新 {len(profile_updates)} 条画像字段",
            data={"contact_id": contact_id, "profile_audit_status": audit.get("status"), "html_len": len(html), "profile_text_len": len(profile_text), "profile_text_preview": profile_text[:120]},
        )
        return profile_updates

    def _profile_html(self, page) -> str:
        selectors = [PROFILE_SELECTOR, ".profile", ".profile__section"]
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count() > 0:
                # Wait for profile content to finish rendering (SPA async load).
                # profile__name or profile__age appearing signals the panel is ready.
                try:
                    page.wait_for_selector(
                        ".profile__name, .profile__age, .profile__about, .pill__title",
                        timeout=3000,
                        state="attached",
                    )
                except Exception:
                    time.sleep(0.8)
                return locator.first.evaluate("element => element.parentElement ? element.parentElement.innerHTML : element.innerHTML")
        for selector in [".contact__avatar", ".profile__name", "[data-qa-role='avatar']"]:
            try:
                target = page.locator(selector).first
                if target.count() > 0:
                    target.click()
                    time.sleep(0.8)
                    profile = page.locator(PROFILE_SELECTOR)
                    if profile.count() > 0:
                        return profile.first.evaluate("element => element.parentElement ? element.parentElement.innerHTML : element.innerHTML")
            except Exception as exc:
                self._status["last_error"] = str(exc)
        return ""
