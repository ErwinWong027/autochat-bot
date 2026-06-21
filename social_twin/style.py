from __future__ import annotations

import re
from dataclasses import dataclass


AI_WORDS = [
    "我理解",
    "你的感受",
    "听起来",
    "从某种程度",
    "总的来说",
    "总结",
    "建议你",
    "不妨",
    "或许可以",
    "作为一个",
    "陪伴",
    "能量",
    "治愈",
]

JOKE_WORDS = ["哈哈", "笑死", "开玩笑", "梗", "段子"]
METAPHOR_WORDS = ["像", "仿佛", "如同", "就像", "好比"]
QUESTION_ENDINGS = ("?", "？", "吗", "呢", "嘛")


@dataclass(frozen=True)
class StyleResult:
    text: str
    issues: list[str]
    needs_rewrite: bool


def remove_emoji(text: str) -> str:
    return re.sub(r"[\U00010000-\U0010ffff]", "", text)


def normalize_draft(text: str) -> str:
    text = remove_emoji(text).strip()
    text = re.split(r"[\r\n。！？!?；;]", text)[0].strip()
    text = re.sub(r"[，,。.!！?？~～；;：:]+$", "", text).strip()
    return text


def inspect_style(text: str, recent_assistant: list[str] | None = None) -> StyleResult:
    recent_assistant = recent_assistant or []
    cleaned = normalize_draft(text)
    issues: list[str] = []
    if not cleaned:
        issues.append("empty")
    if len(cleaned) > 18:
        issues.append("too_long")
    if "\n" in text or len(re.split(r"[。！？!?；;]", text.strip())) > 2:
        issues.append("paragraph_or_multi_sentence")
    if any(word in cleaned for word in AI_WORDS):
        issues.append("ai_tone")
    if any(word in cleaned for word in JOKE_WORDS):
        issues.append("forced_joke")
    if any(word in cleaned for word in METAPHOR_WORDS):
        issues.append("metaphor")
    if cleaned.endswith(QUESTION_ENDINGS):
        recent_question_count = sum(normalize_draft(item).endswith(QUESTION_ENDINGS) for item in recent_assistant[-2:])
        if recent_question_count >= 1:
            issues.append("too_many_questions")
    return StyleResult(text=cleaned, issues=issues, needs_rewrite=bool(issues))
