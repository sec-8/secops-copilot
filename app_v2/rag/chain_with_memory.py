"""
v2 RAG Chain + 三层 Memory
- 短期：RedisChatMessageHistory（RunnableWithMessageHistory 自动管）
- 长期：asyncpg + store_items 表（手写 SQL）
- 向量：LanceDB secops_memory
"""
import os
import sys
import logging
# import asyncio
import asyncpg
from pathlib import Path
from typing import Optional

sys.stdout.reconfigure(encoding="utf-8")
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# if sys.platform == "win32":
#     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
# from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
# from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.redis.aio import AsyncRedisSaver


from app_v2.rag.retriever import HybridLangChainRetriever
from app_v2.rag._vector_memory_helper import get_memory_store

from app.config import settings
from rag.hybrid import HybridRetriever
from rag.vector_store import LanceDBVectorStore
from rag.loader import load_all_markdown
from rag.chunker import MarkdownChunker

logger = logging.getLogger(__name__)

# 环境变量
REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379/0")
POSTGRES_URI = os.getenv("POSTGRES_URI", "postgresql://postgres:mysecretpassword@localhost:5432/memory_db")
SESSION_TTL = 24 * 60 * 60  # 24h

# get_session_history（短期对话历史）
def get_session_history(session_id: str, user_id: str) -> BaseChatMessageHistory:
    """
    短期对话历史 = RedisChatMessageHistory
    - session_id 拼接 user_id 前缀（3 用户隔离）
    - 形式："{user_id}:{session_id}"
    - TTL 24h
    
    跟 LangGraph AsyncRedisSaver 关系？
    - 两者并存不冲突：前者管"对话消息 list"、后者管"LangGraph state checkpoint"
    """
    composite_key = f"{user_id}:{session_id}"
    return RedisChatMessageHistory(
        session_id=composite_key,
        url=REDIS_URI,
        ttl=SESSION_TTL,
    )

# asyncpg 长期 Memory 弃用 AsyncPostgresStore!
class LongTermMemory:
    """
    长期 Memory = asyncpg 直查 store_items 表
    - AsyncPostgresStore 封装太厚，aput 静默失败，而且不透明不知道问题在哪
    - 改用 asyncpg.create_pool() + 手写 SQL
    - INSERT/UPDATE/SELECT 完全可控
    - 立即 SELECT 验证（避免 aput 静默失败）
    """
    def __init__(self, dsn: str = POSTGRES_URI):
        self.dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """创建连接池（懒加载）"""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.dsn)
            # 手动建表
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS store_items (
                        namespace TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT,
                        updated_at TIMESTAMP DEFAULT NOW(),
                        PRIMARY KEY (namespace, key)
                    );
                """)
    
    async def put(self, namespace: tuple, key: str, value: str) -> bool:
        """upsert 事实 + 立即 SELECT 验证"""
        ns = ":".join(namespace) # ("user_facts", "user-001") -> "user_facts:user-001"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO store_items (namespace, key, value)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (namespace, key)
                   DO UPDATE SET value = $3, updated_at = NOW();
                """,
                ns, key, value
            )
            # 立即验证（直查 > 依赖 SDK）
            row = await conn.fetchrow(
                "SELECT value FROM store_items WHERE namespace = $1 AND key = $2",
                ns, key
            )
            return row is not None and row["value"] == value
    
    async def get_all(self, namespace: tuple) -> list[dict]:
        """查某用户所有事实"""
        ns = ":".join(namespace) 
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM store_items WHERE namespace = $1 ORDER BY updated_at DESC",
                ns
            )
            return [{"key": r["key"], "value": r["value"]} for r in rows]
    
# 抽事实（复数 key）
async def extract_facts(text: str, llm) -> list[dict]:
    """
    抽事实 + 复数 key 优化
    - programming_languages 复数 key + value 拼逗号
    - 置信度 > 0.7 过滤
    """
    extraction_prompt = f"""
    分析以下对话， 提取用户陈述的确定事实。
    规则：
    1. 只抽"明确陈述"的事实，如"我叫 X"、"我是 X 职业"、"我用 X 语言"等，
    请注意不明确表述，如"想成为 X"、"打算 X"、"可能 X" 不抽
    2. 同一类多个值用**复数 key** + value 拼逗号分隔
    3. 只提取明确陈述的事实，不做推断，置信度 confidence 0~1
    4. 明确陈述的事实 confidence 必须 >= 0.8，模糊/不确定的陈述（"可能"、
    "大概"）confidence 给 < 0.5，会被过滤
    
    对话：{text}
    
    输出示例（只输出 JSON 数组，不要其他内容）：
    [
        {{"key": "user_name", "value": "小明", "confidence": 0.95}},
        {{"key": "job", "value": "程序员", "confidence": 0.9}},
        {{"key": "programming_languages", "value": "JavaScript, Python, Go"}},
    ]
    没有可提取的事实输出空数组 []
    """
    response = await llm.ainvoke([HumanMessage(content=extraction_prompt)])
    import json
    try:
        facts = json.loads(response.content)
        if not isinstance(facts, list):
            return []
        return [f for f in facts if isinstance(f, dict) and f.get("confidence", 0) > 0.7]
    except (json.JSONDecodeError, TypeError) as e:
        # 友好提示：记录原始响应，知道为什么失败
        logger.warning(f"extract_facts JSON 解析失败: {type(e).__name__}, response={response.content[:200]}")
        return []
    
