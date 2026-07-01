from __future__ import annotations

import time
from typing import Any

from ..android_base import AndroidBaseConnector, AndroidMessage


# Verify with: adb shell uiautomator dump /dev/stdout | grep -i "tencent\|input\|send\|unread"
_PKG = "com.tencent.mm"
_CHAT_LIST_ID = f"{_PKG}:id/conversation_list"
_UNREAD_BADGE_ID = f"{_PKG}:id/unread_count"
_CONTACT_NAME_ID = f"{_PKG}:id/cell_name"
_INPUT_ID = f"{_PKG}:id/input_text"
_SEND_BTN_ID = f"{_PKG}:id/send"
_MSG_LIST_ID = f"{_PKG}:id/message_list"


class WeChatConnector(AndroidBaseConnector):
    app_name = "wechat"
    package_name = _PKG
    channel = "wechat"

    def _find_unread_contacts(self, device) -> list[dict[str, Any]]:
        """WeChat chat list may not expose standard resource IDs.
        Primary strategy: scan notification badge counts on the 微信 tab,
        then enumerate conversation list for unread items.
        """
        contacts = []
        try:
            # Ensure we're on the 微信 (Chats) tab
            wechat_tab = device(description="微信") or device(text="微信")
            if wechat_tab.exists(timeout=2):
                wechat_tab.click()
                time.sleep(0.8)

            # Try to iterate conversation list
            items = device(resourceId=_CHAT_LIST_ID).child(className="android.widget.LinearLayout")
            found_via_list = False
            for item in items:
                badge = item.child(resourceId=_UNREAD_BADGE_ID)
                if not badge.exists:
                    continue
                try:
                    count_text = badge.get_text()
                    if not count_text or count_text == "0":
                        continue
                except Exception:
                    continue
                try:
                    name_elem = item.child(resourceId=_CONTACT_NAME_ID)
                    name = name_elem.get_text() if name_elem.exists else ""
                except Exception:
                    name = ""
                if name:
                    contacts.append({"contact_id": name, "name": name})
                    found_via_list = True

            if not found_via_list:
                # WeChat sometimes uses dot badges without text counts
                # Fall back to finding any red dot badges in conversation list
                dot_badges = device(resourceId=_UNREAD_BADGE_ID)
                for badge in dot_badges:
                    try:
                        # Get the parent item and extract name
                        parent = badge.parent()
                        if parent:
                            name_elem = parent.child(resourceId=_CONTACT_NAME_ID)
                            name = name_elem.get_text() if name_elem.exists else ""
                            if name and not any(c["contact_id"] == name for c in contacts):
                                contacts.append({"contact_id": name, "name": name})
                    except Exception:
                        continue

        except Exception:
            pass
        return contacts

    def _open_conversation(self, device, contact_id: str, name: str, open_bounds: str | None = None) -> bool:
        # Try clicking the contact directly in the chat list
        try:
            item = device(resourceId=_CONTACT_NAME_ID, text=name)
            if item.exists(timeout=2):
                item.click()
                time.sleep(1.2)
                return True
        except Exception:
            pass
        # Fall back to notification tap (most reliable for WeChat)
        return super()._open_conversation(device, contact_id, name, open_bounds)

    def _read_conversation(self, device, contact_id: str, thread_id: str = "") -> list[AndroidMessage]:
        messages: list[AndroidMessage] = []
        try:
            screen_width = device.info.get("displayWidth", 1080)
            msg_list = device(resourceId=_MSG_LIST_ID)
            if msg_list.exists(timeout=3):
                msg_list.fling.toEnd(max_swipes=5)
                time.sleep(0.5)

            index = 0
            # WeChat message bubbles are typically in LinearLayout or FrameLayout items
            items = device(resourceId=_MSG_LIST_ID).child(className="android.widget.LinearLayout")
            if not device(resourceId=_MSG_LIST_ID).exists:
                # Fallback: try to get all visible text elements and infer from position
                items = device(className="android.widget.ListView", packageName=_PKG).child(
                    className="android.widget.LinearLayout"
                )

            for item in items:
                text_elem = item.child(className="android.widget.TextView")
                if not text_elem.exists:
                    continue
                text = text_elem.get_text()
                if not text or len(text.strip()) == 0:
                    continue
                try:
                    bounds = item.info.get("bounds", {})
                    right = bounds.get("right", 0)
                    role = "out" if right > screen_width * 0.72 else "in"
                except Exception:
                    role = "in"
                messages.append(AndroidMessage(role=role, text=text, index=index))
                index += 1
        except Exception:
            pass
        return messages

    def _send_reply(self, device, text: str) -> bool:
        try:
            input_box = device(resourceId=_INPUT_ID)
            if not input_box.exists(timeout=3):
                # Try clicking the bottom area to activate input
                device.click(0.5, 0.95)
                time.sleep(0.5)
                input_box = device(resourceId=_INPUT_ID)
            if not input_box.exists(timeout=2):
                return False
            input_box.set_text(text)
            time.sleep(0.3)
            send_btn = device(resourceId=_SEND_BTN_ID)
            if send_btn.exists(timeout=2):
                send_btn.click()
                time.sleep(1.0)
                return True
            input_box.press("enter")
            time.sleep(1.0)
            return True
        except Exception:
            return False

    def _fill_reply(self, device, text: str) -> None:
        try:
            device(resourceId=_INPUT_ID).set_text(text)
        except Exception:
            pass
