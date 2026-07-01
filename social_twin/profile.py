from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .llm import LLMClient, parse_json_object


PROFILE_FIELDS = [
    "name",
    "age",
    "height",
    "education",
    "job",
    "company",
    "school",
    "zodiac",
    "location",
    "hometown",
    "about_me",
    "personality_traits",
    "interests_hobbies",
    "profile_prompts",
    "compatibility_points",
    "raw_evidence",
]


@dataclass(frozen=True)
class ProfileAnalysis:
    updates: list[dict[str, Any]]
    raw: str = ""
    prompt: str = ""


class ProfileAnalyzer:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm

    def analyze_text(self, text: str, source: str = "profile_text") -> ProfileAnalysis:
        text = (text or "").strip()
        if not text:
            return ProfileAnalysis(updates=[])
        if self.llm:
            the_prompt = self._prompt(text)
            try:
                raw = self.llm.chat(
                    model=self.settings.reply_model,
                    messages=[{"role": "user", "content": the_prompt}],
                    temperature=0.1,
                    max_tokens=2000,
                )
                return ProfileAnalysis(updates=self._normalize_llm_updates(raw, source), raw=raw, prompt=the_prompt)
            except Exception:
                return ProfileAnalysis(updates=self.extract_rules(text, source=source), raw=text, prompt=the_prompt)
        return ProfileAnalysis(updates=self.extract_rules(text, source=source), raw=text)

    def analyze_image(self, image_path: str, text_hint: str = "") -> ProfileAnalysis:
        if not self.llm:
            return ProfileAnalysis(updates=self.extract_rules(text_hint, source="profile_image_text_hint"), raw=text_hint)
        the_prompt = self._prompt(text_hint or "请读取这张社交主页截图")
        raw = self.llm.chat_with_image(
            model=self.settings.profile_vision_model,
            prompt=the_prompt,
            image_path=image_path,
            temperature=0.1,
            max_tokens=1200,
        )
        return ProfileAnalysis(updates=self._normalize_llm_updates(raw, "profile_image"), raw=raw, prompt=the_prompt)

    def extract_from_message(self, message: str) -> list[dict[str, Any]]:
        return self.extract_rules(message, source="message")

    def extract_rules(self, text: str, source: str) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []

        # Single-match patterns: first listed pattern per field wins (seen_fields guard).
        # "做" removed from job prefixes: "做事很积极" is a personality trait, not a job title.
        # "在" removed from company prefixes: "能稳定在西安工作" incorrectly maps city to company.
        patterns = [
            ("age", r"(\d{2})岁", 0.85),
            ("height", r"身高\s*(\d{3})", 0.85),
            ("height", r"(\d{3})\s*(?:cm|厘米|公分)", 0.85),
            ("zodiac", r"(白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼)座?", 0.8),
            ("education", r"(博士|硕士|研究生|本科|大专|高中)", 0.78),
            ("hometown", r"(?:老家|家乡|来自|人在|我在|坐标)(?:是|在)?([\u4e00-\u9fa5]{2,8}?)(?:，|,|。| |做|从事|$)", 0.68),
            ("job", r"\d{2}岁[·•]([\u4e00-\u9fa5A-Za-z0-9]{2,15})", 0.82),
            ("job", r"(?:从事|职业是|工作是|职业[：:])([\u4e00-\u9fa5A-Za-z0-9]{2,12})", 0.7),
            ("company", r"(?:任职于|供职于)([\u4e00-\u9fa5A-Za-z0-9]{2,20})(?:公司|工作|上班)", 0.68),
            ("location", r"IP属地[：:]\s*([\u4e00-\u9fa5]{2,6})", 0.8),
        ]
        seen_fields: set[str] = set()
        for field, pattern, confidence in patterns:
            if field in seen_fields:
                continue
            match = re.search(pattern, text)
            if match:
                updates.append(self._update(field, match.group(1), confidence, source, text))
                seen_fields.add(field)

        # School: collect all university/college names across multiple declaration formats.
        school_names = list(dict.fromkeys(
            re.findall(r"学校[：:]\s*([\u4e00-\u9fa5A-Za-z0-9]{2,15}(?:大学|学院))", text)
            + re.findall(r"([\u4e00-\u9fa5A-Za-z]{2,10}(?:大学|学院))毕业", text)
            + re.findall(r"(?:毕业于|就读于)([\u4e00-\u9fa5A-Za-z0-9]{2,15}(?:大学|学院))", text)
        ))
        if school_names:
            updates.append(self._update("school", "、".join(school_names), 0.78, source, text))

        # profile_prompts: "恋爱目标\n答案" format common on Tantan.
        dating_m = re.search(r"恋爱目标\s*\n?([\u4e00-\u9fa5A-Za-z0-9，,。！？\s]{4,60}?)(?:\n|$)", text)
        if dating_m:
            updates.append(self._update(
                "profile_prompts",
                json.dumps([{"title": "恋爱目标", "answer": dating_m.group(1).strip()}], ensure_ascii=False),
                0.72, source, text,
            ))

        hobbies = []
        for word in [
            "音乐",
            "旅行",
            "健身",
            "运动",
            "动漫",
            "电影",
            "摄影",
            "阅读",
            "哲学",
            "美食",
            "滑雪",
            "露营",
            "小酌",
            "散步",
            "宅家",
            "星盘",
            "算命",
            "桌游",
            "剧本杀",
            "绘画",
            "茶艺",
            "骑行",
        ]:
            if word in text:
                hobbies.append(word)
        if hobbies:
            updates.append(self._update("interests_hobbies", "、".join(dict.fromkeys(hobbies)), 0.65, source, text))

        personalities = []
        trait_patterns = OrderedDict(
            [
                ("INFJ-A", r"\bINFJ-A\b"),
                ("INFJ", r"\bINFJ\b"),
                ("ENTJ", r"\bENTJ\b"),
                ("INFP", r"\bINFP\b"),
                ("INTJ", r"\bINTJ\b"),
                ("ENFP", r"\bENFP\b"),
                ("恋爱脑但看重精神连接", r"恋爱脑.{0,12}精神"),
                ("brat", r"\bbrat\b|Brat"),
                ("朋友多", r"朋友很多|朋友多"),
                ("谨慎慢热", r"客气-观察-谨慎|谨慎|慢热"),
                ("吐槽型亲密", r"吐槽|骂街"),
                ("温柔承接", r"温柔|承接"),
                ("焦虑型回避依恋", r"焦虑型回避依恋|回避依恋"),
                ("高敏感", r"高敏感|敏感"),
                ("真诚至上", r"真诚至上|真诚"),
                ("自我反省", r"自我反省|反省"),
                ("自我认知清晰", r"认知.{0,8}清楚|认知.{0,8}清晰"),
                ("需要情绪安全", r"情绪安全|安全感"),
                ("追求稳定关系", r"持稳的关系|稳定.{0,8}关系"),
                ("慕强", r"慕强"),
                ("悲观务实", r"悲观务实"),
                ("神经大条", r"神经大条"),
                ("被动型", r"被动型性格|被动型"),
                ("不喜欢网聊", r"不喜欢网聊|讨厌网聊"),
                ("外向", r"外向"),
                ("内向", r"内向"),
                ("开朗", r"开朗"),
                ("理性", r"理性"),
                ("感性", r"感性"),
                ("独立", r"独立"),
            ]
        )
        for label, pattern in trait_patterns.items():
            if re.search(pattern, text, flags=re.IGNORECASE):
                personalities.append(label)
        if personalities:
            updates.append(self._update("personality_traits", "、".join(dict.fromkeys(personalities)), 0.82, source, text))
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
