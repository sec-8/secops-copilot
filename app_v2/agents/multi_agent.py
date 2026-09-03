"""Multi-Agent StateGraph - Supervisor + RAG + Tool（agent 协同）"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
import asyncio
import re
import json
import uuid
import logging
from typing import TypedDict, List

from langgraph.graph import StateGraph, START, END
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.output_parsers import JsonOutputParser

from app_v2.agents.supervisor import dispatch, IP_PATTERN, TOOL_KEYWORDS
from app_v2.rag.chain import get_chain, get_retriever, llm, build_prompt, format_docs, safe_parse
from app_v2.rag.chain_with_memory import LongTermMemory, extract_facts, get_session_history
from app_v2.llm.custom_chat_model import RouterChatModel  # 提升到模块顶部，供 RAG stream 调用
from rag.decompose import decompose_query # 问题拆分
from app_v2.rag.json_stripper import JsonEnvelopeStripper # JSON 信封剥离器
# trace
from observability.tracer import tracer

logger = logging.getLogger(__name__)
# MCP Server 启动命令：子进程方式 stdio
# 透传：启动时给 MCP server 一个 --top-k 默认值，动态调整
import os
_DEFAULT_TOP_K = int(os.environ.get("MCP_TOP_K", "3"))
MCP_SERVER_CMD = [
    "uv", "run", "python", "-m", "app_v2.mcp.server",
    "--top-k", str(_DEFAULT_TOP_K),
]

# State 定义 
class MultiAgentState(TypedDict):
    """Multi-Agent 共享 State
    - messages: 消息历史（LangChain Message 列表）
    - next_agent: Supervisor 分派结果
    - final_answer: 最终答案
    - user_id:显式声明（Memory 用）
    """
    messages: List
    next_agent: str
    final_answer: str
    user_id: str
# 节点函数   
# 节点1: Supervisor
async def supervisor_node(state: MultiAgentState) -> dict:
    last_msg = state["messages"][-1]
    question = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    agent = await dispatch(question)
    logger.info(f"Supervisor 分派: {question[:30]} → {agent}")
    return {"next_agent": agent}
# 节点2: RAG Agent    
async def rag_node(state: MultiAgentState) -> dict:
    last_msg = state["messages"][-1]
    question = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    # 直接出答案，看不到过程
    # chain = get_chain() # 懒加载
    # result = await chain.ainvoke(question)
    # final_answer = result.get("answer", "")
    # return {"final_answer": final_answer, "messages": [AIMessage(content=final_answer)]}
    # 展示思考流程版
    # 拆分子问题
    sub_queries = await asyncio.to_thread(decompose_query, question)
    sub_queries = sub_queries[:4] or [question]
    # 逐个检索
    retriever = get_retriever(k=5, multi_query=False)
    all_docs = []
    tool_messages = []
    for q in sub_queries:
        docs = await asyncio.to_thread(retriever.invoke, q)
        all_docs.extend(docs)
        
        contexts = [d.page_content for d in docs]
        preview = "\n".join(f"- {cont[:200]}" for cont in contexts[:3]) if contexts else "无相关文档"
        
        tool_messages.append(AIMessage(content=json.dumps({
            "type": "tool_thought",
            "tool": "search_knowledge",
            "query": q,
            "result_preview": preview,
            "docs_count": len(docs),
        }, ensure_ascii=False)))
    # 生成答案
    context_text = format_docs(all_docs)
    prompt = build_prompt(strict_mode=False)
    messages = prompt.format_messages(context=context_text, question=question)
    response = await asyncio.to_thread(llm.invoke, messages)
    parser = JsonOutputParser()
    parsed = safe_parse(response.content, parser)
    
    answer = parsed.get("answer", "无答案")
    # 返回：思考记录 + 最终答案
    return {
        "final_answer": answer,
        "messages": tool_messages + [AIMessage(content=answer)],
    }
    
# 节点3: Tool Agent
async def tool_node(state: MultiAgentState) -> dict:
    last_msg = state["messages"][-1]
    question = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    # 入口白名单：只有 IP 查询 / 日志解析 才进 tool 路径（防 LLM 误派"今天天气如何"类问题）
    if not (IP_PATTERN.search(question) or any(kw in question for kw in TOOL_KEYWORDS)):
        return {
            "final_answer": "该问题不在工具能力范围内（仅支持 IP 查询与日志解析），已转交知识库处理。",
            "messages": [AIMessage(content="该问题不在工具能力范围内（仅支持 IP 查询与日志解析），已转交知识库处理。")],
        }
    mcp_client = MultiServerMCPClient({
        "secops": {
            "command": MCP_SERVER_CMD[0],
            "args": MCP_SERVER_CMD[1:],
            "transport": "stdio",
        }
    })
    try: 
        async with mcp_client.session("secops") as session:
            tools = await load_mcp_tools(session)
            from langgraph.prebuilt import create_react_agent
            llm = RouterChatModel(temperature=0)
            agent = create_react_agent(llm, tools)
            result = await agent.ainvoke({"messages": [("user", question)]})
            messages = result["messages"]
            # 最后一条 AI 消息作为 final_answer
            final_answer = ""
            # reversed 反向循环
            for m in reversed(messages):
                if type(m).__name__ == "AIMessage" and m.content:
                    final_answer = m.content
                    break
            return {"final_answer": final_answer, "messages": [AIMessage(content=final_answer)]}
    finally:
        # MultiServerMCPClient 需 aclose
        if hasattr(mcp_client, "aclose"):
            await mcp_client.aclose()     

# 抽 memory 共用方法
async def _memory_core(question: str, user_id: str) -> str:
    """Memory agent 核心逻辑（流式 + 同步共用）
    返回 final_answer 字符串
    """
    ltm = LongTermMemory()
    await ltm.connect()
    try:
        namespace = ("user_facts", user_id)
        from app_v2.llm.custom_chat_model import RouterChatModel
        
        if "记住" in question:
            llm = RouterChatModel(temperature=0)
            facts = await extract_facts(question, llm)
            if not facts:
                return "未识别到明确事实，请换种说法试试（如'我叫XX'）。"
            lines = []
            for fact in facts:
                key, value = fact["key"], fact["value"]
                ok = await ltm.put(namespace, key, value)
                # tracer emit event
                tracer.emit_event("ltm_put", {
                    "user_id": user_id,
                    "key": key,
                    "status": "ok" if ok else "fail",
                })
                
                mark = "✅" if ok else "❌"
                lines.append(f"{mark} {key} = {value}")
            return "已记住：\n" + "\n".join(lines)
        else:
            facts = await ltm.get_all(namespace)
            # tracer emit event
            tracer.emit_event("ltm_read", {
                "user_id": user_id,
                "fact_count": len(facts),
            })
            if not facts:
                return f"暂无 {user_id} 的事实记录，先告诉我一些吧（如'记住：我叫XX'）。"
            facts_text = "\n".join(f"- {t['key']}: {t['value']}" for t in facts)
            llm = RouterChatModel(temperature=0)
            response = await llm.ainvoke([
                SystemMessage(content=f"你是助手。基于用户事实回答问题。\n用户事实：\n{facts_text}"),
                HumanMessage(content=question)
            ])
            return response.content
    finally:
        if ltm._pool is not None:
            await ltm._pool.close()

# 节点3: Memory Agent * 2
async def memory_node(state: MultiAgentState) -> dict:
    last_msg = state["messages"][-1]
    question = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    user_id = state.get("user_id", "anonymous")
    try:
        final_answer = await _memory_core(question, user_id)
    except Exception as e:
        logger.error(f"memory_node 异常: {e}")
        final_answer = f"记忆功能暂不可用（PG 未启动）: {str(e)[:80]}"
    return {"final_answer": final_answer, "messages": [AIMessage(content=final_answer)]}
                
# 图构建
def build_graph():
    workflow = StateGraph(MultiAgentState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("rag", rag_node)
    workflow.add_node("tool", tool_node)
    workflow.add_node("memory", memory_node)
    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        lambda s: s["next_agent"],
        {"rag": "rag", "tool": "tool", "memory": "memory"}
    )
    workflow.add_edge("rag", END)
    workflow.add_edge("tool", END)
    workflow.add_edge("memory", END)
    return workflow.compile()

# 入口
# 非流式用回答
async def ask(question: str, user_id: str = "anonymous") -> dict:
    """端到端调用 - supervisor → rag/tool → final_answer"""
    graph = build_graph()
    result = await graph.ainvoke({
        "messages": [("user", question)],
        "user_id": user_id,
        "next_agent": "",
        "final_answer": ""
    })
    return result
#  流式版回答（ask）
# call_id 生成唯一id
def _gen_call_id(prefix: str) -> str:
    """生成唯一 call_id（uuid 方式）"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"
