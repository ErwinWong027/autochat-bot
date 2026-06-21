# 社交策略数字分身 v1 使用说明书

结论：本项目包含手动草稿模式和 Bumble Agent。手动模式只生成草稿；Bumble Agent 在双开关允许时会按 Enter 自动发送。

## 1. 环境准备

进入项目目录：

```bash
cd /Users/erwinwong/Documents/autochat-bot
```

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

## 2. 配置模型

在项目根目录创建 `.env`：

```bash
DASHSCOPE_API_KEY=你的DashScope Key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DECISION_MODEL=qwen3.7-plus
REPLY_MODEL=qwen3.7-plus
CHEAP_MODEL=qwen3.6-flash
PREMIUM_MODEL=qwen3.7-max
PROFILE_VISION_MODEL=qwen3-vl-plus
PROFILE_OCR_MODEL=qwen-vl-ocr-latest
AUTO_SEND_ENABLED=false
BROWSER_AGENT_TARGET_URL=
BROWSER_AGENT_POLL_SECONDS=5
BUMBLE_TARGET_URL=https://eu1.bumble.com/app/connections
BUMBLE_POLL_SECONDS=5
BUMBLE_USER_DATA_DIR=./browser_profiles/bumble
```

默认日常链路使用 `qwen3.7-plus`，不使用 `qwen3.7-max`。

图片主页识别使用 `qwen3-vl-plus`。

## 3. 知识库学习机制

系统会读取 `all_chapters.json` 的所有 A 端回复：

- `annotated_strategy`：带 `thinking/summary` 的策略样本，用来学“为什么这么回”
- `natural_dialogue`：没有 `thinking/summary` 的自然对话样本，用来学“真人语气、节奏、接话方式”

当前目标覆盖：

```text
total_a_replies = 178
annotated_strategy = 53
natural_dialogue = 125
vector_rows = 178
```

检查命令：

```bash
python3 - <<'PY'
from social_twin.service import DigitalTwinService
svc = DigitalTwinService()
svc.initialize()
print(svc.report())
PY
```

## 4. 运行 FastAPI

启动服务：

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

知识覆盖报告：

```bash
curl http://localhost:8000/knowledge/report
```

分析主页文字：

```bash
curl -X POST http://localhost:8000/contacts/alice/profile/analyze \
  -F 'profile_text=身高168cm，水瓶座，硕士，杭州，做金融，喜欢音乐旅行'
```

分析主页截图：

```bash
curl -X POST http://localhost:8000/contacts/alice/profile/analyze \
  -F 'profile_text=这是某社交软件主页截图' \
  -F 'image=@/absolute/path/profile.png'
```

读取联系人画像：

```bash
curl http://localhost:8000/contacts/alice/profile
```

生成草稿：

```bash
curl -X POST http://localhost:8000/draft \
  -H "Content-Type: application/json" \
  -d '{
    "contact_id": "alice",
    "channel": "manual",
    "message": "最近真的好累",
    "contact_identity": "朋友",
    "contact_profile": "工作压力大",
    "relationship_stage": "熟悉",
    "recent_emotion": "疲惫",
    "interaction_frequency": "每周几次",
    "preferences": "喜欢短句，不喜欢说教"
  }'
```

返回重点字段：

- `draft`：回复草稿
- `technique`：本轮沟通技术
- `scenario`：场景识别
- `relationship_state`：关系状态
- `strategy_cases`：策略案例召回
- `natural_cases`：自然对话案例召回
- `conversation_id`：独立会话 ID
- `style_issues`：风格审查结果
- `profile_updates`：本轮对话自动补全/更新的画像信息

## 5. 运行 Gradio 调试界面

```bash
python digital_twin.py
```

浏览器打开：

```text
http://localhost:7860
```

Gradio 适合手动输入联系人信息和对方消息，检查草稿、策略、召回案例和风格问题。

Gradio 里有三个区域：

- `画像分析`：上传主页截图或粘贴主页文字，生成联系人画像
- `回复草稿`：输入对方消息，生成回复草稿，并自动更新画像
- `浏览器Agent`：配置网页选择器，启动/停止网页自动回复

## 6. Bumble 专用 Agent

Bumble 专用 Agent 不需要手动填写 CSS 选择器。它会使用 `BUMBLE_USER_DATA_DIR` 指向的持久浏览器目录保存登录态。

第一次使用：

1. 在 Gradio 的 `浏览器Agent` → `Bumble专用` 输入 `https://eu1.bumble.com/app/connections`。
2. 点击 `启动 Bumble Agent`。
3. 系统会打开一个新的自动化 Chromium 窗口。
4. 在这个新窗口里手动登录 Bumble。
5. 登录成功后保持窗口开着，Agent 才能扫描联系人和消息。

