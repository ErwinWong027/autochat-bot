from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from social_twin.service import DigitalTwinService, DraftRequest


app = FastAPI(title="Social Strategy Digital Twin", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
service = DigitalTwinService()
browser_agent = None
bumble_agent = None
android_agents: dict = {}


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


class DraftIn(BaseModel):
    contact_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    channel: str = "manual"
    conversation_id: Optional[str] = None
    message_id: str = ""
    contact_profile: str = ""
    contact_identity: str = ""
    relationship_stage: str = ""
    taboos: str = ""
    preferences: str = ""
    recent_emotion: str = ""
    interaction_frequency: str = ""


class ProfileUpdateIn(BaseModel):
    updates: List[dict]


class BrowserAgentRunIn(BaseModel):
    target_url: str = ""
    unread_selector: str = ""
    message_selector: str = ""
    input_selector: str = ""
    send_selector: str = ""
    contact_selector: str = ""
    auto_send_enabled: Optional[bool] = None
    poll_seconds: int = 5


class BumbleRunIn(BaseModel):
    target_url: str = ""
    auto_send_enabled: Optional[bool] = None
    poll_seconds: int = 5
    refresh_profile: bool = False


class AndroidRunIn(BaseModel):
    adb_address: str = ""
    auto_send_enabled: Optional[bool] = None
    poll_seconds: int = 0


@app.on_event("startup")
def startup() -> None:
    global browser_agent, bumble_agent, android_agents
    from social_twin.android_apps import REGISTRY
    from social_twin.bumble import BumbleConnector
    from social_twin.connectors import BrowserAgentConnector

    service.initialize()
    browser_agent = BrowserAgentConnector(service)
    bumble_agent = BumbleConnector(service)
    android_agents = {app_key: cls(service) for app_key, cls in REGISTRY.items()}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/knowledge/report")
def knowledge_report() -> dict:
    return service.report()


@app.get("/contacts")
def list_contacts() -> dict:
    return {
        "summary": service.memory.contact_overview(),
        "contacts": service.memory.list_contacts(),
    }


@app.get("/contacts/{contact_id}")
def contact_detail(contact_id: str) -> dict:
    return service.memory.get_contact_detail(contact_id)


@app.post("/draft")
def create_draft(payload: DraftIn) -> dict:
    return service.create_draft(DraftRequest(**payload.dict()))


@app.post("/contacts/{contact_id}/profile/analyze")
async def analyze_profile(
    contact_id: str,
    profile_text: str = Form(""),
    image: Optional[UploadFile] = File(None),
) -> dict:
    image_path = ""
    if image and image.filename:
        suffix = Path(image.filename).suffix or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await image.read())
            image_path = tmp.name
    return service.analyze_profile(contact_id=contact_id, text=profile_text, image_path=image_path)


@app.get("/contacts/{contact_id}/profile")
def get_profile(contact_id: str) -> dict:
    return service.get_profile(contact_id)


@app.post("/contacts/{contact_id}/profile/update")
def update_profile(contact_id: str, payload: ProfileUpdateIn) -> dict:
    return service.update_profile(contact_id, payload.updates)


@app.post("/agent/browser/run")
def browser_agent_run(payload: BrowserAgentRunIn) -> dict:
    from social_twin.connectors import BrowserAgentConfig

    return browser_agent.run(BrowserAgentConfig(**payload.dict()))


@app.post("/agent/browser/stop")
def browser_agent_stop() -> dict:
    return browser_agent.stop()


@app.post("/agent/bumble/run")
def bumble_agent_run(payload: BumbleRunIn) -> dict:
    from social_twin.bumble import BumbleConfig

    return bumble_agent.run(BumbleConfig(**payload.dict()))


@app.post("/agent/bumble/stop")
def bumble_agent_stop() -> dict:
    return bumble_agent.stop()


@app.get("/agent/bumble/status")
def bumble_agent_status() -> dict:
    return bumble_agent.status()


@app.post("/agent/android/{app}/run")
def android_agent_run(app: str, payload: AndroidRunIn) -> dict:
    if app not in android_agents:
        raise HTTPException(status_code=404, detail=f"未知 app: {app}，支持: {list(android_agents)}")
    from social_twin.android_base import AndroidConfig

    return android_agents[app].run(AndroidConfig(**payload.dict()))


@app.post("/agent/android/{app}/stop")
def android_agent_stop(app: str) -> dict:
    if app not in android_agents:
        raise HTTPException(status_code=404, detail=f"未知 app: {app}")
    return android_agents[app].stop()


@app.get("/agent/android/{app}/status")
def android_agent_status(app: str) -> dict:
    if app not in android_agents:
        raise HTTPException(status_code=404, detail=f"未知 app: {app}")
    return android_agents[app].status()


frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")
