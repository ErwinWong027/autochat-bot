from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KnowledgeSample:
    source_id: str
    source_type: str
    sample_type: str
    technique: str
    context: str
    context_3: str
    context_5: str
    dialogue_summary: str
    reply: str
    thinking: str
    summary: str
    position: str
    reply_style: str
    usage: list[str]
    precautions: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CoverageReport:
    chapters: int
    techniques: int
    dialogue_blocks: int
    total_a_replies: int
    annotated_strategy: int
    natural_dialogue: int
    total_indexed_samples: int
    persona_samples: int
    technique_names: list[str]

    @property
    def indexed_samples(self) -> int:
        return self.total_indexed_samples

    @property
    def missing_thinking_summary(self) -> int:
        return self.natural_dialogue

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["indexed_samples"] = self.indexed_samples
        payload["missing_thinking_summary"] = self.missing_thinking_summary
        return payload


def _technique_from_chapter(chapter_key: str) -> str:
    match = re.search(r"[:：]\s*(.+)", chapter_key)
    return match.group(1).strip() if match else chapter_key.strip()


def _stable_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _position(turn_index: int, dialogue_len: int) -> str:
    if turn_index <= 1:
        return "开场"
    if turn_index >= max(0, dialogue_len - 2):
        return "收束"
    return "推进" if turn_index >= 5 else "承接"


def _reply_style(reply: str) -> str:
    if len(reply) <= 6:
        return "短句"
    if reply.endswith(("?", "？", "吗", "呢", "嘛")):
        return "确认"
    if any(word in reply for word in ["哈哈", "笑死", "哦吼", "生气了", "哥哥"]):
        return "调侃"
    if any(word in reply for word in ["累", "辛苦", "注意", "缓", "别"]):
        return "安抚"
    if any(word in reply for word in ["下次", "请我", "公开", "发誓"]):
        return "轻推"
    if any(word in reply for word in ["那就好", "嗯", "好", "可以"]):
        return "接话"
    if any(word in reply for word in ["但是", "不过", "看来"]):
        return "转向"
    return "拉扯"


def load_strategy_knowledge(path: str | Path) -> tuple[dict[str, dict[str, Any]], list[KnowledgeSample], CoverageReport]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    technique_theory: dict[str, dict[str, Any]] = {}
    samples: list[KnowledgeSample] = []
    dialogue_blocks = 0
    total_a_replies = 0
    annotated_strategy = 0
    natural_dialogue = 0

    for chapter_key, cases in data.items():
        technique = _technique_from_chapter(chapter_key)
        theory = next((item.get("theory") for item in cases if "theory" in item), {}) or {}
        technique_theory[technique] = theory
        usage = list(theory.get("usage", []))
        precautions = list(theory.get("precautions", []))

        for item_index, item in enumerate(cases):
            dialogue = item.get("dialogue")
            if not dialogue:
                continue
            dialogue_blocks += 1
            history: list[str] = []
            for turn_index, turn in enumerate(dialogue):
                reply = turn.get("reply") or {}
                content = str(reply.get("content", "")).strip()
                role = turn.get("role")
                if not content:
                    continue
                if role == "B":
                    history.append(f"B: {content}")
                    continue
                if role != "A":
                    continue

                total_a_replies += 1
                thinking = str(turn.get("thinking", "")).strip()
                summary = str(turn.get("summary", "")).strip()
                sample_type = "annotated_strategy" if thinking and summary else "natural_dialogue"
                if sample_type == "annotated_strategy":
                    annotated_strategy += 1
                else:
                    natural_dialogue += 1
                    summary = "自然对话样本"
                context_3 = " | ".join(history[-3:]) if history else "[开场]"
                context_5 = " | ".join(history[-5:]) if history else "[开场]"
                full_dialogue = " | ".join(
                    f"{item_turn.get('role')}: {(item_turn.get('reply') or {}).get('content', '')}"
                    for item_turn in dialogue
                    if (item_turn.get("reply") or {}).get("content")
                )
                source_id = _stable_id(chapter_key, str(item_index), str(turn_index), content, sample_type)
                samples.append(
                    KnowledgeSample(
                        source_id=source_id,
                        source_type="strategy",
                        sample_type=sample_type,
                        technique=technique,
                        context=context_5,
                        context_3=context_3,
                        context_5=context_5,
                        dialogue_summary=full_dialogue[:500],
                        reply=content,
                        thinking=thinking,
                        summary=summary,
                        position=_position(turn_index, len(dialogue)),
                        reply_style=_reply_style(content),
                        usage=usage,
                        precautions=precautions,
                        metadata={
                            "chapter": chapter_key,
                            "item_index": item_index,
                            "turn_index": turn_index,
                        },
                    )
                )
                history.append(f"A: {content}")

    report = CoverageReport(
        chapters=len(data),
        techniques=len(technique_theory),
        dialogue_blocks=dialogue_blocks,
        total_a_replies=total_a_replies,
        annotated_strategy=annotated_strategy,
        natural_dialogue=natural_dialogue,
        total_indexed_samples=len(samples),
        persona_samples=0,
        technique_names=list(technique_theory.keys()),
    )
    return technique_theory, samples, report


def load_persona_dialogues(persona_dir: str | Path) -> list[KnowledgeSample]:
    base = Path(persona_dir)
    if not base.exists():
        return []

    samples: list[KnowledgeSample] = []
    for path in sorted(base.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("dialogues", [])
        for index, row in enumerate(rows):
            their_message = str(row.get("their_message", row.get("user_message", ""))).strip()
            my_reply = str(row.get("my_reply", row.get("reply", ""))).strip()
            if not their_message or not my_reply:
                continue
            technique = str(row.get("technique", "人物记忆")).strip() or "人物记忆"
            context = str(row.get("context", f"B: {their_message}")).strip()
            relation = str(row.get("relation", "")).strip()
            identity = str(row.get("identity", "")).strip()
            source_id = _stable_id(str(path), str(index), their_message, my_reply)
            samples.append(
                KnowledgeSample(
                    source_id=source_id,
                    source_type="persona",
                    sample_type="persona_dialogue",
                    technique=technique,
                    context=context,
                    context_3=context,
                    context_5=context,
                    dialogue_summary=context,
                    reply=my_reply,
                    thinking=str(row.get("thinking", "")).strip(),
                    summary=str(row.get("summary", row.get("effect", ""))).strip(),
                    position=str(row.get("position", "承接")).strip() or "承接",
                    reply_style=str(row.get("reply_style", _reply_style(my_reply))).strip() or _reply_style(my_reply),
                    usage=[value for value in [identity, relation] if value],
                    precautions=list(row.get("precautions", [])),
                    metadata={
                        "file": str(path),
                        "index": index,
                        "identity": identity,
                        "relation": relation,
                        "scene": row.get("scene", ""),
                        "tags": row.get("tags", []),
                    },
                )
            )
    return samples
