# rag/loader.py
from pathlib import Path
from typing import List, Dict

def load_all_markdown(root_dir: Path) -> List[Dict]:
    """遍历 root_dir 下所有 *.md，返回 [{"text": content, "source": rel_path}, ...]"""
    documents = []
    # rglob 递归匹配所有 .md 文件
    for md_path in root_dir.rglob("*.md"):
        # 读取文件内容，utf-8 编码
        with open(md_path, "r", encoding="utf-8") as f:
            text = f.read()
        # 算相对路径（相对于 root_dir）
        rel_path = md_path.relative_to(root_dir)
        documents.append({
            "text": text,
            "source": str(rel_path),
        })
    return documents

if __name__ == "__main__":
    # 测试：项目根目录就是 __file__ 的上级上级
    PROJECT_ROOT = Path(__file__).parent.parent
    docs = load_all_markdown(PROJECT_ROOT / "knowledge")
    print(f"加载了 {len(docs)} 篇笔记")
    for doc in docs[:5]:
        print(f"- {doc['source']}: {len(doc['text'])} 字符")
