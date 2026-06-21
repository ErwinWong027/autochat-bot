import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str
    dashscope_base_url: str
    decision_model: str
    reply_model: str
    cheap_model: str
    premium_model: str
    profile_vision_model: str
    profile_ocr_model: str
    auto_send_enabled: bool
    browser_agent_target_url: str
    browser_agent_poll_seconds: int
    bumble_target_url: str
    bumble_poll_seconds: int
    bumble_user_data_dir: str
    knowledge_path: str
    persona_dir: str
    lance_db_path: str
    lance_table: str
    sqlite_path: str
    embed_model: str
    server_host: str
    server_port: int


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        decision_model=os.getenv("DECISION_MODEL", "qwen3.7-plus"),
        reply_model=os.getenv("REPLY_MODEL", "qwen3.7-plus"),
        cheap_model=os.getenv("CHEAP_MODEL", "qwen3.6-flash"),
        premium_model=os.getenv("PREMIUM_MODEL", "qwen3.7-max"),
        profile_vision_model=os.getenv("PROFILE_VISION_MODEL", "qwen3-vl-plus"),
        profile_ocr_model=os.getenv("PROFILE_OCR_MODEL", "qwen-vl-ocr-latest"),
        auto_send_enabled=os.getenv("AUTO_SEND_ENABLED", "false").lower() == "true",
        browser_agent_target_url=os.getenv("BROWSER_AGENT_TARGET_URL", ""),
        browser_agent_poll_seconds=int(os.getenv("BROWSER_AGENT_POLL_SECONDS", "5")),
        bumble_target_url=os.getenv("BUMBLE_TARGET_URL", "https://bumble.com/app/connections"),
        bumble_poll_seconds=int(os.getenv("BUMBLE_POLL_SECONDS", "5")),
        bumble_user_data_dir=os.getenv("BUMBLE_USER_DATA_DIR", "./browser_profiles/bumble"),
        knowledge_path=os.getenv("KNOWLEDGE_PATH", "all_chapters.json"),
        persona_dir=os.getenv("PERSONA_DIALOGUES_DIR", "persona_dialogues"),
        lance_db_path=os.getenv("LANCE_DB_PATH", "./lancedb"),
        lance_table=os.getenv("LANCE_TABLE", "dialogue_cases_v2"),
        sqlite_path=os.getenv("SQLITE_PATH", "social_twin.db"),
        embed_model=os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2"),
        server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.getenv("SERVER_PORT", "8000")),
    )
