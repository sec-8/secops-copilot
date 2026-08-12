import requests, sys
r = requests.post(
    'http://127.0.0.1:8000/chat/stream',
    json={'text': '如何检测 DOM 型 XSS？'},
    stream=True,
    timeout=30
)
for line in r.iter_lines(decode_unicode=True):
    if line:
        print('  >>', line[:200])
        sys.stdout.flush()
