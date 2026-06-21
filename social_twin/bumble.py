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
from .service import DigitalTwinService, DraftRequest


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
    "PROFILE_CHECKING": 300,
    "PROFILE_SKIPPED": 304,
    "PROFILE_READ": 310,
    "PROFILE_READ_FAILED": 399,
    "MESSAGES_READ": 400,
    "PENDING_GROUP_FOUND": 410,
    "NO_PENDING_GROUP": 411,
    "DRAFTING": 500,
    "DRAFT_ITEM_START": 501,
    "DRAFT_FAILED": 509,
    "DRAFTED": 510,
    "FILLED_TEXTAREA": 600,
    "SENT_BY_ENTER": 610,
    "IDLE": 700,
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
        "platform_name": "基础资料",
        "age": "基础资料",
        "gender": "基础资料",
        "height": "基础资料",
        "zodiac": "基础资料",
        "education": "基础资料",
        "location": "基础资料",
        "hometown": "基础资料",
        "exercise": "生活方式",
        "drinking": "生活方式",
        "religion": "生活方式",
        "politics": "生活方式",
        "dating_intentions": "关系意图",
        "family_plans": "关系意图",
        "social_preferences": "关系意图",
        "bio": "自我介绍",
        "profile_prompts": "问答内容",
        "hobbies": "兴趣偏好",
        "interest_tags": "兴趣偏好",
        "personality": "兴趣偏好",
        "photo_urls": "照片信息",
        "photo_description": "照片信息",
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


def is_turn_label(text: str) -> bool:
    normalized = " ".join((text or "").lower().split())
    return any(keyword.lower() in normalized for keyword in TURN_LABEL_KEYWORDS)


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
        updates.append(_update("platform_name", parser.name, 0.95, source, parser.name))
    if parser.age:
        updates.append(_update("age", parser.age, 0.9, source, parser.age))
    about = "\n".join(parser.about).strip()
    if about:
        updates.append(_update("bio", about, 0.86, source, about))
        updates.extend(ProfileAnalyzer.extract_rules(ProfileAnalyzer.__new__(ProfileAnalyzer), about, source=source))
    for badge in dict.fromkeys(item.strip() for item in parser.badges if item.strip()):
        classified = _classify_badge(badge)
        if classified:
            field, confidence = classified
            updates.append(_update(field, badge, confidence, source, badge))
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
        photos = json.dumps(list(dict.fromkeys(parser.photos)), ensure_ascii=False)
        updates.append(_update("photo_urls", photos, 0.88, source, photos))
    return categorized_updates(_dedupe_updates(updates))


