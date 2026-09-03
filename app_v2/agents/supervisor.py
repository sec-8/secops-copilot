"""Supervisor Agent 方案：规则 fast path + LLM slow path + rag 兜底"""
import re
import json
import asyncio
import logging
from langchain_core.messages import SystemMessage, HumanMessage
from app_v2.llm.custom_chat_model import RouterChatModel # 复用 Router 4 tier

logger = logging.getLogger(__name__)

# fast path 规则 
# 规则1：IP 正则（精确匹配 IPv4 格式）
IP_PATTERN = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

# 规则2：关键词清单
TOOL_KEYWORDS = ["解析日志", "parse_log", "解析这条"]  # parse_log_fields
MEMORY_KEYWORDS = ["记住", "我叫", "叫什么", "我叫什么", "我是", "是我"]  # memory
# 顶部加常量
RAG_FALLBACK_KW = ["ping", "DNS", "TCP", "UDP", "三次握手"]  # 模糊网络概念 → rag

def rule_dispatch(question: str) -> str | None:
    if IP_PATTERN.search(question):
        return "tool"
    if any(keyword in question for keyword in TOOL_KEYWORDS):
        return "tool"
    if any(keyword in question for keyword in MEMORY_KEYWORDS):
        return "memory"
    if any(keyword in question for keyword in RAG_FALLBACK_KW):
        return "rag"  # 模糊网络概念 → 走知识库
    return None

# slow path LLM 
SUPERVISOR_PROMPT = """你是 Supervisor，负责把用户问题分给 3 个专业 agent：
- rag：知识库检索（如"什么是 X"、"X 怎么防御"）
- tool：工具调用（IP 查询、日志解析）
- memory：长期记忆（如"记住 X"、"我叫什么"）

只输出 JSON：{{"agent": "rag|tool|memory"}}
不要其他内容。"""

async def llm_dispatch(question: str) -> str:
    llm = RouterChatModel(temperature=0)
    messages = [
        SystemMessage(content=SUPERVISOR_PROMPT),
        HumanMessage(content=question)
    ]
    
    try:
        res = await llm.ainvoke(messages)
        data = json.loads(res.content)
        agent = data.get("agent", "").strip().lower()
        if agent in ("rag", "tool", "memory"):
            return agent
        return "rag"  # 兜底
    except Exception as e:
        logging.warning(f"LLM 分派失败: {e}")
        return "rag"  # 兜底
    
# 入口
async def dispatch(question: str) -> str:
    """
    流程：
    1. 规则 fast path（不耗 LLM token）
    2. 规则不命中 → LLM slow path
    3. LLM 失败 → 兜底 rag
    
    返回: "rag" | "tool" | "memory"
    """
    agent = rule_dispatch(question)
    if agent is not None:
        logger.info(f"规则分派: {question[:30]} → {agent}")
        return agent
    agent = await llm_dispatch(question)
    logger.info(f"规则分派: {question[:30]} → {agent}")
    return agent

if __name__ == "__main__":
    print(asyncio.run(dispatch("XSS 防御")))
    print(asyncio.run(dispatch("8.8.8.8 是否恶意")))
    print(asyncio.run(dispatch("解析 192.168.1.1")))
    print(asyncio.run(dispatch("ping 一下")))