"""v2 MCP Server - 把 v1 3 个 tool 暴露成 MCP Tool

设计依据：
- stdio 传输
- 模块顶层 import v1 tools 触发懒加载
- parse_log_fields 内部 str() + JSON encoder default=str
- search_knowledge 内部 slice 前 top_k 个
- mcp.run(transport="stdio")
"""
import asyncio
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# 模块全局 - 启动时从 CLI 读取，handle_call_tool 读
_GLOBAL_TOP_K = 3

# 让脚本能 import 到项目里的 app / rag 包
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")  # 防 Windows GBK 乱码

# 预导入 v1 工具到模块顶层
from app.tools import (
    query_ip_reputation,
    search_knowledge,
    parse_log_fields,
    TOOLS_SCHEMA,
)

# logger 写 stderr（防 print 污染 JSON-RPC）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("secops-tools")

# 按 token 精准切
import tiktoken
_ENC = tiktoken.get_encoding("cl100k_base")
# search_knowledge 截断 top_k
def _search_knowledge_with_topk(query: str, top_k: int = 3) -> str:
    """包装 v1 search_knowledge，截断前 top_k 个 chunk"""
    raw = search_knowledge(query)
    if not raw or "未找到" in raw:
        return raw
    # 按 token 切（精准对齐 v1 chunk_size=256）
    # 256 tokens * top_k = top_k 个 chunk 的容量
    max_tokens = 256 * top_k
    tokens = _ENC.encode(raw)
    return _ENC.decode(tokens[:max_tokens])

# MCP 工具调用处理器
@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """MCP 协议入口 - 列出可用工具"""
    tools = []
    for schema in TOOLS_SCHEMA:
        tools.append(Tool(
            name=schema["function"]["name"],
            description=schema["function"]["description"],
            inputSchema=schema["function"]["parameters"],
        ))
    return tools

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """MCP 协议入口 - 路由到 v1 工具"""
    try:
        logger.info(f"调用工具(call_tool): {name} arguments={arguments}")
        
        if name == "search_knowledge":
            # top_k 从模块全局取（CLI 启动时传），LLM 拿不到参数
            result = _search_knowledge_with_topk(
                query=arguments["query"],
                top_k=_GLOBAL_TOP_K,
            )
            return [TextContent(type="text", text=str(result))]
        elif name == "query_ip_reputation":
            result = query_ip_reputation(ip=arguments["ip"])
            # 内部 str() 包 None + 防御
            result_safe = {k: (str(v) if v is not None else None) for k,v in result.items()}
            return [TextContent(type="text", text=json.dumps(result_safe, ensure_ascii=False, default=str))]
        elif name == "parse_log_fields":
            # json.dumps default=str 兜底 IPv4Address
            result = parse_log_fields(log_text=arguments["log_text"])
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
        else:
            return [TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}))]
    except Exception as e:
        logging.error(f"工具 {name} 执行失败: {e}", exc_info=True)
        return [TextContent(type=="text", text=json.dumps({"error": f"工具{name}失败：{str(e)[:200]}"}))]
    
async def main():
    """MCP Server stdio 入口 - 启动时接收 --top-k 透传"""
    parser = argparse.ArgumentParser(description="v2 MCP Server - 暴露 v1 3 tool")
    parser.add_argument(
        "--top-k", type=int, default=3,
        help="search_knowledge top_k 截断（CLI 透传，LLM 拿不到，安全优先）",
    )
    args = parser.parse_args()
    # 模块全局 - handle_call_tool 读这个
    global _GLOBAL_TOP_K
    _GLOBAL_TOP_K = args.top_k
    logging.info(f"MCP Server 启动 - stdio 模式 - top_k={args.top_k}")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )
        
if __name__ == "__main__":
    asyncio.run(main())