"""懒加载单例 router

设计：第一次调 get_router() 时构造，之后复用。
- 没 key 的 tier 跳过（节省初始化）
- RefuseClient 永远在最后
"""
from .router import LLMRouter
from .client.ollama_client import OllamaClient
from .client.ark_client import ArkClient
from .client.deepseek_client import DeepSeekClient
from .client.refuse_client import RefuseClient
from app.config import settings

_router_instance = None

def get_router() -> LLMRouter:
    """懒加载构造 LLMRouter
    Tier 顺序：
      Tier 1: Ark（火山方舟，最快最稳）
      Tier 2: DeepSeek（key 默认空）
      Tier 3: Ollama（本地，慢但兜底）
      Tier 4: RefuseClient（永远成功）
    """
    global _router_instance
    if _router_instance is not None:
        return _router_instance
    
    clients = []
    # Tier 1: Ark
    if settings.OPENAI_API_KEY:
        clients.append(ArkClient(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=settings.OPENAI_MODEL,
        ))
        
    # Tier 2: DeepSeek
    if settings.DEEPSEEK_API_KEY:
        clients.append(DeepSeekClient(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            model=settings.DEEPSEEK_MODEL,
        ))
    # Tier 3: Ollama
    clients.append(OllamaClient(
        api_key="ollama",
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.OLLAMA_MODEL,
    ))
    # Tier 4: RefuseClient（永远成功，必须最后一个）
    clients.append(RefuseClient())
    
    _router_instance = LLMRouter(clients, max_retries_per_tier=2)
    return _router_instance