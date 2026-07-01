from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
import re
from typing import Any, Callable

from .config import Settings, load_settings
from .knowledge import CoverageReport, KnowledgeSample, load_persona_dialogues, load_strategy_knowledge
from .llm import LLMClient, parse_json_object
from .memory import MemoryStore
from .profile import ProfileAnalysis, ProfileAnalyzer
from .style import inspect_style
from .vector_store import LanceVectorStore


@dataclass(frozen=True)
class DraftRequest:
    contact_id: str
    message: str
    thread_id: str = ""
    platform: str = ""
    channel: str = "manual"
    conversation_id: str | None = None
    message_id: str = ""
    extra_context: str = ""
    pending_group_context: str = ""
    memory_context: str = ""
    profile_context: str = ""
    contact_profile: str = ""
    contact_identity: str = ""
    relationship_stage: str = ""
    taboos: str = ""
    preferences: str = ""
    recent_emotion: str = ""
    interaction_frequency: str = ""


@dataclass(frozen=True)
class ReplyGroupRequest:
    thread_id: str
    contact_id: str
    platform: str
    pending_messages: list[str]
    message_ids: list[str]
    pending_group_context: str = ""
    memory_context: str = ""
    profile_context: str = ""
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
        self._last_llm_prompts: dict[str, str] = {}

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
            prompt_key = "profile_image_analysis"
            prompt_model = self.settings.profile_vision_model
        else:
            analysis = self._analyze_profile_text_with_timeout(analyzer, text=text, source="profile_text")
            prompt_key = "profile_text_analysis"
            prompt_model = self.settings.reply_model
        if analysis.prompt:
            self.memory.store_llm_prompt(contact_id, prompt_key, analysis.prompt, prompt_model)
        profile_text = text.strip()  # always use original input; analysis.raw is the LLM's JSON response, not profile text
        updates = list(analysis.updates)
        if profile_text:
            updates.append(
                {
                    "field": "raw_evidence",
                    "value": profile_text,
                    "confidence": 0.95,
                    "source": "profile_text",
                    "evidence": profile_text,
                }
            )
        thread = self.memory.get_thread(contact_id)
        if thread:
            platform_contact_id = str(thread["platform_contact_id"])
            updates = self.memory.apply_profile_updates(platform_contact_id, updates)
            profile = self.memory.get_contact_profile(platform_contact_id)
            display_name = str(profile.get("contact", {}).get("display_name") or thread.get("display_name") or "")
            audit = self.memory.assess_profile_coverage(platform_contact_id, display_name, profile_text)
            return {
                "contact_id": platform_contact_id,
                "thread_id": contact_id,
                "updates": updates,
                "profile": profile,
                "profile_audit": audit,
                "raw": analysis.raw,
            }
        updates = self.memory.apply_profile_updates(contact_id, updates)
        profile = self.memory.get_contact_profile(contact_id)
        display_name = str(profile.get("contact", {}).get("display_name") or "")
        audit = self.memory.assess_profile_coverage(contact_id, display_name, profile_text)
        return {
            "contact_id": contact_id,
            "updates": updates,
            "profile": profile,
            "profile_audit": audit,
            "raw": analysis.raw,
        }

    def _analyze_profile_text_with_timeout(
        self,
        analyzer: ProfileAnalyzer,
        text: str,
        source: str,
        timeout_seconds: float = 180.0,
    ) -> ProfileAnalysis:
        text = (text or "").strip()
        if not text or analyzer.llm is None:
            return analyzer.analyze_text(text=text, source=source)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="profile-analysis")
        future = executor.submit(analyzer.analyze_text, text, source)
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError:
            future.cancel()
            prompt = analyzer._prompt(text)
            return ProfileAnalysis(updates=analyzer.extract_rules(text, source=source), raw=text, prompt=prompt)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def update_profile(self, contact_id: str, updates: list[dict[str, Any]]) -> dict[str, Any]:
        thread = self.memory.get_thread(contact_id)
        if thread:
            platform_contact_id = str(thread["platform_contact_id"])
            applied = self.memory.apply_profile_updates(platform_contact_id, updates)
            return {
                "contact_id": platform_contact_id,
                "thread_id": contact_id,
                "updates": applied,
                "profile": self.memory.get_contact_profile(platform_contact_id),
            }
        applied = self.memory.apply_profile_updates(contact_id, updates)
        return {"contact_id": contact_id, "updates": applied, "profile": self.memory.get_contact_profile(contact_id)}

    def get_profile(self, contact_id: str) -> dict[str, Any]:
        thread = self.memory.get_thread(contact_id)
        if thread:
            return self.memory.get_contact_profile(str(thread["platform_contact_id"]))
        return self.memory.get_contact_profile(contact_id)

    def update_thread_memory_from_messages(self, thread_id: str, new_messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not new_messages:
            return self.memory.get_thread_memory(thread_id)
        current = self.memory.get_thread_memory(thread_id)
        profile = self.memory.get_contact_profile(self.memory.profile_contact_id(thread_id))
        fallback = self._fallback_thread_memory(current, new_messages, profile.get("fields", {}))
        if not self.settings.dashscope_api_key:
            self.memory.update_thread_memory(thread_id, fallback)
            return fallback
        try:
            prompt = f"""
你是联系人长期记忆维护器。只根据明确证据更新这个联系人的长期 memory，不要编造。
当前memory:
{json.dumps(current, ensure_ascii=False)}
结构化画像:
{json.dumps(profile.get("fields", {}), ensure_ascii=False)}
新增消息:
{json.dumps(new_messages, ensure_ascii=False)}

输出JSON，字段固定：
{{
  "working_summary": "...",
  "pinned_facts": {{}},
  "preferences": {{}},
  "taboos": {{}},
  "relationship_state": "...",
  "topic_history": [],
  "reply_history": []
}}
"""
            contact_id = self.memory.profile_contact_id(thread_id)
            self.memory.store_llm_prompt(contact_id, "memory_update", prompt, self.settings.reply_model)
            raw = self._llm().chat(
                model=self.settings.reply_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1200,
            )
            parsed = parse_json_object(raw)
            memory = {
                "working_summary": str(parsed.get("working_summary") or fallback["working_summary"]),
                "pinned_facts": parsed.get("pinned_facts") or fallback["pinned_facts"],
                "preferences": parsed.get("preferences") or fallback["preferences"],
                "taboos": parsed.get("taboos") or fallback["taboos"],
                "relationship_state": str(parsed.get("relationship_state") or fallback["relationship_state"]),
                "topic_history": parsed.get("topic_history") or fallback["topic_history"],
                "reply_history": parsed.get("reply_history") or fallback["reply_history"],
            }
        except Exception:
            memory = fallback
        self.memory.update_thread_memory(thread_id, memory)
        return memory

    def _fallback_thread_memory(
        self, current: dict[str, Any], new_messages: list[dict[str, Any]], fields: dict[str, Any]
    ) -> dict[str, Any]:
        snippets = []
        for message in new_messages[-8:]:
            role = "对方" if message.get("role") == "user" else "我方"
            content = str(message.get("content", "")).strip()
            if content:
                snippets.append(f"{role}: {content}")
        prior = str(current.get("working_summary", "")).strip()
        addition = "；".join(snippets)
        summary = "；".join(item for item in [prior, addition] if item)
        if len(summary) > 1200:
            summary = summary[-1200:]
        pinned = dict(current.get("pinned_facts") or {})
        preferences = dict(current.get("preferences") or {})
        taboos = dict(current.get("taboos") or {})
        topics = list(current.get("topic_history") or [])
        for field, value in fields.items():
            field_value = value.get("value") if isinstance(value, dict) else ""
            if field_value and field in {"name", "age", "height", "education", "job", "company", "school", "zodiac", "location", "hometown"}:
                pinned[field] = field_value
            if field == "interests_hobbies" and field_value:
                preferences["interests_hobbies"] = field_value
        for message in new_messages:
            content = str(message.get("content", ""))
            for word in ["不喜欢", "讨厌", "别", "不要"]:
                if word in content:
                    taboos.setdefault("explicit", [])
                    if content not in taboos["explicit"]:
                        taboos["explicit"].append(content)
            for word in ["音乐", "旅行", "健身", "运动", "动漫", "电影", "摄影", "阅读", "股票", "星盘"]:
                if word in content and word not in topics:
                    topics.append(word)
        return {
            "working_summary": summary,
            "pinned_facts": pinned,
            "preferences": preferences,
            "taboos": taboos,
            "relationship_state": current.get("relationship_state") or ("持续互动" if len(summary) > 80 else "初识"),
            "topic_history": topics[-30:],
            "reply_history": list(current.get("reply_history") or [])[-30:],
        }

    def create_draft(
        self,
        request: DraftRequest,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if self.vector_store is None:
            self.initialize()
        assert self.vector_store is not None

        platform = request.platform or request.channel or "manual"
        thread = self.memory.get_thread(request.thread_id or request.contact_id)
        if thread:
            thread_id = thread["thread_id"]
            platform_contact_id = thread["platform_contact_id"]
            platform = thread["platform"]
        else:
            platform_contact_id = request.contact_id
            thread_id = self.memory.get_or_create_thread(platform, platform_contact_id, request.contact_identity)

        self.memory.add_thread_message(
            thread_id=thread_id,
            platform=platform,
            platform_message_id=request.message_id or f"manual:{request.message}",
            role="user",
            content=request.message,
        )
        profile_updates = self.memory.apply_profile_updates(
            platform_contact_id,
            ProfileAnalyzer(self.settings, None).extract_from_message(request.message),
        )
        recent = self.memory.recent_thread_messages(thread_id, limit=20)
        context = self._format_context(recent)
        recent_techniques = self.memory.thread_recent_techniques(thread_id, limit=3)
        scenario = self._detect_scenario(request.message, context)
        structured_profile = self.memory.get_contact_profile(platform_contact_id)
        thread_memory = self.memory.get_thread_memory(thread_id)
        memory_text = self._format_thread_memory(thread_memory)
        relationship_state = request.relationship_stage or thread_memory.get("relationship_state") or self._relationship_state(request, recent)
        effective_request = DraftRequest(
            contact_id=platform_contact_id,
            message=request.message,
            thread_id=thread_id,
            platform=platform,
            channel=platform,
            conversation_id=thread_id,
            message_id=request.message_id,
            extra_context="\n".join(
                item
                for item in [
                    request.extra_context,
                    f"当前pending_group:\n{request.pending_group_context}" if request.pending_group_context else "",
                    f"长期memory:\n{request.memory_context or memory_text}" if (request.memory_context or memory_text) else "",
                    f"profile上下文:\n{request.profile_context}" if request.profile_context else "",
                ]
                if item
            ),
            contact_profile=request.contact_profile,
            contact_identity=request.contact_identity,
            relationship_stage=relationship_state,
            taboos=request.taboos or json.dumps(thread_memory.get("taboos", {}), ensure_ascii=False),
            preferences=request.preferences or json.dumps(thread_memory.get("preferences", {}), ensure_ascii=False),
            recent_emotion=request.recent_emotion,
            interaction_frequency=request.interaction_frequency,
        )
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
            request=effective_request,
            recent=recent,
            structured_profile=structured_profile,
        )
        assistant_recent = [item["content"] for item in recent if item["role"] in ("assistant", "draft", "sent", "out")]
        style = inspect_style(draft, assistant_recent)
        style = self._inspect_reply_fit(style.text, style.issues, request.message, assistant_recent)
        if style.needs_rewrite:
            draft = self._rewrite_reply(style.text or draft, style.issues, context, avoid=assistant_recent[-5:], request=effective_request)
            style = inspect_style(draft, assistant_recent)
            style = self._inspect_reply_fit(style.text, style.issues, request.message, assistant_recent)
        if style.needs_rewrite:
            draft = self._fallback_natural_reply(natural_cases + persona_cases, avoid=assistant_recent[-5:])
            style = inspect_style(draft, assistant_recent)

        final_draft = style.text
        self.memory.add_thread_message(
            thread_id=thread_id,
            platform=platform,
            platform_message_id=request.message_id or f"draft:{request.message}",
            role="draft",
            content=final_draft,
            technique=technique,
            decision_reason=decision["reason"],
        )
        reply_history = list(thread_memory.get("reply_history") or [])
        reply_history.append({"incoming": request.message, "draft": final_draft, "technique": technique})
        thread_memory["reply_history"] = reply_history[-30:]
        self.memory.update_thread_memory(thread_id, thread_memory)
        return {
            "conversation_id": thread_id,
            "thread_id": thread_id,
            "contact_id": platform_contact_id,
            "channel": platform,
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

    def create_reply_group(
        self,
        request: ReplyGroupRequest,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if self.vector_store is None:
            self.initialize()
        assert self.vector_store is not None

        pending_messages = [str(item).strip() for item in request.pending_messages if str(item).strip()]
        message_ids = [str(item).strip() for item in request.message_ids]
        if not pending_messages or len(pending_messages) != len(message_ids):
            raise ValueError("pending_messages and message_ids must be non-empty and equal length")

        platform = request.platform or "manual"
        thread = self.memory.get_thread(request.thread_id or request.contact_id)
        if thread:
            thread_id = str(thread["thread_id"])
            platform_contact_id = str(thread["platform_contact_id"])
            platform = str(thread["platform"])
        else:
            thread_id = self.memory.get_or_create_thread(platform, request.contact_id, request.contact_identity)
            platform_contact_id = request.contact_id

        profile_updates: list[dict[str, Any]] = []
        for incoming, message_id in zip(pending_messages, message_ids):
            self.memory.add_thread_message(
                thread_id=thread_id,
                platform=platform,
                platform_message_id=message_id,
                role="user",
                content=incoming,
            )
            profile_updates.extend(
                self.memory.apply_profile_updates(
                    platform_contact_id,
                    ProfileAnalyzer(self.settings, None).extract_from_message(incoming),
                )
            )

        recent = self.memory.recent_thread_messages(thread_id, limit=20)
        context = self._format_context(recent)
        group_text = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(pending_messages))
        recent_techniques = self.memory.thread_recent_techniques(thread_id, limit=3)
        scenario = self._detect_scenario(" ".join(pending_messages), context)
        structured_profile = self.memory.get_contact_profile(platform_contact_id)
        thread_memory = self.memory.get_thread_memory(thread_id)
        memory_text = self._format_thread_memory(thread_memory)
        relationship_state = request.relationship_stage or thread_memory.get("relationship_state") or (
            "持续互动" if len(recent) > 8 else "建立熟悉感" if len(recent) > 2 else "初识"
        )
        effective_request = ReplyGroupRequest(
            thread_id=thread_id,
            contact_id=platform_contact_id,
            platform=platform,
            pending_messages=pending_messages,
            message_ids=message_ids,
            pending_group_context=request.pending_group_context or group_text,
            memory_context=request.memory_context or memory_text,
            profile_context=request.profile_context,
            contact_profile=request.contact_profile,
            contact_identity=request.contact_identity,
            relationship_stage=relationship_state,
            taboos=request.taboos or json.dumps(thread_memory.get("taboos", {}), ensure_ascii=False),
            preferences=request.preferences or json.dumps(thread_memory.get("preferences", {}), ensure_ascii=False),
            recent_emotion=request.recent_emotion,
            interaction_frequency=request.interaction_frequency,
        )
        if progress_callback:
            progress_callback("正在分析对话中，请等待……")
        decision = self._decide_technique(context, scenario, recent_techniques)
        technique = decision["selected_technique"]
        if progress_callback:
            progress_callback("正在数据检索 RAG 中，请等待……")
        strategy_cases = self.vector_store.query(context, technique=technique, sample_type="annotated_strategy", n_results=3)
        if not strategy_cases:
            strategy_cases = self.vector_store.query(context, sample_type="annotated_strategy", n_results=3)
        natural_cases = self.vector_store.query(context, technique=technique, sample_type="natural_dialogue", n_results=5)
        if not natural_cases:
            natural_cases = self.vector_store.query(context, sample_type="natural_dialogue", n_results=5)
        persona_cases = self.vector_store.query(context, sample_type="persona_dialogue", n_results=3)
        if progress_callback:
            progress_callback("正在生成回复中，请等待……")
        decision_prompt = decision.get("decision_prompt", "")

        drafts = self._generate_reply_group(
            context=context,
            scenario=scenario,
            relationship_state=relationship_state,
            technique=technique,
            reason=decision["reason"],
            strategy_cases=strategy_cases,
            natural_cases=natural_cases,
            persona_cases=persona_cases,
            request=effective_request,
            recent=recent,
            structured_profile=structured_profile,
        )
        if len(drafts) != len(pending_messages):
            raise ValueError("reply group length does not match pending group length")
        self.memory.store_llm_prompt(platform_contact_id, "technique_decision", decision_prompt, self.settings.decision_model)
        self.memory.store_llm_prompt(platform_contact_id, "reply_generation", self._last_llm_prompts.get(platform_contact_id, ""), self.settings.reply_model)

        assistant_recent = [item["content"] for item in recent if item["role"] in ("assistant", "draft", "sent", "out")]
        final_drafts: list[str] = []
        for draft in drafts:
            style = inspect_style(draft, assistant_recent + final_drafts)
            style = self._inspect_reply_fit(style.text, style.issues, " ".join(pending_messages), assistant_recent + final_drafts)
            if style.needs_rewrite:
                boundary = DraftRequest(
                    contact_id=platform_contact_id,
                    message=" ".join(pending_messages),
                    thread_id=thread_id,
                    platform=platform,
                    extra_context=f"当前语义群：\n{group_text}",
                )
                draft = self._rewrite_reply(style.text or draft, style.issues, context, avoid=(assistant_recent + final_drafts)[-5:], request=boundary)
                style = inspect_style(draft, assistant_recent + final_drafts)
                style = self._inspect_reply_fit(style.text, style.issues, " ".join(pending_messages), assistant_recent + final_drafts)
            if style.needs_rewrite:
                draft = self._fallback_natural_reply(natural_cases + persona_cases, avoid=(assistant_recent + final_drafts)[-5:])
                style = inspect_style(draft, assistant_recent + final_drafts)
            final_drafts.append(style.text)

        reply_group = []
        for incoming, message_id, draft in zip(pending_messages, message_ids, final_drafts):
            self.memory.add_thread_message(
                thread_id=thread_id,
                platform=platform,
                platform_message_id=message_id,
                role="draft",
                content=draft,
                technique=technique,
                decision_reason=decision["reason"],
            )
            reply_group.append(
                {
                    "incoming": incoming,
                    "draft": draft,
                    "message_id": message_id,
                    "technique": technique,
                    "reason": decision["reason"],
                }
            )

        reply_history = list(thread_memory.get("reply_history") or [])
        reply_history.append({"incoming_group": pending_messages, "draft_group": final_drafts, "technique": technique})
        thread_memory["reply_history"] = reply_history[-30:]
        self.memory.update_thread_memory(thread_id, thread_memory)
        return {
            "conversation_id": thread_id,
            "thread_id": thread_id,
            "contact_id": platform_contact_id,
            "channel": platform,
            "reply_group": reply_group,
            "technique": technique,
            "decision_reason": decision["reason"],
            "scenario": scenario,
            "relationship_state": relationship_state,
            "profile_updates": profile_updates,
            "models": {"decision": self.settings.decision_model, "reply": self.settings.reply_model},
        }

    def _format_context(self, messages: list[dict[str, Any]]) -> str:
        role_map = {"user": "B", "assistant": "A", "draft": "A草稿", "sent": "A已发", "out": "A已发"}
        return " | ".join(f"{role_map.get(item['role'], item['role'])}: {item['content']}" for item in messages[-20:])

    def _format_thread_memory(self, memory: dict[str, Any]) -> str:
        parts = [
            f"长期摘要：{memory.get('working_summary') or '无'}",
            f"固定事实：{json.dumps(memory.get('pinned_facts') or {}, ensure_ascii=False)}",
            f"偏好：{json.dumps(memory.get('preferences') or {}, ensure_ascii=False)}",
            f"禁忌：{json.dumps(memory.get('taboos') or {}, ensure_ascii=False)}",
            f"关系阶段：{memory.get('relationship_state') or '未知'}",
            f"话题历史：{json.dumps(memory.get('topic_history') or [], ensure_ascii=False)}",
            f"回复历史：{json.dumps(memory.get('reply_history') or [], ensure_ascii=False)}",
        ]
        return "\n".join(parts)

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
            return {"selected_technique": selected, "reason": str(parsed.get("reason", "按场景匹配")).strip(), "decision_prompt": prompt}
        except Exception:
            fallback = "延词" if "延词" in preferred else (preferred[0] if preferred else available[0])
            return {"selected_technique": fallback, "reason": "模型决策失败，使用规则回退", "decision_prompt": prompt}

    def _format_reply_profile(self, structured_profile: dict[str, Any]) -> str:
        fields = structured_profile.get("fields", {})
        if not isinstance(fields, dict):
            return ""
        skip_fields = {"raw_evidence"}
        priority = [
            "name",
            "age",
            "location",
            "hometown",
            "job",
            "company",
            "education",
            "school",
            "interests_hobbies",
            "personality_traits",
            "about_me",
            "relationship_goal",
            "profile_prompts",
            "compatibility_points",
            "height",
            "zodiac",
        ]
        ordered = [field for field in priority if field in fields]
        ordered.extend(field for field in fields if field not in set(ordered) and field not in skip_fields)
        lines: list[str] = []
        for field in ordered:
            if field in skip_fields:
                continue
            item = fields.get(field)
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            value = re.sub(r"\s+", " ", value)
            if len(value) > 120:
                value = value[:117] + "..."
            lines.append(f"- {field}: {value}")
            if len(lines) >= 12:
                break
        return "\n".join(lines)

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
        recent_reply_text = " / ".join(item["content"] for item in recent if item["role"] in ("assistant", "draft", "sent"))
        structured_profile_text = self._format_reply_profile(structured_profile)
        prompt = f"""
你是 Erwin 的社交回复草稿生成器，不是聊天机器人。
目标：给出一条可以直接发出去的短回复。

硬约束：
- 只输出一句中文短句，4到18个中文字符
- 不解释，不分段，不加标点，不加emoji
- 不讨好，不鸡汤，不总结，不使用AI助手语气
- 不乱开玩笑，不用比喻修辞
- 不使用命令、审判、扣帽子、居高临下或压迫的语气
- 避免“你就是… / 你应该… / 默认… / 就直说 / 算你识相 / 受宠若惊”这类强判定压迫式句式
- 少用问句，最近回复已有问句时禁止问句
- 如果补充上下文包含语义群，要按整组语义理解，不要机械逐条对应
- 如果当前输入是单条消息，只生成一条回复
- 不要和最近我方回复重复或同义改写

人物基调：
商学院硕士，创业者，美股投资者，兼职老师。水瓶座ENTJ。克制、聪明、松弛、略有神秘感。

补充上下文：{request.extra_context or '无'}
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
        self._last_llm_prompts[request.contact_id] = prompt
        self.memory.store_llm_prompt(request.contact_id, "reply_generation", prompt, self.settings.reply_model)
        return self._llm().chat(
            model=self.settings.reply_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
            max_tokens=60,
        )

    def _generate_reply_group(
        self,
        context: str,
        scenario: str,
        relationship_state: str,
        technique: str,
        reason: str,
        strategy_cases: list[dict[str, Any]],
        natural_cases: list[dict[str, Any]],
        persona_cases: list[dict[str, Any]],
        request: ReplyGroupRequest,
        recent: list[dict[str, Any]],
        structured_profile: dict[str, Any],
    ) -> list[str]:
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
        recent_reply_text = " / ".join(item["content"] for item in recent if item["role"] in ("assistant", "draft", "sent"))
        structured_profile_text = self._format_reply_profile(structured_profile)
        pending_text = request.pending_group_context or "\n".join(
            f"{index + 1}. {text}" for index, text in enumerate(request.pending_messages)
        )
        prompt = f"""
你是 Erwin 的社交回复草稿生成器，不是聊天机器人。
目标：把对方连续发来的 pending_group 当成一个完整语义群，生成 {len(request.pending_messages)} 条可逐条发送的短回复。

硬约束：
- 只输出JSON数组，数组长度必须等于 {len(request.pending_messages)}
- 数组每一项是一个中文短句字符串，4到18个中文字符
- 每一项都是对整组语义的连续拆句，不是按序号逐条回答对方原句
- 不解释，不分段，不加标点，不加emoji
- 不讨好，不鸡汤，不总结，不使用AI助手语气
- 不乱开玩笑，不用比喻修辞
- 不使用命令、审判、扣帽子、居高临下或压迫的语气
- 避免“你就是… / 你应该… / 默认… / 就直说 / 算你识相 / 受宠若惊”这类强判定压迫式句式
- 少用问句，最近回复已有问句时禁止问句
- 多条回复之间要连贯、递进，不能重复或同义改写

人物基调：
商学院硕士，创业者，美股投资者，兼职老师。水瓶座ENTJ。克制、聪明、松弛、略有神秘感。

结构化画像：
{structured_profile_text or '无'}
联系人身份：{request.contact_identity or '未知'}
关系阶段：{relationship_state}
最近情绪：{request.recent_emotion or '未知'}
互动频率：{request.interaction_frequency or '未知'}
禁忌：{request.taboos or '无'}
偏好：{request.preferences or '未知'}
长期memory：
{request.memory_context or '无'}
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

pending_group：
{pending_text}

上下文：{context}
"""
        self._last_llm_prompts[request.contact_id] = prompt
        raw = self._llm().chat(
            model=self.settings.reply_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
            max_tokens=180,
        )
        try:
            parsed = self._parse_json_array(raw)
        except Exception:
            if len(request.pending_messages) != 1:
                raise
            parsed = [self._clean_single_reply_text(raw)]
        if len(request.pending_messages) == 1 and len(parsed) > 1:
            parsed = [parsed[0]]
        if len(parsed) != len(request.pending_messages):
            raise ValueError("reply group length does not match pending group length")
        return [str(item).strip() for item in parsed]

    def _clean_single_reply_text(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        text = text.strip()
        if text.startswith("["):
            text = text[1:]
        if text.endswith("]"):
            text = text[:-1]
        text = text.strip().strip("\"'，,。.!！?？")
        if "\n" in text:
            text = next((line.strip().strip("\"'，,。.!！?？") for line in text.splitlines() if line.strip()), text)
        return text[:18] or "刚看到"

    def _parse_json_array(self, text: str) -> list[Any]:
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", text, flags=re.S)
            if not match:
                raise
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, list):
            raise ValueError("expected JSON array")
        return parsed

    def _inspect_reply_fit(self, text: str, issues: list[str], incoming: str, recent_assistant: list[str]):
        clean_issues = list(dict.fromkeys(issues))
        if self._is_premature_self_answer(incoming, text):
            clean_issues.append("premature_self_answer")
        if self._is_duplicate_reply(text, recent_assistant):
            clean_issues.append("duplicate_recent_reply")
        return type(inspect_style(text))(text=text, issues=list(dict.fromkeys(clean_issues)), needs_rewrite=bool(clean_issues))

    def get_last_llm_prompt(self, contact_id: str) -> str:
        return self._last_llm_prompts.get(contact_id, "")

    def _is_premature_self_answer(self, incoming: str, draft: str) -> bool:
        incoming = (incoming or "").strip()
        draft = (draft or "").strip()
        asks_about_me = any(marker in incoming for marker in ["你呢", "你平时", "你一般", "你会", "你也", "你是", "你有", "你吗", "你？"])
        is_question = asks_about_me or incoming.endswith(("?", "？", "吗", "嘛", "呢"))
        self_answer = draft.startswith(("我", "我的", "一般我", "我一般", "我周末", "我平时"))
        return self_answer and not is_question

    def _is_duplicate_reply(self, draft: str, recent_assistant: list[str]) -> bool:
        draft = (draft or "").strip()
        if not draft:
            return False
        for previous in recent_assistant[-5:]:
            previous = (previous or "").strip()
            if not previous:
                continue
            if draft == previous:
                return True
            if SequenceMatcher(None, draft, previous).ratio() >= 0.82:
                return True
        return False

    def _rewrite_reply(
        self,
        draft: str,
        issues: list[str],
        context: str,
        avoid: list[str] | None = None,
        request: DraftRequest | None = None,
    ) -> str:
        avoid_text = " / ".join(item for item in (avoid or []) if item)
        prompt = f"""
把下面回复改成更像真人发出的短消息。
问题：{','.join(issues)}
上下文：{context}
当前回复边界：{request.extra_context if request else '无'}
禁止重复这些回复：{avoid_text or '无'}
原回复：{draft}

只输出一句中文短句，4到18个中文字符。不解释，不标点，不emoji，不问句，不比喻，不讨好。
不使用命令、审判、扣帽子、居高临下或压迫的语气。
避免“你就是… / 你应该… / 默认… / 就直说 / 算你识相 / 受宠若惊”这类强判定压迫式句式。
如果问题包含 premature_self_answer，只回应当前对方这句话，不要提前回答后面的“你呢”。
如果问题包含 duplicate_recent_reply，必须换一个语义和措辞都不同的回复。
"""
        try:
            if request and request.contact_id:
                self.memory.store_llm_prompt(request.contact_id, "reply_rewrite", prompt, self.settings.reply_model)
            return self._llm().chat(
                model=self.settings.reply_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=40,
            )
        except Exception:
            return draft[:18]

    def _fallback_natural_reply(self, cases: list[dict[str, Any]], avoid: list[str] | None = None) -> str:
        avoid = avoid or []
        for case in cases:
            result = inspect_style(case["reply"])
            if not result.needs_rewrite and 4 <= len(result.text) <= 18 and not self._is_duplicate_reply(result.text, avoid):
                return result.text
        for case in cases:
            text = str(case["reply"]).strip()
            if text and not self._is_duplicate_reply(text[:18], avoid):
                return text[:18]
        return "先缓一口气"
