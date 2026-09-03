"""增量 JSON 信封剥离器"""

class JsonEnvelopeStripper:
    """流式 JSON 信封剥离器，按状态机处理
    数据结构： ['{"has_answer": true/false, "answer": "..."}',...]"""
    OVERFLOW_LIMIT = 4096 # 超限降级
    
    def __init__(self):
        # 公共状态
        self._verdict = None   # None / "refuse" / "fallback"
        self._closed = False
        # 内部状态
        self._state = 'S0'     # 'S0' | 'S1' | 'DONE'
        # 缓冲区（始终保留尚未处理的部分）
        self._buffer = ''
        self._pos = 0 # 下一个要处理的字符索引（相对于 _buffer）
        # S1 状态
        self._in_string = False       # 当前是否在 answer 字符串内部
        self._escape = False          # 上一个字符是否为反斜杠（跨 chunk）
        self._unicode_mode = False    # 是否正在收集 \uXXXX
        self._unicode_hex = []        # 已收集的十六进制数字（最多 4 个）
        self._answer_ended = False    # answer 字符串是否已结束（遇到未转义引号）
        self._found_close_brace = False  # 是否已遇到闭合 '}'
    
    # 公共属性
    @property
    def verdict(self) -> str | None:
        """None=未决 / "refuse" / "fallback"（S0 两个出口）"""
        return self._verdict
    @property
    def closed(self) -> bool:
        """S1 遇 "} 闭合 → True；流耗尽仍 False = 异常，调用方兜底"""
        return self._closed
    # 去空白
    def _skip_whitespace(self, pos: int) -> int:
        """从 pos 开始跳过空白字符（空格、制表符、换行、回车），返回第一个非空白字符的位置"""
        n = len(self._buffer)
        while pos < n and self._buffer[pos] in ' \t\n\r':
            pos += 1
        return pos
    
    def _trim_buffer(self):
        """丢弃已处理的部分，保留未处理的部分"""
        if self._pos > 0:
            self._buffer = self._buffer[self._pos:]
            self._pos = 0
    
    def _terminate(self, verdict: str):
        """终止处理，设置 verdict 并进入 DONE"""
        self._state = 'DONE'
        if verdict == 'closed':
            self._closed = True
        else:
            self._verdict = verdict
            self._closed = False
        self._buffer = ''  # 丢弃剩余 buffer
        self._pos = 0
        
    # S0 处理
    def _find_has_answer(self):
        """从 self._buffer 中提取 has_answer 的值，返回 True/False/None"""
        idx = self._buffer.find('"has_answer"')
        if idx == -1 or idx >= self.OVERFLOW_LIMIT:
            return None
        pos = idx + len('"has_answer"')
        # 跳过空白
        pos = self._skip_whitespace(pos)
        if pos >= len(self._buffer) or self._buffer[pos] != ':':
            return None
        pos += 1
        if pos >= len(self._buffer):
            return None
        pos = self._skip_whitespace(pos)
        # 匹配 true / false
        if self._buffer.startswith('true', pos):
            end = pos + 4
            if end == len(self._buffer) or self._buffer[end] in " ,}\t\n\r":
                return True
            return None
        if self._buffer.startswith('false', pos):
            end = pos + 5
            if end == len(self._buffer) or self._buffer[end] in " ,}\t\n\r":
                return False
            return None
        return None
    
    def _find_answer_start(self):
        """返回 buffer 中 answer 字符串值第一个字符的索引（引号之后），若未找到返回 None"""
        idx = self._buffer.find('"answer"')
        if idx == -1 or idx >= self.OVERFLOW_LIMIT:
            return None
        pos = idx + len('"answer"')
        pos = self._skip_whitespace(pos)
        if pos >= len(self._buffer) or self._buffer[pos] != ':':
            return None
        pos += 1
        if pos >= len(self._buffer):
            return None
        pos = self._skip_whitespace(pos)
        if pos >= len(self._buffer) or self._buffer[pos] != '"':
            return None
        return pos + 1
        
    def _process_S0(self):
        """在 S0 中尝试判定 has_answer 并定位 answer 内容起始"""
        if not hasattr(self, '_has_answer_checked'):
            # 先找 has_answer 的值
            has_answer = self._find_has_answer()
            if has_answer is None:
                if len(self._buffer) >= self.OVERFLOW_LIMIT:
                    self._terminate('fallback')
                return
            self._has_answer_checked = True
            if has_answer is False:
                self._terminate('refuse')
                return
        # 此时 has_answer 为 True，寻找 answer 起始
        start = self._find_answer_start()
        if start is None:
            if len(self._buffer) >= self.OVERFLOW_LIMIT:
                self._terminate('fallback')
            return
        
        # 进入 S1 状态
        self._state = 'S1'
        self._pos = 0
        self._buffer = self._buffer[start:]  # 丢弃前面无用部分
        self._in_string = True
        self._answer_ended = False
        self._found_close_brace = False
        
    # S1 处理
    def _process_S1(self) -> list[str]:
        """在 S1 中处理 answer 字符串，返回可发射的文本块"""
        emitted = []
        i = self._pos
        n = len(self._buffer)

        if self._found_close_brace:
            # 已经遇到闭合 '}'，不再处理
            self._terminate('closed')
            return emitted
        
        while i < n:
            c = self._buffer[i]
            # 如果 answer 已结束，寻找闭合 }
            if self._answer_ended and not self._in_string:
                if c == '}':
                    self._found_close_brace = True
                    self._terminate('closed')
                    self._pos = i + 1
                    self._trim_buffer()
                    return emitted
                i += 1
                continue
            # 字符串内部处理
            if not self._in_string:
                i += 1
                continue
            # Unicode 收集模式
            if self._unicode_mode:
                self._unicode_hex.append(c)
                if len(self._unicode_hex) == 4:
                    try:
                        emitted.append(chr(int(''.join(self._unicode_hex), 16)))
                    except ValueError:
                        emitted.append('\\u' + ''.join(self._unicode_hex))
                    self._unicode_mode = False
                    self._unicode_hex = []
                i += 1
                continue
            # 转义处理
            if self._escape:
                self._escape = False
                if c == 'u':
                    self._unicode_mode = True
                    self._unicode_hex = []
                elif c == 'n':
                    emitted.append('\n')
                elif c == 't':
                    emitted.append('\t')
                elif c == '"':
                    emitted.append('"')
                elif c == '\\':
                    emitted.append('\\')    
                else:
                    emitted.append('\\' + c)
                i += 1
                continue
            # 普通字符处理
            if c == '\\':
                self._escape = True
                i += 1
                continue
            if c == '"':
                self._in_string = False
                self._answer_ended = True
                i += 1
                continue
            # 正常发射普通字符
            emitted.append(c)
            i += 1
        self._pos = i
        self._trim_buffer()
        return emitted
    
    # 主入口     
    def feed(self, chunk: str) -> list[str]:
        """喂一个 chunk → 返回本次可发射的已还原文本（可为空列表）"""
        if self._state == "DONE":
            return []
        
        self._buffer += chunk
        if self._state == "S0":
            self._process_S0()
        if self._state == "S1":
            return self._process_S1()
        
        return [] # 如果处理过程中进入 DONE，feed 会直接返回空，兜底
    