# 打字机
async def _tokenize(content: str):
    """打字机：按标点拆 yield token 事件"""
    chunks = re.split(r'([。，！？；\n])', content)
    for c in chunks:
        if c.strip():
            yield {"type": "token", "content": c}
# Rag 流式 - 展示思考流程
async def _rag_stream(question: str, user_id: str, session_id: str):
    """RAG agent 流式：检索 + LLM + 拒答"""
    try:
        # tracer span
        with tracer.current_trace().span("agent", "RAG Agent") as rag_agent_span:
            rag_agent_span.set_tag("user_id", user_id)
            rag_agent_span.set_tag("session_id", session_id)
            # 检索阶段
            call_id = _gen_call_id("rag")
            # 展示思考流程 ReAct
            # 从 chain 包一切 改成 手动拆问题 → 逐 query 检索发事件 → 合并生成
            # 短期 Memory
            history = get_session_history(session_id, user_id)
            history.add_user_message(question)
            # 子问题拆分
            # 子问题拆分（decompose 走 LLM，实测数秒 → label 必须在它之前发）
            yield {"type": "thinking_start", "iteration": 3, "label": "拆解子问题中"}
            sub_queries = await asyncio.to_thread(decompose_query, question)
            sub_queries = sub_queries[:4] or [question] # 兜底
            # 逐个检索，每步发事件
            retriever = get_retriever(k=5, multi_query=False)
            all_docs = []
            for i, query in enumerate(sub_queries):
                with tracer.current_trace().span("rag", f"search_knowledge - {query[:20]}") as rag_span:
                    rag_span.set_tag("query", query)
                    rag_span.set_tag("iteration", i + 1)
                    yield {"type": "thinking_start", "iteration": i + 4, "label": f"检索子问题 {i + 1}/{len(sub_queries)}: {query[:30]}"}
                    yield {
                        "type": "tool_call", 
                        "name": "search_knowledge", 
                        "args": {"query": query, "call_id": call_id},
                        "call_id": call_id
                    }
                    
                    docs = await asyncio.to_thread(retriever.invoke, query)
                    all_docs.extend(docs)
                    
                    contexts = [d.page_content for d in docs]
                    preview = "\n".join(f"- {cont[:200]}" for cont in contexts[:3]) if contexts else "无相关文档"
                    yield {
                        "type": "tool_result", 
                        "name": "search_knowledge", 
                        "result": preview, 
                        "call_id": call_id
                    }
                    # 思考过程写入短期 Redis memory（JSON 格式）
                    history.add_ai_message(json.dumps({
                        "type": "tool_thought",
                        "call_id": call_id,
                        "tool": "search_knowledge",
                        "query": query,
                        "result_preview": preview,
                        "docs_count": len(docs),
                    }, ensure_ascii=False))
            # 合并上下文生成答案
            context_text = format_docs(all_docs)
            prompt = build_prompt(strict_mode=False) # multi_query 用宽松模式
            messages = prompt.format_messages(context=context_text, question=question)
            # 细化状态：LLM 生成是最大空窗（解决前的过渡），先让用户知道在生成
            yield {"type": "thinking_start", "iteration": len(sub_queries) + 4, "label": f"整合 {len(all_docs)} 篇资料，生成答案中"}
            with tracer.current_trace().span("llm", "RAG 生成") as llm_span:
                # trace span 写 messages  
                llm_span.set_tag("context_docs", len(all_docs))
                llm_span.set_tag("sub_queries", len(sub_queries))
                llm_span.set_input(messages)
                # 真流式尝试
                stripper = JsonEnvelopeStripper()
                emitted = []              # 本次真流式已发射的 token（收尾写 memory / 兜底判断用）
                stream_ok = False         # closed 或 refuse 才算成功
                stream_exc = None         # 记录异常，便于终态 log
                answer = None             # 最终答案
                has_answer = True         # 是否有答案，默认 True
                
                try:
                    async for chunk in llm.astream(messages):
                        pieces = stripper.feed(chunk.content)
                        for p in pieces:
                            emitted.append(p)
                            yield {"type": "token", "content": p}
                        # 三个终止信号在一个 chunk 处理完后统一检查
                        if stripper.verdict == "refuse":
                            has_answer = False
                            answer = "知识库未收录相关内容，无法回答这个问题"
                            async for ev in _tokenize(answer):
                                yield ev
                            stream_ok = True  # refuse 也是合法结局，不该走兜底
                            break
                        if stripper.verdict == "fallback":
                            # S0 阶段零发射就 fallback，留给 try 外走老路径
                            break
                        if stripper.closed:
                            answer = ''.join(emitted)
                            stream_ok = True
                            break
                except Exception as e:
                    stream_exc = e
                    logger.error(f"rag_node 流式异常: {e}")
                
                # try 外判断结局：引入的三个 case（正常闭 / refuse / 截断或异常）都收敛到这里
                if not stream_ok:
                    if emitted:
                        # 异常 / 截断但已发过半 → 将就收尾，避免重复渲染
                        answer = ''.join(emitted)
                        has_answer = True
                        logger.error(f"rag_node 截断降级: 返回已发射的 {len(emitted)} 字符（stream_exc={stream_exc}）")
                    else:
                        # S0 fallback / 流式极早期异常 → 走老路径（带 4 tier 降级）
                        response = await asyncio.to_thread(llm.invoke, messages)
                        parsed = safe_parse(response.content, JsonOutputParser())
                        answer = parsed.get("answer", "无答案")
                        has_answer = parsed.get("has_answer", True)
                        async for ev in _tokenize(answer):
                            yield ev
                # trace span 写 answer            
                llm_span.set_output(answer)
                llm_span.set_tag("has_answer", has_answer)
                # 拿不到 tier 是流式限制，所以没有tier
                # llm_span.set_tag("tier", ???)
                
            # 短期 memory 写回答
            history.add_ai_message(answer)
            # 长期 memory 抽事实
            ltm = LongTermMemory()
            await ltm.connect()
            try: 
                memory_llm = RouterChatModel(temperature=0)
                extracted = await extract_facts(question, memory_llm)
                # trace emit fact count
                tracer.emit_event("memory_write", {
                    "user_id": user_id,
                    "fact_count": len(extracted),
                    "namespace": "user_facts",
                })
                namespace = ("user_facts", user_id)
                for fact in extracted:
                    await ltm.put(namespace, fact["key"], fact["value"])
            finally:
                if ltm._pool is not None:
                    await ltm._pool.close() 
            # final_answer
            citations = list(dict.fromkeys([doc.metadata.get("source", "?") for doc in all_docs]))
            yield {
                "type": "final_answer",
                "content": answer,
                "call_id": call_id,
                "citations": citations,
                "has_answer": has_answer,
            } 
    except Exception as e:
        logger.error(f"_rag_stream 异常: {e}")
        yield {"type": "error", "content": f"RAG 处理失败: {str(e)[:200]}"}    
    # 直答模式， 不展示思考过程
    # yield {
    #     "type": "tool_call", 
    #     "name": "search_knowledge", 
    #     "args": {"query": question, "call_id": call_id}
    # }
    # # 短期 Memory
    # history = get_session_history(session_id, user_id)
    # history.add_user_message(question)
    # # LCEL chain 同步调（ainvoke 不稳定，用 invoke + asyncio.to_thread）
    # try:
    #     # 懒加载 chain
    #     chain = get_chain()
    #     result = await asyncio.to_thread(chain.invoke, question) # await chain.ainvoke(question) 异步调更好？
    # except Exception as e:
    #     yield {"type": "final_answer", "content": f"链调用失败：{e}", "call_id": call_id}
    #     return
    
    # # 解析结果
    # answer = result.get("answer", "无答案")
    # has_answer = result.get("has_answer", False)
    # citations = result.get("citations", [])
    # contexts = result.get("contexts", [])
    # # 工具结果 上下文
    # ctx_preview = "\n".join(f"- {cont[:200]}" for cont in contexts[:3]) if contexts else "无相关文档"
    # yield {
    #     "type": "tool_result", 
    #     "name": "search_knowledge", 
    #     "result": ctx_preview, 
    #     "call_id": call_id
    # }
    # # 打字机：按标点拆（LCEL chain 不 yield token，手动拆）
    # async for ev in _tokenize(answer):
    #     yield ev
    
    # # 写回短期 Memory
    # history.add_ai_message(answer)
    # # 抽事实 + 写长期 Memory
    # ltm = LongTermMemory()
    # await ltm.connect()
    # try: 
    #     llm = RouterChatModel(temperature=0)
    #     extracted = await extract_facts(question, llm)
    #     namespace = ("user_facts", user_id)
    #     for fact in extracted:
    #         await ltm.put(namespace, fact["key"], fact["value"])
    # finally:
    #     if ltm._pool is not None:
    #         await ltm._pool.close() 
    # # final_answer
    # yield {
    #     "type": "final_answer",
    #     "content": answer,
    #     "call_id": call_id,
    #     "citations": citations,
    #     "has_answer": has_answer,
    # }      
   
