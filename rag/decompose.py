"""拆解用户的问题"""
import sys
import json
from openai import OpenAI
from app.config import settings
from typing import List

sys.stdout.reconfigure(encoding="utf-8")

SYSTEM = """你是一个问题分解专家。你的任务是将用户问题拆解为若干**互不重叠、独立可检索**的子问题。

规则（必须严格遵守）：
1. **判断是否拆分**：检查问题中是否明确提及**两个或以上不同的技术实体**（例如“A和B”、“C与D”）。  
   - 如果只涉及**一个技术实体**（即使提问方式为“如何防御”“怎么检测”“原理是什么”“如何判定”“什么情况下会如何”等），则视为单一检索意图，**不拆分**，直接返回用户问题。  
   - 如果涉及**多个技术实体**（通常由“和”、“与”、“及”、“、”等连接，或通过“区别”、“对比”、“联合”等词暗示），则视为多个检索意图，**需要拆分**。  
   - **特别注意**：即使问题中没有显式连接词，但包含“对比”、“差异”、“共同防御”等明确指向多个实体的词，也应拆分。
2. 如果有多个检索意图，请为每个意图生成一个子问题，子问题必须保持用户问题的疑问词（如“为什么”“如何”“哪些”），不能全部改成“是什么”。
3. 子问题数量控制在1~3个之间（不含用户问题）。如果超过3个，只保留最重要的3个。
4. 最后，**必须将用户问题作为最后一个元素**添加到sub_queries列表中。
5. 子问题之间、子问题与用户问题之间不能有语义重复。如果重复，只保留一个。
6. **每个子问题必须包含至少一个明确的实体、属性或关系词（如具体名词、动词、条件），避免使用过于宽泛的词汇（如“系统”“方法”），以确保检索系统能精准匹配文档。**

输出严格 JSON 格式：{"sub_queries": ["子问题1", "子问题2", ..., "用户问题"]}
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