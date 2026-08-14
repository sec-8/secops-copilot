# -*- coding: utf-8 -*-
"""
RAGAS 评测脚本
--------------------------------------------------
思路（拆开评，别互相污染）：
  - 有据题（ground_truth != 拒答话术）→ 跑 RAGAS 三指标（faithfulness / answer_relevancy / context_precision）
  - 无据题（ground_truth == 拒答话术）→ 单独统计「拒答命中率」（has_answer==False 才算对）

裁判 LLM：Ark（ark-code-latest，走 OPENAI 档位）—— 聪明，判对错
Embedding：本地 Ollama nomic-embed-text —— 与生产向量库同一把尺子，一致性优先 + 免费
"""
import sys
import json
import argparse
from pathlib import Path
from typing import  Dict

# 让脚本能 import 到项目里的 rag / app 包
sys.stdout.reconfigure(encoding="utf-8")  # 防 Windows GBK emoji 崩
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datasets import Dataset
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision
from ragas.llms import LangchainLLMWrapper
from ragas.embeddings import LangchainEmbeddingsWrapper
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import settings
from rag.vector_store import LanceDBVectorStore
from rag.loader import load_all_markdown
from rag.chunker import MarkdownChunker
from rag.ask import RAGAsker
from rag.hybrid import HybridRetriever

# 拒答话术（用来区分有据题 / 无据题）
REFUSAL_TEXT = "知识库未收录相关内容，无法回答这个问题"

# 单条样本某指标低于多少就落盘当 badcase 复盘
BADCASE_THRESHOLD = 0.55  # 0~1 的数


def load_dataset(path: Path):
    """读 jsonl 评测集，按有据 / 无据拆成两拨"""
    grounded, ungrounded = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            # 无据题：ground_truth 就是拒答话术 且 expected_sources 为空
            if item["ground_truth"] == REFUSAL_TEXT and not item["expected_sources"]:
                ungrounded.append(item)
            else:
                grounded.append(item)
    return grounded, ungrounded


def build_judge_and_embedding():
    """裁判 LLM = Ark；Embedding = 本地 Ollama nomic-embed-text"""
    # --- 裁判 LLM：Ark（OPENAI 档位）---
    judge_llm = ChatOpenAI(
        model="deepseek-v4-flash",          # ark-code-latest
        # api_key=settings.DEEPSEEK_API_KEY,
        # base_url=settings.DEEPSEEK_BASE_URL,  
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
        temperature=0,
    )
    # --- Embedding：本地 Ollama（OpenAI 兼容端点 /v1）---
    judge_emb = OpenAIEmbeddings(
        model="nomic-embed-text",
        api_key="ollama",                     # 占位，本地不校验
        base_url=settings.OLLAMA_BASE_URL,    # http://localhost:11434/v1
        check_embedding_ctx_length=False,     # 关掉 openai 的 tiktoken 长度检查（本地模型不吃这套）
    )
    return LangchainLLMWrapper(judge_llm), LangchainEmbeddingsWrapper(judge_emb)


def run_system_on_grounded(asker: RAGAsker, grounded: list):
    """对每条有据题跑 ask()，收集 ragas 需要的 4 字段"""
    rows = {"question": [], "answer": [], "contexts": [], "ground_truth": []}
    for item in grounded:
        res = asker.ask(item["question"])
        rows["question"].append(item["question"])
        rows["answer"].append(res["answer"])
        rows["contexts"].append(res["contexts"] if res["contexts"] else [""])  # ragas 不接受空 contexts
        rows["ground_truth"].append(item["ground_truth"])
    return Dataset.from_dict(rows)


def eval_ungrounded(asker: RAGAsker, ungrounded: list):
    """无据题：只看拒没拒。返回 (命中数, 总数, 漏网详情)"""
    hit, misses = 0, []
    for item in ungrounded:
        res = asker.ask(item["question"])
        if res["has_answer"] is False:
            hit += 1
        else:
            misses.append({"question": item["question"], "answer": res["answer"]})
    return hit, len(ungrounded), misses

def build_asker(retriever_type:str, store, chunked_docs):
    if retriever_type == "vector":
        return RAGAsker(store)
    elif retriever_type == "hybrid":
        return RAGAsker(HybridRetriever(store, chunked_docs))
    elif retriever_type == "hybrid_multi":
        # 多路检索：先生成 HybridRetriever，再给它装上子问题分解开关
        # RAGAsker.ask() 内部如果调 hybrid_search_multi 就走 multi 路径
        # 走 search 就走单路 —— 所以这里需要先让 RAGAsker 认得 multi
        from rag.hybrid import HybridRetriever
        from rag.ask import RAGAsker
        hr = HybridRetriever(store, chunked_docs)
        return RAGAsker(hr, multi_query=True)
    else:
        raise ValueError(f"未知检索器：{retriever_type}")

