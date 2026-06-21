from __future__ import annotations

import gradio as gr

from social_twin.bumble import BumbleConfig, BumbleConnector
from social_twin.connectors import BrowserAgentConfig, BrowserAgentConnector
from social_twin.service import DigitalTwinService, DraftRequest


service = DigitalTwinService()
service.initialize()
browser_agent = BrowserAgentConnector(service)
bumble_agent = BumbleConnector(service)


def analyze_profile(contact_id: str, profile_text: str, image_path: str | None):
    result = service.analyze_profile(
        contact_id=contact_id.strip() or "default",
        text=profile_text.strip(),
        image_path=image_path or "",
    )
    return result["profile"], result["updates"]


def load_profile(contact_id: str):
    return service.get_profile(contact_id.strip() or "default")


def start_browser_agent(
    target_url: str,
    unread_selector: str,
    message_selector: str,
    input_selector: str,
    send_selector: str,
    contact_selector: str,
):
    return browser_agent.run(
        BrowserAgentConfig(
            target_url=target_url.strip(),
            unread_selector=unread_selector.strip(),
            message_selector=message_selector.strip(),
            input_selector=input_selector.strip(),
            send_selector=send_selector.strip(),
            contact_selector=contact_selector.strip(),
            poll_seconds=service.settings.browser_agent_poll_seconds,
        )
    )


def stop_browser_agent():
    return browser_agent.stop()


def start_bumble_agent(target_url: str, auto_send_enabled: bool, refresh_profile: bool):
    status = bumble_agent.run(
        BumbleConfig(
            target_url=target_url.strip() or service.settings.bumble_target_url,
            auto_send_enabled=auto_send_enabled,
            poll_seconds=service.settings.bumble_poll_seconds,
            refresh_profile=refresh_profile,
        )
    )
    return _format_bumble_status(status)


def stop_bumble_agent():
    return _format_bumble_status(bumble_agent.stop())


def bumble_status():
    return _format_bumble_status(bumble_agent.status())


def _format_bumble_status(status: dict):
    logs = status.get("logs", [])
    log_text = "\n".join(
        f"[{item.get('time')}] {item.get('stage')}({item.get('status_code')}) "
        f"{'OK' if item.get('ok') else 'ERR'} - {item.get('message')} {item.get('data') or ''}"
        for item in logs[-30:]
    )
    stage = f"{status.get('stage', '')} ({status.get('status_code', '')})"
    return status, stage, status.get("last_error", ""), log_text


def make_draft(
    contact_id: str,
    message: str,
    channel: str,
    contact_profile: str,
    contact_identity: str,
    relationship_stage: str,
    taboos: str,
    preferences: str,
    recent_emotion: str,
    interaction_frequency: str,
):
    result = service.create_draft(
        DraftRequest(
            contact_id=contact_id.strip() or "default",
            message=message.strip(),
            channel=channel.strip() or "manual",
            contact_profile=contact_profile.strip(),
            contact_identity=contact_identity.strip(),
            relationship_stage=relationship_stage.strip(),
            taboos=taboos.strip(),
            preferences=preferences.strip(),
            recent_emotion=recent_emotion.strip(),
            interaction_frequency=interaction_frequency.strip(),
        )
    )
    cases = "\n".join(f"- {case['technique']}：{case['reply']}" for case in result["retrieved_cases"])
    detail = (
        f"技术：{result['technique']}\n"
        f"场景：{result['scenario']}\n"
        f"理由：{result['decision_reason']}\n"
        f"会话：{result['conversation_id']}\n"
        f"风格问题：{','.join(result['style_issues']) or '无'}\n"
        f"画像更新：{result['profile_updates']}\n"
        f"模型：{result['models']}\n\n"
        f"召回案例：\n{cases}"
    )
    return result["draft"], detail, ""


