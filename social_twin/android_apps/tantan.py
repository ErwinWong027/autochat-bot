from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
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


class TantanConnector(AndroidBaseConnector):
    app_name = "tantan"
    package_name = _PKG
    channel = "tantan"

    def _find_unread_contacts(self, device) -> list[dict[str, Any]]:
        """解析 hierarchy XML 找出需要回复的会话，滚动列表确保不遗漏屏幕外的角标。

        主路径：有小红点（未读角标）。
        回退路径：无红点，但预览文字与上次处理时不同（或从未处理过）。
        """
        self._ensure_message_tab(device)
        contacts: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        def _parse_screen() -> int:
            """解析当前屏幕，返回新增的联系人数量。"""
            new_count = 0
            try:
                h = device.dump_hierarchy()
                xml_root = ET.fromstring(h)
                for item in xml_root.iter("node"):
                    if item.get("resource-id", "") != _CONV_ITEM_ROOT:
                        continue
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
                    if not name or name in _OFFICIAL_ACCOUNTS or name in seen_names:
                        continue
                    if preview in _SYSTEM_MSG_TEXTS:
                        continue
                    seen_names.add(name)
                    if has_red_dot:
                        contacts.append({"contact_id": name, "name": name, "preview": preview})
                        new_count += 1
                    elif self._contact_last_preview.get(name) != preview:
                        contacts.append({"contact_id": name, "name": name, "preview": preview})
                        new_count += 1
            except Exception:
                pass
            return new_count

        try:
            # 先滚回顶部，从头扫
            chat_list = device(resourceId=_CHAT_LIST_ID)
            if chat_list.exists(timeout=2):
                chat_list.fling.toBeginning(max_swipes=5)
                time.sleep(0.5)

            _parse_screen()
            # 最多滚动 10 屏，连续 2 屏无新联系人就停
            empty_screens = 0
            for _ in range(10):
                before = len(contacts)
                device.swipe(
                    device.info.get("displayWidth", 720) // 2,
                    int(device.info.get("displayHeight", 1640) * 0.75),
                    device.info.get("displayWidth", 720) // 2,
                    int(device.info.get("displayHeight", 1640) * 0.30),
                    steps=15,
                )
                time.sleep(0.6)
                _parse_screen()
                if len(contacts) == before:
                    empty_screens += 1
                    if empty_screens >= 2:
                        break
                else:
                    empty_screens = 0
        except Exception:
            pass

        return contacts

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

    def _open_conversation(self, device, contact_id: str, name: str) -> bool:
        self._ensure_message_tab(device)
        try:
            item = device(resourceId=_CONTACT_NAME_ID, text=name)
            if item.exists(timeout=3):
                item.click()
                time.sleep(1.5)
                return True
        except Exception:
            pass
        return super()._open_conversation(device, contact_id, name)

    def _read_conversation(self, device, contact_id: str) -> list[AndroidMessage]:
        """完整读取聊天记录，按 content_wrapper + profile_image 位置判断 in/out。

        支持文字、表情贴纸、图片、语音消息，非文字消息记为占位符。
        """
        messages: list[AndroidMessage] = []
        try:
            msg_list_el = device(resourceId=_MSG_LIST_ID)
            if not msg_list_el.exists(timeout=4):
                return messages
            msg_list_el.fling.toEnd(max_swipes=8)
            time.sleep(0.6)

            screen_width = device.info.get("displayWidth", 720)
            h = device.dump_hierarchy()
            xml_root = ET.fromstring(h)

            # 找到 msg_act_list 容器
            list_node = None
            for node in xml_root.iter("node"):
                if node.get("resource-id", "") == _MSG_LIST_ID:
                    list_node = node
                    break
            if list_node is None:
                return messages

            index = 0
            # content_wrapper 在直接子节点的下一层（匿名 LinearLayout 包裹），用 iter 查找
            seen_wrappers: set[str] = set()
            for wrapper in list_node.iter("node"):
                rid = wrapper.get("resource-id", "").split("id/")[-1]
                if rid != "content_wrapper":
                    continue
                bounds = wrapper.get("bounds", "")
                if bounds in seen_wrappers:
                    continue
                seen_wrappers.add(bounds)

                # 找 profile_image 判断 in/out（存在于右侧=out，左侧=in）
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
                    continue  # 日期/时间分隔行，跳过

                # 提取消息内容
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

                # 兜底：content 内有 ImageView 且无文字 → 图片/语音/其他媒体
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

    def _fetch_profile_if_needed(self, device, contact_id: str) -> None:
        try:
            profile = self.service.memory.get_contact_profile(contact_id)
            if len(profile.get("fields", {})) >= _PROFILE_FIELD_MIN:
                self._set_stage("PROFILE_SKIPPED", "画像已够，跳过", data={"contact_id": contact_id})
                return

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

            # 先滚回顶部，确保从头开始读
            _PROFILE_RV_ID = f"{_PKG}:id/profile_view_rv"
            rv = device(resourceId=_PROFILE_RV_ID)
            if rv.exists(timeout=3):
                rv.fling.toBeginning(max_swipes=5)
                time.sleep(0.8)

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

            _collect(self._extract_screen_text(device))
            empty_screens = 0
            for _ in range(8):
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
            if full_text.strip():
                result = self.service.analyze_profile(contact_id, text=full_text)
                field_count = len(result.get("profile", {}).get("fields", {}))
                self._set_stage(
                    "PROFILE_FETCHED",
                    f"画像抓取完成，共 {field_count} 字段",
                    data={"contact_id": contact_id, "fields": field_count},
                )
            else:
                self._log("FETCHING_PROFILE", "未提取到文字，跳过", ok=False)

        except Exception as exc:
            self._log("FETCHING_PROFILE", f"画像抓取失败：{exc}", ok=False)
        finally:
            try:
                device.press("back")
                time.sleep(1.2)
                if not device(resourceId=_INPUT_ID).exists(timeout=2):
                    device.press("back")
                    time.sleep(1.0)
            except Exception:
                pass

    def _extract_screen_text(self, device) -> str:
        """Dump profile page; only read text within profile_view_rv container to avoid background noise."""
        h = device.dump_hierarchy()
        xml_root = ET.fromstring(h)

        # Restrict to the profile RecyclerView to exclude background layers
        _PROFILE_RV_ID = f"{_PKG}:id/profile_view_rv"
        container = None
        for node in xml_root.iter("node"):
            if node.get("resource-id", "") == _PROFILE_RV_ID:
                container = node
                break
        # Fallback: read whole screen if profile container not found
        scope = container if container is not None else xml_root

        seen: set[str] = set()
        lines: list[str] = []
        for node in scope.iter("node"):
            if container is None and node.get("package", "") != _PKG:
                continue
            t = node.get("text", "").strip()
            if not t or len(t) < 2 or t in seen or t in _PROFILE_SKIP_TEXTS:
                continue
            seen.add(t)
            lines.append(t)
        return "\n".join(lines)
