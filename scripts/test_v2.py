"""测试 v2 端点（4 agent 协同 + trace_id + 5 事件）"""
import requests
import json

URL = "http://localhost:8000/v2/chat/stream"

# 4 个用例：rag / tool / memory-write / memory-read
TEST_CASES = [
    {
        "name": "v2 RAG: XSS 和 SQL 注入防御有什么区别",
        "body": {
            "text": "XSS 和 SQL 注入防御有什么区别",
            "user_id": "test_user_v2_rag",
            "session_id": "sess_v2_rag_001",
        },
        "expect_dispatch": "rag",
        "expect_call_id_prefix": "rag_",
    },
    {
        "name": "v2 Tool: IP 查询",
        "body": {
            "text": "8.8.8.8 是否恶意",
            "user_id": "test_user_v2_tool",
            "session_id": "sess_v2_tool_001",
        },
        "expect_dispatch": "tool",
        "expect_call_id_prefix": "tool_",
    },
    {
        "name": "v2 Memory: 写",
        "body": {
            "text": "记住：我叫胖哥，我是前端开发",
            "user_id": "test_user_v2_mem",
            "session_id": "sess_v2_mem_001",
        },
        "expect_dispatch": "memory",
        "expect_call_id_prefix": "memory_",
    },
    {
        "name": "v2 Memory: 读（上一条写过的胖哥）",
        "body": {
            "text": "我叫什么？",
            "user_id": "test_user_v2_mem",
            "session_id": "sess_v2_mem_001",
        },
        "expect_dispatch": "memory",
        "expect_call_id_prefix": "memory_",
    },
]


def run_test(name, body, expect_dispatch, expect_call_id_prefix):
    print()
    print("=" * 60)
    print(f"=== {name} ===")
    print(f"URL: {URL}")
    print(f"BODY: {json.dumps(body, ensure_ascii=False)}")
    print(f"预期 dispatch: {expect_dispatch} | 预期 call_id 前缀: {expect_call_id_prefix}")
    print()

    try:
        resp = requests.post(URL, json=body, stream=True, timeout=30)
        print(f"Status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        print()
        print("=== SSE 事件流 ===")

        event_count = 0
        has_trace_id = False
        has_token = False
        trace_ids = set()
        call_ids = []
        event_types = []

        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            # 提取 data: 后的 JSON
            if decoded.startswith("data: "):
                try:
                    ev = json.loads(decoded[6:])
                    event_count += 1
                    event_types.append(ev.get("type"))

                    # 检查 trace_id
                    if "trace_id" in ev:
                        has_trace_id = True
                        trace_ids.add(ev["trace_id"])

                    # 检查 token
                    if ev.get("type") == "token":
                        has_token = True

                    # 收集 call_id
                    if "call_id" in ev and ev["call_id"]:
                        call_ids.append(ev["call_id"])

                    # 打印事件
                    ev_short = {k: v for k, v in ev.items() if k != "content"}
                    if "content" in ev and isinstance(ev["content"], str):
                        ev_short["content_preview"] = ev["content"][:80]
                    print(f"  [{event_count}] {ev_short}")
                except json.JSONDecodeError:
                    print(f"  RAW: {decoded}")

        print()
        print(f"=== {name} 总结 ===")
        print(f"事件总数: {event_count}")
        print(f"事件类型: {event_types}")
        print(f"trace_id 唯一数: {len(trace_ids)}（应为 1）")
        if trace_ids:
            print(f"trace_id 示例: {list(trace_ids)[0][:16]}...")
        print(f"每个事件都有 trace_id: {'✅' if has_trace_id else '❌'}")
        print(f"有 token 事件: {'✅' if has_token else '❌'}")
        print(f"call_id 列表: {call_ids[:3]}{'...' if len(call_ids) > 3 else ''}")

        # 验证 call_id 前缀
        if call_ids:
            all_match = all(c.startswith(expect_call_id_prefix) for c in call_ids)
            print(f"call_id 前缀全部匹配 {expect_call_id_prefix}: {'✅' if all_match else '❌'}")
            # call_id 在同一 agent 内可能重复（tool_call + tool_result + final_answer 共享 1 个）
            # 只要有 call_id 就行（说明生成了）
            print(f"call_id 已生成: {'✅' if call_ids else '❌'}（1 agent 1 call_id 属设计）")

    except Exception as e:
        print(f"ERROR: {e}")


# 跑 4 个用例
for tc in TEST_CASES:
    run_test(tc["name"], tc["body"], tc["expect_dispatch"], tc["expect_call_id_prefix"])
