from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from .service import DigitalTwinService, DraftRequest


@dataclass(frozen=True)
class ConnectorResult:
    draft: str
    payload: dict
    requires_human_confirmation: bool = True


class Connector(Protocol):
    name: str

    def draft(self, request: DraftRequest) -> ConnectorResult:
        ...


class ManualConnector:
    name = "manual"

    def __init__(self, service: DigitalTwinService):
        self.service = service

    def draft(self, request: DraftRequest) -> ConnectorResult:
        payload = self.service.create_draft(request)
        return ConnectorResult(draft=payload["draft"], payload=payload)


class ApiConnector:
    name = "api"

    def draft(self, request: DraftRequest) -> ConnectorResult:
        raise NotImplementedError("API connector only defines the future integration boundary in v1.")


class BrowserAgentConnector:
    name = "browser_agent"

    def __init__(self, service: DigitalTwinService):
        self.service = service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict = {"running": False, "last_error": "", "last_draft": "", "sent_count": 0}

    def draft(self, request: DraftRequest) -> ConnectorResult:
        payload = self.service.create_draft(request)
        return ConnectorResult(
            draft=payload["draft"],
            payload=payload,
            requires_human_confirmation=not self.service.settings.auto_send_enabled,
        )

    def run(self, config: "BrowserAgentConfig") -> dict:
        if self._thread and self._thread.is_alive():
            return self.status()
        self._stop.clear()
        self._status = {"running": True, "last_error": "", "last_draft": "", "sent_count": 0}
        self._thread = threading.Thread(target=self._loop, args=(config,), daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        self._status["running"] = False
        return self.status()

    def status(self) -> dict:
        return dict(self._status)

    def _loop(self, config: "BrowserAgentConfig") -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._status.update({"running": False, "last_error": f"playwright不可用：{exc}"})
            return

        auto_send = self.service.settings.auto_send_enabled if config.auto_send_enabled is None else config.auto_send_enabled
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                page = browser.new_page()
                page.goto(config.target_url or self.service.settings.browser_agent_target_url)
                while not self._stop.is_set():
                    unread = page.query_selector(config.unread_selector)
                    if not unread:
                        time.sleep(config.poll_seconds)
                        continue
                    unread.click()
                    contact = self._text(page, config.contact_selector) or "browser_contact"
                    message = self._text(page, config.message_selector)
                    if not message:
                        time.sleep(config.poll_seconds)
                        continue
                    payload = self.service.create_draft(
                        DraftRequest(contact_id=contact, message=message, channel="browser_agent")
                    )
                    draft = payload["draft"]
                    self._status["last_draft"] = draft
                    if auto_send:
                        page.fill(config.input_selector, draft)
                        page.click(config.send_selector)
                        self._status["sent_count"] += 1
                    time.sleep(config.poll_seconds)
                browser.close()
        except Exception as exc:
            self._status.update({"last_error": str(exc)})
        finally:
            self._status["running"] = False

    def _text(self, page, selector: str) -> str:
        if not selector:
            return ""
        element = page.query_selector(selector)
        return element.inner_text().strip() if element else ""


@dataclass(frozen=True)
class BrowserAgentConfig:
    target_url: str = ""
    unread_selector: str = ""
    message_selector: str = ""
    input_selector: str = ""
    send_selector: str = ""
    contact_selector: str = ""
    auto_send_enabled: bool | None = None
    poll_seconds: int = 5
