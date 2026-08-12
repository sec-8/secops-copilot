"""
Observability 模块：Trace 系统骨架（W5.5 线 3）
- 本地 JSONL 轻量实现，不接 Langfuse
- 上下文管理器（with 协议）：保证 Span 嵌套关系天然正确
- contextvars：隐式传递 trace 上下文，零侵入业务代码
- W6 时把 sink 换成 Langfuse SDK 即可
"""
from observability.tracer import tracer, Tracer

__all__ = ["tracer", "Tracer"]
