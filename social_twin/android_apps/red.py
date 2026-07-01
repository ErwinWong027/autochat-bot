from __future__ import annotations

import time
from typing import Any

from ..android_base import AndroidBaseConnector, AndroidMessage


# Verify with: adb shell uiautomator dump /dev/stdout | grep -i "xhs\|input\|send\|unread"
_PKG = "com.xingin.xhs"
_CHAT_LIST_ID = f"{_PKG}:id/recycler_view"
_UNREAD_BADGE_ID = f"{_PKG}:id/unread_dot"
_CONTACT_NAME_ID = f"{_PKG}:id/nickname"
_INPUT_ID = f"{_PKG}:id/input_field"
_SEND_BTN_ID = f"{_PKG}:id/send_icon"


class RedConnector(AndroidBaseConnector):
    app_name = "red"
    package_name = _PKG
    channel = "red"

    def _find_unread_contacts(self, device) -> list[dict[str, Any]]:
        contacts = []
        try:
            # Navigate to 消息 tab (icon at bottom nav)
            device(description="消息").click_exists(timeout=2) or device(text="消息").click_exists(timeout=2)
            time.sleep(0.8)
            items = device(resourceId=_CHAT_LIST_ID).child(className="android.view.ViewGroup")
            for item in items:
                if not item.child(resourceId=_UNREAD_BADGE_ID).exists:
                    continue
                try:
                    name_elem = item.child(resourceId=_CONTACT_NAME_ID)
                    name = name_elem.get_text() if name_elem.exists else ""
                except Exception:
                    name = ""
                if name:
                    contacts.append({"contact_id": name, "name": name})
        except Exception:
            pass
        return contacts

    def _open_conversation(self, device, contact_id: str, name: str, open_bounds: str | None = None) -> bool:
        try:
            item = device(resourceId=_CONTACT_NAME_ID, text=name)
            if item.exists(timeout=2):
                item.click()
                time.sleep(1.2)
                return True
        except Exception:
            pass
        return super()._open_conversation(device, contact_id, name, open_bounds)

    def _read_conversation(self, device, contact_id: str, thread_id: str = "") -> list[AndroidMessage]:
        messages: list[AndroidMessage] = []
        try:
            screen_width = device.info.get("displayWidth", 1080)
            index = 0
            # Scroll to end first
            device(className="android.widget.ListView").fling.toEnd(max_swipes=5) if device(
                className="android.widget.ListView"
            ).exists else None
            time.sleep(0.5)
            items = device(className="android.view.ViewGroup", packageName=_PKG)
            for item in items:
                text_elem = item.child(className="android.widget.TextView")
                if not text_elem.exists:
                    continue
                text = text_elem.get_text()
                if not text:
                    continue
                try:
                    bounds = item.info.get("bounds", {})
                    right = bounds.get("right", 0)
                    role = "out" if right > screen_width * 0.75 else "in"
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
