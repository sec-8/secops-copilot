from .openai_compat import OpenAICompatClient

class ArkClient(OpenAICompatClient):
    """火山方舟 OpenAI 兼容端点"""
    name = "ark"