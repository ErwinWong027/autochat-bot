import json
import re
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer
from openai import OpenAI
import gradio as gr

# 加载环境变量
load_dotenv()
API_KEY = os.getenv("DASHSCOPE_API_KEY")
if not API_KEY:
    raise ValueError("请在 .env 文件中设置 DASHSCOPE_API_KEY")

# 全局变量
technique_theory = {}
vector_store = None
conversation_history = []  # 用于网页状态
chat_log_file = "chat_history.json"

# ======================
# LanceDB 向量存储封装
# ======================
class LanceVectorStore:
    def __init__(self, db_path: str = "./lancedb", table_name: str = "dialogue_cases"):
        self.db_path = db_path
        self.table_name = table_name
        self.db = lancedb.connect(db_path)
        self.embedder = SentenceTransformer('all-MiniLM-L6-v2')
        self._ensure_table()

    def _ensure_table(self):
        if self.table_name not in self.db.table_names():
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("context", pa.string()),
                pa.field("reply", pa.string()),
                pa.field("technique", pa.string()),
                pa.field("thinking", pa.string()),
                pa.field("summary", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 384))
            ])
            self.db.create_table(self.table_name, schema=schema, mode="create")
        self.table = self.db.open_table(self.table_name)

    def add(self, samples: list):
        vectors = self.embedder.encode([s["context"] for s in samples], convert_to_numpy=True).tolist()
        data = []
        for i, s in enumerate(samples):
            data.append({
                "id": str(i),
                "context": s["context"],
                "reply": s["reply"],
                "technique": s["technique"],
                "thinking": s["thinking"],
                "summary": s["summary"],
                "vector": vectors[i]
            })
        self.table.add(data)

    def query(self, query_text: str, technique: str = None, n_results: int = 2):
        query_vec = self.embedder.encode([query_text], convert_to_numpy=True)[0].tolist()
        search_builder = self.table.search(query_vec).limit(n_results)
        
        # 按 technique 过滤（LanceDB 支持 SQL-like filter）
        if technique:
            search_builder = search_builder.where(f"technique = '{technique}'", prefilter=True)
        
        results = search_builder.to_pandas()
        if results.empty:
            return {"ids": [[]], "metadatas": [[]]}
        
        # 转为 ChromaDB 兼容格式
        metadatas = []
        for _, row in results.iterrows():
            metadatas.append({
                "reply": row["reply"],
                "technique": row["technique"],
                "thinking": row["thinking"],
                "summary": row["summary"]
            })
        return {
            "ids": [results["id"].tolist()],
            "metadatas": [metadatas]
        }

    def count(self):
        return self.table.count_rows()