# Tool 流式
async def _tool_stream(question: str, user_id: str, session_id: str):
    """Tool agent 流式：MCP 3 tool 调用"""
    call_id = _gen_call_id("tool")
    # tracer span to tool agent
    with tracer.current_trace().span("agent", "Tool Agent") as tool_agent_span:
        tool_agent_span.set_tag("user_id", user_id)
        tool_agent_span.set_tag("session_id", session_id)
        
        # 入口白名单（防 LLM 误派"今天天气如何"等不相关问题）
        if not (IP_PATTERN.search(question) or any(kw in question for kw in TOOL_KEYWORDS)):
            content = "该问题不在工具能力范围内（仅支持 IP 查询与日志解析），已转交知识库处理。"
            history = get_session_history(session_id, user_id)
            history.add_user_message(question)
            history.add_ai_message(content)
            async for ev in _tokenize(content):
                yield ev
            yield {"type": "final_answer", "content": content, "call_id": call_id}
            return
        # 短期 memory：写用户问题（避免 tool/memory 流不写 Redis 导致刷新丢）
        history = get_session_history(session_id, user_id)
        history.add_user_message(question)
        
        mcp_client = MultiServerMCPClient({
            "secops": {
                "command": MCP_SERVER_CMD[0],
                "args": MCP_SERVER_CMD[1:],
                "transport": "stdio",
            }
        })
        try:
            async with mcp_client.session("secops") as session:
                tools = await load_mcp_tools(session)
                tool_map = {t.name: t for t in tools}
                # Supervisor 思想：按问题关键词选 tool
                tool = None
                tool_args = {}
                tool_name = ""
                if "解析" in question or "parse" in question.lower():
                    tool = tool_map.get("parse_log_fields")
                    tool_name = "parse_log_fields"
                    # 提日志字符串
                    # log_match = re.search(r"\[.*?\]|\{.*?\}|GET|POST", question)
                    tool_args = {"log_text": question}
                    
                elif re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", question):
                    tool = tool_map.get("query_ip_reputation")
                    tool_name = "query_ip_reputation"
                    ip_match = re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", question)
                    tool_args = {"ip": ip_match.group(0)}
                
                else:
                    # search_knowledge 兜底
                    tool = tool_map.get("search_knowledge")
                    tool_name = "search_knowledge"
                    tool_args = {"query": question}
                    
                if not tool:
                    yield {
                        "type": "final_answer", 
                        "content": f"工具 {tool_name} 不可用", 
                        "call_id": call_id
                    }
                    return
                # 完整tool调用流程 #
                # 1. tool_call 事件
                yield {
                    "type": "tool_call",
                    "name": tool_name,
                    "args": tool_args,
                    "call_id": call_id,
                }
                # 2. 调 tool 加 tracer span
                with tracer.current_trace().span("tool", tool_name) as tool_span:
                    tool_span.set_tag("tool_name", tool_name)
                    tool_span.set_tag("args", tool_args)
                    tool_span.set_tag("call_id", call_id)
                    
                    result = await tool.ainvoke(tool_args)
                    
                    tool_span.set_tag("status", "success" if result else "empty")
                    
                    result_str = str(result)[:500] if result else "无结果"
                    
                    # 3. tool_result 事件
                    yield {
                        "type": "tool_result",
                        "name": tool_name,
                        "result": result_str,
                        "call_id": call_id,
                    }
                
                # 4. 打字机：按标点拆
                content = f"## {tool_name} 结果\n\n{result_str}"
                # 短期 memory：写 AI 回答
                history.add_ai_message(content)
                async for ev in _tokenize(content):
                    yield ev
                    
                # 5. final_answer
                yield {
                    "type": "final_answer",
                    "content": content,
                    "call_id": call_id,
                }
        finally:
            if hasattr(mcp_client, "aclose"):
                await mcp_client.aclose()
  
