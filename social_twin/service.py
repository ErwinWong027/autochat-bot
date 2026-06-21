from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from .config import Settings, load_settings
from .knowledge import CoverageReport, KnowledgeSample, load_persona_dialogues, load_strategy_knowledge
from .llm import LLMClient, parse_json_object
from .memory import MemoryStore
from .profile import ProfileAnalyzer
from .style import inspect_style
from .vector_store import LanceVectorStore


@dataclass(frozen=True)
class DraftRequest:
    contact_id: str
    message: str
    channel: str = "manual"
    conversation_id: str | None = None
    message_id: str = ""
    contact_profile: str = ""
    contact_identity: str = ""
    relationship_stage: str = ""
    taboos: str = ""
    preferences: str = ""
    recent_emotion: str = ""
    interaction_frequency: str = ""


class DigitalTwinService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.technique_theory: dict[str, dict[str, Any]] = {}
        self.samples: list[KnowledgeSample] = []
        self.coverage_report: CoverageReport | None = None
        self.memory = MemoryStore(self.settings.sqlite_path)
        self.vector_store: LanceVectorStore | None = None
        self.llm: LLMClient | None = None
        self.profile_analyzer: ProfileAnalyzer | None = None

    def initialize(self) -> None:
        theory, strategy_samples, report = load_strategy_knowledge(self.settings.knowledge_path)
        persona_samples = load_persona_dialogues(self.settings.persona_dir)
        self.technique_theory = theory
        self.samples = strategy_samples + persona_samples
        self.coverage_report = CoverageReport(
            chapters=report.chapters,
            techniques=report.techniques,
            dialogue_blocks=report.dialogue_blocks,
            total_a_replies=report.total_a_replies,
            annotated_strategy=report.annotated_strategy,
            natural_dialogue=report.natural_dialogue,
            total_indexed_samples=report.total_indexed_samples + len(persona_samples),
            persona_samples=len(persona_samples),
            technique_names=report.technique_names,
        )
        self.vector_store = LanceVectorStore(
            db_path=self.settings.lance_db_path,
            table_name=self.settings.lance_table,
            embed_model=self.settings.embed_model,
        )
        self.vector_store.sync(self.samples)

    def _llm(self) -> LLMClient:
        if self.llm is None:
            self.llm = LLMClient(self.settings)
        return self.llm

    def _profile_analyzer(self, use_llm: bool = True) -> ProfileAnalyzer:
        if self.profile_analyzer is None or (use_llm and self.profile_analyzer.llm is None):
            self.profile_analyzer = ProfileAnalyzer(self.settings, self._llm() if use_llm else None)
        return self.profile_analyzer

    def report(self) -> dict[str, Any]:
        if self.coverage_report is None:
            self.initialize()
        assert self.coverage_report is not None
        return {
            **self.coverage_report.to_dict(),
            "vector_rows": self.vector_store.count() if self.vector_store else 0,
            "models": {
                "decision": self.settings.decision_model,
                "reply": self.settings.reply_model,
                "cheap": self.settings.cheap_model,
                "premium": self.settings.premium_model,
                "profile_vision": self.settings.profile_vision_model,
                "profile_ocr": self.settings.profile_ocr_model,
            },
        }

    def analyze_profile(self, contact_id: str, text: str = "", image_path: str = "") -> dict[str, Any]:
        analyzer = self._profile_analyzer(use_llm=bool(self.settings.dashscope_api_key))
        if image_path:
            analysis = analyzer.analyze_image(image_path=image_path, text_hint=text)
        else:
            analysis = analyzer.analyze_text(text=text, source="profile_text")
        updates = self.memory.apply_profile_updates(contact_id, analysis.updates)
        return {
            "contact_id": contact_id,
            "updates": updates,
            "profile": self.memory.get_contact_profile(contact_id),
            "raw": analysis.raw,
        }

    def update_profile(self, contact_id: str, updates: list[dict[str, Any]]) -> dict[str, Any]:
        applied = self.memory.apply_profile_updates(contact_id, updates)
        return {"contact_id": contact_id, "updates": applied, "profile": self.memory.get_contact_profile(contact_id)}

    def get_profile(self, contact_id: str) -> dict[str, Any]:
        return self.memory.get_contact_profile(contact_id)

    def create_draft(
        self,
        request: DraftRequest,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if self.vector_store is None:
            self.initialize()
        assert self.vector_store is not None

        self.memory.upsert_contact(
            contact_id=request.contact_id,
            identity=request.contact_identity,
            profile=request.contact_profile,
            relationship_stage=request.relationship_stage,
            taboos=request.taboos,
            preferences=request.preferences,
            recent_emotion=request.recent_emotion,
            interaction_frequency=request.interaction_frequency,
        )
        conversation_id = self.memory.get_or_create_conversation(
            contact_id=request.contact_id,
            channel=request.channel,
            conversation_id=request.conversation_id,
        )
        self.memory.add_message(
            conversation_id=conversation_id,
            contact_id=request.contact_id,
            channel=request.channel,
            role="user",
            content=request.message,
            message_id=request.message_id,
        )
        profile_updates = self.memory.apply_profile_updates(
            request.contact_id,
            ProfileAnalyzer(self.settings, None).extract_from_message(request.message),
        )
        recent = self.memory.recent_messages(conversation_id, limit=10)
        context = self._format_context(recent)
        recent_techniques = self.memory.recent_techniques(conversation_id, limit=3)
        scenario = self._detect_scenario(request.message, context)
        relationship_state = self._relationship_state(request, recent)
        structured_profile = self.memory.get_contact_profile(request.contact_id)
        if progress_callback:
            progress_callback("正在分析对话中，请等待……")
        decision = self._decide_technique(context, scenario, recent_techniques)
        technique = decision["selected_technique"]
        if progress_callback:
            progress_callback("正在数据检索 RAG 中，请等待……")
        strategy_cases = self.vector_store.query(
            context,
            technique=technique,
            sample_type="annotated_strategy",
            n_results=3,
        )
        if not strategy_cases:
            strategy_cases = self.vector_store.query(context, sample_type="annotated_strategy", n_results=3)
        natural_cases = self.vector_store.query(
            context,
            technique=technique,
            sample_type="natural_dialogue",
            n_results=5,
        )
        if not natural_cases:
            natural_cases = self.vector_store.query(context, sample_type="natural_dialogue", n_results=5)
        persona_cases = self.vector_store.query(context, sample_type="persona_dialogue", n_results=3)
        if progress_callback:
            progress_callback("正在生成回复中，请等待……")

        draft = self._generate_reply(
            context=context,
            scenario=scenario,
            relationship_state=relationship_state,
            technique=technique,
            reason=decision["reason"],
            strategy_cases=strategy_cases,
            natural_cases=natural_cases,
            persona_cases=persona_cases,
            request=request,
            recent=recent,
            structured_profile=structured_profile,
        )
        assistant_recent = [item["content"] for item in recent if item["role"] in ("assistant", "draft")]
        style = inspect_style(draft, assistant_recent)
        if style.needs_rewrite:
            draft = self._rewrite_reply(style.text or draft, style.issues, context)
            style = inspect_style(draft, assistant_recent)
        if style.needs_rewrite:
            draft = self._fallback_natural_reply(natural_cases + persona_cases)
            style = inspect_style(draft, assistant_recent)

        final_draft = style.text
        self.memory.add_message(
            conversation_id=conversation_id,
            contact_id=request.contact_id,
            channel=request.channel,
            role="draft",
            content=final_draft,
            message_id=request.message_id,
            technique=technique,
            decision_reason=decision["reason"],
        )
        return {
            "conversation_id": conversation_id,
            "contact_id": request.contact_id,
            "channel": request.channel,
            "draft": final_draft,
            "technique": technique,
            "decision_reason": decision["reason"],
            "scenario": scenario,
            "relationship_state": relationship_state,
            "style_issues": style.issues,
            "profile_updates": profile_updates,
            "contact_profile_structured": structured_profile,
            "strategy_cases": [
                {
                    "source_id": case["source_id"],
                    "source_type": case["source_type"],
                    "sample_type": case["sample_type"],
                    "technique": case["technique"],
                    "reply": case["reply"],
                    "summary": case["summary"],
                }
                for case in strategy_cases
            ],
            "natural_cases": [
                {
                    "source_id": case["source_id"],
                    "source_type": case["source_type"],
                    "sample_type": case["sample_type"],
                    "technique": case["technique"],
                    "reply": case["reply"],
                    "reply_style": case["reply_style"],
                    "position": case["position"],
                }
                for case in natural_cases
            ],
            "persona_cases": [
                {
                    "source_id": case["source_id"],
                    "source_type": case["source_type"],
                    "sample_type": case["sample_type"],
                    "reply": case["reply"],
                    "reply_style": case["reply_style"],
                }
                for case in persona_cases
            ],
            "retrieved_cases": [
                {
                    "source_id": case["source_id"],
                    "source_type": case["source_type"],
                    "sample_type": case["sample_type"],
                    "technique": case["technique"],
                    "reply": case["reply"],
                    "summary": case.get("summary", ""),
                }
                for case in [*strategy_cases, *natural_cases, *persona_cases]
            ],
            "models": {"decision": self.settings.decision_model, "reply": self.settings.reply_model},
        }

    def _format_context(self, messages: list[dict[str, Any]]) -> str:
        role_map = {"user": "B", "assistant": "A", "draft": "A草稿"}
        return " | ".join(f"{role_map.get(item['role'], item['role'])}: {item['content']}" for item in messages[-8:])

    def _detect_scenario(self, message: str, context: str) -> str:
        if any(word in message for word in ["累", "烦", "难受", "崩", "不开心"]):
            return "情绪承接"
        if any(word in message for word in ["为什么", "怎么", "吗", "？", "?"]):
            return "回应问题"
        if any(word in message for word in ["哈哈", "笑", "有趣"]):
            return "轻松互动"
        if len(context) <= 30:
            return "开场"
        return "推进关系"

    def _relationship_state(self, request: DraftRequest, recent: list[dict[str, Any]]) -> str:
        if request.relationship_stage:
            return request.relationship_stage
        turns = len(recent)
        if turns <= 2:
            return "初识"
        if turns <= 8:
            return "建立熟悉感"
        return "持续互动"

    def _decide_technique(self, context: str, scenario: str, recent_techniques: list[str]) -> dict[str, str]:
        available = list(self.technique_theory.keys())
        blocked = set(recent_techniques)
        preferred = [tech for tech in available if tech not in blocked]
        if "反问" in preferred and scenario != "轻松互动":
            preferred.remove("反问")
            preferred.append("反问")
        theory_text = "\n".join(
            f"【{tech}】用途：{'、'.join(self.technique_theory[tech].get('usage', []))}；注意：{'；'.join(self.technique_theory[tech].get('precautions', []))}"
            for tech in preferred
        )
        prompt = f"""
你是社交策略选择器，只做分类，不生成回复。
场景：{scenario}
最近已用技术：{','.join(recent_techniques) or '无'}
可选技术：
{theory_text}
上下文：{context}

规则：
1. 只能从可选技术中选一个。
2. 最近3轮用过的技术不要再选。
3. 非必要少选反问。
输出JSON：{{"selected_technique":"技术名","reason":"一句话理由"}}
"""
        try:
            raw = self._llm().chat(
                model=self.settings.decision_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=180,
            )
            parsed = parse_json_object(raw)
            selected = str(parsed.get("selected_technique", "")).strip()
            if selected not in available or selected in blocked:
                selected = preferred[0] if preferred else available[0]
            return {"selected_technique": selected, "reason": str(parsed.get("reason", "按场景匹配")).strip()}
        except Exception:
            fallback = "延词" if "延词" in preferred else (preferred[0] if preferred else available[0])
            return {"selected_technique": fallback, "reason": "模型决策失败，使用规则回退"}

    def _generate_reply(
        self,
        context: str,
        scenario: str,
        relationship_state: str,
        technique: str,
        reason: str,
        strategy_cases: list[dict[str, Any]],
        natural_cases: list[dict[str, Any]],
        persona_cases: list[dict[str, Any]],
        request: DraftRequest,
        recent: list[dict[str, Any]],
        structured_profile: dict[str, Any],
    ) -> str:
        theory = self.technique_theory.get(technique, {})
        strategy_text = "\n".join(
            f"- 对方：{case['context_5']}\n  回复：{case['reply']}\n  策略：{case['summary']}\n  风格：{case['reply_style']} / 位置：{case['position']}"
            for case in strategy_cases[:3]
        )
        natural_text = "\n".join(
            f"- 对方：{case['context_3']}\n  真人回复：{case['reply']}\n  风格：{case['reply_style']} / 位置：{case['position']}"
            for case in natural_cases[:5]
        )
        persona_text = "\n".join(
            f"- 对方：{case['context_3']}\n  过往回复：{case['reply']}\n  风格：{case['reply_style']}"
            for case in persona_cases[:3]
        )
        recent_reply_text = " / ".join(item["content"] for item in recent if item["role"] in ("assistant", "draft"))
        profile_fields = structured_profile.get("fields", {})
        structured_profile_text = "\n".join(
            f"- {field}: {value['value']} 置信度{value['confidence']}"
            for field, value in profile_fields.items()
        )
        prompt = f"""
你是 Erwin 的社交回复草稿生成器，不是聊天机器人。
目标：给出一条可以直接发出去的短回复。

硬约束：
- 只输出一句中文短句，4到18个中文字符
- 不解释，不分段，不加标点，不加emoji
- 不讨好，不鸡汤，不总结，不使用AI助手语气
- 不乱开玩笑，不用比喻修辞
- 少用问句，最近回复已有问句时禁止问句

人物基调：
商学院硕士，创业者，美股投资者，兼职老师。水瓶座ENTJ。克制、聪明、松弛、略有神秘感。

联系人画像：{request.contact_profile or '未知'}
结构化画像：
{structured_profile_text or '无'}
联系人身份：{request.contact_identity or '未知'}
关系阶段：{relationship_state}
最近情绪：{request.recent_emotion or '未知'}
互动频率：{request.interaction_frequency or '未知'}
禁忌：{request.taboos or '无'}
偏好：{request.preferences or '未知'}
场景：{scenario}
技术：{technique}
选择理由：{reason}
技术用途：{'、'.join(theory.get('usage', []))}
注意事项：{'；'.join(theory.get('precautions', []))}
最近我方回复：{recent_reply_text or '无'}

策略案例，学习为什么这么回：
{strategy_text or '无'}

自然对话案例，学习真人语气和节奏：
{natural_text or '无'}

人物/关系案例，学习特定对象互动：
{persona_text or '无'}

上下文：{context}
"""
        return self._llm().chat(
            model=self.settings.reply_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
            max_tokens=60,
        )

    def _rewrite_reply(self, draft: str, issues: list[str], context: str) -> str:
        prompt = f"""
把下面回复改成更像真人发出的短消息。
问题：{','.join(issues)}
上下文：{context}
原回复：{draft}

只输出一句中文短句，4到18个中文字符。不解释，不标点，不emoji，不问句，不比喻，不讨好。
"""
        try:
            return self._llm().chat(
                model=self.settings.reply_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=40,
            )
        except Exception:
            return draft[:18]

    def _fallback_natural_reply(self, cases: list[dict[str, Any]]) -> str:
        for case in cases:
            result = inspect_style(case["reply"])
            if not result.needs_rewrite and 4 <= len(result.text) <= 18:
                return result.text
        for case in cases:
            text = str(case["reply"]).strip()
            if text:
                return text[:18]
        return "先缓一口气"
