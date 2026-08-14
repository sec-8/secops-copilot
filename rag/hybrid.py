# Hybrid Search = BM25 关键词检索 + 向量检索，用 RRF 融合两路排名
# ------------------------------------------------------------------
# 设计：对外暴露一个 search(query, top_k) 方法，签名和 LanceDBVectorStore.search 一致，
#       这样 ask.py 里只要把 vector_store 换成 HybridRetriever 就能无缝接入。
import jieba
import sys
from typing import List, Dict
from pathlib import Path
from rank_bm25 import BM25Okapi

from rag.vector_store import LanceDBVectorStore
from rag.loader import load_all_markdown
from rag.chunker import MarkdownChunker
from rag.decompose import decompose_query
# ------------------------------------------------------------------
# 安全领域自定义词典：强制这些术语不被 jieba 切开
# （通用分词器词典里没有这些，不加会被切碎 → BM25 命中率崩）
SECURITY_TERMS = [
    "路径遍历", "路径攻击", "系统路径", "白名单", "沙箱", "目录穿越",
    "C2外联", "C2", "prompt注入", "提示词注入", "注入攻击",
    "DGA", "beacon", "心跳外联", "威胁情报",
    "三级降级", "向量检索", "状态图", "工具调用",
    "ReAct", "LangGraph", "embedding", "chunk", "overlap",
]

# 查询同义词扩展：用户口语用词 → 文档标准用词
# 精确匹配替换（不做模糊）→ 只追加标准词，不丢原词
# 目的：让 BM25 用标准词额外命中文档，向量侧也受益
QUERY_ALIAS = {
    "路径攻击": "路径遍历 目录穿越",
    "prompt注入": "prompt注入 prompt 注入 提示词注入",
    "prompt 注入": "prompt注入 prompt 注入 提示词注入",
    "SQL 注入": "SQL注入 SQL 注入 spl注入 spl 注入",
    "SQL注入": "SQL注入 SQL 注入 spl注入 spl 注入",
}

RRF_K = 60  # RRF 常数，削弱头部名次碾压，标准取 60