# Memory 流式
async def _memory_stream(question: str, user_id: str, session_id: str):
    """Memory agent 流式：长期记忆读/写"""
    call_id = _gen_call_id("memory")
    # tracer span to memory agent
    with tracer.current_trace().span("agent", "Memory Agent") as memory_agent_span:
        memory_agent_span.set_tag("user_id", user_id)
        memory_agent_span.set_tag("op_type", "write" if "记住" in question else "read")
        
        # 短期 memory：写用户问题
        history = get_session_history(session_id, user_id)
        history.add_user_message(question)
        try:
            content = await _memory_core(question, user_id)
        except Exception as e:
            content = f"记忆功能异常：{str(e)[:100]}"   
        # 短期 memory：写 AI 回答
        history.add_ai_message(content)
        # 打字机
        async for ev in _tokenize(content):
            yield ev
            
        yield {
            "type": "final_answer",
            "content": content,
            "call_id": call_id,
        }   
    
async def stream(question: str, user_id: str, session_id: str):
    """
    流式版 ask() - yield 4 类事件 + token
    事件：thinking_start / tool_call / tool_result / token / final_answer
    """
    # tracer.current_trace()是同步方法，如果在多用户并发下，可能会出现 trace_id 被覆盖的情况
    # 所以真实多用户场景需要 在这里使用 async with 来确保每个请求都有独立的 span
    with tracer.current_trace().span("agent", "Multi-Agent 协同") as agent_span:
        agent_span.set_tag("user_id", user_id)
        agent_span.set_tag("session_id", session_id)
        agent_span.set_tag("question_preview", question[:100])
        # 第 1 轮 思考中
        # 细化状态：supervisor 决策可能走 LLM slow path（实测数秒），label 让 StatusBar 有内容可显
        yield {"type": "thinking_start", "iteration": 1, "label": "Supervisor 分派中"}
        # Supervisor 决策
        agent = await dispatch(question)
        yield {"type": "thinking_start", "iteration": 2, "label": f"分派至 {agent} agent"}
        # 根据 supervisor 决策，调对应 agent 流式
        if agent == "rag":
            async for event in _rag_stream(question, user_id, session_id):
                yield event
        elif agent == "tool":
            async for event in _tool_stream(question, user_id, session_id):
                yield event
        elif agent == "memory":
            async for event in _memory_stream(question, user_id, session_id):
                yield event

if __name__ == "__main__":
    async def test_stream():
        print("=" * 60)
        print("测试流式: XSS 和 SQL 注入防御有什么区别")
        print("=" * 60)
        async for ev in stream("XSS 和 SQL 注入防御有什么区别", user_id="test_user", session_id="test_session"):
            print(f"  {ev['type']}: {str(ev)[:200]}")
    
    asyncio.run(test_stream())