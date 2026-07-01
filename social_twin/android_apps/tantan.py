from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

from ..android_base import AndroidBaseConnector, AndroidMessage

# Resource IDs verified live on device 2026-06-23
_PKG = "com.tantan.x"
_CHAT_LIST_ID    = f"{_PKG}:id/list"                              # RecyclerView 消息列表
_CONV_ITEM_ROOT  = f"{_PKG}:id/conversationItemRoot"              # 每条会话行
_CONTACT_NAME_ID = f"{_PKG}:id/conversation_item_title"           # 联系人名字
_LAST_MSG_ID     = f"{_PKG}:id/conversation_item_message"         # 最新消息预览
_RED_DOT_ID      = f"{_PKG}:id/conversation_item_red_dot"         # 未读小红点
_MSG_LIST_ID     = f"{_PKG}:id/msg_act_list"                      # 聊天记录 RecyclerView
_MSG_TEXT_ID     = f"{_PKG}:id/msg_item_text"                     # 消息气泡文字
_INPUT_ID        = f"{_PKG}:id/messagesBarGreyEdt"                # 输入框 EditText
_SEND_BTN_ID     = f"{_PKG}:id/barSend"                           # 发送按钮（输入文字后出现）
_BACK_BTN_ID     = f"{_PKG}:id/msg_action_bar_back_icon"          # 返回键
_MSG_TAB_ID      = f"{_PKG}:id/conversationRoot"                  # 底部消息 tab
_PROFILE_ENTRY_ID = f"{_PKG}:id/msg_action_bar_avatar_nickname_root"  # 顶栏头像+昵称区（clickable）
_PROFILE_MSG_AVATAR_ID = f"{_PKG}:id/profile_avatar_layout"  # 消息气泡左侧头像（坐标点击进 profile）

# 个人主页上需要跳过的 UI 文字（按钮、tab 标签等）
_PROFILE_SKIP_TEXTS = {
    "聊天", "发消息", "更多", "喜欢", "超级喜欢", "举报", "拉黑",
    "关注", "粉丝", "动态", "相册", "点赞", "收藏",
    "发现", "消息", "探索", "我", "附近",
}

# 系统提示消息 / 官方账号消息，不算作待回复
_SYSTEM_MSG_TEXTS = {
    "你们可以相互发消息了",
    "已相互喜欢，可以开始聊天了",
    "hi，我们可以聊天啦！",
    "我们可以聊天啦！",
}
# 官方/系统账号名称，跳过
_OFFICIAL_ACCOUNTS = {"牵手红娘", "探探官方", "探探小助手"}

# 画像字段最少数量，低于此才重新抓取
_PROFILE_FIELD_MIN = 3

_PROFILE_RV_ID = f"{_PKG}:id/profile_view_rv"
_LIST_SCROLL_STEPS = 24
_LIST_SCROLL_MAX_ROWS = 7
_RECENTLY_SENT_CONTACT_COOLDOWN_MINUTES = 30


def _norm_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _node_text_lines(scope: ET.Element, *, require_package: bool = False) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for node in scope.iter("node"):
        if require_package and node.get("package", "") != _PKG:
            continue
        t = node.get("text", "").strip()
        if not t or len(t) < 2 or t in seen or t in _PROFILE_SKIP_TEXTS:
            continue
        seen.add(t)
        lines.append(t)
    return lines


