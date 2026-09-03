"""v2 LLM Wrapper: 包装 v1 router.chat 给 LangChain 用

仅做数据结构转换（LangChain Message <-> V1 Dict）降级逻辑
完全由 v1 router.chat 内部承载
"""
import json 
import asyncio
import uuid
from typing import Any, Dict, List, Optional, Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_function
from langchain_core.tools import BaseTool

from app.llm.factory import get_router

def _align_tool_call_ids(messages: list[BaseMessage]) -> None:
    """统一 tool_call_id（AIMessage 重新生成 UUID，ToolMessage 同步对齐）
    
    问题背景：
    - LangChain AIMessage.tool_calls[].id 是 UUID（lc_xxxx）
    - MCP adapter 给 ToolMessage.tool_call_id = "call_fun"（默认值）
    
    修法：先扫一遍 messages，生成 id map（old_id -> new_uuid），
         然后原地修改 AIMessage / ToolMessage 的 id。
    """
    # 第一遍：建 id map
    id_map: Dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool in msg.tool_calls:
                old_id = tool["id"]
                if old_id not in id_map:
                    id_map[old_id] = f"call_{uuid.uuid4().hex[:12]}"
    # 第二遍：原地改 id
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool in msg.tool_calls:
                old_id = tool["id"]
                if old_id in id_map:
                    tool["id"] = id_map[old_id]
        elif isinstance(msg, ToolMessage):
            old_id = msg.tool_call_id
            if old_id in id_map:
                msg.tool_call_id = id_map[old_id]

def _lc_messages_to_dict(msg: BaseMessage) -> Dict[str, Any]:
    """LangChain BaseMessage -> v1 识别的 dict 格式
    
    LangChain -> V1 入参映射：
    - SystemMessage -> {"role": "system",    "content": ...}
    - HumanMessage  -> {"role": "user",      "content": ...}
    - AIMessage     -> {"role": "assistant", "content": ..., "tool_calls": ...}
    - ToolMessage   -> {"role": "tool",      "tool_call_id": ..., "content": ...}
    """
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content}
    if isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content}
    if isinstance(msg, AIMessage):
        d: Dict[str, Any] = {"role": "assistant", "content": msg.content or ''}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tool["id"],
                    "type": "function",
                    "function":{
                        "name": tool["name"],
                        # arguments 必须是 str JSON，不能是 dict
                        "arguments": json.dumps(tool["args"], ensure_ascii=False),
                    }
                }
                for tool in msg.tool_calls
            ]
        return d
    if isinstance(msg, ToolMessage):
        # MCP adapter 给的 content 是 list[{"type": "text", "text": "..."}]
        # 直接 str() 会变 '[{"type": "text", "text": "..."}]'，Ark 不认
        # 解 list 拿真正的 text 拼成 str
        content = msg.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text_field = item.get("text", "")
                    if text_field:
                        parts.append(text_field)
                elif isinstance(item, str):
                    parts.append(item)
            text = "\n".join(parts) if parts else str(content)
        else:
            text = str(content)
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": text,
        }
    # 兜底：未识别类型按 user 处理
    return {"role": "user", "content": str(msg.content)}

def _result_to_ai_message(result: Dict[str, Any]) -> AIMessage:
    """v1 router.chat 返回值 -> LangChain AIMessage
    
    V1 返回值: result = {"content", "model", "tier", "usage", "tool_calls", "raw"}
    -> LangChain AIMessage:
    - tool_calls 已在 result 顶层（v1 openai_compat.py 已转 list[dict]）
    - tier / usage 透传到 additional_kwargs 便于 tracer 取
    """
    tool_calls = []
    if result.get("tool_calls"):
        for tool in result["tool_calls"]:
            args = tool["arguments"]
            # 修复：str JSON -> dict（Pydantic 验证要求）
            if isinstance(args, str):
                args = json.loads(args)
            tool_calls.append({
                "id": tool["id"],
                "name": tool["name"],
                "args": args,
            })
    return AIMessage(
        content=result.get("content") or "",
        tool_calls=tool_calls,
        additional_kwargs={
            "tier": result.get("tier"),
            "model": result.get("model"),
            "usage": result.get("usage"),
        }
    )
    
class RouterChatModel(BaseChatModel):
    """v1 LLMRouter 的 LangChain 包装器
    
    继承 BaseChatModel 让 create_react_agent 能直接用
    透传 v1 router.chat，不改 v1 降级链路
    """
    # Pydantic 字段（BaseChatModel 抽象要求）
    bound_tools: List[Dict[str, Any]] = [] # v1 TOOLS_SCHEMA 格式
    temperature: float = 0
    
    class Config:
        arbitrary_types_allowed = True
    
    @property
    def _llm_type(self) -> str:
        return "v1_router_wrapper"
    
    def bind_tools(self, tools: List[BaseTool], **kwargs: Any) -> "RouterChatModel":
        """LangChain bind_tools 接口：保存 v1 TOOLS_SCHEMA 格式
        转换链：LangChain BaseTool -> convert_to_openai_function -> 包成 v1 格式
        """
        schemas = []
        for tool in tools:
            # convert_to_openai_function 返内层 function dict
            inner = convert_to_openai_function(tool)
            # 包成 v1 TOOLS_SCHEMA 格式（router.chat 直接用）
            schemas.append({
                "type": "function",
                "function": inner
            })
        # 返回新实例（不可变，符合 LangChain 链式规范）
        return self.__class__(
            bound_tools=schemas,
            temperature=self.temperature
        )
        
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any
    ) -> ChatResult:
        """同步入口 - create_react_agent 走这条
        1. 对齐 tool_call_id
        2. LangChain messages -> v1 dict list
        3. router.chat(messages=v1_msgs, tools=self.bound_tools, temperature=...)
        4. result -> AIMessage
        """
        _align_tool_call_ids(messages)
        messages_v1 = [_lc_messages_to_dict(m) for m in messages]
        router = get_router()
        
        # 透传：tools + temperature + 其他 kwargs
        call_kwargs: Dict[str, Any] = {"temperature": self.temperature}
        if self.bound_tools:
            call_kwargs["tools"] = self.bound_tools
        # stop 参数 v1 router 不直接支持，落到 kwargs 让 OpenAI 透传    
        if stop is not None:
            call_kwargs["stop"] = stop
        call_kwargs.update(kwargs)
        
        result = router.chat(messages=messages_v1, **call_kwargs)
        ai_msg = _result_to_ai_message(result)
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])
    
    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any
    ) -> ChatResult:
        """异步入口 - MCP stdio 走这条（v1 router 无 achat）
            用 asyncio.to_thread 把同步调用包成异步
        """
        _align_tool_call_ids(messages)
        messages_v1 = [_lc_messages_to_dict(m) for m in messages]
        router = get_router()
        
        # 第二轮不传 tools
        has_tool_msg = any(m.get("role") == "tool" for m in messages_v1)
        call_kwargs: Dict[str, Any] = {"temperature": self.temperature}
        if self.bound_tools and not has_tool_msg:
            call_kwargs["tools"] = self.bound_tools
        if stop is not None:
            call_kwargs["stop"] = stop
        call_kwargs.update(kwargs)
        result = await asyncio.to_thread(
            router.chat, messages=messages_v1, **call_kwargs
        )
        ai_msg = _result_to_ai_message(result)
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])
    
    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any
    ) -> Iterator[ChatGeneration]:
        """流式垫片
        调 _generate 拿完整结果，yield 一个包含全部内容的 Chunk
        """
        result = self._generate(messages, stop, run_manager, **kwargs)
        yield result.generations[0]
                