# 社交策略数字分身 v2

本地运行的 AI 社交助手：RAG 记忆库 + 结构化画像 + 草稿生成器 + 多平台自动回复 Agent（Bumble 网页版 + 探探/牵手 Android 真机）。

---

## 功能概览

- **RAG 知识库**：读取 `all_chapters.json` 索引所有策略案例，LanceDB 向量检索，每次生成前召回相关案例。
- **结构化联系人画像**：自动从对话和主页文字提取年龄/职业/爱好/性格/恋爱意图等字段，存入 SQLite，持续补全。
- **草稿生成**：场景识别 → 沟通技术选择 → RAG 召回 → LLM 生成 → 风格审查（去 AI 腔、压制问句/讨好/比喻），最多重写两次。
- **Bumble Agent**：Playwright 自动化，扫描 `Your move` 联系人，抓取 profile，逐条回复待回复消息，自动发送。
- **探探/牵手 Android Agent**：ADB + uiautomator2，手机真机无人值守，自动检测未读消息，抓取完整画像，自动发送回复。

---

## 环境要求

- Python 3.9+
- Node.js 18+（前端）
- ADB（Android 真机 Agent）
- 阿里云 DashScope API Key（LLM 调用）

---

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# Bumble Agent 需要 Playwright
python -m playwright install chromium
```

`.env`：
```bash
DASHSCOPE_API_KEY=你的Key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
REPLY_MODEL=qwen3.7-plus
DECISION_MODEL=qwen3.7-plus
CHEAP_MODEL=qwen3.6-flash
PREMIUM_MODEL=qwen3.7-max
PROFILE_VISION_MODEL=qwen3-vl-plus
PROFILE_OCR_MODEL=qwen-vl-ocr-latest
AUTO_SEND_ENABLED=true
ANDROID_AUTO_SEND_ENABLED=true
BUMBLE_TARGET_URL=https://eu1.bumble.com/app/connections
BUMBLE_POLL_SECONDS=5
BUMBLE_USER_DATA_DIR=./browser_profiles/bumble
```

---

## 启动

```bash
# 启动后端（含前端静态托管）
uvicorn app:app --host 0.0.0.0 --port 8000

# 前端开发模式（独立热重载）
cd frontend && npm install && npm run dev
```

控制台：
- `http://localhost:8000/about` — 产品介绍
- `http://localhost:8000/agent` — Bumble / Android Agent 控制台
- `http://localhost:8000/board` — 联系人看板、画像、消息记录

---

## Android Agent（探探/牵手）

### 前提

1. 手机通过 USB 连接 Mac，开启 USB 调试
2. `adb devices` 能看到设备
3. 锁屏设置为"滑动解锁"或"无"（否则屏幕亮起后仍无法操作）

### 启动

```bash
# 启动 Agent，自动发送
curl -X POST http://localhost:8000/agent/android/tantan/run \
  -H "Content-Type: application/json" \
  -d '{"auto_send_enabled": true}'

# 查看状态和日志
curl http://localhost:8000/agent/android/tantan/status

# 停止
curl -X POST http://localhost:8000/agent/android/tantan/stop
```

### 运行流程

```
CONNECTING_ADB → LAUNCHING_APP → APP_READY → SCANNING_CONTACTS（循环）
  → CONTACT_FOUND → CONTACT_OPENED
  → FETCHING_PROFILE（画像字段<3时）或 PROFILE_SKIPPED（已够）
  → MESSAGES_READ → PENDING_GROUP_FOUND → DRAFTING → DRAFTED → SENT
```

### 未读检测逻辑（双重）

1. **小红点**：`conversation_item_red_dot` 存在 → 处理
2. **预览变化**：无小红点但预览文字与上次处理时不同 → 也进去检查

小红点点进去后消失，所以不能只靠小红点。

### 画像抓取

