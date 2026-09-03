"""
v2 HybridLCRetriever
- 把 v1 HybridRetriever 包成 LangChain BaseRetriever
- 不改 v1 源码
"""
from langchain_core.retrievers import BaseRetriever
from langchain_core.documents import Document
from typing import List
from rag.hybrid import HybridRetriever

class HybridLangChainRetriever(BaseRetriever):
    """把 v1 HybridRetriever 包成 LangChain BaseRetriever"""
    hybrid: HybridRetriever
    k: int = 5
    multi_query: bool = False
        
    def _get_relevant_documents(self, query: str) -> List[Document]:
        """调 v1 hybrid.search() → 转 LangChain Document
        - 保留 source / score 到 Document.metadata
        - multi_query 时走 hybrid_search_multi
        """
        if self.multi_query:
            from rag.decompose import decompose_query
            sub_queries = decompose_query(query)
            if len(sub_queries) == 1:
                context = self.hybrid.search(query, top_k=self.k)
            else: 
                context = self.hybrid.hybrid_search_multi(sub_queries, top_k=self.k)
        else: 
            context = self.hybrid.search(query, top_k=self.k)
            
        return [
            Document(
                page_content=r["text"], 
                metadata={
                    "source": r["source"], 
                    "score": r["score"],
                    "in_topk": r.get("in_topk", True)
                }
            )
            for r in context
        ]
    
    async def _aget_relevant_documents(self, query) -> List[Document]:
        """异步版
        - 直接调 sync 版（v1 hybrid.search() 不是 async，绕开）
        """
        return self._get_relevant_documents(query)
    
            
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    from pathlib import Path
    from rag.vector_store import LanceDBVectorStore
    from rag.loader import load_all_markdown
    from rag.chunker import MarkdownChunker
    
    PROJECT_ROOT = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(PROJECT_ROOT))
    db_path = PROJECT_ROOT / "data" / "lancedb"
    # 建 BM25 需要 chunked_docs
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    print(f"docs:{len(docs)}")
    chunker = MarkdownChunker()
    chunked_docs = chunker.process_documents(docs)
    
    store = LanceDBVectorStore(db_path)
    hybrid = HybridRetriever(store, chunked_docs)
    retriever = HybridLangChainRetriever(hybrid=hybrid, k=5)
    
    # 测试：单 query
    docs = retriever.invoke("XSS 防御")
    print(f"单 query: 拿到 {len(docs)} 个 Document")
    for d in docs[:3]:
        print(f"  - source={d.metadata.get('source', '?')} score={d.metadata.get('score', 0):.3f}")
        
    # 测试：multi_query
    retriever_multi = HybridLangChainRetriever(hybrid=hybrid, k=5, multi_query=True)
    docs_multi = retriever_multi.invoke("XSS和SQL 注入的防御区别")
    print(f"\nmulti_query: 拿到 {len(docs_multi)} 个 Document")