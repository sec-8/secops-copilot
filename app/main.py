import os
import json
from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from app.agent import run_agent_stream
# 新增端点：/chat/history 
from app_v2.rag.chain_with_memory import get_session_history
# v2 端点（4 agent 协同 + Memory）
from app_v2.agents.multi_agent import stream as multi_agent_stream
from observability.tracer import tracer 

app = FastAPI(title="SecOps Copilot")


class ExtractReq(BaseModel):
    text: str


class ChatReq(BaseModel):
    text: str
    user_id: str = "anonymous"
    session_id: str = "default" 

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/chat/stream")
def chat_stream(req: ChatReq):
    """SSE 流式聊天，根据用户和对话id区分"""
    def event_generator():
        for event in run_agent_stream(
            req.text,
            user_id=req.user_id,
            session_id=req.session_id,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )

REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379/0")
SESSION_TTL = 24 * 60 * 60  # 24h

@app.get("/chat/history")
async def chat_history(session_id:str, user_id: str):
    """拉短期对话历史（Redis 24h）"""
    history = get_session_history(session_id, user_id)
    # RedisChatMessageHistory.messages 是 BaseMessage 列表
    # 转换：HumanMessage → {role:"user", content:...}, AIMessage → {role:"ai", content:...}
    msgs = []
    for m in history.messages:
        if m.type == "human":
            msgs.append({"role": "user", "content": m.content})
        elif m.type == "ai":
            content = m.content
            tool_calls = []
            # 尝试解析 JSON 工具思考记录
            if content.strip().startswith("{"):
                try:
                    parsed = json.loads(content)
                    if parsed.get("type") == "tool_thought":
                        tool_calls.append({
                            "name": parsed.get("tool"),
                            "args": {"query": parsed.get("query")},
                            "result": parsed.get("result_preview"),
                            "call_id": parsed.get("call_id"),
                        })
                        content = f"[检索] {parsed.get('query')}"
                except json.JSONDecodeError:
                    pass
            msg = {"role": "ai", "content": content}   
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
    return {"messages": msgs, "user_id": user_id, "session_id": session_id}

@app.post("/v2/chat/stream")
def v2_chat_stream(req: ChatReq):
    """
    v2 链流式（4 agent 协同 + 短期 Memory + 长期 Memory + 完整可观测）
    事件类型：thinking_start / tool_call / tool_result / token / final_answer
    每个事件都带 trace_id
    """
    async def event_generator():
        try:
            with tracer.start_trace() as trace:  # 用 with 协议，__exit__ 自动 end
                trace_id = trace.trace_id
                # 关键：multi_agent_stream 是 async generator，必须 async for
                async for event in multi_agent_stream(
                    req.text,
                    user_id=req.user_id,
                    session_id=req.session_id,
                ):
                    event["trace_id"] = trace_id  # 给所有事件加 trace_id
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 异常也 yield 给前端，不直接断流
            err_event = {"type": "error", "content": f"服务端异常: {str(e)[:200]}"}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )
