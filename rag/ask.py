# RAG 问答接口：检索 + LLM 生成 + 引用输出
from typing import List, Dict, Tuple, Union
from pathlib import Path
from rag.vector_store import LanceDBVectorStore
from rag.hybrid import HybridRetriever
from observability import tracer
from rag.llm_caller import call_llm_json

# 默认配置
DEFAULT_TOP_K = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.5  # 低于这个分数直接拒答
DEFAULT_SYSTEM_PROMPT = """你是 SecOps 安全知识库助手，基于用户提供的上下文回答问题。
要求：
1. 只使用上下文里的信息回答，不要编造
2. 如果上下文包含与问题相关的信息（哪怕只有部分相关），你必须回答，输出格式强制为JSON：{"answer": "基于上下文的回答", "has_answer": true}
3. 只有在上下文完全没有相关信息时，才输出：{"answer": "知识库未收录相关内容，无法回答这个问题", "has_answer": false}
4. 如果上下文信息部分相关但不完整，请基于已有信息给出**部分答案**，并明确说明哪些内容在文档中未找到依据（例如："根据文档，A 和 B 成立，但关于 C 没有提及"）。
5. 绝对禁止使用外部知识或常识来补充文档中缺失的信息，但允许基于文档内容进行合理推断
6. 不要输出 JSON 以外的任何内容"""

class RAGAsker:
    def __init__(
        self,
        vector_store: Union[LanceDBVectorStore, HybridRetriever],
        top_k: int = DEFAULT_TOP_K,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        multi_query:bool=False
    ):
        self.vector_store = vector_store
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.system_prompt = system_prompt
        self.multi_query = multi_query
    
    def check_no_relevant(self, results: List[Dict]) -> bool:
        """
        判断检索结果是否没有相关内容：
        - 所有结果分数都低于 threshold → 返回 True（应该拒答）
        - 至少一个高于 → 返回 False（可以回答）
        """
        should_refuse:bool = True
        for item in results:
            if item["score"] >= DEFAULT_SIMILARITY_THRESHOLD or item.get("in_topk", False):
                should_refuse = False
        return should_refuse        
    
    def build_context(self, results: List[Dict]) -> Tuple[str, List[str], List[str]]:
        """
        把检索结果拼成交照给 LLM 的上下文文本，同时收集引用来源
        返回：(context_text, citations, contexts)
        """
        context_text:str = ""
        citations:List[str] = []
        contexts:List[str] = []
        for item in results:
            txt = item["source"] + ':' + item["text"]
            context_text = context_text + txt + '\n' if context_text else txt
            if item["score"] >= self.similarity_threshold or item.get("in_topk", False):
                contexts.append(txt)
            if item["source"] not in citations:
                citations.append(item["source"])
        return context_text, citations, contexts
    
    def _retrieve(self, question: str):
        """纯检索（multi_query 也在这）"""
        if self.multi_query and isinstance(self.vector_store, HybridRetriever):
            from rag.decompose import decompose_query
            sub_queries = decompose_query(question)
            if len(sub_queries) == 1:
                return self.vector_store.search(question, top_k=self.top_k) 
            return self.vector_store.hybrid_search_multi(sub_queries, top_k=self.top_k)
        return self.vector_store.search(question, top_k=self.top_k)
    
    def ask(self, question: str) -> Dict:
        """
        完整 RAG 问答流程：
        1. 检索 → 2. 无据判断 → 3. 拼上下文 → 4. 调用 LLM → 5. 返回结果 + 引用
        """
        current_trace = tracer.current_trace()

        # 1. 向量检索
        if current_trace:
            with current_trace.span("rag", "知识库检索") as rag_span:
                rag_span.set_tag("query", question[:200])
                rag_span.set_tag("top_k", self.top_k)
                rag_span.set_tag("multi_query", self.multi_query)

                results=self._retrieve(question)
                rag_span.set_tag("retrieved_count", len(results))
                
                if results:
                    rag_span.set_tag("top_scores", [
                        {"source": r.get("source", "?"), "score": round(r.get("score", 0), 4)}
                        for r in results[:3]
                    ])
        else:
            results = self._retrieve(question)
        # 2. 判断是否无据
        if self.check_no_relevant(results):
            if current_trace:
                tracer.emit_event("rag_refuse", {
                    "question": question[:200],
                    "reason": "no_relevant_chunks",
                    "retrieved_count": len(results),
                })
            return {
                "answer": "知识库未收录相关内容，无法回答这个问题",
                "citations": [],
                "has_answer": False,
                "contexts": []
            }

        # 3. 拼上下文 + 收集引用
        context, citations, contexts = self.build_context(results)
        messages = [
            {"role": "system", "content": self.system_prompt + "\n\n上下文：\n" + context},
            {"role": "user", "content": question}
        ]
        # 4. 调用 LLM（三级降级 用 call_llm_json 一行调用，自动降级 + 解析 JSON）—— LLM span 包整个循环
        result = None
        error_msg = None

        if current_trace:
            with current_trace.span("llm", "RAG生成") as llm_span:
                llm_span.set_input(messages)  # input 在循环前定型
                
                try:
                    result = call_llm_json(messages, max_retries=1) # max_retries=1 首次+重试一次
                    llm_span.set_output(result["raw_content"])
                    llm_span.set_tag("model", result["model"])
                    llm_span.set_tag("tier", result["tier"])
                    if result["usage"]:
                        llm_span.set_tag("prompt_tokens", result["usage"]["prompt_tokens"])
                        llm_span.set_tag("completion_tokens", result["usage"]["completion_tokens"])
                except ValueError as e:
                    # call_llm_json 解析失败兜底
                    error_msg = str(e)
                    llm_span.set_output(f"json_parse_failed: {error_msg[:200]}")
                except Exception as e:
                    # 所有 tier 都 fail（理论上 RefuseClient 会接住，但防御性兜底）
                    error_msg = str(e)
                    llm_span.set_output(f"llm_call_failed: {error_msg[:200]}")    
        else:
            try:
                result = call_llm_json(messages, max_retries=1)
            except Exception as e:
                error_msg = str(e)

        # 5. 解析 raw（兜底处理）
        if result is None:
            return {
                "answer": "LLM 调用失败：所有 tier 都失败",
                "citations": [],
                "has_answer": False,
                "contexts": []
            }

        answer = result["data"]
        if answer.get("has_answer"):
            return {
                "answer": answer["answer"],
                "citations": citations,
                "has_answer": True,
                "contexts": contexts
            }
        return {
            "answer": "知识库未收录相关内容，无法回答这个问题",
            "citations": [],
            "has_answer": False,
            "contexts": contexts
        }

# 测试入口
if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).parent.parent
    db_path = PROJECT_ROOT / "data" / "lancedb"
    
    store = LanceDBVectorStore(db_path)
    asker = RAGAsker(store)
    
    test_question = "路径遍历 白名单 resolve"
    print(f"问题: {test_question}")
    result = asker.ask(test_question)
    print(f"\n回答:\n{result['answer']}")
    print(f"\n引用: {result['citations']}")
    print(f"\n是否有答案: {result['has_answer']}")