- 点消息气泡旁左侧头像（`profile_avatar_layout`）进入 profile 页
- 先滚到顶部，再向下滚动（最多 8 屏，连续 2 屏无新内容停止）
- 只读 `profile_view_rv` 容器内的文字，避免背景聊天列表污染
- 跨屏全局去重，LLM 从中提取结构化字段（年龄/职业/爱好/性格/恋爱意图等）

### 消息读取

- 遍历 `content_wrapper` 节点，用 `profile_image` 的水平位置判断 in/out
- 文字/表情/图片/媒体消息全部记录（非文字记为 `[表情]`/`[图片/媒体]`）
- 系统匹配消息（"hi，我们可以聊天啦！"等）自动过滤，不进消息记录和待回复队列

### 回复逻辑

- `extract_pending_incoming_group`：取最后一条 out 消息之后的所有 in 消息
- 每条 in 消息生成一条草稿，顺序发送
- SHA-256 哈希去重，7 天内已发送过的消息不重复生成

### 数据存储

| 角色 | 含义 |
|---|---|
| `user` | 对方发来的消息 |
| `draft` | bot 生成草稿（待发送） |
| `sent` | 已成功发送 |

---

## Bumble Agent

```bash
# 启动
curl -X POST http://localhost:8000/agent/bumble/run \
  -H "Content-Type: application/json" \
  -d '{"auto_send_enabled": true, "poll_seconds": 5}'

# 状态
curl http://localhost:8000/agent/bumble/status

# 停止
curl -X POST http://localhost:8000/agent/bumble/stop
```

运行时输入框会显示阶段提示：`正在读取 profile…` → `正在分析对话…` → `正在 RAG 检索…` → `正在生成第 N 句…` → 草稿填入。

---

## 手动 API

分析联系人主页：
```bash
curl -X POST http://localhost:8000/contacts/alice/profile/analyze \
  -F 'profile_text=身高168cm，水瓶座，硕士，杭州，做金融，喜欢音乐旅行'
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
    "relationship_stage": "熟悉",
    "recent_emotion": "疲惫"
  }'
```

返回：`draft` / `technique` / `scenario` / `retrieved_cases` / `style_issues` / `profile_updates`

读取画像：
```bash
curl http://localhost:8000/contacts/alice/profile
curl http://localhost:8000/contacts
```

---

## 工作原理

1. 识别场景（陌生人开场 / 日常互动 / 情绪支持 / 约见面等）
2. 根据历史技术和当前场景选择沟通技术
3. RAG 召回策略案例（annotated_strategy）、自然对话案例（natural_dialogue）、人物关系案例（persona_dialogue）
4. LLM 生成 4–18 字中文短句草稿
5. 风格审查：压制 AI 腔、长段、连续问句、讨好、比喻。不通过则重写，最多两次；最终回退到自然样本

---

## 知识库格式

`all_chapters.json`（策略案例）：
```json
[
  {
    "context_3": "对方说的话",
    "reply": "我的回复",
    "thinking": "为什么这么回",
    "summary": "策略摘要",
    "reply_style": "克制"
  }
]
```

`persona_dialogues/*.json`（人物关系案例）：
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

---

## 架构

```
app.py                      FastAPI 服务入口
frontend/                   Vite React 控制台
social_twin/
  service.py                核心链路：场景→技术→RAG→生成→审查
  memory.py                 SQLite：联系人/会话/消息/画像
  profile.py                画像提取（规则+LLM）
  vector_store.py           LanceDB 案例检索
  style.py                  风格审查
  llm.py                    LLM 调用封装
  android_base.py           Android Agent 基类（线程/状态/消息读写）
  android_apps/tantan.py    探探/牵手连接器
  bumble.py                 Bumble 连接器（Playwright）
  connectors.py             浏览器通用 Agent
```

---

## 验证

```bash
python3 -m py_compile app.py social_twin/*.py social_twin/android_apps/*.py
cd frontend && npm run build
```
