"""SecOps Copilot 只读工具集"""
import sys
from pathlib import Path
# 让脚本能 import 到项目里的 rag / app 包
sys.stdout.reconfigure(encoding="utf-8")  # 防 Windows GBK emoji 崩
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

#RAG懒加载的依赖
from rag.vector_store import LanceDBVectorStore
from rag.hybrid import HybridRetriever
from rag.ask import RAGAsker
from rag.loader import load_all_markdown
from rag.chunker import MarkdownChunker
from app.security import (
    is_read_allowed,
    dry_run_wrapper
)


_asker = None # 模块级单例，先置空
def get_asker():
    """懒加载 RAGAsker 单例：第一次调用才构建（重活只干一次）"""
    global _asker
    db_path = PROJECT_ROOT / "data" / "lancedb"
    if _asker is None:
        docs = load_all_markdown(PROJECT_ROOT / "knowledge")
        chunked = MarkdownChunker().process_documents(docs)
        # 切好的 chunks 写入向量库,向量库数据
        store = LanceDBVectorStore(db_path)
        retriever = HybridRetriever(store, chunked)
        _asker = RAGAsker(retriever)
    return _asker

# ---- 工具 1：IP 威胁情报查询 ----
@dry_run_wrapper("query_ip_reputation")
def query_ip_reputation(ip: str) -> dict:
  
    mockIp = [
        {"ip": "1.2.3.4","tag": "SQL注入","source":"high"},
        {"ip": "192.168.4.5","tag": "DNS","source":"medium"},
        {"ip": "19.11.45.52","tag": "SSH","source":"critical"}
    ]
    
    for item in mockIp:
        if item["ip"] == ip:
            return {"ip": ip, "malicious": True, "tags": item["tag"], "source": item["source"]}

    return {"ip": ip, "malicious": False, "tags": None, "source": "low"}
            


# ---- 工具 2：安全知识库检索 ----
@dry_run_wrapper("search_knowledge")
def search_knowledge(query: str) -> str:
    """检索本地安全知识库中检索相关片段，返回相关片段（不做最终生成，交给 Agent 主 LLM）"""
    # 弃用
    # file_path = "knowledge/sample_sop.md"
    # allowed, msg = is_read_allowed(file_path)
    # if not allowed:
    #     return f"【安全拦截】{msg}" # 拦截时返回明确错误，不执行读取
    asker = get_asker()
    result = asker.ask(query) # 返回 {answer, citations, has_answer, contexts}
    # 只取 contexts + citations，丢掉 answer
    contexts = result["contexts"] #  ask.py 里 txt = item["source"] + ':' + item["text"]， contexts.append(txt),已经带来源了不用再取citations
    # 如果 contexts 是空列表（检索没命中/被拒答）
    if not contexts:
        return "【检索结果】知识库中未找到与该查询相关的片段。"
    context_text:str = "".join([item + "\n" for item in contexts])
    return context_text

# ---- 工具 3：日志字段解析 ----
@dry_run_wrapper("parse_log_fields")
def parse_log_fields(log_text: str) -> dict:
    import re
    import ipaddress
    """从原始日志文本中解析出结构化字段。"""
    # 用正则提取 IP、端口、时间等
    match_ip = re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", log_text)
    match_port = re.search(r"port[:\s]+(\d+)", log_text)
    ip = ipaddress.ip_address(match_ip.group()) if match_ip else None
    port = match_port.group() if match_port else None 

    return {"ip": ip, "port": port}


# 工具 Schema：告诉模型有哪些工具可用
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_ip_reputation",          # 必须和函数名一致
            "description": "查询指定 IP 的威胁情报，返回是否恶意、标签及评级，仅用于IP的威胁情报匹配，其他内容无用。",  # 模型靠这句判断要不要调
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {
                        "type": "string",
                        "description": "要查询的 IP 地址，如 1.2.3.4"
                    }
                },
                "required": ["ip"]    # 哪些参数必填
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",          
            "description": "安全知识库检索，返回检索到的原始片段供你参考研判，不含最终结论。【仅用于网络安全相关内容检索，不适用于其他内容】",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要检索的关键词/问题，如 'prompt注入怎么防"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "parse_log_fields",          
            "description": "日志与告警字段解析，返回通过正则匹配到的解析内容，满足数据按分类格式化输出，仅用于从原始日志文本提取字段，不做威胁判定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_text": {
                        "type": "string",
                        "description": "要解析的日志或告警内容，如 检测到来自IP 192.168.10.55的SSH暴力破解攻击，5分钟内失败登录尝试达120次"
                    }
                },
                "required": ["log_text"]
            }
        }
    }
] 
    