# AgentState 显式声明 user_id
class AgentState(MessagesState):
    """
    LangGraph 状态
    
    class AgentState(MessagesState): pass → user_id 不保留
    修复：显式声明 user_id: str
    
    LangGraph State 未声明字段不会保留，必须显式声明 + 节点透传
    """  
    user_id: str
    
def build_graph(retriever, llm, ltm: LongTermMemory):
    """
    构建带三层 Memory 的 RAG 链
    - 短期：AsyncRedisSaver checkpointer（LangGraph 自动管）
    - 长期：LongTermMemory asyncpg（chain_node 显式查/写）
    - 向量：get_memory_store() LanceDB（chain_node 入口前查）
    """
    async def chain_node(state: AgentState, runtime) -> dict:
        # 从anonymous取user_id
        user_id = state.get("user_id", "anonymous")
        
        last_msg = state["messages"][-1]
        question = last_msg.content if hasattr(last_msg, 'content') else str(last_msg)
        # 查向量 Memory
        memory_store = get_memory_store()
        similar = memory_store.search(question, top_k=3)
        vector_memory_text = "\n".join(
            f"[{d['source']}] {d['text'][:200]}"  # d['source'] 显示来源，d['text'] 显示历史
            for d in similar
        ) if similar else "（暂无向量历史）"
        
        # 拼 system_msg（向量 Memory）
        system_msg = SystemMessage(content=f"""
        你是 SecOps 安全知识库助手，基于用户提供的上下文回答问题。
        
        【历史相似对话】
        {vector_memory_text}

        要求：
        1. 只使用上下文里的信息回答，不要编造
        2. 如果上下文包含与问题相关的信息（哪怕只有部分相关），你必须回答
        3. 只有在上下文完全没有相关信息时，才拒答
        4. 如果上下文信息部分相关但不完整，请基于已有信息给出**部分答案**，并明确说明哪
        些内容在文档中未找到依据（例如："根据文档，A 和 B 成立，但关于 C 没有提及"）。
        5. 绝对禁止使用外部知识或常识来补充文档中缺失的信息
        6. 必须输出合法 JSON，不要输出 JSON 以外的任何内容
        
        输出 JSON 格式：{{"has_answer": true/false, "answer": "..."}}""")
        
        # 查长期事实
        namespace = ("user_facts", user_id)
        facts = await ltm.get_all(namespace)
        facts_text = "\n".join(f"- {t['key']}:{t['value']}" for t in facts) or "（暂无）"
        # RAG
        docs = retriever.invoke(question)
        context_text = "\n".join(
            f"{doc.metadata.get('source', '?')}:{doc.page_content}" for doc in docs
        ) or "（无相关上下文）"
        # 长期事实拼到上下文
        full_context = f"【用户长期事实】\n{facts_text}\n\n【知识库】\n{context_text}"
        # 调LLM
        human_msg= HumanMessage(content=f"上下文：{full_context}\n问题：{question}")
        # 排除最后一条 user msg 避免重复
        history = state["messages"][:-1]
        response = await llm.ainvoke([system_msg] + history + [human_msg])
        # 抽事实 + 写长期记忆（logger 而非 print：服务进程内 print 会混进 uvicorn 日志且无法按级别过滤）
        extracted = await extract_facts(question, llm)
        logger.debug(f"[抽事实] LLM 返回: {extracted}")
        for fact in extracted:
            key, value = fact["key"], fact["value"]
            ok = await ltm.put(namespace, key, value)
            logger.info(f"{'✅' if ok else '❌'} 写入长期事实: {key} = {value}")
        # 透传 user_id 避免回退到 anonymous  
        return {"messages": [response], "user_id": user_id}
    
    builder = StateGraph(AgentState)
    builder.add_node("chain_code", chain_node)
    builder.add_edge(START, "chain_code")
    builder.add_edge("chain_code", END)
    return builder

# 完整图编译（短期 Redis + 长期 PG）
async def compile_graph():
    """懒加载完整图（短期 Redis + 长期 PG）
    关键设计：
    - checkpointer.asetup() 只调一次（重复调报"索引已存在"）
    - 短期 + 长期 一起 compile
    """
    os.environ.setdefault("REDIS_URL", REDIS_URI)
    # RAG 组件
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    chunker = MarkdownChunker()
    chunked_docs = chunker.process_documents(docs)
    store = LanceDBVectorStore(PROJECT_ROOT / "data" / "lancedb")
    hybrid = HybridRetriever(store, chunked_docs)
    retriever = HybridLangChainRetriever(hybrid=hybrid, k=5)
    
    # LLM
    args = settings.primary_client_args()
    llm = ChatOpenAI(
        api_key=args["api_key"],
        base_url=args["base_url"],
        model=args["model"],
        temperature=0
    )
    # 长期 Memory
    ltm = LongTermMemory()
    await ltm.connect()
    
    # 短期 + 长期 一起编译
    graph_builder = build_graph(retriever, llm, ltm)
    async with AsyncRedisSaver.from_conn_string(REDIS_URI) as checkpointer:
        await checkpointer.asetup()
        graph = graph_builder.compile(checkpointer=checkpointer)
        return graph
    