# ======================
# 初始化：加载 JSON + 构建向量库
# ======================
def initialize_system():
    global technique_theory, vector_store
    
    print("🔄 正在加载知识库...")
    with open("all_chapters.json", "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    # 提取 theory
    technique_theory = {}
    dialogue_samples = []

    for chapter_key, cases in raw_data.items():
        technique_match = re.search(r"[:：]\s*(.+)", chapter_key)
        technique = technique_match.group(1).strip() if technique_match else "未知"
        
        for item in cases:
            if "theory" in item:
                technique_theory[technique] = item["theory"]
                break
        
        for item in cases:
            if "dialogue" not in item:
                continue
            dialogue = item["dialogue"]
            history = []
            for turn in dialogue:
                if "reply" not in turn:
                    continue
                role = turn["role"]
                content = turn["reply"]["content"]
                if role == "B":
                    history.append(f"B: {content}")
                elif role == "A":
                    if "thinking" in turn and "summary" in turn:
                        context = " | ".join(history[-5:]) if history else "[开场]"
                        dialogue_samples.append({
                            "context": context,
                            "reply": content,
                            "technique": technique,
                            "thinking": turn["thinking"],
                            "summary": turn["summary"]
                        })
                    history.append(f"A: {content}")

    # 初始化 LanceDB
    vector_store = LanceVectorStore()

    if vector_store.count() == 0:
        print(f"🔍 发现新数据，正在索引 {len(dialogue_samples)} 个案例...")
        vector_store.add(dialogue_samples)
    
    print(f"✅ 系统就绪！加载了 {len(technique_theory)} 种技术，{len(dialogue_samples)} 个案例。")
    return True

# ======================
# 核心推理函数（保持不变）
# ======================
def decide_technique(current_context: str) -> dict:
    theory_text = ""
    for tech, th in technique_theory.items():
        usage = "、".join(th.get("usage", []))
        precautions = "；".join(th.get("precautions", []))
        theory_text += f"【{tech}】\n- 用途：{usage}\n- 注意事项：{precautions}\n\n"
    
    prompt = f"""
你是一个社交策略专家，当前对话上下文：
「{current_context}」

以下是可用沟通技术及其规则：
{theory_text}

请选出**最应使用的单一 technique**，并说明理由。
输出严格为 JSON 格式：{{"selected_technique": "xxx", "reason": "xxx"}}
"""
    client = OpenAI(api_key=API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    resp = client.chat.completions.create(
        model="qwen-turbo",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=200
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except:
        return {"selected_technique": "询问", "reason": "默认回退"}

def generate_reply(current_context: str, decision: dict) -> str:
    tech = decision["selected_technique"]
    th = technique_theory.get(tech, {})
    
    # 使用 LanceDB 查询（支持 where 过滤）
    results = vector_store.query(
        query_text=current_context,
        technique=tech,
        n_results=2
    )
    
    cases_text = ""
    for i in range(min(2, len(results["ids"][0]))):
        rep = results["metadatas"][0][i]["reply"]
        think = results["metadatas"][0][i]["thinking"]
        summ = results["metadatas"][0][i]["summary"]
        cases_text += f"回复：{rep}\n思考：{think}\n策略：{summ}\n\n"
    
    usage = "、".join(th.get("usage", []))
    precautions = "；".join(th.get("precautions", []))
    
    prompt = f"""
对话多用短句，每句不超过12字，末尾没有标点符号。有神秘感。

你正在使用「{tech}」技术。
- 用途：{usage}
- 注意事项：{precautions}
减少结尾的问句频率，禁止连续超过2次使用相同的{tech}。

参考案例：
{cases_text}

当前上下文：「{current_context}」
人物简历：名字Erwin，商学院硕士，创业者，美股投资者，兼职老师。水瓶座ENTJ，比较喜欢音乐、哲学、运动和动漫。
生成一条自然、有效的回复。只输出回复内容。
"""
    client = OpenAI(api_key=API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    resp = client.chat.completions.create(
        model="qwen3-max",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=150
    )
    return resp.choices[0].message.content.strip()

# ======================
# 聊天与保存日志（保持不变）
# ======================
def save_chat_log(user_msg, bot_reply, decision):
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user_input": user_msg,
        "bot_reply": bot_reply,
        "technique_used": decision["selected_technique"],
        "decision_reason": decision["reason"]
    }
    
    if os.path.exists(chat_log_file):
        with open(chat_log_file, "r", encoding="utf-8") as f:
            logs = json.load(f)
    else:
        logs = []
    
    logs.append(log_entry)
    
    with open(chat_log_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

def chat_with_bot(user_message, history):
    global conversation_history
    
    # 更新全局历史（用于上下文）
    conversation_history.append(f"B: {user_message}")
    current_context = " | ".join(conversation_history[-5:])
    
    # 决策 + 生成回复
    decision = decide_technique(current_context)
    reply = generate_reply(current_context, decision)
    
    # 保存日志
    save_chat_log(user_message, reply, decision)
    
    # 更新全局历史
    conversation_history.append(f"A: {reply}")
    
    # ✅ 关键修复：使用新格式 [{"role": ..., "content": ...}, ...]
    new_history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply}
    ]
    return new_history, ""


def reset_conversation():
    global conversation_history
    conversation_history = []
    return [], ""  # 返回空列表，符合新格式

# ======================
# 启动 Gradio 界面（保持不变）
# ======================
if __name__ == "__main__":
    initialize_system()
    
    with gr.Blocks(title="🧠 你的数字分身 - 社交策略版") as demo:
        gr.Markdown("## 💬 和你的高拟真数字分身聊天")
        gr.Markdown("它会根据你的沟通体系，动态选择「询问」「延词」「借势」等技术进行回复。")
        
        chatbot = gr.Chatbot(height=500)
        msg = gr.Textbox(label="输入对方的消息", placeholder="例如：'最近好累啊...'")
        with gr.Row():
            submit_btn = gr.Button("发送")
            clear_btn = gr.Button("清空对话")
        
        submit_btn.click(
            fn=chat_with_bot,
            inputs=[msg, chatbot],
            outputs=[chatbot, msg]
        )
        msg.submit(
            fn=chat_with_bot,
            inputs=[msg, chatbot],
            outputs=[chatbot, msg]
        )
        clear_btn.click(
            fn=reset_conversation,
            inputs=[],
            outputs=[chatbot, msg]
        )
        
        gr.Markdown(f"📝 所有对话将自动保存至 `{chat_log_file}`")
    
    demo.launch(server_name="0.0.0.0", server_port=7860, share=True)