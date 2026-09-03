"""测试 v1 端点（铁律回归测试 - v1 业务 0 改）"""
import requests
import json

URL = "http://localhost:8000/chat/stream"
BODY = {
    "text": "XSS 防御",
    "user_id": "test_user_v1",
    "session_id": "sess_v1_001",
}

print("=" * 60)
print("=== 测试 1: v1 端点（铁律回归）===")
print(f"URL: {URL}")
print(f"BODY: {json.dumps(BODY, ensure_ascii=False)}")
print()

resp = requests.post(URL, json=BODY, stream=True, timeout=30)
print(f"Status: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('Content-Type')}")
print()
print("=== SSE 事件流 ===")
event_count = 0
for line in resp.iter_lines():
    if line:
        decoded = line.decode("utf-8")
        print(decoded)
        event_count += 1

print()
print(f"=== 总结: 共 {event_count} 个事件 ===")
