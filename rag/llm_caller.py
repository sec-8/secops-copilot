"""LLM 调用封装层
设计：把"调 LLM + 降级 + 解析 JSON + 异常处理"包成一层
- 调 router（自动降级）
- 自动加 response_format={"type": "json_object"}（RAG 场景几乎都要 JSON）
- 自动捕获 JSON 解析失败 → 返回拒答（不要让 RAG 主流程崩）
"""
import json
from typing import List, Dict, Any
from app.llm.factory import get_router

def call_llm_json(
    messages: List[Dict],
    max_retries: int = 1,
    **kwargs
) -> Dict[str, Any]:
    
    router = get_router()
    
    for attempt in range(max_retries + 1):
        # 强制 JSON 模式
        response = router.chat(
            messages,
            response_format={"type": "json_object"},
            **kwargs
        )
        
        content = response["content"]
        try:
            data = json.loads(content)
            return {
                "data": data,
                "tier": response["tier"],
                "model": response["model"],
                "usage": response["usage"],
                "raw_content": content
            }
        except json.JSONDecodeError as e:
            if attempt < max_retries:
                # 重试时加提示, 告知模型返回出错了。这是 LLM 应用工程的标准模式
                messages = messages + [{
                    "role": "user",
                    "content": f"上一轮返回的不是合法 JSON，错误：{str(e)[:100]}。请重新返回严格 JSON。"
                }]
                continue
        #  重试次数用完后
        raise ValueError(f"LLM 返回的 JSON 解析失败（重试 {max_retries} 次后仍失败）: {e}")   