"""
输出卫生
实跑发现 B3/C1/C2 的 final_answer 暴露：
- 工具列表（query_ip_reputation / search_knowledge / parse_log_fields）
- 系统路径（J:\programs\secops-copilot\.env）
- 系统命令（cat / type / Get-Content）
【设计决策】：
  Q1：单文件（可复用 + 可单测 + 关注点分离）
  Q2：静默替换（不附加提示，对标 OpenAI 安全过滤）
  Q3：黑名单（精确字典替换，不做白名单）
【黑名单三组】：
  - 工具名  →  占位符"某个内部工具"
  - 系统路径  →  占位符"<系统路径>"
  - 系统命令  →  占位符"<系统命令>"
【双保险范式】（prompt 软约束 + 代码层 post-check）：
  本文件是"代码层 post-check"——已加 SYSTEM prompt 约束不生效，代码兜底。
【不破 trace】：
  filter 在 yield final_answer 之前做，tracer 仍能记录原 msg.content
  （让 Langfuse Dashboard 看到"原输出"+"过滤后输出"对照）
"""
import re
import sys
from typing import Tuple, List

sys.stdout.reconfigure(encoding="utf-8")
# 占位符
PLACEHOLDER_TOOL = "[工具占位符]"
PLACEHOLDER_PATH = "[路径占位符]"
PLACEHOLDER_CMD = "[命令占位符]"
# 三个黑名单字典（精确匹配）
TOOL_NAME_MAP = {
    # 工具名 → 占位符
    "query_ip_reputation": PLACEHOLDER_TOOL,
    "search_knowledge": PLACEHOLDER_TOOL,
    "parse_log_fields": PLACEHOLDER_TOOL,
}
# Windows 绝对路径模式 ----------
# J:\..., C:\..., D:\... 一直到 Z:\...
# 路径字符允许：字母数字 . _ - ，遇到 \ + 字母或空格就停
# 已知限制：中文标点（、，；。）不在排除列表，贪婪会吞 cat/命令
# 例：J:\programs\.env，cat  →  路径 regex 会吞到 "cat" 前空格，整段被路径占位符替换
# 严重度低：LLM 实际极少同时输出"路径+中文标点+命令"
PATH_PATTERN = re.compile(r"[A-Za-z]:\\(?:[^\s\"'<>|*?\r\n\\])+")
# 系统命令（带单词边界，仅匹配完整命令名） ----------
# 注意：cat/type 在英文里可能是动词，必须用 \b 单词边界
# 但仅在 Windows 命令语境下替换：前面的字符是行首/空白/标点，且后面跟 空白 + 文件名
COMMAND_PATTERN = [
    # 严格化：cat/type 必须"前面是空白/行首 + 后面跟空格 + 文件名"才算
    # 提示：cat/type 在英文里是动词，命令语境是"cat + 空格 + 文件路径/文件名"
    # 文件名特征：以 .txt/.env/.log 结尾，或纯字母数字无空格
    r"\bcat\s+[\w./]+\.\w+\b",   
    r"\btype\s+[\w./]+\.\w+\b",      
    r"\bGet-Content\b",  # PowerShell
    r"\brm\s+-rf\b",     # 强制删除
    r"\bdel\s+/[sqf]\b", # Windows 删除（/s /q /f）
    r"\bchmod\s+777\b",  # 危险权限
    r"\bsudo\s+", 
]
# 编译所有 pattern
COMMAND_PATTERNS = [re.compile(p) for p in COMMAND_PATTERN] 
# 工具名 pattern 列表（用 \b 单词边界）
TOOL_PATTERNS = [re.compile(rf"\b{re.escape(name)}\b") for name in TOOL_NAME_MAP]  

def sanitize_output(text: str) -> tuple[str, list[str]]:
    """输出卫生：替换 final_answer 中的工具名/系统路径/系统命令为占位符"""
    if not text:
        return text, []
    
    safe_text = text  # 复制原始文本，后续逐步替换
    sanitized_terms: List[str] = [] # 存放所有被替换掉的原始子串（去重）
    seen = set()  # 函数内局部变量,性能优化
    # 工具名替换
    # 遍历每个正则模式（编译后的 Pattern 对象）
    for pattern in TOOL_PATTERNS:
        # 定义替换回调函数，会在每次匹配时被调用
        # 使用默认参数 _p=pattern 来“捕获”当前循环的 pattern 值，
        # 避免闭包延迟绑定导致所有回调都使用最后一个 pattern
        def _replace_tool(m, _p=pattern):
            term = m.group(0)  # 获取本次匹配到的完整子串
        # 如果该子串还没记录过，则加入列表（去重）
            if term not in seen:
                seen.add(term)
                sanitized_terms.append(term)
            return PLACEHOLDER_TOOL # 所有匹配统一替换为占位符

        # 在当前 safe_text 上执行替换，将当前 pattern 匹配的所有内容
        # 替换为 _replace_tool 的返回值（即占位符）
        safe_text = pattern.sub(_replace_tool, safe_text)    
    # 循环结束后：
    # - safe_text 中所有匹配任意模式的部分都被替换成了 PLACEHOLDER_TOOL
    # - sanitized_terms 包含了所有被匹配到的不同原始子串（按首次出现顺序）
   
    # Windows 绝对路径
    def _replace_path(m):
        term = m.group(0)
        if term not in seen:
            seen.add(term)
            sanitized_terms.append(term)
        return PLACEHOLDER_PATH
    safe_text = PATH_PATTERN.sub(_replace_path, safe_text)
    
    # 系统命令
    for pattern in COMMAND_PATTERNS:
        def _replace_command(m, _p=pattern):
            term = m.group(0)
            if term not in seen:
                seen.add(term)
                sanitized_terms.append(term)
            return PLACEHOLDER_CMD
        safe_text = pattern.sub(_replace_command, safe_text)    
    
    return safe_text, sanitized_terms

if __name__ == "__main__":
    test_cases = [
        # 应该被清洗
        ("请用 query_ip_reputation 工具查询", "[工具占位符]"),
        ("J:\\programs\\secops-copilot\\.env 里有", "[路径占位符]"),
        ("用 cat 查看文件内容", "[命令占位符]"),
        ("用 type 文件.txt", "[命令占位符]"),
        # 不应该被误伤
        ("我输入 SQL 注入", "我输入 SQL 注入"),
        ("I have a cat at home", "I have a cat at home"),  # 英文 cat 是名词
        ("search_knowledgeable 是形容词", "search_knowledgeable 是形容词"),  # 子字符串不匹配
    ]
    for raw, _ in test_cases:
        cleaned, terms = sanitize_output(raw)
        print(f"原: {raw!r}")
        print(f"清: {cleaned!r}")
        print(f"替: {terms}")
        print()
