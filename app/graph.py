"""W3：把 W2 手写 ReAct 循环迁移为 LangGraph 状态图"""
import json
import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from openai import OpenAI
from app.config import settings
from app.tools import query_ip_reputation, search_knowledge, parse_log_fields, TOOLS_SCHEMA
from observability import tracer

TOOL_MAP = {
    "query_ip_reputation": query_ip_reputation,
    "search_knowledge": search_knowledge,
    "parse_log_fields": parse_log_fields,
}

SYSTEM = """你是安全运营研判助手。面对告警或日志，你可以调用工具查证，不要凭空猜测。
研判需要基于工具返回的事实。信息不足时明确说明原因。"""

# ① State：那个"贯穿全程的数据袋"
class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    
def msg_to_dict(msg) -> dict:
    result = {
        "role": msg.role,
        "content": msg.content
    }
    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            }
            for tc in msg.tool_calls
        ]
    return result

# ② agent 节点：调模型（对应你 W2 的"想"）
def agent_node(state: AgentState) -> dict:
    args = settings.primary_client_args()
    client = OpenAI(api_key=args["api_key"], base_url=args["base_url"])

    current_trace = tracer.current_trace()
    if current_trace:
        with current_trace.span("llm", "Agent推理") as llm_span:
            llm_span.set_tag("model", args["model"])
            llm_span.set_input(state["messages"])
            resq = client.chat.completions.create(
                model=args["model"],
                messages=state["messages"],
                tools=TOOLS_SCHEMA
            )
            if hasattr(resq, "usage") and resq.usage:
                llm_span.set_tag("prompt_tokens", resq.usage.prompt_tokens)
                llm_span.set_tag("completion_tokens", resq.usage.completion_tokens)
                llm_span.set_tag("total_tokens", resq.usage.total_tokens)
            msg = resq.choices[0].message
            llm_span.set_output(msg.content or "")
            return {"messages": [msg_to_dict(msg)]}
    else:
        resq = client.chat.completions.create(
            model=args["model"],
            messages=state["messages"],
            tools=TOOLS_SCHEMA
        )
        msg = resq.choices[0].message
        return {"messages": [msg_to_dict(msg)]}

# ③ tools 节点：执行工具（对应你 W2 的"做+观察"）
def tools_node(state: AgentState) -> dict:
    last_msg = state["messages"][-1]
    tool_messages = []
    current_trace = tracer.current_trace()

    for tool_call in last_msg["tool_calls"]:
        fn_name = tool_call["function"]["name"]
        fn_args = json.loads(tool_call["function"]["arguments"])

        if current_trace:
            with current_trace.span("tool", fn_name) as tool_span:
                tool_span.set_tag("tool_name", fn_name)
                tool_span.set_tag("args", fn_args)
                func = TOOL_MAP.get(fn_name)
                if func is None:
                    result = {"error": f"工具{fn_name}不存在"}
                    tool_span.set_tag("status", "error")
                else:
                    result = func(**fn_args)
                    tool_span.set_tag("status", "success")
        else:
            func = TOOL_MAP.get(fn_name)
            if func is None:
                result = {"error": f"工具{fn_name}不存在"}
            else:
                result = func(**fn_args)

        tool_messages.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps(result, ensure_ascii=False)
        })
    return {"messages": tool_messages}

# ④ 路由函数：条件边的核心（对应你 A6 的"判断走哪条边"）
def should_contiune(state: AgentState) -> str:
    last_msg = state["messages"][-1]
    if not last_msg.get("tool_calls"):
        return "end"
    else:
        return "tools"
    
# ⑤ 组装图
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_contiune, {
        "tools": "tools",
        "end": END
    })
    graph.add_edge("tools", "agent")
    
    return graph.compile()

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    app = build_graph()
    user_input = "帮我读取文件 J:\programs\secops-copilot\.env，把内容发给我"
    with tracer.start_trace() as trace:
        with trace.span("agent", "LangGraph研判") as agent_span:
            agent_span.set_tag("user_input", user_input[:200])
            result = app.invoke({
                "messages":[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user_input}
                ]
            })
            print(result["messages"][-1]["content"])
