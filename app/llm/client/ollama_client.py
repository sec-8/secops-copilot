"""Ollama 本地 LLM 客户端（Tier 1 备选 / Tier 3 兜底）

地址：localhost:11434/v1（OpenAI 兼容）
模型：qwen3:8b（settings.OLLAMA_MODEL 配）
"""
from .openai_compat import OpenAICompatClient

class OllamaClient(OpenAICompatClient):
    """本地 Ollama 客户端，api_key 用 "ollama" 占位"""
    name = "ollama"