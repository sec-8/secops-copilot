"""LLM 客户端抽象基类

设计：所有 LLM 客户端实现统一接口，方便 LLMRouter 串联降级链。
抛异常 = 这个 tier 失败，router 会自动降级。
返回统一 dict 格式（见 chat() docstring）。
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class LLMClient(ABC):
    """所有 LLM 客户端的基类 - 统一接口
    
    name: 子类必须设置（用于 trace / 日志）
    """
    name: str = "base"
    
    @abstractmethod
    def chat(
        self,
        messages: List[Dict],
        temperature: float = 0,
        response_format: Optional[Dict] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """调 LLM，返回统一格式：
        
        Returns:
            {
                "content": str,                       # LLM 返回的文本
                "model": str,                         # 哪个模型
                "tier": str,                          # 哪个 tier（"ollama" / "ark" / "deepseek" / "refuse"）
                "usage": {"prompt_tokens": int, "completion_tokens": int},
                "tool_calls": Optional[List[Dict]],    # 工具调用决策（None = 不调工具）
                                                    # 每项: {"id": str, "name": str, "arguments": str}
                "raw": Any,                           # 原始 response（debug 用）
            }
        
        Raises:
            任何异常表示这个 tier 失败，LLMRouter 会降级到下一层
        """
        raise NotImplementedError