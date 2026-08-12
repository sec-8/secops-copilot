import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "glm-5.2")
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    # 工具治理：dry-run 模式（模拟执行）
    DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
    # Langfuse 
    LANGFUSE_SINK_ENABLED = os.getenv("LANGFUSE_SINK_ENABLED", "false")
    LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
    LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    
    def primary_client_args(self):
        """优先 DeepSeek，其次 OpenAI，最后本地 Ollama 兜底。"""
        if self.DEEPSEEK_API_KEY:
            return {
                "api_key": self.DEEPSEEK_API_KEY,
                "base_url": self.DEEPSEEK_BASE_URL,
                "model": self.DEEPSEEK_MODEL,
            }
        if self.OPENAI_API_KEY:
            return {
                "api_key": self.OPENAI_API_KEY,
                "base_url": self.OPENAI_BASE_URL,
                "model": self.OPENAI_MODEL,
            }
        return {
            "api_key": "ollama",
            "base_url": self.OLLAMA_BASE_URL,
            "model": self.OLLAMA_MODEL,
        }
    # 一次性返回所有可用档位的列表, 测试model是否可用后传参降级
    def get_all_tiers(self) -> list[dict]:
        """返回所有可用档位，按优先级排序：DeepSeek → OpenAI → Ollama"""
        tiers = []
        if self.DEEPSEEK_API_KEY:
            tiers.append({"name": "Deepseek", 
                        "api_key": self.DEEPSEEK_API_KEY,
                        "base_url": self.DEEPSEEK_BASE_URL,
                        "model": "deepseek-chat", })
        if self.OPENAI_API_KEY:    
            tiers.append({"name": "OpenAI", 
                        "api_key": self.OPENAI_API_KEY,
                        "base_url": self.OPENAI_BASE_URL,
                        "model": self.OPENAI_MODEL })
        tiers.append({"name":"Ollama",
                    "api_key": "ollama",
                    "base_url": self.OLLAMA_BASE_URL,
                    "model": self.OLLAMA_MODEL})
        return tiers

settings = Settings()