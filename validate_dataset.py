import json

path = r'J:\programs\secops-copilot\eval\rag_dataset.jsonl'
lines = []
with open(path, encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            lines.append(data)
        except json.JSONDecodeError as e:
            print(f'Line {i}: JSON ERROR - {e}')
            break

grounded = [l for l in lines if l['ground_truth'] != '知识库未收录相关内容，无法回答这个问题']
ungrounded = [l for l in lines if l['ground_truth'] == '知识库未收录相关内容，无法回答这个问题']

print(f'Total: {len(lines)} 条')
print(f'  有据: {len(grounded)} 条')
print(f'  无据: {len(ungrounded)} 条')

for l in lines:
    assert 'question' in l and 'ground_truth' in l and 'expected_sources' in l

print('All lines valid JSON ✅')
