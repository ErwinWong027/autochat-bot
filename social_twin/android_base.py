from __future__ import annotations

import hashlib
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .service import DigitalTwinService, DraftRequest


ANDROID_STAGE_CODES = {
    "INIT": 0,
    "CONNECTING_ADB": 85,
    "LAUNCHING_APP": 90,
    "APP_READY": 110,
    "SCANNING_CONTACTS": 200,
    "NO_CONTACT": 204,
    "NOTIFICATION_FALLBACK": 205,
    "CONTACT_FOUND": 210,
    "CONTACT_OPENED": 220,
    "FETCHING_PROFILE": 225,
    "PROFILE_FETCHED": 230,
    "PROFILE_SKIPPED": 231,
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
    "SENT": 610,
    "SEND_NOT_CONFIRMED": 611,
    "IDLE": 700,
    "STOPPED": 800,
    "ERROR": 900,
}


@dataclass(frozen=True)
class AndroidMessage:
    role: str   # "in" | "out"
    text: str
    index: int


@dataclass(frozen=True)
class AndroidConfig:
    adb_address: str = ""
    auto_send_enabled: bool | None = None
    poll_seconds: int = 0


def android_message_hash(contact_id: str, incoming_text: str) -> str:
    raw = f"{contact_id}:{incoming_text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def extract_pending_incoming_group(messages: list[AndroidMessage]) -> list[AndroidMessage]:
    """Return the trailing block of 'in' messages after the last 'out' message."""
    last_out = -1
    for i, msg in enumerate(messages):
        if msg.role == "out":
            last_out = i
    return [msg for msg in messages[last_out + 1:] if msg.role == "in"]


