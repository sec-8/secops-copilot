"""
v2 trace 嵌套基线探针
目标：调用 v2 端点 → 数 trace.jsonl 里 span_start/span_end 数量 → 验证 BUG 现状
预期：修复前只 2 条（trace_start + trace_end），修复后 >10 条
用法：uv run python scripts/probe_v2_trace.py
"""
import json
import time
import requests
from pathlib import Path

LOG_FILE = Path(__file__).parent.parent / "logs" / "trace.jsonl"
V2_URL = "http://localhost:8000/v2/chat/stream"

def count_trace_events(trace_id_prefix=None):
    """数 jsonl 里某 trace_id 的 span_start / span_end 条数"""
    if not LOG_FILE.exists():
        return 0, []
    counts = {"trace_start": 0, "span_start": 0, "span_end": 0, "trace_end": 0, "event": 0}
    matched_events = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type") or ev.get("event")
            if t in counts:
                if trace_id_prefix and not ev.get("trace_id", "").startswith(trace_id_prefix):
                    continue
                counts[t] += 1
                matched_events.append((t, ev.get("name") or ev.get("span_type", ""), ev.get("trace_id", "")[:8]))
    return counts, matched_events

def probe(question: str = "XSS 防御"):
    print(f"[PROBE] 问题: {question}")
    print(f"[PROBE] 调用 {V2_URL}")
    # 记下 log 行数，对比前后增量
    before = sum(1 for _ in open(LOG_FILE, encoding="utf-8")) if LOG_FILE.exists() else 0
    t0 = time.time()
    r = requests.post(V2_URL, json={"text": question, "user_id": "probe", "session_id": "probe"}, stream=True, timeout=120)
    trace_id = None
    for line in r.iter_lines():
        if not line:
            continue
        s = line.decode("utf-8")
        if s.startswith("data: "):
            try:
                ev = json.loads(s[6:])
                if ev.get("type") == "thinking_start" and not trace_id:
                    trace_id = ev.get("trace_id", "")
                    print(f"[PROBE] trace_id: {trace_id[:16]}")
            except json.JSONDecodeError:
                pass
    elapsed = time.time() - t0
    print(f"[PROBE] 流式耗时: {elapsed:.1f}s")
    # 等 jsonl flush
    time.sleep(1)
    after = sum(1 for _ in open(LOG_FILE, encoding="utf-8")) if LOG_FILE.exists() else 0
    print(f"[PROBE] jsonl 增量: {after - before} 行")
    # 数本次 trace 的事件
    if trace_id:
        counts, events = count_trace_events(trace_id[:16])
        print(f"[PROBE] 本次 trace ({trace_id[:8]}) 事件统计: {counts}")
        print(f"[PROBE] 嵌套层级明细:")
        for t, name, tid in events:
            print(f"   - {t:12s} | {name:20s} | trace={tid}")
    else:
        print("[PROBE] ❌ 未拿到 trace_id")

if __name__ == "__main__":
    probe()