class HybridRetriever:
    def __init__(self, vector_store: LanceDBVectorStore, chunked_docs: List[Dict]):
        """
        vector_store: 现成的向量检索器（第 3 步直接复用）
        chunked_docs: chunker 产出的 [{"source":..., "chunk_list":[...]}, ...]
                      用来建 BM25 索引（BM25 需要拿到全库所有 chunk 的原文）
        """
        self.vector_store = vector_store
        # 加载安全术语到 jieba 词典
        for term in SECURITY_TERMS:
            jieba.add_word(term)
        # 把 chunked_docs 摊平成一个扁平列表：每个元素 = 一个 chunk
        # self.corpus[i] 对应 self.chunk_meta[i]，下标一一对应
        self.chunk_meta: List[Dict] = []   # [{"text":..., "source":..., "chunk_index":...}, ...]
        for doc in chunked_docs:
            for idx, text in enumerate(doc["chunk_list"]):
                self.chunk_meta.append({
                    "text": text,
                    "source": doc["source"],
                    "chunk_index": idx,
                })
        # 对每个 chunk 分词，建 BM25 索引
        tokenized_corpus = [self._tokenize(item["text"]) for item in self.chunk_meta]
        self.bm25 = BM25Okapi(tokenized_corpus)

    def _tokenize(self, text: str) -> List[str]:
        """jieba 分词，返回词列表（BM25 吃的是词序列，不是整句）"""
        return list(jieba.cut(text))

    def _bm25_search(self, query: str, top_k: int) -> List[Dict]:
        """BM25 关键词检索，返回按分数降序的 top_k 个 chunk（带 chunk_meta 下标）"""
        tokens = self._tokenize(query)
        scores = self.bm25.get_scores(tokens)   # 每个 chunk 一个分，len == len(chunk_meta)
        # 取分数最高的 top_k 个下标
        ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [{"idx": i, "score": scores[i], **self.chunk_meta[i]} for i in ranked_idx]

    def _vector_search(self, query: str, top_k: int) -> List[Dict]:
        """向量检索，复用现成的 vector_store。为了和 BM25 对齐，给每个结果补一个 idx"""
        results = self.vector_store.search(query, top_k=top_k)
        # vector_store 返回的没有 chunk_meta 下标，用 (source, text) 反查对齐
        for r in results:
            r["idx"] = self._find_idx(r["source"], r["text"])
        return results

    def _find_idx(self, source: str, text: str) -> int:
        """用 (source, text) 在 chunk_meta 里反查下标，找不到返回 -1"""
        for i, item in enumerate(self.chunk_meta):
            if item["source"] == source and item["text"] == text:
                return i
        return -1

    # ==================================================================
    # RRF 融合
    # ==================================================================
    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """
        Hybrid 检索主入口。签名与 LanceDBVectorStore.search 一致，方便 ask.py 无缝替换。

        步骤：
          1. 各跑一路，拿两份排名（这里多召回一些，比如 top_k*2，给融合更多素材）
          2. RRF 融合：对每一路的排名，给每个 chunk 累加 1/(RRF_K + rank)
             - rank 从 0 还是 1 开始？想清楚（对结果影响不大，但要一致）
             - 用 chunk 的 idx 作为 key 来累加分数（同一个 chunk 可能同时出现在两路）
          3. 按融合总分降序，取 top_k
          4. 返回格式必须和 vector_store.search 一致：
             [{"text":..., "source":..., "chunk_index":..., "score":...}, ...]
             ⚠️ 注意 score：ask.py 的 check_no_relevant / build_context 用 score >= 0.5 判阈值！
                RRF 分数量纲和余弦完全不同（RRF 通常是 0.0x 级别的小数）——
                你直接返回 RRF 分数，会导致所有结果都 < 0.5 被全部拒答！
                这个阈值问题怎么解决？先想想，是改这里的 score，还是改 ask.py 的判断逻辑。

        recall_k 建议 = top_k * 2
        """
        # 同义词扩展：用户口语 → 文档标准词，两路都用扩展后的 query
        expanded = query
        for alias, synonyms in QUERY_ALIAS.items():
            if alias in query:
                expanded = query + " " + synonyms
                break
        recall_k = top_k * 2
        bm25_hits = self._bm25_search(expanded, recall_k)
        vec_hits = self._vector_search(expanded, recall_k)
        # 余弦分映射
        cosine_map = {f"{item['source']}_{item['chunk_index']}": item["score"] for item in vec_hits}
        retrieval_results = [bm25_hits, vec_hits]
        fused_scores = {} # 存储每个文档的最终RRF得分
        doc_map = {}     # 存储文档的完整元数据（方便最后拿出来返回）
        for ranked in retrieval_results:
            # 枚举列表，idx从0开始，但排名rank要从1开始
            for idx, item in enumerate(ranked):
                # 1. 生成唯一ID（组合键）
                doc_id = f"{item['source']}_{item['chunk_index']}"
                # 2. 缓存完整的文档信息（只存第一次出现的）
                if doc_id not in doc_map:
                    doc_map[doc_id] = item
                # 3. 计算排名：索引+1,从1开始算排名
                rank = idx + 1
                # 4. RRF核心累加公式
                fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        # 按得分降序排序，只取前 top_k 个ID 
        top_k_ids = sorted(fused_scores.keys(), key=lambda doc_id:fused_scores[doc_id], reverse=True)[:top_k]
        
        # 构建前top_k个的文档
        finally_results = []
        for doc_id in top_k_ids:
            item = doc_map[doc_id].copy()
            # 这行保留！score 继续填真余弦（独占的就是0.0，诚实）
            item["score"] = cosine_map.get(doc_id, 0.0)
            item['fused_score'] = fused_scores[doc_id]
            item['in_topk'] = True
            #     打印出来：source + 这个 chunk 的文本前50字，看看它到底是不是垃圾
            # if item["score"]==0.0:                         
            #     print(f"⚠️ BM25独占被挡: {item['source']} | {item['text'][:50]}")
            finally_results.append(item)      
        
        return finally_results

    def hybrid_search_multi(self, queries: List[str], top_k: int = 5) -> List[Dict]:
        # 每个 query 单独 hybrid_search
        # 收集所有 chunks，按 chunk.id 分组，收集所有排名
        # 累加 RRF 得分，重新排序
        # 去重，取最终 top_k 返回
        # 1. 每个子问题检索，保留结果，同时摘出第一名
        results_arr = []
        for text in queries:
            res = self.search(text, top_k=top_k)
            if not res:
                continue
            results_arr.append(res)  
        # 再一次 RRF 融合
        fused_scores = {} # 存储每个文档的最终RRF得分
        doc_map = {}  # 存储文档的完整元数据
        for ranked in results_arr:
            for idx, item in enumerate(ranked):
                doc_id = f"{item['source']}_{item['chunk_index']}"
                if doc_id not in doc_map:
                    doc_map[doc_id] = item
                rank = idx + 1
                fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        # 按得分降序排序
        sorted_ids = sorted(fused_scores.keys(), key=lambda doc_id:fused_scores[doc_id], reverse=True)    
        final_results = []  
        seen = set() 
        
        for doc_id in sorted_ids:
            # 去重
            if doc_id in seen:
                continue
            seen.add(doc_id)
            item = doc_map[doc_id].copy()
            item['fused_score'] = fused_scores[doc_id]
            item['in_topk'] = True
            final_results.append(item)   
            if len(final_results) >= top_k:
                break
        
        return final_results    
        
# 测试入口
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8") 
    PROJECT_ROOT = Path(__file__).parent.parent
    sys.path.insert(0, str(PROJECT_ROOT))
    db_path = PROJECT_ROOT / "data" / "lancedb"

    # 建 BM25 需要 chunked_docs
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    chunker = MarkdownChunker()
    chunked_docs = chunker.process_documents(docs)

    store = LanceDBVectorStore(db_path)
    retriever = HybridRetriever(store, chunked_docs)
    
    test_query = "JWT和SSTI的区别是什么？"
    sub_queries = decompose_query(test_query)
    
    print(f"问题: {",".join(sub_queries)}\n")
    results = retriever.hybrid_search_multi(sub_queries, top_k=5)
    for i, r in enumerate(results):
        print(f"{i+1}. [{r['score']:.4f}] {r['source']}")
        print(f"   {r['text'][:80]}...\n")
