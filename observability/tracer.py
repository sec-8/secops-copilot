"""
轻量 Trace 系统骨架（W5.5 线 3）

设计：
- contextvars 隐式传递 trace（非侵入，并发安全）
- with 协议管理 Span 生命周期（退出自动关 span）
- 落盘：logs/trace.jsonl（每行一个事件，W6 切换 Langfuse）

你要实现：
  1. Span 类：start() / end() / set_tag() / __enter__ / __exit__
  2. Trace 类：__enter__ / __exit__ / span()
  3. Tracer 类：start_trace() / current_trace() / emit_event()
  4. tracer 单例（模块底部）
  5. JSONL 落盘：_write_line() 线程安全追加一行

埋点事件类型：
  trace_start   — 启动 trace，生成 trace_id
  span_start    — 进入 span（LLM 调用 / tool_call / RAG 检索）
  span_end      — 退出 span（记录 end_time + tags）
  event         — 关键事件（注入拦截 / 降级 / 拒答）

span 类型约定：
  "agent"       — ReAct 主循环总 span
  "llm"         — 模型调用（model / prompt_tokens / completion_tokens）
  "tool"        — 工具调用（tool_name / args / latency / status）
  "rag"         — RAG 检索（query / top_k / retrieved_count）

参考库：OpenTelemetry Span 设计（start/end/tags/parent_id）
"""

import json
import os  # W6 Day1: 读 Langfuse 开关环境变量
import time
import uuid
import contextvars  # 保留 import 仅为说明历史演进（list stack 模式后不再使用）
from pathlib import Path
from typing import Optional, Any
from threading import Lock
# 不在顶部 import langfuse_sink（防 langfuse 装不上时 tracer 都加载不了）
# 所有 sink 调用都走函数内 import（_is_langfuse_enabled / _write_line 里）

# ===== trace 上下文（list stack 模式，W6 Day2 步骤3 改造）=====
# 原设计：contextvars —— 跨 SSE 流式 generator 推进时会跨 worker thread，
# anyio 调用 token.reset() 抛 'Token was created in a different Context'。
# 新设计：list stack + 模块级锁 —— W6 是单线程场景，list 的 push/pop 行为
# 跟 contextvar 的 set/reset 等价（后进先出），但不受 Context 限制。
# 调用方零改动：tracer.current_trace() / trace.span() / emit_event() 接口不变。
_trace_stack: list = []     # 当前活跃的 Trace（栈顶为最新）
_span_stack: list = []      # 当前活跃的 Span（栈顶为最新）
_STACK_LOCK = Lock()        # push/pop 的互发锁（防御未来多线程）

# ===== 落盘路径 =====
PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "trace.jsonl" # 统一写入一个文件

