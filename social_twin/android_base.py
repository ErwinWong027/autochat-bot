from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .service import DigitalTwinService, ReplyGroupRequest


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
    "STOPPING": 799,
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


def android_message_key(message: AndroidMessage) -> tuple[str, str]:
    role = "user" if message.role == "in" else "sent"
    return role, " ".join((message.text or "").strip().split())


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
        if self._status.get("running"):
            self._set_stage("STOPPING", "收到停止指令，等待当前安全点停止")
        else:
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
                    completed = self._process_contact(
                        device,
                        contact_id,
                        name,
                        auto_send,
                        candidate.get("preview", ""),
                        candidate.get("bounds"),
                    )
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

    def _process_contact(self, device, contact_id: str, name: str, auto_send: bool, list_preview: str = "", open_bounds: str | None = None) -> bool:
        self._status.update({"last_contact_id": contact_id, "last_contact_name": name})
        self._update_contact_status(contact_id, name=name, stage="CONTACT_FOUND")
        self._set_stage("CONTACT_FOUND", "找到待回复联系人", data={"contact_id": contact_id, "name": name})

        if not self._open_conversation(device, contact_id, name, open_bounds):
            self._log("CONTACT_FOUND", "无法打开会话，跳过", ok=False, data={"contact_id": contact_id})
            return True
        if not self._is_current_contact(device, contact_id, name):
            self._update_contact_status(contact_id, stage="CONTACT_BINDING_MISMATCH", sent_count=0)
            self._set_stage(
                "CONTACT_BINDING_MISMATCH",
                "当前聊天页联系人与目标联系人不一致，跳过",
                ok=False,
                data={"contact_id": contact_id, "name": name},
            )
            return True

        self._update_contact_status(contact_id, stage="CONTACT_OPENED")
        self._set_stage("CONTACT_OPENED", "已打开联系人会话")

        profile_ok = self._fetch_profile_if_needed(device, contact_id)
        if profile_ok is False:
            self._update_contact_status(contact_id, stage="CONTACT_PROFILE_UNVERIFIED", sent_count=0)
            self._set_stage(
                "CONTACT_PROFILE_UNVERIFIED",
                "profile 未安全采集，跳过发送",
                ok=False,
                data={"contact_id": contact_id, "name": name},
            )
            return True
        time.sleep(1.0)  # profile 返回后给聊天界面时间重新渲染
        if not self._is_current_contact(device, contact_id, name):
            self._update_contact_status(contact_id, stage="CONTACT_BINDING_MISMATCH", sent_count=0)
            self._set_stage(
                "CONTACT_BINDING_MISMATCH",
                "profile 返回后当前聊天页联系人与目标联系人不一致，跳过",
                ok=False,
                data={"contact_id": contact_id, "name": name},
            )
            return True

        thread_id = self.service.memory.get_or_create_thread(self.channel, contact_id, name)
        self._update_contact_status(contact_id, thread_id=thread_id)

        try:
            messages = self._read_conversation(device, contact_id, thread_id)
        except Exception as exc:
            self._set_stage("MESSAGES_READ", f"读取消息失败：{exc}", ok=False)
            return True
        if (
            not messages
            and list_preview.strip()
            and not self._is_system_message_text(list_preview.strip())
        ):
            messages = [AndroidMessage(role="in", text=list_preview.strip(), index=0)]
            self._log(
                "MESSAGES_READ",
                "聊天页读取为 0，使用未读列表预览作为 incoming",
                data={"contact_id": contact_id, "preview": list_preview.strip()},
            )

        inserted = self._sync_thread_messages(thread_id, contact_id, messages)
        if inserted:
            try:
                self.service.update_thread_memory_from_messages(thread_id, inserted)
                self._set_stage("MEMORY_UPDATED", "长期 memory 已更新", data={"thread_id": thread_id, "new_message_count": len(inserted)})
            except Exception as exc:
                self._set_stage("MEMORY_UPDATE_FAILED", f"长期 memory 更新失败：{exc}", ok=False, data={"thread_id": thread_id})
        self._set_stage("MESSAGES_READ", f"读取到 {len(messages)} 条消息")
        empty_reply = self._empty_thread_fallback_reply()
        if not messages and empty_reply:
            self._status.update({
                "last_draft": empty_reply,
                "draft_count": int(self._status.get("draft_count", 0)) + 1,
                "pending_group_count": 0,
            })
            self._update_contact_status(contact_id, pending_group_count=0, pending_group=[], last_reply_group=[
                {"incoming": "", "draft": empty_reply, "message_id": "", "technique": "empty_thread_fallback", "reason": "消息读取为 0 条"}
            ])
            self._set_stage("DRAFTED", "消息读取为 0 条，准备 fallback 草稿", data={"draft": empty_reply})
            if auto_send:
                if not self._should_send_empty_thread_fallback(contact_id, thread_id):
                    self._update_contact_status(contact_id, stage="NO_UNSENT_DRAFT", sent_count=0)
                    self._set_stage(
                        "NO_UNSENT_DRAFT",
                        "消息读取为 0 条，跳过 fallback 自动发送",
                        data={"contact_id": contact_id, "draft": empty_reply},
                    )
                    self._contact_processed_at[contact_id] = time.time()
                    return True
                ok = self._send_reply(device, empty_reply)
                if not ok:
                    self._update_contact_status(contact_id, stage="SEND_NOT_CONFIRMED", sent_count=0)
                    self._set_stage("SEND_NOT_CONFIRMED", "fallback 发送未确认，跳过该联系人继续", ok=False, data={"contact_id": contact_id, "draft": empty_reply})
                    self._contact_processed_at[contact_id] = time.time()
                    return True
                self._record_sent_message(thread_id, contact_id, empty_reply, "empty_thread_fallback", "消息读取为 0 条")
                self._status["sent_count"] = int(self._status.get("sent_count", 0)) + 1
                self._update_contact_status(contact_id, stage="SENT", sent_count=1)
                self._set_stage("SENT", "已发送 1 条 fallback")
                return True
            self._fill_reply(device, empty_reply)
            self._update_contact_status(contact_id, stage="DRAFTED", sent_count=0)
            return True
        pending_rows = self.service.memory.pending_thread_messages(thread_id)
        if pending_rows and hasattr(self.service.memory, "upsert_thread_pending_group"):
            self._status["thread_pending_group_hash"] = self.service.memory.upsert_thread_pending_group(
                thread_id,
                [str(item["platform_message_id"]) for item in pending_rows],
                "pending",
            )
        pending_by_key: dict[tuple[str, str], list[AndroidMessage]] = {}
        for message in messages:
            pending_by_key.setdefault(android_message_key(message), []).append(message)
        pending_group: list[AndroidMessage] = []
        for item in pending_rows:
            key = ("user", " ".join(str(item.get("content", "")).strip().split()))
            bucket = pending_by_key.get(key, [])
            if bucket:
                pending_group.append(bucket.pop(0))
        self._status["pending_group_count"] = len(pending_group)
        self._update_contact_status(
            contact_id,
            pending_group_count=len(pending_group),
            pending_group=[m.text for m in pending_group],
        )

        if not pending_group:
            if auto_send:
                reply_group = self._unsent_draft_reply_group(thread_id)
                if reply_group:
                    self._update_contact_status(contact_id, stage="SENDING_EXISTING_DRAFTS", last_reply_group=reply_group)
                    self._set_stage(
                        "SENDING_EXISTING_DRAFTS",
                        f"发现 {len(reply_group)} 条已留痕未发送草稿，自动逐条发送",
                        data={"reply_count": len(reply_group)},
                    )
                    sent = self._send_reply_group(device, thread_id, contact_id, name, reply_group)
                    self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
                    if sent == len(reply_group):
                        self._update_contact_status(contact_id, stage="SENT", sent_count=sent)
                        self._set_stage("SENT", f"已发送 {sent} 条")
                        return True
                    self._update_contact_status(contact_id, stage="SEND_NOT_CONFIRMED", sent_count=sent)
                    return False
            self._update_contact_status(contact_id, stage="NO_PENDING_GROUP")
            self._set_stage("NO_PENDING_GROUP", "没有待回复消息组")
            return True

        self._set_stage(
            "PENDING_GROUP_FOUND",
            f"找到 {len(pending_group)} 条待回复消息",
            data={"pending_group": [m.text for m in pending_group]},
        )
        self._update_contact_status(contact_id, stage="DRAFTING")
        self._set_stage(
            "DRAFTING",
            "开始按 pending_group 合并生成草稿",
            data={"pending_group_count": len(pending_group), "reply_count": len(pending_group)},
        )

        reply_group = self._create_reply_group(thread_id, contact_id, pending_group)
        if self._stop.is_set():
            self._update_contact_status(contact_id, stage="STOPPING")
            self._set_stage("STOPPING", "停止指令已生效，跳过发送")
            return False
        if not reply_group:
            self._set_stage("NO_UNSENT_DRAFT", "没有未发送草稿")
            return True

        self._status.update({
            "last_draft": reply_group[-1]["draft"],
            "draft_count": int(self._status.get("draft_count", 0)) + len(reply_group),
        })
        self._update_contact_status(contact_id, stage="DRAFTED", last_reply_group=reply_group)
        self._set_stage("DRAFTED", f"准备 {len(reply_group)} 条草稿")

        if auto_send:
            sent = self._send_reply_group(device, thread_id, contact_id, name, reply_group)
            if sent < len(reply_group):
                return False
        else:
            sent = 0
            # Fill only the first draft without sending
            if reply_group:
                try:
                    self._fill_reply(device, reply_group[0]["draft"])
                except Exception:
                    pass

        group_hash = self._status.get("thread_pending_group_hash", "")
        memory = getattr(self.service, "memory", None)
        if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
            status = "sent" if auto_send and sent == len(reply_group) else "drafted"
            memory.thread_pending_group_status(str(group_hash), status)
        self._status["sent_count"] = int(self._status.get("sent_count", 0)) + sent
        self._update_contact_status(contact_id, stage="SENT", sent_count=sent)
        self._set_stage("SENT", f"已发送 {sent} 条")
        return True

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

    def _send_reply_group(self, device, thread_id: str, contact_id: str, name: str, reply_group: list[dict[str, str]]) -> int:
        sent = 0
        memory = getattr(self.service, "memory", None)
        for item in reply_group:
            if self._stop.is_set():
                self._set_stage("STOPPING", "停止指令已生效，停止发送剩余草稿")
                return sent
            try:
                if name and not self._is_reply_box_available(device):
                    self._log("DRAFTED", "发送前不在聊天页，重新打开会话", data={"contact_id": contact_id, "name": name})
                    if not self._open_conversation(device, contact_id, name):
                        self._set_stage(
                            "SEND_NOT_CONFIRMED",
                            "发送前无法重新打开会话",
                            ok=False,
                            data={"contact_id": contact_id, "name": name},
                        )
                        return sent
                if not self._is_current_contact(device, contact_id, name):
                    self._set_stage(
                        "CONTACT_BINDING_MISMATCH",
                        "发送前当前聊天页联系人与目标联系人不一致，停止发送",
                        ok=False,
                        data={"contact_id": contact_id, "name": name},
                    )
                    return sent
                ok = self._send_reply(device, item["draft"])
            except Exception as exc:
                self._set_stage("SEND_NOT_CONFIRMED", f"发送异常：{exc}", ok=False)
                group_hash = self._status.get("thread_pending_group_hash", "")
                if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
                    memory.thread_pending_group_status(str(group_hash), "discarded")
                return sent
            if not ok:
                self._set_stage(
                    "SEND_NOT_CONFIRMED",
                    "发送未确认，已停止 Agent",
                    ok=False,
                    data={"contact_id": contact_id, "draft": item["draft"]},
                )
                group_hash = self._status.get("thread_pending_group_hash", "")
                if group_hash and memory is not None and hasattr(memory, "thread_pending_group_status"):
                    memory.thread_pending_group_status(str(group_hash), "discarded")
                return sent
            sent += 1
            message_key = item.get("message_id", "")
            if message_key:
                self._sent_messages.add(message_key)
                self._sent_message_at[message_key] = time.time()
                if memory is not None:
                    if hasattr(memory, "mark_thread_sent"):
                        memory.mark_thread_sent(thread_id, message_key)
                    else:
                        memory.mark_sent(message_key, contact_id)
            time.sleep(0.5)
        return sent

    def _is_reply_box_available(self, device) -> bool:
        return False

    def _empty_thread_fallback_reply(self) -> str:
        return ""

    def _should_send_empty_thread_fallback(self, contact_id: str, thread_id: str) -> bool:
        return True

    def _is_system_message_text(self, text: str) -> bool:
        return False

    def _is_current_contact(self, device, contact_id: str, name: str) -> bool:
        return True

    def _record_sent_message(
        self,
        thread_id: str,
        contact_id: str,
        draft: str,
        technique: str = "",
        reason: str = "",
    ) -> None:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "add_thread_message"):
            return
        message_id = android_message_hash(contact_id, f"sent:{time.time_ns()}:{draft}")
        memory.add_thread_message(
            thread_id,
            self.channel,
            message_id,
            "sent",
            draft,
            technique=technique,
            decision_reason=reason,
        )

    def _android_ui_message_hash(self, contact_id: str, message: AndroidMessage) -> str:
        return android_message_hash(contact_id, f"{message.role}:{message.index}:{message.text}")

    def _sync_thread_messages(self, thread_id: str, contact_id: str, messages: list[AndroidMessage]) -> list[dict[str, Any]]:
        payload = []
        for message in messages:
            role = "user" if message.role == "in" else "sent"
            payload.append(
                {
                    "platform_message_id": self._android_ui_message_hash(contact_id, message),
                    "role": role,
                    "content": message.text,
                    "order_index": message.index,
                }
            )
        return self.service.memory.sync_thread_messages_incremental(thread_id, self.channel, payload)

    def _thread_has_existing_messages(self, thread_id: str) -> bool:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "all_thread_messages"):
            return False
        try:
            return bool(memory.all_thread_messages(thread_id))
        except Exception:
            return False

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

    def _read_conversation(self, device, contact_id: str, thread_id: str = "") -> list[AndroidMessage]:
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

    def _open_conversation(self, device, contact_id: str, name: str, open_bounds: str | None = None) -> bool:
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
        self, thread_id: str, contact_id: str, pending_group: list[AndroidMessage]
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
        pending_message_ids = self._pending_message_ids_for_group(thread_id, contact_id, pending_group)
        if memory is not None and hasattr(memory, "upsert_thread_pending_group"):
            self._status["thread_pending_group_hash"] = memory.upsert_thread_pending_group(
                thread_id,
                pending_message_ids,
                "drafting",
            )
        active_pending: list[AndroidMessage] = []
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
            cached = self._cached_draft(
                memory, message_key, contact_id, incoming.text, legacy_message_key=self._android_ui_message_hash(contact_id, incoming)
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
                platform=self.channel,
                pending_messages=[item.text for item in active_pending],
                message_ids=active_message_ids,
                pending_group_context="\n".join(f"{index + 1}. {message.text}" for index, message in enumerate(active_pending)),
                memory_context=memory_context,
                profile_context=profile_text,
                contact_identity=f"{self.app_name}联系人",
                contact_profile=profile_text,
            )
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
        pending_group: list[AndroidMessage],
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
        pending_group: list[AndroidMessage],
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

    def _pending_message_ids_for_group(self, thread_id: str, contact_id: str, pending_group: list[AndroidMessage]) -> list[str]:
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

    def _stable_message_id_for_pending(self, thread_id: str, contact_id: str, incoming: AndroidMessage) -> str:
        memory = getattr(self.service, "memory", None)
        if memory is None or not hasattr(memory, "stable_thread_message_id"):
            return self._android_ui_message_hash(contact_id, incoming)
        normalized = " ".join((incoming.text or "").strip().split())
        occurrence = 0
        if hasattr(memory, "all_thread_messages"):
            for row in memory.all_thread_messages(thread_id):
                if row.get("role") != "user":
                    continue
                if " ".join(str(row.get("content", "")).strip().split()) == normalized:
                    occurrence += 1
        return memory.stable_thread_message_id(thread_id, "user", normalized, max(occurrence, 1))

    def _cached_draft(
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
        draft = (
            memory.recover_draft_for_message(message_key, contact_id)
            if hasattr(memory, "recover_draft_for_message")
            else ""
        )
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