if __name__ == '__main__':
    def run_test(chunks, expected_output=None, expected_verdict=None, desc=''):
        stripper = JsonEnvelopeStripper()
        output_parts  = []
        for chunk in chunks:
            emitted = stripper.feed(chunk)
            output_parts .extend(emitted)
        output = ''.join(output_parts)
        closed = stripper.closed
        verdict = stripper.verdict
        ok = True
        if expected_output is not None and output != expected_output:
            ok = False
            print(f"❌ {desc} 输出不匹配: {repr(output)} != {repr(expected_output)}")
        if expected_verdict is not None:
            if expected_verdict == 'closed':
                if not closed:
                    ok = False
                    print(f"❌ {desc} 未正常闭合")
            else:
                if verdict != expected_verdict:
                    ok = False
                    print(f"❌ {desc} verdict 不匹配: {verdict} != {expected_verdict}")
        if ok:
            print(f"✅ {desc} 通过")
            
    run_test(['{"has_answer": true, "answer": "hello world"}'], "hello world", "closed", "1. 单chunk happy path")
    run_test(['{"has_ans', 'wer": true, "answer": "hello world"}'], "hello world", "closed", "2. JSON拆两半")
    run_test(['{"has_answer": true, "answer": "line1\\nline2"}'], "line1\nline2", "closed", "3. \\n还原")
    run_test(['{"has_answer": true, "answer": "苦涩的\\u00', 'e5海风"}'], "苦涩的\u00e5海风", "closed", "4. \\u切中间")
    run_test(['{"has_answer": false, "answer": "should not see"}'], "", "refuse", "5. has_answer false")
    run_test(['根据上下文XX： {"has_answer": true, "answer": "answer"}'], "answer", "closed", "6. 前缀废话")
    prefix = 'x' * 5000 + '=' * 100
    run_test([prefix + '{"has_answer": true, "answer": "answer"}'], "", "fallback", "7. 废话超4KB fallback")
    run_test(['{"has_answer": true, "answer": "\\\\n"}'], "\\n", "closed", "8. \\\\n不是换行")
    run_test(['{"has_answer": true, "answer": "hello"} 康师傅电话kkl }'], "hello", "closed", "9. 尾部多余字段丢弃")
    run_test(['{"has_answer": true, "answer": "line1\\', 'nline2"}'], "line1\nline2", "closed", "10. 转义切边界")