"""
v2 LCEL RAG Chain
- 25 行重写 v1 ask.py 80 行手写 RAG
- 0 改 v1 源码
- 0 行重复
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import (
    RunnableParallel, RunnablePassthrough, RunnableLambda, RunnableBranch
)
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI

from app_v2.rag.retriever import HybridLangChainRetriever
from rag.hybrid import HybridRetriever
from rag.vector_store import LanceDBVectorStore
from rag.loader import load_all_markdown
from rag.chunker import MarkdownChunker
from pathlib import Path
from app.config import settings
from typing import List
from langchain_core.documents import Document
# Parser 降级方案，暂未启用     
def safe_parse(parser_output, parser_obj=None):
    """parser 降级"""
    try:
        return parser_obj.parse(parser_output) if parser_obj else {"answer": parser_output, "has_answer": True}
    except OutputParserException:
        return {"answer": "LLM 输出 JSON 格式异常，无法解析", "has_answer": False}

# 拒答检查
def check_no_relevant(parallel_output: dict) -> dict:
    docs = parallel_output["docs"] 
    should_refuse = True
    for doc in docs:
        if doc.metadata.get("score", 0) >= 0.5 or doc.metadata.get("in_topk", False):
            should_refuse = False
    # 保留原 dict 结构（question 字段），不然 branch 拿不到 question
    return {**parallel_output, "refuse": should_refuse}

# 上下文拼装
def format_docs(docs: List[Document]) -> str:
    # context_text = ""
    # for doc in docs:
    #     txt = doc.metadata.get("source", "?") + ':' + doc.page_content
    #     context_text = context_text + txt + '\n' if context_text else txt
    # return context_text
    # 更优雅的写法
    context_text = [f"{doc.metadata.get('source', '?')}:{doc.page_content}" for doc in docs]
    return "\n".join(context_text) + '\n' if context_text else ""

# prompt
def build_prompt(strict_mode: bool = True) -> ChatPromptTemplate:
    """根据 multi_query 模式选 prompt
    - strict_mode=True (单 query) → 严格拒答（防沾边）
    - strict_mode=False (多 query) → 宽松（对比题兼容）
    """
    strict_clause = "   - 必须是**主题直接相关**，不接受\"沾边推断\"；" if strict_mode else ""
    partial_clause = "，但允许基于文档内容进行合理推断" if not strict_mode else ""
    
    template = f"""
        你是 SecOps 安全知识库助手，基于用户提供的上下文回答问题。

        要求：
        1. 只使用上下文里的信息回答，不要编造
        2. 如果上下文包含与问题相关的信息（哪怕只有部分相关），你必须回答，输出格式强制为JSON：{{{{"has_answer": true, "answer": "基于上下文的回答"}}}}
        {strict_clause}
        3. 只有在上下文完全没有相关信息时，才输出：{{{{"has_answer": false, "answer": "知识库未收录相关内容，无法回答这个问题"}}}}
        4. 如果上下文信息部分相关但不完整，请基于已有信息给出**部分答案**，并明确说明哪些内容在文档中未找到依据（例如：「根据文档，A 和 B 成立，但关于 C 没有提及」）。
        5. 绝对禁止使用外部知识或常识来补充文档中缺失的信息{partial_clause}
        6. 不要输出 JSON 以外的任何内容
        7. answer 字段内**禁止使用裸双引号 ""**，用中文引号「」或单引号 ''
        8. **禁止使用反引号** `` ` `` 包裹代码（如 `` `HttpOnly` ``），用书名号《》或纯文本（如 HttpOnly）替代
        
        上下文：
        {{context}}

        问题：{{question}}

        输出 JSON 格式：{{{{"has_answer": true/false, "answer": "..."}}}}
        """
    return ChatPromptTemplate.from_template(template)

args = settings.primary_client_args() 
llm = ChatOpenAI(
    api_key=args["api_key"],
    base_url=args["base_url"],
    model=args["model"],
    temperature=0
)
# 懒加载方法
_retriever_singleton = None
_retriever_multi_singleton = None
def get_retriever(k: int = 5, multi_query: bool = False) -> HybridLangChainRetriever:
    """返回单例 retriever；multi_query=False 时不内部二次分解问题"""
    global _retriever_singleton, _retriever_multi_singleton
    target = _retriever_multi_singleton if multi_query else _retriever_singleton
    if target is None:
        PROJECT_ROOT = Path("J:/programs/secops-copilot")
        docs_raw = load_all_markdown(PROJECT_ROOT / "knowledge")
        chunker = MarkdownChunker()
        chunked_docs = chunker.process_documents(docs_raw)
        store = LanceDBVectorStore(PROJECT_ROOT / "data" / "lancedb")
        hybrid = HybridRetriever(store, chunked_docs)
        target = HybridLangChainRetriever(hybrid=hybrid, k=k, multi_query=multi_query)
        if multi_query:
            _retriever_multi_singleton = target
        else:
            _retriever_singleton = target
    return target

_chain = None
_chain_multi = None
def get_chain(multi_query: bool = False):
    """v2 LCEL RAG chain
    - 第 1 次调才初始化（import 不触发）
    - 后续调复用单例
    """
    global _chain, _chain_multi
    target_chain = _chain_multi if multi_query else _chain
    if target_chain is None:
        retriever = get_retriever(k=5, multi_query=multi_query)
        # 提示词
        prompt = build_prompt(strict_mode= not multi_query)
        # 拒答分支（不调 LLM，直接返拒答 dict）
        refuse_branch = RunnableLambda(lambda x: {
            "answer": "知识库未收录相关内容，无法回答这个问题",
            "has_answer": False,
            "citations": [],
            "contexts": []
        })
        # 回答分支
        parser = JsonOutputParser()
        # RunnableParallel 双流
        answer_branch = (
            RunnableParallel(
                # 流 1：调 LLM 生成答案（只取 parsed）
                parsed=(
                    RunnableLambda(lambda x: {"context": format_docs(x["docs"]), "question": x["question"]})
                    | prompt
                    | llm
                    | RunnableLambda(lambda msg: safe_parse(msg.content, parser))  # parser
                ),
                # 流 2：原样透传整个 dict（保留 docs）
                original=RunnablePassthrough()
            )
            # "最后合并"（parsed 拿答案 + original 拿 docs 提 citations/contexts）
            | RunnableLambda(lambda x: {
                # 拒绝话术一致
                "answer": "知识库未收录相关内容，无法回答这个问题" if not x["parsed"].get("has_answer", True) else x["parsed"].get("answer", ""),
                "has_answer": x["parsed"].get("has_answer", True),
                # list(set([doc.metadatset...也行，但 set 去重会乱序，dict.fromkeys 保序
                "citations": [] if not x["parsed"].get("has_answer", True) else list(dict.fromkeys([doc.metadata.get("source", "?") for doc in x["original"]["docs"]])),
                "contexts": [ doc.page_content for doc in x["original"]["docs"]]
            })
        )
        # LCEL 链拼装
        # RunnableParallel 拿 docs（走 retriever）+ question（RunnablePassthrough）
        parallel = RunnableParallel({
            "docs": retriever,
            "question": RunnablePassthrough() # 透传输入的 question 字段
        })
        # RunnableLambda(check_no_relevant) 走拒答检查
        check = RunnableLambda(check_no_relevant)
        # RunnableBranch，当 refuse=True 时走 refuse_branch（拒答短路），否则走 answer_branch
        branch = RunnableBranch(
            (lambda x: x["refuse"], refuse_branch),  # 条件为 True 时走拒答
            answer_branch                            # 默认分支
        )
        target_chain = parallel | check | branch
    return target_chain


if __name__ == "__main__":
    chain = get_chain()
    # result = chain.invoke("XSS 防御？")
    # print(f"XSS 防御？\n 答：{result['answer']}")
    result = chain.invoke("什么是DNS隧道攻击？")
    print(f"什么是DNS隧道攻击？\n 答：{result['answer']}")