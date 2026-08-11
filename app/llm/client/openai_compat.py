"""OpenAI 兼容协议的客户端基类
被 OllamaClient / ArkClient / DeepSeekClient 共用。
- Ollama: localhost:11434/v1（本地 qwen3:8b）
- Ark: https://ark.cn-beijing.volces.com/api/coding/v3（火山方舟）
- DeepSeek: https://api.deepseek.com（API）
"""
from typing import List, Dict, Any, Optional
from openai import OpenAI
from .base import LLMClient


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容协议的客户端基类
    
    子类只需设置 name = "ollama" / "ark" / "deepseek"
    """
    def __init__(self, api_key: str, base_url: str, model: str):
        """
        Args:
            api_key: API key（Ollama 用 "ollama" 占位）
            base_url: OpenAI 兼容端点
            model: 模型名
        """
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        # name 由子类 class 属性设置（"ollama" / "ark" / "deepseek"）
    
    def chat(
        self,
        messages: List[Dict],
        temperature: float = 0,
        response_format: Optional[Dict] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """调 OpenAI 兼容 SDK，返回统一格式
        
        注意：response_format 和 tools 都是 None 时不传（避免无效参数）
        """
        # 组装 create 参数
        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            create_kwargs["response_format"] = response_format
        if tools is not None:
            create_kwargs["tools"] = tools
        # 透传其他 kwargs（比如 agent.py 可能传 tool_choice）
        create_kwargs.update(kwargs)
        
        # 调 OpenAI SDK（失败抛异常 → router 降级）
        response = self.client.chat.completions.create(**create_kwargs)
        
        # 转统一格式
        msg = response.choices[0].message
        
        # tool_calls: OpenAI SDK 返回 list[Choice]（每个有 .id/.function.name/.function.arguments）
        # 转为 list[dict]（arguments 是字符串，调用方自己 json.loads）
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]
        
        # usage: OpenAI SDK 有 response.usage.prompt_tokens / completion_tokens
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
        
        return {
            "content": msg.content or "",
            "model": self.model,
            "tier": self.name,
            "usage": usage,
            "tool_calls": tool_calls,
            "raw": response,
        }