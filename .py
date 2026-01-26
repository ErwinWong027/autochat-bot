import chromadb
from chromadb.utils import embedding_functions

# 测试初始化
client = chromadb.Client()
ef = embedding_functions.DefaultEmbeddingFunction()

# 测试基本操作
coll = client.create_collection("test", embedding_function=ef)
coll.add(documents=["Hello, world!"], ids=["1"])
result = coll.query(query_texts=["Hi"], n_results=1)

print("✅ 全链路测试通过！")
print("回复:", result["documents"][0][0])