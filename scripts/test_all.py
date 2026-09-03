"""一键跑全部测试（v1 + v2 + history）"""
import subprocess
import sys
from pathlib import Path

TEST_DIR = Path(__file__).parent

# 3 个测试脚本（按顺序）
TESTS = [
    "test_v1.py",
    "test_v2.py",
    "test_history.py",
]


def run_test(script_name: str) -> int:
    """跑一个测试脚本"""
    script_path = TEST_DIR / script_name
    print()
    print("#" * 70)
    print(f"# 开始跑: {script_name}")
    print("#" * 70)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=False,  # 实时输出
        text=True,
    )

    print()
    print(f"# {script_name} 退出码: {result.returncode}")
    return result.returncode


def main():
    print("=" * 70)
    print("# SecOps Copilot 一键测试")
    print("# 前提：服务已启动（uv run uvicorn app.main:app --reload --port 8000）")
    print("=" * 70)

    failed = []
    for test in TESTS:
        rc = run_test(test)
        if rc != 0:
            failed.append(test)

    print()
    print("=" * 70)
    print("# 全部测试完成")
    if failed:
        print(f"# 失败: {failed}")
    else:
        print("# 全部通过 ✅")
    print("=" * 70)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
