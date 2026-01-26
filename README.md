# 数字孪生（社交策略版）

- 目标：构建一个“高拟真数字分身”，根据对话上下文自动选择沟通技术（如「询问」「延词」「借势」等），生成短句回复，并保存对话日志
- 技术栈：LanceDB + sentence-transformers（向量检索）、DashScope（Qwen3 系列模型）、Gradio（可视化聊天界面）
- 入口脚本：[digital_twin.py](file:///c:/Users/willi/OneDrive/桌面/digital_twin/digital_twin.py)

## 功能概述
- 加载知识库：从 `all_chapters.json` 提取每种沟通技术的规则（用途、注意事项）与对话案例
- 构建向量库：使用 `all-MiniLM-L6-v2` 将案例上下文嵌入，存入 LanceDB（本地目录 `./lancedb`）
- 技术决策：综合最近 5 条上下文，调用 Qwen3 模型选择最合适的单一技术并给出理由
- 回复生成：基于所选技术与检索到的相似案例，生成不超过 12 字的自然短句回复
- 日志记录：把每次用户输入、系统回复、所用技术、选择理由写入 `chat_history.json`
- Web 界面：Gradio 聊天窗，支持发送、清空操作，并自动保存日志

## 架构与数据流
- 知识源：`all_chapters.json` 包含多个章节，每章内含：
  - `theory`：技术规则（用途 usage、注意事项 precautions）
  - `dialogue`：按轮次列出的对话，包含角色 `A`/`B`、以及 `reply.content`、`thinking`、`summary`
- 数据处理：
  - 读取 JSON → 提取「历史片段上下文」与「A 端回复」→ 嵌入向量 → 写入 LanceDB 表 `dialogue_cases`
  - 运行时根据当前上下文 → 让大模型选择技术 → 在向量库中过滤该技术的案例并检索相似样本 → 生成最终回复
- 关键模块：
  - 向量库封装：类 LanceVectorStore（添加/查询/计数）参见 [digital_twin.py](file:///c:/Users/willi/OneDrive/桌面/digital_twin/digital_twin.py)
  - 决策与生成：`decide_technique()`、`generate_reply()`，调用 DashScope 兼容的 `OpenAI` 客户端
  - 前端：Gradio `Blocks` + `Chatbot`，监听文本框与按钮事件

## 运行与配置
- 环境变量：在 `.env` 写入 `DASHSCOPE_API_KEY=你的Key`
- 安装依赖（建议虚拟环境）：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install python-dotenv lancedb pyarrow sentence-transformers openai gradio
```

- 运行：

```powershell
python digital_twin.py
```

- 默认行为：
  - LanceDB 数据目录：`./lancedb`
  - Web：`http://0.0.0.0:7860`，`share=True` 会生成公网临时链接
  - 日志：`chat_history.json`

## 数据文件说明（all_chapters.json）
- 每个章节键名形如：`"第X章：询问"`，脚本会解析冒号后的技术名
- 典型结构示例（精简）：

```json
{
  "第1章：询问": [
    {
      "theory": {
        "usage": ["获取更多信息", "引导对方表达"],
        "precautions": ["避免质问语气", "保持同理心"]
      }
    },
    {
      "dialogue": [
        {"role": "B", "reply": {"content": "最近好累啊"}},
        {"role": "A", "reply": {"content": "怎么累的"}, "thinking": "...", "summary": "开放式提问"}
      ]
    }
  ]
}
```

## 定制与扩展
- 模型与提示词：
  - `decide_technique()` 与 `generate_reply()` 中的提示可调整，用于控制风格与约束
  - 如需个性化人设，可参考 `digital_twin4.py` 的生成提示，替换为你的信息
- 技术库扩展：
  - 在 `all_chapters.json` 添加新的章节与案例，脚本首次运行会自动索引到 LanceDB
- 存储替换：
  - 目前默认使用 LanceDB；如需迁移到 ChromaDB，需要改写向量存取逻辑

## 项目结构
- `digital_twin.py`：主应用（Gradio UI、决策与回复、日志）
- `digital_twin4.py`：变体示例（更个性化的生成提示）
- `all_chapters.json`：沟通技术与案例库
- `chat_history.json`：对话日志
- `lancedb/`：向量数据本地存储
- `.gradio/`：Gradio 配置
- `.env`：DashScope 密钥
- `.py`：ChromaDB 最小化连通性测试脚本（如不需要可忽略）

## 常见问题
- DashScope 403 或鉴权失败：检查 `.env` 中 `DASHSCOPE_API_KEY` 是否正确，或网络是否能访问 `https://dashscope.aliyuncs.com`
- 首次运行空向量库：会自动索引 `all_chapters.json` 的案例，时间取决于数据量
- 中文分词与短句：生成阶段已加入“每句不超过 12 字”的约束，可在提示词中调整

## Git 使用快捷指令
- 初始化后首次提交：
```powershell
git add .
git commit -m "初始化项目与 README 与数据结构说明"
```