def _classify_badge(value: str) -> tuple[str, float] | None:
    if value.endswith("cm") or value.endswith("厘米"):
        return "height", 0.94
    if value in ZODIAC_VALUES:
        return "zodiac", 0.9
    if value in GENDER_VALUES:
        return "gender", 0.9
    if any(marker.lower() in value.lower() for marker in EDUCATION_MARKERS):
        return "education", 0.86
    if value in EXERCISE_VALUES:
        return "exercise", 0.76
    if value in DRINKING_VALUES:
        return "drinking", 0.78
    if value in INTENTION_VALUES:
        return "dating_intentions", 0.84
    if value in FAMILY_VALUES:
        return "family_plans", 0.82
    if value in RELIGION_VALUES:
        return "religion", 0.8
    if value in POLITICS_VALUES:
        return "politics", 0.8
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
            self._seen_messages: set[str] = memory.load_sent_hashes(since_days=7)
        else:
            self._seen_messages = set()
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
        self._status["running"] = False
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
                    contact = self._first_action_contact(page)
                    if contact is None:
                        self._set_stage("NO_CONTACT", "未找到轮到您了联系人", data={"contact_count": self._status["contact_count"]})
                        time.sleep(poll_seconds)
                        continue
                    uid = contact.get_attribute("data-qa-uid") or "bumble_unknown"
                    contact_id = bumble_contact_id(uid)
                    name = contact.get_attribute("data-qa-name") or uid
                    self._update_contact_status(contact_id, name=name, stage="CONTACT_FOUND")
                    self._set_stage("CONTACT_FOUND", "找到待回复联系人", data={"contact_id": contact_id, "name": name})
                    self._open_contact(contact)
                    time.sleep(0.8)
                    self._update_contact_status(contact_id, stage="CONTACT_OPENED")
                    self._set_stage("CONTACT_OPENED", "已打开联系人会话", data={"contact_id": contact_id, "name": name})
                    self.service.memory.upsert_contact(contact_id=contact_id, display_name=name)
                    input_box = page.locator(INPUT_SELECTOR).first
                    self._fill_progress(input_box, "正在读取 profile 生成画像中，请等待……")
                    profile_updates = self._ensure_profile(page, contact_id, name, config.refresh_profile)
                    self._fill_progress(input_box, "正在分析对话中，请等待……")
                    pending_group = self._pending_incoming_group(page)
                    self._status["pending_group_count"] = len(pending_group)
                    self._update_contact_status(
                        contact_id,
                        message_count=self._status["message_count"],
                        last_out_index=self._status["last_out_index"],
                        pending_group_count=len(pending_group),
                        pending_group=[item.text for item in pending_group],
                    )
                    if not pending_group:
                        self._update_contact_status(contact_id, stage="NO_PENDING_GROUP")
                        self._set_stage(
                            "NO_PENDING_GROUP",
                            "没有待回复消息组",
                            data={
                                "message_count": self._status["message_count"],
                                "last_out_index": self._status["last_out_index"],
                            },
                        )
                        time.sleep(poll_seconds)
                        continue
                    self._set_stage(
                        "PENDING_GROUP_FOUND",
                        f"找到 {len(pending_group)} 条待回复 incoming",
                        data={"pending_group": [item.text for item in pending_group]},
                    )
                    self._update_contact_status(contact_id, stage="DRAFTING")
                    self._set_stage("DRAFTING", "开始逐条生成草稿", data={"pending_group_count": len(pending_group)})
                    reply_group = self._create_reply_group(contact_id, pending_group, input_box)
                    if not reply_group:
                        self._update_contact_status(contact_id, stage="IDLE")
                        self._set_stage("IDLE", "待回复消息都已处理过，跳过")
                        time.sleep(poll_seconds)
                        continue
                    self._update_contact_status(contact_id, stage="DRAFTED", last_reply_group=reply_group)
                    self._set_stage("DRAFTED", f"生成 {len(reply_group)} 条草稿")
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
                    sent = self._fill_or_send_reply_group(input_box, reply_group, auto_send)
                    self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
                    if sent:
                        self._update_contact_status(contact_id, stage="SENT_BY_ENTER", sent_count=sent)
                        self._set_stage("SENT_BY_ENTER", f"已按 Enter 发送 {sent} 条")
                    else:
                        self._update_contact_status(contact_id, stage="FILLED_TEXTAREA", sent_count=0)
                        self._set_stage("FILLED_TEXTAREA", "自动发送关闭，只填入第一条草稿")
                    time.sleep(poll_seconds)
                browser.close()
        except Exception as exc:
            self._set_stage("ERROR", str(exc), ok=False, data={"traceback": traceback.format_exc()})
        finally:
            self._status["running"] = False
            if self._status.get("stage") != "ERROR":
                self._set_stage("STOPPED", "Bumble Agent 已停止")

    def _first_action_contact(self, page):
        contacts = page.locator(ACTION_CONTACT_SELECTOR)
        self._status["contact_count"] = contacts.count()
        candidates = []
        for index in range(contacts.count()):
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
                    "move_text": move_text,
                }
            )
            if is_turn_label(move_text):
                self._log("SCANNING_CONTACTS", "命中待回复联系人", data={"candidate": candidates[-1]})
                return item
        self._log("SCANNING_CONTACTS", "候选联系人未命中文本", data={"candidates": candidates[:20]})
        return None

    def _open_contact(self, contact) -> None:
        classes = contact.get_attribute("class") or ""
        if "is-selected" in classes:
            self._log("CONTACT_FOUND", "联系人已选中，跳过点击")
            return
        try:
            contact.click(timeout=3000)
            return
        except Exception as exc:
            self._log("CONTACT_FOUND", "普通点击失败，改用 JS click", ok=False, data={"error": str(exc)})
        contact.evaluate("element => element.click()")

    def _latest_incoming(self, page) -> str:
        messages = page.locator(INCOMING_MESSAGE_SELECTOR)
        if messages.count() == 0:
            return ""
        return messages.nth(messages.count() - 1).inner_text().strip()

    def _pending_incoming_group(self, page) -> list[BumbleMessage]:
        messages = []
        nodes = page.locator(".messages-list__conversation .message")
        for index in range(nodes.count()):
            node = nodes.nth(index)
            classes = node.get_attribute("class") or ""
            role = ""
            if "message--in" in classes:
                role = "in"
            elif "message--out" in classes:
                role = "out"
            if not role:
                continue
            try:
                text = node.locator(".message-bubble__text").inner_text(timeout=1000).strip()
            except Exception:
                text = ""
            if text:
                messages.append(BumbleMessage(role=role, text=text, index=index))
        self._status["message_count"] = len(messages)
        self._status["last_out_index"] = max((item.index for item in messages if item.role == "out"), default=-1)
        self._set_stage(
            "MESSAGES_READ",
            f"读取到 {len(messages)} 条消息",
            data={"last_out_index": self._status["last_out_index"]},
        )
        return extract_pending_incoming_group(messages)

    def _create_reply_group(
        self, contact_id: str, pending_group: list[BumbleMessage], input_box=None
    ) -> list[dict[str, str]]:
        memory = getattr(self.service, "memory", None)
        profile = memory.get_contact_profile(contact_id) if memory is not None else {}
        profile_text = "；".join(
            f"{key}:{value.get('value')}"
            for key, value in profile.get("fields", {}).items()
            if value.get("value")
        )
        total = len(pending_group)
        reply_group = []
        for i, incoming in enumerate(pending_group):
            message_key = bumble_message_hash(contact_id, incoming.text)
            if message_key in self._seen_messages:
                self._status["skipped_duplicate_count"] = int(self._status.get("skipped_duplicate_count", 0)) + 1
                continue
            self._fill_progress(input_box, f"正在生成第 {i + 1}/{total} 句回复，请等待……")
            try:
                self._set_stage(
                    "DRAFT_ITEM_START",
                    f"开始生成第 {i + 1}/{total} 条草稿",
                    data={"contact_id": contact_id, "incoming": incoming.text, "message_id": message_key},
                )
                request = DraftRequest(
                    contact_id=contact_id,
                    message=incoming.text,
                    channel="bumble",
                    message_id=message_key,
                    contact_identity="Bumble联系人",
                    contact_profile=profile_text,
                )
                progress = self._reply_progress(input_box, i + 1, total)
                try:
                    payload = self.service.create_draft(request, progress_callback=progress)
                except TypeError as exc:
                    if "progress_callback" not in str(exc):
                        raise
                    payload = self.service.create_draft(request)
            except Exception as exc:
                self._set_stage(
                    "DRAFT_FAILED",
                    f"草稿生成失败：{exc}",
                    ok=False,
                    data={"contact_id": contact_id, "incoming": incoming.text},
                )
                continue
            draft = payload["draft"]
            self._set_stage(
                "DRAFTED",
                f"第 {i + 1}/{total} 条草稿生成完成",
                data={"contact_id": contact_id, "incoming": incoming.text, "draft": draft},
            )
            self._seen_messages.add(message_key)
            if memory is not None:
                memory.mark_sent(message_key, contact_id)
            reply_group.append({"incoming": incoming.text, "draft": draft, "message_id": message_key})
        return reply_group

    def _reply_progress(self, input_box, index: int, total: int):
        def progress(stage: str) -> None:
            if stage == "正在数据检索 RAG 中，请等待……":
                self._fill_progress(input_box, stage)
                return
            if stage == "正在分析对话中，请等待……":
                self._fill_progress(input_box, stage)
                return
            self._fill_progress(input_box, f"正在生成第 {index}/{total} 句回复，请等待……")

        return progress

    def _fill_or_send_reply_group(self, input_box, reply_group: list[dict[str, str]], auto_send: bool) -> int:
        if not reply_group:
            return 0
        if not auto_send:
            input_box.fill(reply_group[0]["draft"])
            return 0
        sent = 0
        for item in reply_group:
            input_box.fill(item["draft"])
            input_box.press("Enter")
            sent += 1
            time.sleep(0.4)
        return sent

    def _ensure_profile(self, page, contact_id: str, name: str, refresh: bool) -> list[dict[str, Any]]:
        self._update_contact_status(contact_id, profile_stage="PROFILE_CHECKING")
        self._set_stage("PROFILE_CHECKING", "检查 Bumble 画像", data={"contact_id": contact_id})
        profile = self.service.memory.get_contact_profile(contact_id)
        fields = profile.get("fields", {})
        has_bumble_profile = any(field in fields for field in ("photo_urls", "bio", "profile_prompts", "photo_description"))
        if has_bumble_profile and not refresh:
            self._update_contact_status(contact_id, profile_stage="PROFILE_SKIPPED", profile_updates_count=0)
            self._set_stage("PROFILE_SKIPPED", "已有 Bumble 画像，跳过读取", data={"contact_id": contact_id})
            return []
        applied = self.service.memory.apply_profile_updates(
            contact_id,
            categorized_updates([_update("platform_name", name, 0.95, "bumble_contact", name)]),
        )
        html = self._profile_html(page)
        if not html:
            self._update_contact_status(contact_id, profile_stage="PROFILE_READ_FAILED", profile_updates_count=len(applied))
            self._set_stage("PROFILE_READ_FAILED", "未读取到 Bumble profile DOM", ok=False, data={"contact_id": contact_id})
            return applied
        updates = extract_bumble_profile_updates(html, source="bumble_profile")
        profile_updates = applied + self.service.memory.apply_profile_updates(contact_id, updates)
        self._update_contact_status(contact_id, profile_stage="PROFILE_READ", profile_updates_count=len(profile_updates))
        self._set_stage("PROFILE_READ", f"读取并更新 {len(profile_updates)} 条画像字段", data={"contact_id": contact_id})
        return profile_updates

    def _profile_html(self, page) -> str:
        selectors = [PROFILE_SELECTOR, ".profile", ".profile__section"]
        for selector in selectors:
            locator = page.locator(selector)
            if locator.count() > 0:
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
