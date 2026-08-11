from .openai_compat import OpenAICompatClient


class DeepSeekClient(OpenAICompatClient):
    """Deepseek OpenAI 兼容端点"""
    name = "deepseek"