with gr.Blocks(title="社交策略数字分身 v1") as demo:
    gr.Markdown("## 社交策略数字分身 v1")
    gr.Markdown("手动模式只生成草稿；Bumble Agent 在双开关允许时会按 Enter 自动发送。")
    with gr.Row():
        contact_id = gr.Textbox(label="联系人ID", value="default")
        channel = gr.Textbox(label="渠道", value="manual")
    with gr.Tab("画像分析"):
        profile_image = gr.Image(label="社交主页截图", type="filepath")
        profile_text = gr.Textbox(label="主页文字/补充信息", lines=6)
        analyze_btn = gr.Button("分析并更新画像")
        profile_json = gr.JSON(label="当前联系人画像")
        profile_updates = gr.JSON(label="本次画像更新")
        load_profile_btn = gr.Button("读取当前画像")
        analyze_btn.click(
            fn=analyze_profile,
            inputs=[contact_id, profile_text, profile_image],
            outputs=[profile_json, profile_updates],
        )
        load_profile_btn.click(fn=load_profile, inputs=[contact_id], outputs=[profile_json])
    with gr.Tab("回复草稿"):
        message = gr.Textbox(label="对方消息", lines=3)
        with gr.Accordion("手动画像补充", open=False):
            contact_profile = gr.Textbox(label="画像", lines=2)
            contact_identity = gr.Textbox(label="身份")
            relationship_stage = gr.Textbox(label="关系阶段")
            recent_emotion = gr.Textbox(label="最近情绪")
            interaction_frequency = gr.Textbox(label="互动频率")
            taboos = gr.Textbox(label="禁忌")
            preferences = gr.Textbox(label="偏好")
        submit = gr.Button("生成草稿")
        draft = gr.Textbox(label="回复草稿")
        detail = gr.Textbox(label="策略详情", lines=12)
        submit.click(
            fn=make_draft,
            inputs=[
                contact_id,
                message,
                channel,
                contact_profile,
                contact_identity,
                relationship_stage,
                taboos,
                preferences,
                recent_emotion,
                interaction_frequency,
            ],
            outputs=[draft, detail, message],
        )
    with gr.Tab("浏览器Agent"):
        with gr.Tab("Bumble专用"):
            gr.Markdown("读取 `轮到您了` 联系人，首次自动抓取画像，再生成草稿组。只有 `.env` 的 `AUTO_SEND_ENABLED=true` 且下方开关打开时，才会按 Enter 自动发送。")
            bumble_url = gr.Textbox(label="Bumble URL", value=service.settings.bumble_target_url)
            bumble_user_data_dir = gr.Textbox(label="登录态目录", value=service.settings.bumble_user_data_dir, interactive=False)
            bumble_auto_send = gr.Checkbox(label="本次允许自动发送", value=service.settings.auto_send_enabled)
            bumble_refresh_profile = gr.Checkbox(label="强制刷新画像", value=False)
            with gr.Row():
                start_bumble = gr.Button("启动 Bumble Agent")
                stop_bumble = gr.Button("停止 Bumble Agent")
                check_bumble = gr.Button("查看 Bumble 状态")
            bumble_agent_status = gr.JSON(label="Bumble Agent状态")
            bumble_stage = gr.Textbox(label="当前阶段")
            bumble_error = gr.Textbox(label="最后错误")
            bumble_logs = gr.Textbox(label="最近日志", lines=12)
            start_bumble.click(
                fn=start_bumble_agent,
                inputs=[bumble_url, bumble_auto_send, bumble_refresh_profile],
                outputs=[bumble_agent_status, bumble_stage, bumble_error, bumble_logs],
            )
            stop_bumble.click(fn=stop_bumble_agent, inputs=[], outputs=[bumble_agent_status, bumble_stage, bumble_error, bumble_logs])
            check_bumble.click(fn=bumble_status, inputs=[], outputs=[bumble_agent_status, bumble_stage, bumble_error, bumble_logs])
        with gr.Tab("通用选择器"):
            gr.Markdown("保留通用网页 Agent，用于非 Bumble 网页。")
            target_url = gr.Textbox(label="目标网页URL")
            unread_selector = gr.Textbox(label="未读消息选择器")
            message_selector = gr.Textbox(label="消息文本选择器")
            input_selector = gr.Textbox(label="输入框选择器")
            send_selector = gr.Textbox(label="发送按钮选择器")
            contact_selector = gr.Textbox(label="联系人选择器")
            with gr.Row():
                start_agent = gr.Button("启动通用浏览器Agent")
                stop_agent = gr.Button("停止通用浏览器Agent")
            agent_status = gr.JSON(label="通用Agent状态")
            start_agent.click(
                fn=start_browser_agent,
                inputs=[target_url, unread_selector, message_selector, input_selector, send_selector, contact_selector],
                outputs=[agent_status],
            )
            stop_agent.click(fn=stop_browser_agent, inputs=[], outputs=[agent_status])


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False)
