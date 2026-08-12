"""拆解用户的问题"""
import sys
import json
from openai import OpenAI
from app.config import settings
from typing import List

sys.stdout.reconfigure(encoding="utf-8")

SYSTEM = """你是一个问题分解专家。你的任务是将用户问题拆解为若干**互不重叠、独立可检索**的子问题。

规则（必须严格遵守）：
1. 先判断问题中是否包含超过一个**独立的检索意图**（即需要从不同维度或不同实体去回答）。
2. 如果只有一个检索意图，直接返回原问题（此时sub_queries只包含原问题）。
3. 如果有多个检索意图，请为每个意图生成一个子问题，子问题必须保持原问题的疑问词（如“为什么”“如何”“哪些”），不能全部改成“是什么”。
4. 子问题数量控制在1~3个之间（不含原问题）。如果超过3个，只保留最重要的3个。
5. 最后，**必须将原问题作为最后一个元素**添加到sub_queries列表中。
6. 子问题之间、子问题与原问题之间不能有语义重复。如果重复，只保留一个。
7. **每个子问题必须包含至少一个明确的实体、属性或关系词（如具体名词、动词、条件），避免使用过于宽泛的词汇（如“系统”“方法”），以确保检索系统能精准匹配文档。**

输出严格 JSON 格式：{"sub_queries": ["子问题1", "子问题2", ..., "原问题"]}
只输出JSON，不要任何其他解释。"""

def decompose_query(query: str) -> List[str]:
    args = settings.primary_client_args()
    client = OpenAI(api_key=args["api_key"], base_url=args["base_url"])
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": query}
    ]
    resp = client.chat.completions.create(
        model=args["model"],
        messages=messages,
        temperature=0
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    try: 
        queries = json.loads(raw)   
    except json.JSONDecodeError:
        return [query]
    if len(queries.get("sub_queries", [query])) > 1:
        print(f"decompose中queries的内容:{raw}")
    return queries.get("sub_queries", [query])

if __name__ == "__main__":
    result = decompose_query("JWT和SSTI的区别是什么？")
    print(",".join(result))    