from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .llm import LLMClient, parse_json_object


PROFILE_FIELDS = [
    "platform_name",
    "age",
    "gender",
    "photo_description",
    "photo_urls",
    "height",
    "zodiac",
    "education",
    "location",
    "job",
    "hometown",
    "income",
    "exercise",
    "drinking",
    "religion",
    "politics",
    "dating_intentions",
    "family_plans",
    "hobbies",
    "interest_tags",
    "bio",
    "profile_prompts",
    "personality",
    "social_preferences",
]


@dataclass(frozen=True)
class ProfileAnalysis:
    updates: list[dict[str, Any]]
    raw: str = ""


class ProfileAnalyzer:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm

    def analyze_text(self, text: str, source: str = "profile_text") -> ProfileAnalysis:
        text = (text or "").strip()
        if not text:
            return ProfileAnalysis(updates=[])
        if self.llm:
            try:
                raw = self.llm.chat(
                    model=self.settings.reply_model,
                    messages=[{"role": "user", "content": self._prompt(text)}],
                    temperature=0.1,
                    max_tokens=900,
                )
                return ProfileAnalysis(updates=self._normalize_llm_updates(raw, source), raw=raw)
            except Exception:
                pass
        return ProfileAnalysis(updates=self.extract_rules(text, source=source), raw=text)

    def analyze_image(self, image_path: str, text_hint: str = "") -> ProfileAnalysis:
        if not self.llm:
            return ProfileAnalysis(updates=self.extract_rules(text_hint, source="profile_image_text_hint"), raw=text_hint)
        raw = self.llm.chat_with_image(
            model=self.settings.profile_vision_model,
            prompt=self._prompt(text_hint or "请读取这张社交主页截图"),
            image_path=image_path,
            temperature=0.1,
            max_tokens=1200,
        )
        return ProfileAnalysis(updates=self._normalize_llm_updates(raw, "profile_image"), raw=raw)

    def extract_from_message(self, message: str) -> list[dict[str, Any]]:
        return self.extract_rules(message, source="message")

    def extract_rules(self, text: str, source: str) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        patterns = [
            ("height", r"(\d{3})\s*(?:cm|厘米|公分)", 0.85),
            ("zodiac", r"(白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼)座?", 0.8),
            ("education", r"(博士|硕士|研究生|本科|大专|高中)", 0.78),
            ("hometown", r"(?:老家|家乡|来自|人在|我在|坐标)(?:是|在)?([\u4e00-\u9fa5]{2,8}?)(?:，|,|。| |做|从事|$)", 0.68),
            ("job", r"(?:做|从事|职业是|工作是)([\u4e00-\u9fa5A-Za-z0-9]{2,12})", 0.7),
            ("income", r"(\d{1,3}(?:-\d{1,3})?万|\d{1,3}k|\d{1,3}K)", 0.72),
        ]
        for field, pattern, confidence in patterns:
            match = re.search(pattern, text)
            if match:
                updates.append(self._update(field, match.group(1), confidence, source, text))

        hobbies = []
        for word in ["音乐", "旅行", "健身", "运动", "动漫", "电影", "摄影", "阅读", "哲学", "美食", "滑雪", "露营"]:
            if word in text:
                hobbies.append(word)
        if hobbies:
            updates.append(self._update("hobbies", "、".join(dict.fromkeys(hobbies)), 0.65, source, text))

        personalities = []
        for word in ["外向", "内向", "慢热", "开朗", "理性", "感性", "独立", "温柔", "ENTJ", "INFP", "INTJ", "ENFP"]:
            if word in text:
                personalities.append(word)
        if personalities:
            updates.append(self._update("personality", "、".join(dict.fromkeys(personalities)), 0.65, source, text))
        return updates

    def _prompt(self, text: str) -> str:
        return f"""
你是联系人画像抽取器。只抽取社交主页或对话中明确出现的信息，不要推断未展示事实。
需要字段：{', '.join(PROFILE_FIELDS)}
输出JSON：{{"updates":[{{"field":"字段名","value":"值","confidence":0.0到1.0,"evidence":"证据原文"}}]}}
如果字段没有明确证据，不要输出该字段。
输入：
{text}
"""

    def _normalize_llm_updates(self, raw: str, source: str) -> list[dict[str, Any]]:
        try:
            payload = parse_json_object(raw)
        except Exception:
            return self.extract_rules(raw, source)
        rows = payload.get("updates", payload if isinstance(payload, list) else [])
        updates = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            field = str(row.get("field", "")).strip()
            value = row.get("value", "")
            if field not in PROFILE_FIELDS or value in ("", None, []):
                continue
            if isinstance(value, list):
                value = "、".join(str(item) for item in value if str(item).strip())
            updates.append(
                self._update(
                    field=field,
                    value=str(value).strip(),
                    confidence=float(row.get("confidence", 0.6)),
                    source=source,
                    evidence=str(row.get("evidence", "")).strip(),
                )
            )
        return updates

    def _update(self, field: str, value: str, confidence: float, source: str, evidence: str) -> dict[str, Any]:
        return {
            "field": field,
            "value": value.strip(),
            "confidence": max(0.0, min(1.0, confidence)),
            "source": source,
            "evidence": evidence.strip(),
        }
