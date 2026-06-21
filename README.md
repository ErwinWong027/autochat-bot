# 社交策略数字分身 v1

结论：这是“本地建议器 + RAG 记忆库 + 草稿生成器”，只生成回复草稿，不自动发送。

## 功能
- 读取 `all_chapters.json`，全量学习所有 A 端回复，生成知识覆盖报告，索引可追踪案例。
- 使用 LanceDB 做案例检索，SQLite 区分联系人、渠道、会话和消息。
- 使用 FastAPI 提供 `/draft` 草稿接口，Gradio 只作为本地调试界面。
- 支持上传社交主页截图或粘贴主页文字，自动生成结构化联系人画像。
- 每轮对话会自动抽取可用信息，补全联系人画像并保留证据。
- 默认模型为 `qwen3.7-plus`，`qwen3.7-max` 只保留为高级备用配置。
- 回复前做风格审查，压制长段落、AI 腔、讨好、乱开玩笑、比喻、连续问句。

## 运行
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

`.env` 示例：
```bash
DASHSCOPE_API_KEY=你的Key
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

启动 FastAPI：
```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

启动 Gradio 调试界面：
```bash
python digital_twin.py
```

## API
健康检查：
```bash
curl http://localhost:8000/health
```

知识覆盖报告：
```bash
curl http://localhost:8000/knowledge/report
```

分析联系人主页：
```bash
curl -X POST http://localhost:8000/contacts/alice/profile/analyze \
  -F 'profile_text=身高168cm，水瓶座，硕士，杭州，做金融，喜欢音乐旅行'
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
    "contact_profile": "朋友，工作压力大",
    "relationship_stage": "熟悉",
    "recent_emotion": "疲惫",
    "interaction_frequency": "每周几次",
    "preferences": "喜欢短句，不喜欢说教"
  }'
```

返回包含：
- `draft`：只生成草稿，不发送
- `technique`：本轮使用的沟通技术
- `scenario`：场景识别
- `retrieved_cases`：召回案例
- `conversation_id`：独立会话 ID
- `style_issues`：风格审查结果
- `profile_updates`：本轮对话自动补全/更新的画像信息

## Bumble Agent 使用
结论：Bumble Agent 默认只生成草稿并填入输入框，不发送。

首次运行前安装浏览器驱动：
```bash
python -m playwright install chromium
```

启动服务后运行 Bumble Agent：
```bash
curl -X POST http://127.0.0.1:8000/agent/bumble/run \
  -H "Content-Type: application/json" \
  -d '{
    "target_url": "https://eu1.bumble.com/app/connections",
    "auto_send_enabled": false,
    "poll_seconds": 5
  }'
```

查看状态：
```bash
curl http://127.0.0.1:8000/agent/bumble/status
```

停止 Agent：
```bash
curl -X POST http://127.0.0.1:8000/agent/bumble/stop
```

运行过程会在 Bumble 输入框显示阶段提示：
- `正在读取 profile 生成画像中，请等待……`
- `正在分析对话中，请等待……`
- `正在数据检索 RAG 中，请等待……`
- `正在生成第 1/3 句回复，请等待……`
- 生成完成后用草稿替代提示文字。

自动发送需要同时满足两个条件：
- `.env` 中 `AUTO_SEND_ENABLED=true`
- 启动 Agent 时 `auto_send_enabled=true`

## 工作原理
结论：系统是“结构化画像 + 会话记忆 + RAG 案例 + 风格审查”的草稿生成链路。

流程：
1. 读取联系人、消息和 profile DOM。
2. 用 SQLite 保存联系人、会话、消息、画像字段和证据。
3. 从 `all_chapters.json` 和 `persona_dialogues/*.json` 建立 LanceDB 案例索引。
4. 每轮先识别场景和关系状态，再选择沟通技术。
5. 用 RAG 召回策略案例、自然对话案例和人物关系案例。
6. 调用模型生成短句草稿。
7. 做风格审查，必要时重写或回退到自然样本。

性能机制：
- RAG 本地检索通常不是主要慢点，主要耗时来自远端 LLM 调用。
- 当前 Bumble 对待回复消息逐条生成，质量稳定但慢。
- 更快方案是同一 pending group 只做一次分析、一次 RAG、一次模型生成，直接返回多句草稿。
- `sent_messages` 只记录成功生成过的消息，失败不会被误标记为已处理。

## 数据入口
`all_chapters.json` 负责策略库。所有 A 端回复都会入库：
- `annotated_strategy`：带 `thinking` 和 `summary`，用于学习策略解释。
- `natural_dialogue`：没有 `thinking/summary`，用于学习真人语气、节奏和上下文接法。

当前覆盖目标：
- `total_a_replies = 178`
- `annotated_strategy = 53`
- `natural_dialogue = 125`
- `vector_rows = 178`

额外人物/关系对话放到 `persona_dialogues/*.json`，格式：
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

## 架构
- `app.py`：FastAPI 服务入口
- `digital_twin.py`：Gradio 调试入口
- `social_twin/knowledge.py`：知识摄取与覆盖报告
- `social_twin/vector_store.py`：LanceDB 检索
- `social_twin/memory.py`：SQLite 多联系人会话记忆
- `social_twin/service.py`：场景识别、策略选择、案例检索、草稿生成
- `social_twin/style.py`：去 AI 味风格审查
- `social_twin/connectors.py`：手动、API、浏览器 Agent 接入边界；v1 只返回草稿，需要人工确认
- `social_twin/profile.py`：主页截图/文字画像分析与对话画像更新

## 浏览器 Agent
第一版支持网页自动化边界：打开网页、查找未读消息、生成草稿、填入输入框、点击发送。

全自动发送受 `.env` 中 `AUTO_SEND_ENABLED` 控制。启用前需要安装浏览器驱动：

```bash
python -m playwright install chromium
```

桌面 App 和手机自动化暂不实现。

## 最近更新
- 新增 FastAPI 服务入口和 Bumble 专用 Agent。
- 新增联系人画像分析、画像证据库和对话画像自动更新。
- 新增 `sent_messages` 去重，避免同一 incoming 重复生成。
- Bumble Agent 支持按联系人独立状态、读取 profile、识别待回复消息组、填入草稿。
- Bumble Agent 输入框会显示真实阶段提示：profile、分析、RAG、逐句生成。
- 修复 `SentenceTransformer.encode` 参数兼容问题，避免 `Prompt name 'True'`。
- 草稿生成失败不再写入已处理消息，失败消息可重试。
- 测试替身不再强依赖真实 SQLite memory，单元测试可隔离 Bumble 状态逻辑。

## 验证
```bash
python3 -m py_compile app.py digital_twin.py digital_twin4.py social_twin/*.py
python3 -m unittest
```
