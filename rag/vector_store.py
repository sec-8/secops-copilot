# 把切好的 chunks 写入向量库，实现检索
import lancedb
from typing import List, Dict
from pathlib import Path
import requests

DEFAULT_EMBEDDING_URL = "http://localhost:11434/api/embeddings"   # 模型链接
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text" # 模型链接名字
DEFAULT_TABLE_NAME = "secops_knowledge" # 表名

class LanceDBVectorStore:
    def __init__( # “接收外部传参，并把这些参数挂载到当前实例（self）身上”的初始化入口。
        self,
        db_path: Path,
        embedding_url: str = DEFAULT_EMBEDDING_URL,
        embedding_model:str = DEFAULT_EMBEDDING_MODEL,
        table_name:str = DEFAULT_TABLE_NAME
    ):
        self.db_path = db_path
        self.embedding_url = embedding_url
        self.embedding_model = embedding_model
        self.table_name = table_name
        
        self.db = None
        self.table = None
        self.connect()
    # 链接    
    def connect(self):    
        self.db = lancedb.connect(str(self.db_path))
        tables = self.db.list_tables() # list_tables() 可以查有哪些表
        if self.table_name in  tables.tables:
            # 表已存在，直接打开加载
            self.table = self.db.open_table(self.table_name)
        else:
            # 表不存在，等 add 时创建
            self.table = None
            
    def embed_text(self, text: str) -> List[float]:
        """调用 Ollma 接口获取文本 embedding"""
        payload = {
            "model": self.embedding_model,
            "prompt": text
        }
        resp = requests.post(self.embedding_url, json=payload)
        resp.raise_for_status() # 出错抛异常
        data = resp.json()
        return data["embedding"] # Ollama 返回格式就是 {"embedding": [ 数组 ]}
    
    def add_chunks(self, chunked_docs: List[Dict]) -> None:
        # 把切好的 chunks 加入向量库
        data = []
        for doc in chunked_docs:
            source = doc["source"]
            for chunk_idx, text in enumerate(doc["chunk_list"]):   # enumerate(iterable, start=0)，它会把一个可迭代对象（比如列表）包装成一个枚举对象，每次迭代返回一个包含索引和对应元素的元组 (index, value)
                print(f"Embedding {source} chunk {chunk_idx}...")
                embedding = self.embed_text(text)
                data.append({
                    "vector": embedding,
                    "text": text,
                    "source": source,
                    "chunk_index": chunk_idx
                }) 
        # 第一次 add，表不存在，自动创建
        if self.table is None:
            if data:
                self.table = self.db.create_table(self.table_name, data=data)
            else:
                raise ValueError("No data to create table")
        else:
            # 表存在，直接加
            if data:
                self.table.add(data)      
              
   
    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        query_embedding = self.embed_text(query)
        # 搜索转 pandas 再转字典列表
        # query_embedding：先把「问题」这段文字，用 embedding 模型转成一个 1024 维的向量（一串数字）
        # .search(query_embedding)：拿这个向量，去库里跟每个 chunk 的向量算距离
        # .metric("cosine")：用余弦距离算 —— 两个向量方向越接近，距离越小
        
        df = self.table.search(query_embedding).metric("cosine").limit(top_k).to_pandas()
        # 转成我们要的格式，带分数（LanceDB 返回的 _distance 越小越相似）
        # _distance：算出来的余弦距离，范围 0~2，越小越相似
        # score = 1 - _distance（你在 vector_store 里转的）：越大越相似，业务层好用
        # 这个score分数是纯数学计算出来的，衡量的是「问题」和「某段笔记」在语义向量空间里有多接近，全程没有 LLM 参与打分
        results = []
        for _, row in df.iterrows():
            results.append({
                "text": row["text"],
                "source": row["source"],
                "chunk_index": row["chunk_index"],
                "score": 1 - row["_distance"] # 转成 0~1 分数，越大越相似
            })
        return results
    
    # 重建方法 
    def rebuild(self, chunked_docs: List[Dict]) -> None:
        """重建整个表（全量更新用）"""
        self.connect()
        # 重建必须先删表，保证干净
        tables = self.db.list_tables()
        if self.table_name in tables.tables:
            self.db.drop_table(self.table_name)
        # 加入 chunks
        self.add_chunks(chunked_docs)
        # 关键：drop+create 后旧连接缓存了失效的 manifest，
        # 重新 connect 刷新连接与 table handle，否则 search 会读到已删分片 → Not found
        self.db = lancedb.connect(str(self.db_path))
        self.table = self.db.open_table(self.table_name)
        
if __name__ == "__main__":
    from pathlib import Path
    from rag.loader import load_all_markdown
    from rag.chunker import MarkdownChunker
    
    # 动态获取当前项目的根目录绝对路径
    PROJECT_ROOT = Path(__file__).parent.parent # 一个内置变量，代表当前脚本文件（即你写的这个 .py 文件）的完整路径
    # 拼接出数据库文件存放的具体文件夹路径 
    db_path = PROJECT_ROOT / "data" / "lancedb"
    # 确保这个文件夹存在，如果不存在就立刻创建它， parents=True（创建父级目录）， exist_ok=True（存在即忽略）
    db_path.mkdir(parents=True, exist_ok=True)

    # 1.  加载 与 切分
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    chunker = MarkdownChunker()
    chunked_docs = chunker.process_documents(docs)
    print(f"加载 {len(docs)} 文档，切分得到 {sum(d['chunk_index'] for d in chunked_docs)} chunks")

    # 2. 写入向量库
    store = LanceDBVectorStore(db_path)
    store.rebuild(chunked_docs)
    print("写入完成")

    # 3 测试检索
    # test_query = "三级降级是怎么实现的？"
    # results = store.search(test_query, top_k=3)
    # print(f"\n测试查询: {test_query}")
    # for i, res in enumerate(results):
    #     print(f"\n{i+1}. {res['source']} (score: {res['score']:.4f})")
    #     print(f"   {res['text'][:200]}...")