class AndroidBaseConnector:
    app_name: str = "android"
    package_name: str = ""
    channel: str = "android"

    def __init__(self, service: DigitalTwinService) -> None:
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
        self._contact_last_preview: dict[str, str] = {}
        self._status: dict[str, Any] = self._initial_status()

    def _initial_status(self) -> dict[str, Any]:
        return {
            "running": False,
            "stage": "INIT",
            "status_code": ANDROID_STAGE_CODES["INIT"],
            "app": self.app_name,
            "last_error": "",
            "last_contact_id": "",
            "last_contact_name": "",
            "last_draft": "",
            "pending_group_count": 0,
            "skipped_duplicate_count": 0,
            "sent_count": 0,
            "draft_count": 0,
            "contact_count": 0,
            "contacts": {},
            "logs": [],
        }

    def run(self, config: AndroidConfig | None = None) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._status = self._initial_status()
        self._status["running"] = True
        self._set_stage("INIT", f"{self.app_name} Android Agent 启动")
        self._thread = threading.Thread(
            target=self._loop,
            args=(config or AndroidConfig(),),
            daemon=True,
        )
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
        self._status["status_code"] = ANDROID_STAGE_CODES.get(stage, -1)
        if not ok:
            self._status["last_error"] = message
        self._log(stage, message, ok=ok, data=data or {})

    def _log(self, stage: str, message: str, ok: bool = True, data: dict[str, Any] | None = None) -> None:
        logs = list(self._status.get("logs", []))
        logs.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "status_code": ANDROID_STAGE_CODES.get(stage, -1),
            "ok": ok,
            "message": message,
            "data": data or {},
        })
        self._status["logs"] = logs[-100:]

    def _update_contact_status(self, contact_id: str, **updates: Any) -> None:
        contacts = dict(self._status.get("contacts", {}))
        current = dict(contacts.get(contact_id, {}))
        current.update(updates)
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        contacts[contact_id] = current
        self._status["contacts"] = contacts

    def _loop(self, config: AndroidConfig) -> None:
        try:
            import uiautomator2 as u2
        except Exception as exc:
            self._status.update({"running": False, "last_error": f"uiautomator2不可用：{exc}"})
            return

        settings = self.service.settings
        adb_address = config.adb_address or settings.android_adb_address
        request_auto_send = True if config.auto_send_enabled is None else config.auto_send_enabled
        auto_send = settings.android_auto_send_enabled and request_auto_send
        poll_seconds = config.poll_seconds or settings.android_poll_seconds

        try:
            self._set_stage("CONNECTING_ADB", f"正在连接 ADB: {adb_address or '(auto)'}")
            device = u2.connect(adb_address) if adb_address else u2.connect()
            # 保持屏幕常亮，防止 UI 自动化失败
            device.shell("settings put global stay_on_while_plugged_in 3")
            device.shell("settings put system screen_off_timeout 2147483647")
            device.screen_on()

            self._set_stage("LAUNCHING_APP", f"启动 {self.app_name} ({self.package_name})")
            device.app_start(self.package_name)
            time.sleep(2.5)
            self._set_stage("APP_READY", f"{self.app_name} 已就绪", data={"adb_address": adb_address})

            while not self._stop.is_set():
                self._set_stage("SCANNING_CONTACTS", f"扫描 {self.app_name} 未读消息")
                try:
                    candidates = self._find_unread_contacts(device)
                except Exception as exc:
                    self._log("SCANNING_CONTACTS", f"UI扫描失败，转通知栏：{exc}", ok=False)
                    self._set_stage("NOTIFICATION_FALLBACK", "使用通知栏兜底扫描")
                    candidates = self._find_via_notifications(device)

                self._status["contact_count"] = len(candidates)

                if not candidates:
                    self._set_stage("NO_CONTACT", "未发现未读联系人")
                    time.sleep(poll_seconds)
                    continue

                any_processed = False
                for candidate in candidates:
                    if self._stop.is_set():
                        break
                    contact_id = f"{self.app_name}:{candidate['contact_id']}"
                    name = candidate.get("name", candidate["contact_id"])
                    cooldown = max(30, poll_seconds * 2)
                    if time.time() - self._contact_processed_at.get(contact_id, 0) < cooldown:
                        self._log("SCANNING_CONTACTS", "联系人刚处理过，本轮跳过", data={"contact_id": contact_id})
                        continue
                    completed = self._process_contact(device, contact_id, name, auto_send)
                    if completed:
                        self._contact_processed_at[contact_id] = time.time()
                        raw_id = candidate["contact_id"]
                        if "preview" in candidate:
                            self._contact_last_preview[raw_id] = candidate["preview"]
                        any_processed = True
                    else:
                        self._stop.set()
                        break

                if not any_processed:
                    time.sleep(poll_seconds)
                    continue
                time.sleep(poll_seconds)

        except Exception as exc:
            self._set_stage("ERROR", str(exc), ok=False, data={"traceback": traceback.format_exc()})
        finally:
            self._status["running"] = False
            if self._status.get("stage") != "ERROR":
                self._set_stage("STOPPED", f"{self.app_name} Android Agent 已停止")

    def _process_contact(self, device, contact_id: str, name: str, auto_send: bool) -> bool:
        self._status.update({"last_contact_id": contact_id, "last_contact_name": name})
        self._update_contact_status(contact_id, name=name, stage="CONTACT_FOUND")
        self._set_stage("CONTACT_FOUND", "找到待回复联系人", data={"contact_id": contact_id, "name": name})
        self.service.memory.upsert_contact(contact_id=contact_id, display_name=name)

        if not self._open_conversation(device, contact_id, name):
            self._log("CONTACT_FOUND", "无法打开会话，跳过", ok=False, data={"contact_id": contact_id})
            return True

        self._update_contact_status(contact_id, stage="CONTACT_OPENED")
        self._set_stage("CONTACT_OPENED", "已打开联系人会话")

        self._fetch_profile_if_needed(device, contact_id)
        time.sleep(1.0)  # profile 返回后给聊天界面时间重新渲染

        try:
            messages = self._read_conversation(device, contact_id)
        except Exception as exc:
            self._set_stage("MESSAGES_READ", f"读取消息失败：{exc}", ok=False)
            return True

        self._set_stage("MESSAGES_READ", f"读取到 {len(messages)} 条消息")
        pending_group = extract_pending_incoming_group(messages)
        self._status["pending_group_count"] = len(pending_group)
        self._update_contact_status(
            contact_id,
            pending_group_count=len(pending_group),
            pending_group=[m.text for m in pending_group],
        )

        if not pending_group:
            self._update_contact_status(contact_id, stage="NO_PENDING_GROUP")
            self._set_stage("NO_PENDING_GROUP", "没有待回复消息组")
            return True

        self._set_stage(
            "PENDING_GROUP_FOUND",
            f"找到 {len(pending_group)} 条待回复消息",
            data={"pending_group": [m.text for m in pending_group]},
        )
        self._update_contact_status(contact_id, stage="DRAFTING")
        self._set_stage("DRAFTING", "开始生成草稿")

        reply_group = self._create_reply_group(contact_id, pending_group)
        if not reply_group:
            self._set_stage("NO_UNSENT_DRAFT", "没有未发送草稿")
            return True

        self._status.update({
            "last_draft": reply_group[-1]["draft"],
            "draft_count": int(self._status.get("draft_count", 0)) + len(reply_group),
        })
        self._update_contact_status(contact_id, stage="DRAFTED", last_reply_group=reply_group)
        self._set_stage("DRAFTED", f"准备 {len(reply_group)} 条草稿")

        sent = 0
        memory = getattr(self.service, "memory", None)
        if auto_send:
            for item in reply_group:
                try:
                    ok = self._send_reply(device, item["draft"])
                except Exception as exc:
                    self._set_stage("SEND_NOT_CONFIRMED", f"发送异常：{exc}", ok=False)
                    return False
                if not ok:
                    self._set_stage(
                        "SEND_NOT_CONFIRMED",
                        "发送未确认，已停止 Agent",
                        ok=False,
                        data={"contact_id": contact_id, "draft": item["draft"]},
                    )
                    return False
                sent += 1
                message_key = item.get("message_id", "")
                if message_key:
                    self._sent_messages.add(message_key)
                    self._sent_message_at[message_key] = time.time()
                    if memory is not None:
                        memory.mark_sent(message_key, contact_id)
                time.sleep(0.5)
        else:
            # Fill only the first draft without sending
            if reply_group:
                try:
                    self._fill_reply(device, reply_group[0]["draft"])
                except Exception:
                    pass

        self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
        self._update_contact_status(contact_id, stage="SENT", sent_count=sent)
        self._set_stage("SENT", f"已发送 {sent} 条")
        return True

    # ------------------------------------------------------------------
    # Subclasses must implement these three methods
    # ------------------------------------------------------------------

    def _find_unread_contacts(self, device) -> list[dict[str, Any]]:
        """Scan the app's chat list and return contacts with unread messages.

        Returns a list of dicts: [{"contact_id": str, "name": str}, ...]
        contact_id must be unique within the app (e.g., username or numeric id).
        The caller will prepend app_name: to form the full contact_id.
        """
        raise NotImplementedError

    def _read_conversation(self, device, contact_id: str) -> list[AndroidMessage]:
        """Read all visible messages from the currently open conversation.

        Returns list of AndroidMessage(role="in"|"out", text, index).
        """
        raise NotImplementedError

    def _send_reply(self, device, text: str) -> bool:
        """Type text into the input box and send.

        Returns True if the message was confirmed sent.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Overridable helpers
    # ------------------------------------------------------------------

    def _open_conversation(self, device, contact_id: str, name: str) -> bool:
        """Navigate to the conversation for this contact.

        Default: pull down notifications, tap the first one matching the sender name.
        Subclasses override this when they have a reliable in-app navigation method.
        """
        try:
            device.open_notification()
            time.sleep(1.2)
            elem = device(text=name)
            if elem.exists(timeout=2):
                elem.click()
                time.sleep(1.5)
                return True
            # Try partial match
            elem = device(textContains=name[:4]) if len(name) >= 4 else None
            if elem and elem.exists(timeout=1):
                elem.click()
                time.sleep(1.5)
                return True
            device.press("back")
            return False
        except Exception:
            try:
                device.press("back")
            except Exception:
                pass
            return False

    def _fill_reply(self, device, text: str) -> None:
        """Fill text into input box without sending (used when auto_send=False)."""
        # Subclasses can override; default does nothing meaningful without resource IDs
        pass

    def _fetch_profile_if_needed(self, device, contact_id: str) -> None:
        """Navigate to the contact's profile page, extract info, save to DB.

        Called after opening a conversation. Must leave the device back in the
        chat window when it returns (or at minimum not crash the outer flow).
        Default is a no-op; override in subclasses with known navigation IDs.
        """
        pass

    # ------------------------------------------------------------------
    # Notification-based fallback scanner
    # ------------------------------------------------------------------

    def _find_via_notifications(self, device) -> list[dict[str, Any]]:
        """Parse dumpsys notification output to find unread messages for this app."""
        try:
            raw = device.shell(f"dumpsys notification --noredact 2>&1")
        except Exception:
            return []

        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        in_block = False
        title = ""

        for line in raw.splitlines():
            stripped = line.strip()
            if f"pkg={self.package_name}" in stripped:
                in_block = True
                title = ""
                continue
            if not in_block:
                continue
            # Title line: android.title=SomeName or android.title: SomeName
            if "android.title" in stripped:
                for sep in ("android.title=", "android.title: "):
                    if sep in stripped:
                        title = stripped.split(sep, 1)[1].strip().strip('"').strip("'")
                        break
            # End of block when we hit another pkg= line or NotificationRecord
            if stripped.startswith("pkg=") and self.package_name not in stripped:
                if title and title not in seen:
                    seen.add(title)
                    contacts.append({"contact_id": title, "name": title})
                in_block = False
                title = ""

        if in_block and title and title not in seen:
            contacts.append({"contact_id": title, "name": title})

        return contacts[:10]

    # ------------------------------------------------------------------
    # Draft generation (mirrors BumbleConnector._create_reply_group)
    # ------------------------------------------------------------------

    def _create_reply_group(
        self, contact_id: str, pending_group: list[AndroidMessage]
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
            message_key = android_message_hash(contact_id, incoming.text)
            if self._recently_sent(message_key):
                self._status["skipped_duplicate_count"] = int(self._status.get("skipped_duplicate_count", 0)) + 1
                self._set_stage(
                    "SKIPPED_ALREADY_SENT",
                    "消息已发送过，跳过",
                    data={"contact_id": contact_id, "incoming": incoming.text},
                )
                continue
            draft = self._cached_draft(memory, message_key, contact_id, incoming.text)
            if draft:
                self._set_stage("DRAFT_CACHED", "使用已缓存草稿", data={"draft": draft})
                reply_group.append({"incoming": incoming.text, "draft": draft, "message_id": message_key})
                continue
            try:
                self._set_stage(
                    "DRAFT_ITEM_START",
                    f"生成第 {i + 1}/{total} 条草稿",
                    data={"contact_id": contact_id, "incoming": incoming.text},
                )
                request = DraftRequest(
                    contact_id=contact_id,
                    message=incoming.text,
                    channel=self.channel,
                    message_id=message_key,
                    contact_identity=f"{self.app_name}联系人",
                    contact_profile=profile_text,
                )
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
            self._draft_cache[message_key] = draft
            if memory is not None and hasattr(memory, "cache_draft"):
                memory.cache_draft(message_key, contact_id, incoming.text, draft)
            self._set_stage(
                "DRAFT_CACHED",
                f"第 {i + 1}/{total} 条草稿已生成",
                data={"draft": draft},
            )
            reply_group.append({"incoming": incoming.text, "draft": draft, "message_id": message_key})
        return reply_group

    def _cached_draft(self, memory, message_key: str, contact_id: str, incoming: str) -> str:
        if message_key in self._draft_cache:
            return self._draft_cache[message_key]
        if memory is None:
            return ""
        draft = memory.get_cached_draft(message_key) if hasattr(memory, "get_cached_draft") else ""
        if draft:
            self._draft_cache[message_key] = draft
            return draft
        draft = (
            memory.recover_draft_for_message(message_key, contact_id)
            if hasattr(memory, "recover_draft_for_message")
            else ""
        )
        if draft and hasattr(memory, "cache_draft"):
            memory.cache_draft(message_key, contact_id, incoming, draft)
        if draft:
            self._draft_cache[message_key] = draft
        return draft

    def _recently_sent(self, message_key: str) -> bool:
        sent_at = self._sent_message_at.get(message_key, 0)
        return message_key in self._sent_messages and time.time() - sent_at < 120