以后再次启动会复用 `./browser_profiles/bumble` 里的登录态。

识别规则：

- 联系人：`data-qa-role="contact"`
- 需要回复：联系人里出现 `.contact__move-label`，且文字包含 `轮到您了`
- 联系人 ID：`bumble:{data-qa-uid}`
- 联系人名称：`data-qa-name`
- 最新消息组：最后一条 `message--out` 后面的所有 `message--in`
- 输入框：`[data-qa-role="chat-input"] textarea.textarea__input`
- 发送方式：填入 textarea 后按 `Enter`

启动 FastAPI 后可调用：

```bash
curl -X POST http://localhost:8000/agent/bumble/run \
  -H "Content-Type: application/json" \
  -d '{"target_url":"https://eu1.bumble.com/app/connections","auto_send_enabled":false,"poll_seconds":5}'
```

查看状态：

```bash
curl http://localhost:8000/agent/bumble/status
```

状态返回里重点看：

- `stage`：当前阶段，例如 `SCANNING_CONTACTS`、`PROFILE_READ`、`MESSAGES_READ`、`DRAFTING`、`SENT_BY_ENTER`、`ERROR`
- `status_code`：阶段码，例如 `200` 表示正在扫描联系人，`500` 表示正在生成草稿，`900` 表示错误
- `last_error`：最后错误
- `logs`：最近 100 条运行日志，包含时间、阶段、状态码、是否成功和上下文数据
- `contact_count`：扫描到的“轮到您了”联系人数量
- `message_count`：当前会话读取到的消息数量
- `pending_group_count`：最后一条自己发出的消息后，对方新发来的待回复消息数
- `last_reply_group`：本轮生成的完整草稿组
- `contacts`：按 `bumble:{uid}` 分开的联系人运行状态，多个联系人不会共用同一条状态

停止：

```bash
curl -X POST http://localhost:8000/agent/bumble/stop
```

首次遇到联系人且画像缺少 `photo_urls / bio / profile_prompts / photo_description` 时，系统会尝试读取 Bumble profile，并把资料按基础资料、生活方式、关系意图、自我介绍、问答内容、兴趣偏好、照片信息分类写入画像证据库。

自动发送由两个条件共同控制：`.env` 的 `AUTO_SEND_ENABLED=true`，以及启动 Bumble Agent 时 `auto_send_enabled=true`。关闭自动发送时只把第一条草稿填入输入框，不按 Enter。

## 7. 添加其他身份对话

把额外对话放到 `persona_dialogues/*.json`。

示例：

```json
[
  {
    "identity": "朋友",
    "relation": "熟悉",
    "scene": "工作压力",
    "their_message": "最近真的好累",
    "my_reply": "先缓一口气",
    "effect": "承接情绪",
    "tags": ["情绪", "安抚"]
  }
]
```

这些样本会作为 `persona_dialogue` 入库，用来学习不同身份对象的互动方式。

## 8. 本地测试

不消耗 API：

```bash
python3 -m py_compile app.py digital_twin.py digital_twin4.py social_twin/*.py tests/test_v1.py
python3 -m unittest
```

检查 9 个技术是否都有策略样本和自然样本：

```bash
python3 - <<'PY'
from social_twin.service import DigitalTwinService
svc = DigitalTwinService()
svc.initialize()
store = svc.vector_store
for tech in svc.coverage_report.technique_names:
    annotated = store.query("最近好累", technique=tech, sample_type="annotated_strategy", n_results=1)
    natural = store.query("最近好累", technique=tech, sample_type="natural_dialogue", n_results=1)
    print(tech, len(annotated), len(natural))
PY
```

预期每行都是：

```text
技术名 1 1
```

## 9. 当前边界

- 手动回复区只生成草稿
- Bumble Agent 在 `AUTO_SEND_ENABLED=true` 且界面/API 允许自动发送时会发送
- 浏览器 Agent 可全自动发送，但必须设置 `AUTO_SEND_ENABLED=true`
- Bumble Agent 只适配 Bumble Web，不适配手机 App
- 浏览器 Agent 需要网页 CSS 选择器和 Playwright Chromium
- 真实生成草稿会调用 DashScope 并消耗 API
- 去 AI 味已有规则、二次改写和自然短句兜底，但仍需要真实多轮评测继续调参

安装浏览器自动化驱动：

```bash
python -m playwright install chromium
```
