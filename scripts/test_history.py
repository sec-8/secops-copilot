"""测试 /chat/history 端点（短期 Memory 24h）"""
import requests
import json

URL = "http://localhost:8000/chat/history"
PARAMS = {
    "user_id": "test_user_v2_rag",
    "session_id": "sess_v2_rag_001",
}

print("=" * 60)
print("=== 测试 /chat/history（短期 Memory）===")
print(f"URL: {URL}")
print(f"PARAMS: {PARAMS}")
print()

try:
    resp = requests.get(URL, params=PARAMS, timeout=10)
    print(f"Status: {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('Content-Type')}")
    print()
    print("=== 响应 ===")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

    # 验证
    data = resp.json()
    messages = data.get("messages", [])
    print()
    print(f"=== 总结 ===")
    print(f"消息数: {len(messages)}")
    if messages:
        for i, m in enumerate(messages):
            content_preview = m.get("content", "")[:100]
            print(f"  [{i+1}] {m.get('role')}: {content_preview}")
except Exception as e:
    print(f"ERROR: {e}")
