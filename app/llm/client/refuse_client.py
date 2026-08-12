"""拒答客户端（Tier 4 / 最后兜底）

设计：永远成功，永远返回拒答内容。
绝不用工具（也没法用），绝不掉用任何外部 LLM。
安全场景：必须保证"AI 不可用"时仍能给用户一个安全响应。
"""
from typing import List, Dict, Any, Optional
from .base import LLMClient

class RefuseClient(LLMClient):
    """拒答客户端 —— 永远不抛异常，永远返回拒答内容"""
    name = "refuse"
    
    def chat(
        self,
        messages: List[Dict],
        temperature: float = 0,
        response_format: Optional[Dict] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        # 拒答消息：明确告诉用户"系统不可用" + 不引发误以为能用的预期
        # 注意：
        #   - 不说"请联系管理员"（不是用户该做的事）
        #   - 不调任何工具（安全场景：降级时绝不能给工具权限）
        return {
            "content": "抱歉，AI 服务暂时不可用，请稍后再试。",
            "model": "refuse",
            "tier": self.name,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "tool_calls": None,  # 关键：永远不调工具
            "raw": None,
        }