def main():
    # 造一个"命令行参数解析器"。它负责读你在终端敲的 python xxx.py 后面跟的那些 --参数
    parser = argparse.ArgumentParser()
    # 注册一个参数，拆解每个部件：
    # "--retriever"        → 参数名。终端里写 --retriever hybrid。名字决定了后面用 args.retriever 取值（去掉--）
    # choices=[...]        → 白名单。只准传 vector 或 hybrid，传别的（如 --retriever xxx）argparse 直接报错退出，
    #                          这是免费的输入校验，省得你自己 if 判断
    # default="hybrid"     → 不传时的兜底值。你直接 python -m eval.run_ragas（不带--retriever）就等于跑 hybrid
    parser.add_argument("--retriever", choices=["vector", "hybrid", "hybrid_multi"], default="hybrid")
    # 真正执行解析：读 sys.argv（终端输入），按上面的规则填进 args 对象
    # 之后 args.retriever 就是最终值（传了用传的，没传用 default）
    args = parser.parse_args()
    
    dataset_path = PROJECT_ROOT / "eval" / "rag_dataset.jsonl"
    grounded, ungrounded = load_dataset(dataset_path)
    print(f"评测集：有据 {len(grounded)} 条 / 无据 {len(ungrounded)} 条")

    db_path = PROJECT_ROOT / "data" / "lancedb"
    # 间 BM25 需要 chunked_docs
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    chunker = MarkdownChunker()
    chunked_docs = chunker.process_documents(docs)
    # 切好的 chunks 写入向量库,向量库数据
    store = LanceDBVectorStore(db_path)
    # 把Hybrid注入RAGAsker
    # asker = RAGAsker(LanceDBVectorStore(db_path))
    # asker = RAGAsker(HybridRetriever(store, chunked_docs))
    asker = build_asker(args.retriever, store, chunked_docs)
    
    judge_llm, judge_emb = build_judge_and_embedding()


    # context_precision 需要 ground_truth；faithfulness/answer_relevancy 需要 answer+contexts）
    metrics = [
        faithfulness,        # 评 contexts
        answer_relevancy,    # 评 answer
        context_precision,   # 评 ground_truth
    ]
    # ============================================================

    # 有据题跑 RAGAS
    print("\n【1/2】有据题 → RAGAS 三指标")
    ds = run_system_on_grounded(asker, grounded)
    result = evaluate(ds, metrics=metrics, llm=judge_llm, embeddings=judge_emb)
    print("\n=== RAGAS 总分 ===")
    print(result)

    # 落每条明细 + 挑 badcase
    df = result.to_pandas()
    out_csv = PROJECT_ROOT / "eval" / f"ragas_{args.retriever}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"明细已存：{out_csv}")

    # 用哪个指标 < BADCASE_THRESHOLD 判定为 badcase？还是任一指标低就算？
    # 把挑出来的 badcase 打印出来，方便复盘。
    fne = result["faithfulness"]
    ans = result["answer_relevancy"]
    cont = result["context_precision"]
    eval_text:Dict = {
        "faithfulness": "模型回答里每个论断和上下文（contexts）里的内容关联弱，依据性低",
        "answer_relevancy": "模型回答和问题的相关性差，有些答非所问",
        "context_precision": "模型检索的上下文和问题的相关性差，未检索到相关答案"
    }
    unRagas:int = 0
    for i in range(len(grounded)):
        text = ""
        if fne[i] < BADCASE_THRESHOLD:
            text = f"faithfulness（忠实度）的分数为{fne[i]} ，{eval_text['faithfulness']}"
        if ans[i] < BADCASE_THRESHOLD:   
            tx = f"answer_relevancy（答案相关性）的分数为{ans[i]} ，{eval_text['answer_relevancy']}"
            text = text + '；' + tx if text else tx
        if cont[i] < BADCASE_THRESHOLD: 
            cx = f"context_precision（上下文精度）的分数为{cont[i]} ，{eval_text['context_precision']}"
            text = text + '；' + cx if text else cx 
        if text:
            unRagas += 1
            print(f"Q: {grounded[i]['question']} 的 {text}")   
    print(f"有据题不通过率：{unRagas}/{len(grounded)} = {unRagas/len(grounded):.0%}")   
    # ============================================================

    # ---- 2) 无据题算拒答命中率 ----
    print("\n【2/2】无据题 → 拒答命中率")
    hit, total, misses = eval_ungrounded(asker, ungrounded)
    print(f"拒答命中：{hit}/{total} = {hit/total:.0%}")
    if misses:
        print("⚠️ 没拒住的（幻觉风险）：")
        for m in misses:
            print(f"  Q: {m['question']}\n  A: {m['answer']}")


if __name__ == "__main__":
    main()
