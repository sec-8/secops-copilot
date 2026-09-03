# 懒加载单例
import sys
from pathlib import Path
# 让脚本能 import 到项目里的 rag / app 包
sys.stdout.reconfigure(encoding="utf-8")  # 防 Windows GBK emoji 崩
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from rag.vector_store import LanceDBVectorStore

_memory_store = None

def get_memory_store():
    global _memory_store
    db_path = PROJECT_ROOT / "data" / "lancedb"
    db_path.mkdir(parents=True, exist_ok=True)
    if _memory_store is None:
        """懒加载 memory_store 单例：第一次调用才构建（重活只干一次）"""
        # 实例化（表名 = secops_memory）
        _memory_store = LanceDBVectorStore(
            db_path=db_path,
            table_name="secops_memory" 
        )
    return _memory_store