class Span:
    """一次操作（LLM调用/工具调用/RAG检索）的 trace 结点

    要求：
    - start(trace_id) → 写 span_start 到 JSONL，记 _start_time
    - end() → 写 span_end 到 JSONL（latency = end - start，tags 一并写出），防重复关闭
    - set_tag(key, value) → 存到 self.tags dict，返回 self（支持链式调用）
    - __enter__ → 从 _current_trace 取 active trace，调 start()，推入 _current_span
    - __exit__ → 调 end()，恢复 _current_span 到上一个（用 contextvars Token）

    提示：
    - span_id 用 uuid4().hex[:12]
    - _entered 防重复进入
    - JSONL 写盘用 _write_line（下面已定义）
    """
    def __init__(self, name:str = None, parent_id: str = None, span_type: str = None):
        self.span_id = uuid.uuid4().hex[:12] # 12位十六进制 ID
        self.tags = {}                      # 用户标签
        self._name = name
        self.parent_id = parent_id
        self.span_type = span_type 
        self._start_time:float = None
        self._ended: bool = False           # 防重复 end()
        self._entered:bool = False          # 防重复进入上下文
        self.input = None                   # 专给 LLM prompt 用
        self.output = None                  # 专给 LLM completion 用
        
    def start(self, trace_id:str) -> "Span":
        """记录 span 开始，写入 span_start 事件（W6 Day1: 加 parent_id）"""
        if self._start_time is not None:
            raise RuntimeError(f"Span {self.span_id} 已经开始")
        self._start_time = time.time()
        _write_line({
            "type": "span_start",
            "trace_id": trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,   # W6 Day1: sink 重建嵌套需要
            "name": self._name,
            "span_type": self.span_type,    # W6 Day1: 顺便记 type（之前没写盘）
            "timestamp": self._start_time
        })
        return self
    
    def end(self) -> None:
        """结束 span，写入 span_end 事件（含标签和延迟）（W6 Day1: 加 parent_id）"""
        if self._ended:
            return
        if self._start_time is None:
            raise RuntimeError(f"Span {self.span_id} 还未开始")
        self._ended = True
        trace = _trace_stack[-1] if _trace_stack else None
        trace_id = getattr(trace, "trace_id", None)
        latency_ms = (time.time() - self._start_time) * 1000
        _write_line({
            "type": "span_end",
            "span_id": self.span_id,
            "trace_id": trace_id,
            "parent_id": self.parent_id,   # W6 Day1: sink 重建嵌套需要
            "span_type": self.span_type,    # W6 Day1: 同步
            "latency_ms": latency_ms,
            "tags": self.tags.copy(),
            "input": self.input,      # W6 Day2: 加这行（你加）
            "output": self.output,    # W6 Day2: 加这行（你加）
            "timestamp": time.time()
        })
    
    def set_tag(self, key: str, value) -> "Span":
        """链式设置标签"""
        self.tags[key] = value
        return self
    
    def set_input(self, value) -> "Span":
        """W6 Day2: 写 Langfuse input 字段（专给 prompt 用）"""
        """设置 span 的 input（W6 Day2: 长 input 自动截断到 10K 字符）"""
        s = str(value)
        if len(s) > 10000:
            value = s[:10000] + "...[truncated]"
        self.input = value
        return self
            
    def set_output(self, value) -> "Span":
        """W6 Day2: 写 Langfuse output 字段（专给 completion 用）"""  
        self.output = value
        return self 
    
    # ---------- 上下文管理器 ----------
    def __enter__(self) -> 'Span':
        if self._entered:
            raise RuntimeError(f"Span {self.span_id} 已经进入")
        self._entered = True

        trace = _trace_stack[-1] if _trace_stack else None
        if trace is None:
            raise RuntimeError("trace功能未被启用")
        trace_id = getattr(trace, "trace_id", None)
        if trace_id is None:
            raise RuntimeError("Active trace has no trace_id")

        self.start(trace_id)
        with _STACK_LOCK:
            _span_stack.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.end()
        with _STACK_LOCK:
            if _span_stack and _span_stack[-1] is self:
                _span_stack.pop()
            # 重复 __exit__ / 异常路径下不报错，堆栈保持一致
            
               
class Trace:
    """一条完整请求链路的上下文容器

    要求：
    - __init__ 生成 trace_id（uuid4().hex[:16]）
    - __enter__ → 写 trace_start 到 JSONL，推入 _current_trace
    - __exit__ → 写 trace_end 到 JSONL（total_latency_ms），重置 _current_trace 为 None
    - span(span_type, name) → 创建 Span，parent_id 从 _current_span.get() 取
    """
    def __init__(self):
        self.trace_id = uuid.uuid4().hex[:16]
        self._start_time = None
        
    def __enter__(self) -> 'Trace':
        """启动 trace，写入 trace_start 并设为当前 trace"""
        self._start_time = time.time()
        _write_line({
            "type": "trace_start",
            "trace_id": self.trace_id,
            "timestamp": self._start_time
        })
        with _STACK_LOCK:
            _trace_stack.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """结束 trace，写入 trace_end（含总耗时毫秒）"""
        if self._start_time is None:
            return # 未启动则忽略
        total_latency_ms = (time.time() - self._start_time) * 1000
        _write_line({
            "type": "trace_end",
            "trace_id": self.trace_id,
            "total_latency_ms": total_latency_ms,
            "timestamp": time.time()
        })
        with _STACK_LOCK:
            if _trace_stack and _trace_stack[-1] is self:
                _trace_stack.pop()

    def span(self, span_type: str, name: str = None) -> Span:
        """
        创建一个新的 Span，父级为当前活跃的 span（若有）。
        span_type 可作为标签保存，也可用于日志标识。
        """
        parent_id = None
        with _STACK_LOCK:
            current = _span_stack[-1] if _span_stack else None
        if current is not None:
            parent_id = current.span_id
        return Span(name=name, parent_id=parent_id, span_type=span_type)
        
