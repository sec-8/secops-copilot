"""LLM 降级链（Day3 核心）

设计：
- clients 按优先级排序（Tier 1 → Tier 4）
- 每个 client 重试 max_retries_per_tier 次
- 成功 → 立即返回
- 失败 → 发 tier_retry event（每次重试都发）
- 整个 tier 全失败 → 发 tier_failover event → 试下一层
- 所有 tier 全失败 → 调最后一层（RefuseClient 永远成功）兜底
- 每次成功 → 可以在外层 trace 看到 "used_tier" tag
"""
from typing import List, Dict, Any
from observability import tracer
from .client.base import LLMClient

class LLMRouter:
    def __init__(self, clients: List[LLMClient], max_retries_per_tier: int = 2):
        """
        Args:
            clients: 按优先级排序的客户端列表（Tier 1 → Tier 4）
            max_retries_per_tier: 每个 tier 重试次数（默认 2）
        """
        self.clients = clients
        self.max_retries_per_tier = max_retries_per_tier
        
    def chat(self, messages, **kwargs) -> Dict[str, Any]:
        """降级链主入口
        1. 遍历 self.clients
        2. 每个 client 重试 max_retries_per_tier 次
        3. 成功 → 立即返回
        4. 整个 tier 全失败 → 发 tier_failover event → 试下一层
        5. 所有 tier 全失败 → 调最后一层（RefuseClient 永远成功）兜底
        """
        last_error = None
        
        for i, client in enumerate(self.clients):
            for attempt in range(self.max_retries_per_tier):
                try:
                    return client.chat(messages, **kwargs)
                except Exception as e:
                    last_error = e
                    tracer.emit_event("tier_retry", {
                        "tier": client.name,
                        "attempt": attempt + 1,
                        "error": str(e)[:200],
                    })
                    # continue 内层，进入下一次 attempt
            # 这一层全失败 → 降级
            next_tier_name = self.clients[i + 1].name if i + 1 < len(self.clients) else None
            tracer.emit_event("tier_failover", {
                "from_tier": client.name,
                "to_tier": next_tier_name,
                "error": str(last_error)[:200] if last_error else "unknown",
                "attempt": self.max_retries_per_tier,
            })
        # 保底 再调一次最后一层
        return self.clients[-1].chat(messages, **kwargs)
            