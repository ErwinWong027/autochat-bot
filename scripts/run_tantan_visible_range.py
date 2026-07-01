from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from social_twin.android_apps.tantan import (  # noqa: E402
    _BACK_BTN_ID,
    _CHAT_LIST_ID,
    _CONTACT_NAME_ID,
    _CONV_ITEM_ROOT,
    _LAST_MSG_ID,
    _PKG,
    _RED_DOT_ID,
    TantanConnector,
)
from social_twin.service import DigitalTwinService  # noqa: E402


def _bounds_top(bounds: str) -> int:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    return int(match.group(2)) if match else 999999


def _visible_rows(device: Any) -> list[dict[str, Any]]:
    xml_root = ET.fromstring(device.dump_hierarchy())
    rows: list[dict[str, Any]] = []
    for item in xml_root.iter("node"):
        if item.get("resource-id") != _CONV_ITEM_ROOT:
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
        if name:
            rows.append(
                {
                    "name": name,
                    "preview": preview,
                    "has_red_dot": has_red_dot,
                    "bounds": item.get("bounds", ""),
                    "top": _bounds_top(item.get("bounds", "")),
                }
            )
    return sorted(rows, key=lambda row: row["top"])


def _rows_between(rows: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    names = [row["name"] for row in rows]
    if start not in names:
        raise SystemExit(f"当前可见列表未找到起点联系人: {start}")
    if end not in names:
        raise SystemExit(f"当前可见列表未找到终点联系人: {end}")
    start_i = names.index(start)
    end_i = names.index(end)
    if start_i > end_i:
        raise SystemExit(f"起点在终点之后: {start} -> {end}")
    return rows[start_i : end_i + 1]


def _return_to_list(connector: TantanConnector, device: Any) -> bool:
    for _ in range(6):
        try:
            if device(resourceId=_CHAT_LIST_ID).exists(timeout=1):
                return True
            if device(resourceId=_BACK_BTN_ID).exists(timeout=1):
                device(resourceId=_BACK_BTN_ID).click()
            else:
                device.press("back")
            time.sleep(1)
            connector._ensure_message_tab(device)
        except Exception:
            time.sleep(1)
    return bool(device(resourceId=_CHAT_LIST_ID).exists(timeout=1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a visible Tantan contact range once.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--adb-address", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import uiautomator2 as u2

    service = DigitalTwinService()
    service.initialize()
    connector = TantanConnector(service)

    device = u2.connect(args.adb_address) if args.adb_address else u2.connect()
    device.screen_on()
    device.app_start(_PKG)
    time.sleep(1)
    connector._ensure_message_tab(device)
    time.sleep(1)

    rows = _rows_between(_visible_rows(device), args.start, args.end)
    target_names = [row["name"] for row in rows]
    report: list[dict[str, Any]] = []
    print(json.dumps({"visible_range": rows}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return

    for name in target_names:
        if not _return_to_list(connector, device):
            report.append({"name": name, "status": "stopped_not_on_list"})
            break
        current_rows = {row["name"]: row for row in _visible_rows(device)}
        row = current_rows.get(name)
        if not row:
            report.append({"name": name, "status": "skipped_not_visible"})
            continue
        if not row["has_red_dot"]:
            report.append({"name": name, "status": "skipped_no_red_dot"})
            continue
        contact_id = f"{connector.app_name}:{name}"
        before_sent = int(connector.status().get("sent_count", 0))
        ok = connector._process_contact(
            device,
            contact_id=contact_id,
            name=name,
            auto_send=True,
            list_preview=row.get("preview", ""),
            open_bounds=row.get("bounds", ""),
        )
        after_status = connector.status()
        after_sent = int(after_status.get("sent_count", 0))
        report.append(
            {
                "name": name,
                "contact_id": contact_id,
                "ok": ok,
                "sent_delta": after_sent - before_sent,
                "stage": after_status.get("stage"),
                "last_error": after_status.get("last_error"),
            }
        )
        _return_to_list(connector, device)
        time.sleep(1)

    print(json.dumps({"processed": report, "status": connector.status()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