class Tracer:
    """外部唯一入口

    要求：
    - start_trace() → 返回 Trace 实例（别在这调 __enter__，让调用方 with 就行）
    - current_trace() → 返回 _current_trace.get()
    - emit_event(event_type, tags) → 往 JSONL 写一条 event（不创建 span）
        格式：{"event":"event","event_type":...,"trace_id":...,"span_id":...,"timestamp":...,"tags":...}
    """
    def start_trace(self) -> Trace:
        """
        创建并返回一个新的 Trace 实例。
        注意：返回的 Trace 尚未进入上下文，调用者需使用 with 语句启动。
        """
        return Trace()

    def current_trace(self):
        """获取当前活跃的 Trace 对象，若无则返回 None"""
        with _STACK_LOCK:
            return _trace_stack[-1] if _trace_stack else None

    def emit_event(self, event_type: str, tags: dict) -> None:
        """
        在当前 trace/span 上下文中写入一条自定义事件（不创建 span）。
        若没有活跃 trace，则忽略（或可抛出异常，根据业务决定）。
        """
        with _STACK_LOCK:
            trace = _trace_stack[-1] if _trace_stack else None
        if trace is None:
            # 无活跃 trace，可以选择忽略或记录警告；这里直接返回（不写入）
            return
        trace_id = trace.trace_id
        with _STACK_LOCK:
            span = _span_stack[-1] if _span_stack else None
        span_id = span.span_id if span else None

        _write_line({
            "event": "event",
            "event_type": event_type,
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp": time.time(),
            "tags": tags.copy()
        })
        
# ===== W6 Day1: Langfuse sink 开关 =====
# 不在这里直接 import sink（防循环依赖 + 让 sink 失败不破 tracer）
def _is_langfuse_enabled() -> bool:
    """查 Langfuse sink 是否可用：环境变量 + import 成功 + 三个 key 都填了"""
    if os.getenv("LANGFUSE_SINK_ENABLED", "false").lower() != "true":
        return False
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return False
    try:
        from observability.langfuse_sink import is_enabled
        return is_enabled()
    except Exception:
        return False


# ===== JSONL 落盘 =====
_WRITE_LOCK = Lock()


def _write_line(data: dict) -> None:
    """线程安全地追加一行 JSON 到 trace.jsonl
    W6 Day1 改造：先尝试写 Langfuse（不破 JSONL），失败/未启用走双写
    """
    # 1) 尝试写 Langfuse（失败降级，tracer 仍能写 JSONL）
    if _is_langfuse_enabled():
        try:
            from observability.langfuse_sink import write as _langfuse_write
            _langfuse_write(data)
        except Exception as e:
            # 不递归 emit_event（那会再次调 _write_line）—— 直接 print 告警
            print(f"[TRACER] Langfuse 写入失败: {e}，降级 JSONL")
    
    # 2) 始终写 JSONL（双写：本地有数据 + Langfuse 出问题不丢 trace）
    LOG_DIR.mkdir(exist_ok=True) # 确保目录存在
    line = json.dumps(data, ensure_ascii=False, default=str) + "\n"
    with _WRITE_LOCK: # 锁保护文件写入
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)

# ===== 模块级单例 =====
tracer = Tracer()
