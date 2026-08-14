"""
Langfuse v4 sink
设计：
- 收 tracer event dict，按 type 分发到 Langfuse SDK
- 内部维护 _active_observations: dict[span_id, obs_obj] 映射嵌套关系
- Langfuse 写入失败 → emit_event 记录 + 写 JSONL
- Langfuse 未启用或 import 失败 → 直接 return（tracer.py 仍写 JSONL）
"""
from app.config import settings

# === 懒加载 client ===
_client = None
_active_obs: dict[str, object] = {} # span_id -> Langfuse observation obj

def _get_client():
    """懒加载 Langfuse v4 client"""
    global _client
    if _client is not None:
        return _client
    if not settings.LANGFUSE_PUBLIC_KEY:
        return None
    try:
        from langfuse import get_client
        _client = get_client()
        return _client
    except Exception:
        return None
    
# === 映射表（和导出jsonl的字段对应）===
_TYPE_MAP = {
    "agent": "agent",
    "llm": "generation",
    "tool": "tool",
    "rag": "retriever",
    "event": "event",  # emit_event 用
}

def _on_trace_start(event: dict) -> None:
    """trace_start: 开一个顶层 observation（不传 as_type 即为默认 trace）"""
    client = _get_client()
    if client is None:
        return
    obs = client.start_observation(
        as_type="span", # trace 顶层用 span
        name=f"trace-{event['trace_id'][:8]}",
    )
    _active_obs[event["trace_id"]] = obs  # 用 trace_id 当 key
    
    
def _on_span_start(event: dict) -> None:
    """span_start: 开一个嵌套 observation
    关键：用 parent_id 找父 observation，链起来
    """
    client = _get_client()
    if client is None:
        return
    
    span_id = event["span_id"]
    parent_id = event["parent_id"]
    trace_id = event["trace_id"]
    as_type = _TYPE_MAP.get(event.get("span_type", "span"), "span") 
    name = event.get("name", f"span-{span_id[:8]}")
    
    parent_obs = None
    if parent_id and parent_id in _active_obs:
        parent_obs = _active_obs[parent_id]
    elif trace_id in _active_obs:
        parent_obs = _active_obs[trace_id]
    else:
        return
        
    obs = parent_obs.start_observation(
        as_type=as_type,
        name=name,
    )
    # 存储当前 span 以便子级查找
    _active_obs[event["span_id"]] = obs


def _on_span_end(event: dict) -> None:
    """span_end: 关 observation + 写 tags/latency"""
    obs = _active_obs.pop(event["span_id"], None)
    if obs is None:
        return
    # 拍平 tags 到 metadata 顶层
    tags = event.get("tags", {})
    obs.update(metadata={
        **tags,
        "latency_ms": event.get("latency_ms")} # 用 tracer.py 算好的
    )
    update_kwargs = {}
    if event.get("input") is not None:
        update_kwargs["input"] = event.get("input") 
    if event.get("output") is not None:     
        update_kwargs["output"] = event.get("output") 
        
    obs.update(**update_kwargs)
    obs.end()


def _on_event(event: dict) -> None:
    """emit_event: 用 v4 的 event 类型"""
    client = _get_client()
    if client is None:
        return
    parent_obs = _active_obs.get(event.get("span_id")) or _active_obs.get(event["trace_id"])
    if parent_obs is None:
        return
    # start_observation（不是 as_current）→ 不开新顶层，挂在父级下
    obs = parent_obs.start_observation(
        as_type="event",
        name=event.get("event_type", "event"),
    )
    # obs.update(metadata={"tags": event.get("tags", {})}) （v4 event 类型不允许 update after creation）
    obs.end()


def _on_trace_end(event: dict) -> None:
    """trace_end: 关顶层 trace + flush"""
    obs = _active_obs.pop(event["trace_id"], None)
    if obs is not None:
        obs.update(metadata={"total_latency_ms": event.get("total_latency_ms")})
        obs.end()
        for k,v in list(_active_obs.items()):
            if k != event["trace_id"]:
                v.end()
                
    client = _get_client()
    if client is not None:
        client.flush()  # 短应用必加


def write(event: dict) -> None:
    """sink 主入口"""
    client = _get_client()
    if client is None:
        return  # 走 JSONL fallback
    
    try:
        event_type = event.get("type") or event.get("event")
        if event_type == "trace_start":
            _on_trace_start(event)
        elif event_type == "span_start":
            _on_span_start(event)
        elif event_type == "span_end":
            _on_span_end(event)
        elif event_type == "event":
            _on_event(event)
        elif event_type == "trace_end":
            _on_trace_end(event)
    except Exception as e:
        # Langfuse 失败 → 记事件（tracer.py 会写 JSONL）
        # 注意：不要在 except 里调 emit_event，会递归
        print(f"[LANGFUSE_SINK] 写入失败: {e}，已降级到 JSONL")



    
# === 给 tracer.py 用的开关查询 ===
def is_enabled() -> bool:
    """tracer.py 调用前先查这个"""
    return _get_client() is not None        