class TantanConnector(AndroidBaseConnector):
    app_name = "tantan"
    package_name = _PKG
    channel = "tantan"

    def _empty_thread_fallback_reply(self) -> str:
        return "hiii"

    def _should_send_empty_thread_fallback(self, contact_id: str, thread_id: str) -> bool:
        return True

    def _is_system_message_text(self, text: str) -> bool:
        return text.strip() in _SYSTEM_MSG_TEXTS

    def _is_reply_box_available(self, device) -> bool:
        try:
            return device(resourceId=_INPUT_ID).exists(timeout=1)
        except Exception:
            return False

    def _current_conversation_name(self, device) -> str:
        try:
            xml_root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return ""
        for node in xml_root.iter("node"):
            if node.get("resource-id", "") != _PROFILE_ENTRY_ID:
                continue
            lines = _node_text_lines(node)
            if lines:
                return lines[0]
        return ""

    def _is_current_contact(self, device, contact_id: str, name: str) -> bool:
        current = self._current_conversation_name(device)
        if not current:
            self._log(
                "CONTACT_BINDING_MISMATCH",
                "无法读取当前聊天页顶栏联系人，按不可信处理",
                ok=False,
                data={"contact_id": contact_id, "name": name},
            )
            return False
        ok = _norm_name(current) == _norm_name(name)
        if not ok:
            self._log(
                "CONTACT_BINDING_MISMATCH",
                "当前聊天页联系人与目标联系人不一致",
                ok=False,
                data={"contact_id": contact_id, "name": name, "current_name": current},
            )
        return ok

    def _conversation_opened_for(self, device, contact_id: str, name: str) -> bool:
        try:
            if not (device(resourceId=_MSG_LIST_ID).exists(timeout=2) or device(resourceId=_INPUT_ID).exists(timeout=2)):
                return False
            current = ""
            for _ in range(6):
                current = self._current_conversation_name(device)
                if current:
                    break
                time.sleep(0.5)
            if not current:
                self._log(
                    "CONTACT_BINDING_MISMATCH",
                    "无法读取当前聊天页顶栏联系人，按不可信处理",
                    ok=False,
                    data={"contact_id": contact_id, "name": name},
                )
                return False
            ok = _norm_name(current) == _norm_name(name)
            if not ok:
                self._log(
                    "CONTACT_BINDING_MISMATCH",
                    "当前聊天页联系人与目标联系人不一致",
                    ok=False,
                    data={"contact_id": contact_id, "name": name, "current_name": current},
                )
            return ok
        except Exception:
            return False

    def _chat_list_signature(self, device) -> str:
        try:
            xml_root = ET.fromstring(device.dump_hierarchy())
            for item in xml_root.iter("node"):
                if item.get("resource-id", "") != _CONV_ITEM_ROOT:
                    continue
                parts = [item.get("bounds", "")]
                for child in item.iter("node"):
                    rid = child.get("resource-id", "")
                    if rid in (_CONTACT_NAME_ID, _LAST_MSG_ID):
                        parts.append(child.get("text", ""))
                return "|".join(parts)
        except Exception:
            return ""
        return ""

    def _recently_sent_contact(self, full_contact_id: str, name: str) -> bool:
        try:
            thread = self.service.memory.thread_for_contact(self.channel, full_contact_id)
            if not thread:
                return False
            thread_id = str(thread.get("thread_id", ""))
            if not thread_id:
                return False
            cutoff = (
                datetime.now() - timedelta(minutes=_RECENTLY_SENT_CONTACT_COOLDOWN_MINUTES)
            ).isoformat()
            for row in self.service.memory.recent_thread_messages(thread_id, limit=20):
                if row.get("role") == "sent" and str(row.get("created_at", "")) >= cutoff:
                    return True
        except Exception:
            return False
        return False

    def _scroll_chat_list_to_top(self, device, max_swipes: int = 30) -> None:
        """Scroll the conversation list to the top without touching the status bar."""
        try:
            chat_list = device(resourceId=_CHAT_LIST_ID)
            if not chat_list.exists(timeout=2):
                return
            w = device.info.get("displayWidth", 720)
            h = device.info.get("displayHeight", 1640)
            stable_count = 0
            for _ in range(max_swipes):
                before = self._chat_list_signature(device)
                device.swipe(w // 2, int(h * 0.35), w // 2, int(h * 0.75), steps=20)
                time.sleep(0.2)
                after = self._chat_list_signature(device)
                if before and after == before:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
            time.sleep(0.2)
        except Exception:
            pass

    def _visible_conversation_row_height(self, device) -> int:
        try:
            xml_root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return 0
        heights: list[int] = []
        for item in xml_root.iter("node"):
            if item.get("resource-id", "") != _CONV_ITEM_ROOT:
                continue
            if item.get("visible-to-user", "true") == "false":
                continue
            m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", item.get("bounds", ""))
            if not m:
                continue
            _, top, _, bottom = map(int, m.groups())
            height = bottom - top
            if height >= 80:
                heights.append(height)
        if not heights:
            return 0
        heights.sort()
        return heights[len(heights) // 2]

    def _swipe_chat_list_down_small(self, device) -> None:
        w = device.info.get("displayWidth", 720)
        h = device.info.get("displayHeight", 1640)
        row_height = self._visible_conversation_row_height(device)
        max_delta = int(h * 0.22)
        if row_height:
            max_delta = min(max_delta, row_height * _LIST_SCROLL_MAX_ROWS)
        delta = max(120, max_delta)
        center_y = int(h * 0.58)
        start_y = min(int(h * 0.72), center_y + delta // 2)
        end_y = max(int(h * 0.36), center_y - delta // 2)
        device.swipe(w // 2, start_y, w // 2, end_y, steps=_LIST_SCROLL_STEPS)
        time.sleep(0.3)

    def _visible_conversation_bounds_for_name(self, device, name: str) -> str:
        try:
            xml_root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return ""
        target = _norm_name(name)
        for item in xml_root.iter("node"):
            if item.get("resource-id", "") != _CONV_ITEM_ROOT:
                continue
            if item.get("visible-to-user", "true") == "false":
                continue
            item_bounds = item.get("bounds", "")
            for child in item.iter("node"):
                if child.get("resource-id", "") != _CONTACT_NAME_ID:
                    continue
                if _norm_name(child.get("text", "")) == target:
                    return item_bounds
        return ""

    def _click_conversation_bounds(self, device, bounds: str) -> bool:
        m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
        if not m:
            return False
        left, top, right, bottom = map(int, m.groups())
        if bottom - top < 80:
            return False
        device.click((left + right) // 2, (top + bottom) // 2)
        return True

    def _find_unread_contacts(self, device) -> list[dict[str, Any]]:
        """解析 hierarchy XML 找出需要回复的会话，滚动列表确保不遗漏屏幕外的角标。

        主路径：有小红点（未读角标）。
        Tantan 列表预览可能是我方最后一条消息，不能用 preview 变化当待回复信号。
        """
        self._ensure_message_tab(device)
        self._scroll_chat_list_to_top(device)
        contacts: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        screen_count = 0
        red_count = 0
        red_names: list[str] = []

        def _parse_screen() -> int:
            """解析当前屏幕，返回新增的联系人数量。"""
            nonlocal screen_count, red_count, red_names
            new_count = 0
            try:
                h = device.dump_hierarchy()
                xml_root = ET.fromstring(h)
                red_dot_centers: list[tuple[int, int]] = []
                for node in xml_root.iter("node"):
                    if node.get("resource-id", "") != _RED_DOT_ID:
                        continue
                    if node.get("visible-to-user", "true") == "false":
                        continue
                    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                    if m:
                        left, top, right, bottom = map(int, m.groups())
                        red_dot_centers.append(((left + right) // 2, (top + bottom) // 2))
                for item in xml_root.iter("node"):
                    if item.get("resource-id", "") != _CONV_ITEM_ROOT:
                        continue
                    if item.get("visible-to-user", "true") == "false":
                        continue
                    item_bounds = item.get("bounds", "")
                    bounds_match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", item_bounds)
                    if not bounds_match:
                        continue
                    left, top, right, bottom = map(int, bounds_match.groups())
                    if bottom - top < 80:
                        continue
                    screen_count += 1
                    name = ""
                    preview = ""
                    has_red_dot = False
                    for child in item.iter("node"):
                        rid = child.get("resource-id", "")
                        if rid == _CONTACT_NAME_ID:
                            name = child.get("text", "")
                        elif rid == _LAST_MSG_ID:
                            preview = child.get("text", "")
                        elif rid == _RED_DOT_ID:
                            has_red_dot = True
                    if not has_red_dot and red_dot_centers:
                        has_red_dot = any(
                            left <= cx <= right and top <= cy <= bottom
                            for cx, cy in red_dot_centers
                        )
                    if not name or name in _OFFICIAL_ACCOUNTS or name in seen_names:
                        continue
                    full_contact_id = f"{self.app_name}:{name}"
                    if time.time() - self._contact_processed_at.get(full_contact_id, 0) < 600:
                        continue
                    if self._recently_sent_contact(full_contact_id, name):
                        continue
                    seen_names.add(name)
                    if has_red_dot:
                        red_count += 1
                        if len(red_names) < 8:
                            red_names.append(name)
                        contacts.append({"contact_id": name, "name": name, "preview": preview, "bounds": item_bounds})
                        new_count += 1
            except Exception:
                pass
            return new_count

        try:
            _parse_screen()
            if not contacts:
                for _ in range(30):
                    self._swipe_chat_list_down_small(device)
                    if _parse_screen():
                        break
        except Exception:
            pass

        self._log(
            "CONTACT_SCAN_DEBUG",
            "Tantan 未读解析结果",
            data={"screen_items": screen_count, "red_count": red_count, "red_names": red_names, "contacts": len(contacts)},
        )
        return contacts[:1]

    def _ensure_message_tab(self, device) -> None:
        """连按最多 5 次返回键，直到消息列表出现为止。"""
        try:
            for _ in range(5):
                if device(resourceId=_CHAT_LIST_ID).exists(timeout=1):
                    break
                device.press("back")
                time.sleep(1.0)
            if not device(resourceId=_CHAT_LIST_ID).exists(timeout=2):
                device(resourceId=_MSG_TAB_ID).click_exists(timeout=2)
                time.sleep(1)
        except Exception:
            pass

    def _open_conversation(self, device, contact_id: str, name: str, open_bounds: str | None = None) -> bool:
        self._ensure_message_tab(device)
        try:
            current_bounds = self._visible_conversation_bounds_for_name(device, name)
            if current_bounds and (not open_bounds or current_bounds == open_bounds):
                if self._click_conversation_bounds(device, current_bounds):
                    time.sleep(1.5)
                    if self._conversation_opened_for(device, contact_id, name):
                        return True
                    self._ensure_message_tab(device)
            for attempt in range(11):
                current_bounds = self._visible_conversation_bounds_for_name(device, name)
                if current_bounds:
                    self._log(
                        "CONTACT_FOUND",
                        "当前屏幕重新确认目标联系人行，准备点击",
                        data={"contact_id": contact_id, "name": name, "bounds": current_bounds},
                    )
                    if not self._click_conversation_bounds(device, current_bounds):
                        return False
                    time.sleep(1.5)
                    if self._conversation_opened_for(device, contact_id, name):
                        return True
                    self._ensure_message_tab(device)
                if attempt >= 10:
                    break
                self._swipe_chat_list_down_small(device)
        except Exception:
            pass
        self._log("CONTACT_FOUND", "列表内未找到会话，跳过；未打开通知栏", ok=False, data={"contact_id": contact_id, "name": name})
        return False

    def _parse_screen_messages(self, device) -> list[AndroidMessage]:
        """Parse messages currently visible on screen. Returns list in display order (old→new)."""
        messages: list[AndroidMessage] = []
        try:
            screen_width = device.info.get("displayWidth", 720)
            h = device.dump_hierarchy()
            xml_root = ET.fromstring(h)

            list_node = None
            for node in xml_root.iter("node"):
                if node.get("resource-id", "") == _MSG_LIST_ID:
                    list_node = node
                    break
            if list_node is None:
                return messages

            index = 0
            seen_wrappers: set[str] = set()
            for wrapper in list_node.iter("node"):
                rid = wrapper.get("resource-id", "").split("id/")[-1]
                if rid != "content_wrapper":
                    continue
                bounds = wrapper.get("bounds", "")
                if bounds in seen_wrappers:
                    continue
                seen_wrappers.add(bounds)

                role: str | None = None
                for child in wrapper.iter("node"):
                    if child.get("resource-id", "").split("id/")[-1] == "profile_image":
                        b = child.get("bounds", "")
                        m = re.search(r"\[(\d+),\d+\]\[(\d+),\d+\]", b)
                        if m:
                            cx = (int(m.group(1)) + int(m.group(2))) / 2
                            role = "out" if cx > screen_width * 0.5 else "in"
                        break
                if role is None:
                    continue

                text: str | None = None
                for child in wrapper.iter("node"):
                    crid = child.get("resource-id", "").split("id/")[-1]
                    if crid == "msg_item_text":
                        t = child.get("text", "").strip()
                        if t and t not in _SYSTEM_MSG_TEXTS:
                            text = t
                        break
                    if "sticker" in crid or "sticker" in child.get("class", "").lower():
                        text = "[表情]"
                        break
                    if crid in ("msg_card_image", "msg_image_view", "msg_video_thumb"):
                        text = "[图片]"
                        break

                if text is None:
                    for child in wrapper.iter("node"):
                        if child.get("resource-id", "").split("id/")[-1] == "content":
                            for sub in child.iter("node"):
                                sub_rid = sub.get("resource-id", "").split("id/")[-1]
                                if (sub.get("class", "").endswith("ImageView")
                                        and sub_rid
                                        and sub_rid not in ("profile_image",)):
                                    text = "[图片/媒体]"
                                    break
                            break

                if text is not None:
                    messages.append(AndroidMessage(role=role, text=text, index=index))
                    index += 1
        except Exception:
            pass
        return messages

    def _read_conversation_full(self, device) -> list[AndroidMessage]:
        """首次读取：从底部向上最多滚动 12 屏，累积历史消息后滚回底部。"""
        screen_width = device.info.get("displayWidth", 720)
        display_height = device.info.get("displayHeight", 1640)
        cx = screen_width // 2

        accumulated: list[tuple[str, str]] = []  # (role, text) oldest first

        def merge_screen(screen_msgs: list[AndroidMessage]) -> int:
            """Prepend new (older) messages to accumulated. Returns count added."""
            pairs = [(m.role, m.text) for m in screen_msgs]
            if not pairs:
                return 0
            # Find longest suffix of pairs matching prefix of accumulated (overlap region)
            max_overlap = min(len(pairs), len(accumulated))
            overlap = 0
            for size in range(max_overlap, 0, -1):
                if pairs[-size:] == accumulated[:size]:
                    overlap = size
                    break
            new_msgs = pairs[:-overlap] if overlap > 0 else pairs
            accumulated[0:0] = new_msgs
            return len(new_msgs)

        merge_screen(self._parse_screen_messages(device))

        for _ in range(12):
            device.swipe(cx, int(display_height * 0.45), cx, int(display_height * 0.75), steps=15)
            time.sleep(0.6)
            screen_msgs = self._parse_screen_messages(device)
            if not screen_msgs:
                break
            if merge_screen(screen_msgs) == 0:
                break

        try:
            msg_list_el = device(resourceId=_MSG_LIST_ID)
            if msg_list_el.exists(timeout=2):
                msg_list_el.fling.toEnd(max_swipes=8)
                time.sleep(0.6)
        except Exception:
            pass

        return [AndroidMessage(role=role, text=text, index=i) for i, (role, text) in enumerate(accumulated)]

    def _read_conversation(self, device, contact_id: str, thread_id: str = "") -> list[AndroidMessage]:
        messages: list[AndroidMessage] = []
        try:
            msg_list_el = device(resourceId=_MSG_LIST_ID)
            if not msg_list_el.exists(timeout=4):
                return messages
            msg_list_el.fling.toEnd(max_swipes=8)
            time.sleep(0.6)
            if thread_id and self._thread_has_existing_messages(thread_id):
                self._log("MESSAGES_READ", "已有历史消息，跳过全量加载", data={"thread_id": thread_id, "contact_id": contact_id})
                messages = self._parse_screen_messages(device)
            else:
                messages = self._read_conversation_full(device)
        except Exception:
            pass
        return messages

    def _send_reply(self, device, text: str) -> bool:
        try:
            input_box = device(resourceId=_INPUT_ID)
            if not input_box.exists(timeout=4):
                return False
            input_box.click()
            time.sleep(0.3)
            input_box.set_text(text)
            time.sleep(0.5)
            send_btn = device(resourceId=_SEND_BTN_ID)
            if send_btn.exists(timeout=3):
                send_btn.click()
                time.sleep(1.2)
                return True
            input_box.press("enter")
            time.sleep(1.2)
            return True
        except Exception:
            return False

    def _fill_reply(self, device, text: str) -> None:
        try:
            box = device(resourceId=_INPUT_ID)
            box.click()
            time.sleep(0.2)
            box.set_text(text)
        except Exception:
            pass

    def _fetch_profile_if_needed(self, device, contact_id: str) -> bool:
        try:
            profile = self.service.memory.get_contact_profile(contact_id)
            if len(profile.get("fields", {})) >= _PROFILE_FIELD_MIN:
                # 字段已足够，跳过设备抓取；但若 profile_text_analysis 尚未存入，用已有文本补存
                self._ensure_profile_text_analysis(contact_id)
                self._set_stage("PROFILE_SKIPPED", "画像已够，跳过", data={"contact_id": contact_id})
                return True

            self._set_stage("FETCHING_PROFILE", "进入联系人主页抓取画像", data={"contact_id": contact_id})

            # 优先点消息气泡左侧头像（真实 profile 入口），兜底用顶栏区域
            entered = False
            h = device.dump_hierarchy()
            xml_root = ET.fromstring(h)
            for node in xml_root.iter("node"):
                if node.get("resource-id", "") == _PROFILE_MSG_AVATAR_ID:
                    bounds = node.get("bounds", "")
                    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                    if m:
                        cx = (int(m.group(1)) + int(m.group(3))) // 2
                        cy = (int(m.group(2)) + int(m.group(4))) // 2
                        device.click(cx, cy)
                        entered = True
                        break
            if not entered:
                if device(resourceId=_PROFILE_ENTRY_ID).exists(timeout=2):
                    device(resourceId=_PROFILE_ENTRY_ID).click()
                else:
                    w = device.info.get("displayWidth", 1080)
                    device.click(w // 2, 120)

            time.sleep(3.0)

            # 先尝试回到顶部。这里不用 uiautomator2 fling，避免个别页面卡住 agent 线程。
            rv = device(resourceId=_PROFILE_RV_ID)
            if not rv.exists(timeout=3):
                self._log(
                    "FETCHING_PROFILE",
                    "未进入可信 profile 容器，拒绝读取整屏文本",
                    ok=False,
                    data={"contact_id": contact_id},
                )
                return False
            w0 = device.info.get("displayWidth", 720)
            h0 = device.info.get("displayHeight", 1640)
            for _ in range(4):
                device.swipe(w0 // 2, int(h0 * 0.30), w0 // 2, int(h0 * 0.75), steps=15)
                time.sleep(0.25)

            # 滚动 8 屏，跨屏全局去重行，避免重复内容撑大 LLM 输入导致截断
            w = device.info.get("displayWidth", 720)
            h_px = device.info.get("displayHeight", 1640)
            cx_swipe = w // 2
            global_seen: set[str] = set()
            all_lines: list[str] = []

            def _collect(text: str) -> None:
                for line in text.split("\n"):
                    line = line.strip()
                    if line and line not in global_seen:
                        global_seen.add(line)
                        all_lines.append(line)

            started_at = time.monotonic()
            _collect(self._extract_screen_text(device))
            empty_screens = 0
            for _ in range(8):
                if time.monotonic() - started_at > 35:
                    self._log(
                        "FETCHING_PROFILE",
                        "profile 采集超过 35 秒，使用已采集文本继续",
                        ok=False,
                        data={"contact_id": contact_id, "line_count": len(all_lines)},
                    )
                    break
                before = len(all_lines)
                device.swipe(cx_swipe, int(h_px * 0.75), cx_swipe, int(h_px * 0.25), steps=20)
                time.sleep(1.0)
                _collect(self._extract_screen_text(device))
                if len(all_lines) == before:
                    empty_screens += 1
                    if empty_screens >= 2:
                        break  # 连续两屏无新内容，已到底部
                else:
                    empty_screens = 0

            full_text = "\n".join(all_lines)
            if full_text.strip() and not self._profile_text_looks_like_chat_list(full_text):
                self._set_stage(
                    "PROFILE_ANALYZING",
                    "profile 文本采集完成，开始解析画像",
                    data={"contact_id": contact_id, "profile_text_len": len(full_text), "line_count": len(all_lines)},
                )
                result = self.service.analyze_profile(contact_id, text=full_text)
                field_count = len(result.get("profile", {}).get("fields", {}))
                self._set_stage(
                    "PROFILE_FETCHED",
                    f"画像抓取完成，共 {field_count} 字段",
                    data={"contact_id": contact_id, "fields": field_count},
                )
                return True
            else:
                self._log(
                    "FETCHING_PROFILE",
                    "profile 文本为空或疑似会话列表污染，跳过发送",
                    ok=False,
                    data={"contact_id": contact_id, "profile_text_len": len(full_text)},
                )
                return False

        except Exception as exc:
            self._log("FETCHING_PROFILE", f"画像抓取失败：{exc}", ok=False)
            return False
        finally:
            try:
                for _ in range(3):
                    if device(resourceId=_MSG_LIST_ID).exists(timeout=1) or device(resourceId=_INPUT_ID).exists(timeout=1):
                        break
                    device.press("back")
                    time.sleep(1.0)
                    if not device(resourceId=_PROFILE_RV_ID).exists(timeout=1) and (
                        device(resourceId=_MSG_LIST_ID).exists(timeout=1) or device(resourceId=_INPUT_ID).exists(timeout=1)
                    ):
                        break
            except Exception:
                pass

    def _extract_screen_text(self, device) -> str:
        """Dump profile page; only read text within profile_view_rv container to avoid background noise."""
        h = device.dump_hierarchy()
        xml_root = ET.fromstring(h)

        # Restrict to the profile RecyclerView to exclude background layers
        container = None
        for node in xml_root.iter("node"):
            if node.get("resource-id", "") == _PROFILE_RV_ID:
                container = node
                break
        if container is None:
            return ""
        return "\n".join(_node_text_lines(container))

    def _profile_text_looks_like_chat_list(self, text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 8:
            return False
        date_like = sum(1 for line in lines if re.fullmatch(r"\d{2}/\d{2}|\d{1,2}:\d{2}|星期[一二三四五六日天]", line))
        system_like = sum(1 for line in lines if line in _SYSTEM_MSG_TEXTS or line in _OFFICIAL_ACCOUNTS or line in _PROFILE_SKIP_TEXTS)
        return date_like >= 3 or system_like >= 2

    def _unsent_draft_reply_group(self, thread_id: str) -> list[dict[str, str]]:
        return []

    def _ensure_profile_text_analysis(self, contact_id: str) -> None:
        """若 profile_text_analysis 尚未记录，用已存的 profile_text 补存 LLM prompt。"""
        try:
            prompts = self.service.memory.get_last_llm_prompts_for_contact(contact_id)
            if prompts.get("profile_text_analysis"):
                return
            profile_text = self.service.memory.get_profile_text_for_contact(contact_id)
            if not profile_text:
                return
            self.service.analyze_profile(contact_id, text=profile_text)
            self._log("PROFILE_SKIPPED", "补存 profile_text_analysis LLM prompt", data={"contact_id": contact_id})
        except Exception as exc:
            self._log("PROFILE_SKIPPED", f"补存 profile_text_analysis 失败：{exc}", ok=False)
