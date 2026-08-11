import re
import tiktoken
from typing import List, Dict
from rag.loader import load_all_markdown
from pathlib import Path

DEFAULT_CHUNK_SIZE = 256
# overlap 的本质是：让相邻 chunk 共享边界内容，防止语义在切分点断裂。 
# 不是为了拼接还原原文。
DEFAULT_CHUNK_OVERLAP = 100  
CONTEXT_BUFFER_TOKENS = 50000 # 预警缓冲

class MarkdownChunker:
    def __init__(self, chunk_size=DEFAULT_CHUNK_SIZE, chunk_overlap=DEFAULT_CHUNK_OVERLAP,
                encoding_name="cl100k_base", context_buffer=CONTEXT_BUFFER_TOKENS):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.encoding = tiktoken.get_encoding(encoding_name)
        self.context_buffer = context_buffer
        
    def count_tokens(self, text: str) -> int:
        """统计文本 token 数"""
        return len(self.encoding.encode(text)) #encoding 对象不是函数，要调用 .encode() 得到 token ids，再用 len 数个数
    
    def check_context_overflow(self, retrieved_tokens: int, max_model_tokens: int) -> bool:
        """预警：检索结果 + buffer 是否超出模型窗口"""
        if retrieved_tokens > max_model_tokens - self.context_buffer:
            return True
        else:
            return False
    
    def split_by_heading(self, text: str) -> List[Dict]:
        """按 ## 标题切分 markdown，返回 [{"heading": ..., "content": ..., "tokens": ...}, ...]"""
        parts = re.split(r'\n(?=## )', text)
        sections = []
        # 第一部分（如果没有以 ## 开头）是无标题的前言
        if parts[0].strip():
            sections.append({
                "heading": None,
                "content": parts[0].strip(),
                "tokens": self.count_tokens(parts[0].strip())
            })
        # 从第二部分开始，每个元素就是 "标题\n正文"
        for part in parts[1:]:
            lines = part.splitlines()
            head = lines[0].strip() if lines else ""
            cont = "\n".join(lines[1:]).strip()
            sections.append({
                "heading": head,
                "content": cont,
                "tokens": self.count_tokens(part)
            })
            
        return sections    
    
    def split_long_section(self, text: str) -> List[str]:
        """
        如果单个文本已经超过 chunk_size，按 tokens 切分成多个固定大小的块，带 overlap
        """
        tokens = self.encoding.encode(text)
        if len(tokens) <= self.chunk_size:
            return [text]
        
        chunks = []
        start = 0
        while start < len(tokens):
            end = start + self.chunk_size
            chunk_tokens = tokens[start:end]
            chunk_text = self.encoding.decode(chunk_tokens)
            chunks.append(chunk_text)
            start += self.chunk_size - self.chunk_overlap
        return chunks

    def merge_small_sections(self, sections: List[Dict]) -> List[str]:
        """
        将多个 section（按 ## 标题切分好的）合并为文本块，每块长度接近 chunk_size，
        块与块之间带 overlap（字符数）。如果单个小节超长，先拆分它。
        sections: [{"heading": "标题", "content": "正文内容", ...}, ...]
        返回: List[str] 每个元素是一个完整的文本块
        """
        chunks = []
        current_chunk = ""

        for sec in sections:
            # 拼出当前 section 的完整文本
            heading = sec.get('heading', '').strip() if sec.get('heading') else ""
            content = sec.get('content', '').strip()
            text = f"{heading}\n{content}" if heading else content
            
            # 如果当前小节本身就超长，直接拆分，不参与合并
            if self.count_tokens(text) > self.chunk_size:
                # 先把当前累积块保存
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                # 拆分超长小节，直接加入结果
                long_chunks = self.split_long_section(text)
                chunks.extend(long_chunks)
                continue
            
            # 如果当前块是空的，直接用这个 text 初始
            if not current_chunk:
                current_chunk = text
                continue
            
            # 尝试拼接，不超就合并
            candidate = current_chunk + "\n\n" + text
            if self.count_tokens(candidate) <= self.chunk_size:
                current_chunk = candidate
            else:
                # 超了，保存当前块，切 overlap 开新块
                chunks.append(current_chunk)
                all_tokens = self.encoding.encode(current_chunk)
                overlap_tokens = all_tokens[-self.chunk_overlap:] if self.chunk_overlap > 0 and len(all_tokens) >= self.chunk_overlap else []
                overlap_text = self.encoding.decode(overlap_tokens)
                current_chunk = overlap_text + "\n\n" + text if overlap_text else text
        
        # 最后剩下的保存
        if current_chunk:
            chunks.append(current_chunk)
        return chunks
            
    def chunk_document(self, text: str) -> List[str]:
        """完整流程：按标题分节 -> 拆分超长小节 -> 合并小节"""
        sections = self.split_by_heading(text)
        return self.merge_small_sections(sections)
                
                
    def process_documents(self, documents: List[Dict]) -> List[Dict]:
        """处理 loader 返回的文档，切分后带上 source"""
        list = []
        for doc in documents:
            chunks = self.chunk_document(doc["text"])
            list.append({
                "source": doc["source"],
                "chunk_index": len(chunks),
                "chunk_list": chunks
            })
        return list 
    
if __name__ == "__main__":
    PROJECT_ROOT = Path(__file__).parent.parent
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    obj = MarkdownChunker()
    result = obj.process_documents(docs)
    # print("process_documents的结果:\n" + json.dumps(result, indent=2))
    total_chunks = sum(doc["chunk_index"] for doc in result)
    print(f"原始文档数: {len(result)}, 总切分 chunk 数: {total_chunks}")
            
 