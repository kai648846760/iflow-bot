"""Tests for QQ channel streaming split_threshold feature.

核心测试：模拟 loop.py 中 on_chunk 的 QQ 分支（行级缓冲版本）
- 使用 qq_line_buffer + while "\\n" 循环提取完整行
- 代码块内换行符不计入阈值（且 ``` 可以跨 chunk 分割）
- strip() 去除首尾多余空行
- 余量 = qq_segment_buffer + qq_line_buffer
"""

import asyncio
import pytest

from iflow_bot.config.schema import QQConfig


# ---------------------------------------------------------------------------
# 核心辅助：模拟 loop.py on_chunk 中 QQ 分段逻辑（行级缓冲，最终版）
# ---------------------------------------------------------------------------

async def simulate_streaming_chunks(
    chunks: list[str],
    threshold: int,
    record_calls: list | None = None,
) -> list[str]:
    """
    模拟流式 on_chunk 调用（行级缓冲版）。
    返回每次 send 推送的 segment 列表（包括末尾补发的剩余）。
    """
    sent_segments: list[str] = []
    qq_segment_buffer = ""
    qq_line_buffer = ""
    qq_newline_count = 0
    qq_in_code_block = False

    def _record(content: str):
        if record_calls is not None:
            record_calls.append(content)

    async def _send(segment: str):
        sent_segments.append(segment)
        _record(segment)

    # on_chunk 逻辑
    for chunk_text in chunks:
        if threshold > 0:
            qq_line_buffer += chunk_text
            while "\n" in qq_line_buffer:
                idx = qq_line_buffer.index("\n")
                complete_line = qq_line_buffer[:idx]
                qq_line_buffer = qq_line_buffer[idx + 1:]

                if complete_line.strip().startswith("```"):
                    qq_in_code_block = not qq_in_code_block

                qq_segment_buffer += complete_line + "\n"

                if not qq_in_code_block:
                    qq_newline_count += 1
                    if qq_newline_count >= threshold:
                        segment = qq_segment_buffer.strip()
                        qq_segment_buffer = ""
                        qq_newline_count = 0
                        if segment:
                            await _send(segment)

    # final 处理（模拟 final_content 块，QQ 现在在 if final_content: 外面）
    if threshold <= 0:
        full_content = "".join(chunks).strip()
        if full_content:
            await _send(full_content)
    else:
        remainder = (qq_segment_buffer + qq_line_buffer).strip()
        if remainder:
            await _send(remainder)

    return sent_segments


# ---------------------------------------------------------------------------
# 测试：QQConfig 字段
# ---------------------------------------------------------------------------

class TestQQConfigSchema:
    def test_default_split_threshold_is_zero(self):
        assert QQConfig().split_threshold == 0

    def test_custom_split_threshold(self):
        assert QQConfig(split_threshold=2).split_threshold == 2

    def test_large_threshold(self):
        assert QQConfig(split_threshold=100).split_threshold == 100


# ---------------------------------------------------------------------------
# 测试：threshold = 0（不分段）
# ---------------------------------------------------------------------------

class TestNoSplitThreshold0:
    def test_single_chunk(self):
        result = asyncio.run(simulate_streaming_chunks(["Hello World"], 0))
        assert result == ["Hello World"]

    def test_multiple_chunks_joined_and_stripped(self):
        chunks = ["  \nHello ", "World\n", "End\n  "]
        result = asyncio.run(simulate_streaming_chunks(chunks, 0))
        assert len(result) == 1
        assert result[0] == "Hello World\nEnd"

    def test_many_newlines_no_split(self):
        result = asyncio.run(simulate_streaming_chunks(["A\n", "B\n", "C\n"], 0))
        assert len(result) == 1
        assert result[0] == "A\nB\nC"


# ---------------------------------------------------------------------------
# 测试：threshold = 1
# ---------------------------------------------------------------------------

class TestThreshold1:
    def test_single_newline_triggers_push(self):
        result = asyncio.run(simulate_streaming_chunks(["Hello\n", "World"], 1))
        assert result[0] == "Hello"
        assert result[1] == "World"

    def test_multiple_newlines_in_one_chunk(self):
        result = asyncio.run(simulate_streaming_chunks(["A\nB\nC\n"], 1))
        assert result == ["A", "B", "C"]

    def test_no_newline_accumulates_to_remainder(self):
        result = asyncio.run(simulate_streaming_chunks(["Hello", " World!"], 1))
        assert result == ["Hello World!"]


# ---------------------------------------------------------------------------
# 测试：threshold = 2
# ---------------------------------------------------------------------------

class TestThreshold2:
    def test_two_newlines_trigger(self):
        result = asyncio.run(simulate_streaming_chunks(["A\nB\nC\nD\n"], 2))
        assert result == ["A\nB", "C\nD"]

    def test_content_after_trigger_goes_to_next_segment(self):
        result = asyncio.run(simulate_streaming_chunks(["Hello\nWorld\nExtra"], 2))
        assert result[0] == "Hello\nWorld"
        assert result[1] == "Extra"

    def test_trailing_newlines_stripped(self):
        result = asyncio.run(simulate_streaming_chunks(["Line1\n", "Line2\n", "End"], 2))
        assert result[0] == "Line1\nLine2"
        assert result[1] == "End"

    def test_leading_newlines_stripped(self):
        result = asyncio.run(simulate_streaming_chunks(["A\nB\n", "\n\nC\nD\n", "End"], 2))
        assert result[0] == "A\nB"
        assert "C\nD" in result[1]


# ---------------------------------------------------------------------------
# 测试：行级缓冲 - ``` 跨 chunk 分割（问题1 的核心修复）
# ---------------------------------------------------------------------------

class TestCodeBlockCrossChunk:
    def test_backticks_split_across_chunks(self):
        """
        ``` 被分割到多个 chunk 时，行级缓冲确保完整行拼接后再检测。
        chunk1 = "``"，chunk2 = "`\ncode\n```\n\nEnd"
        旧代码：chunk2 的行是 "`" (1个反引号) → 不识别为 ``` → BUG
        新代码：qq_line_buffer 拼接后 = "```\ncode\n..." → 正确识别
        """
        chunks = ["``", "`\n", "code line\n", "```\n", "\nEnd"]
        result = asyncio.run(simulate_streaming_chunks(chunks, 2))
        # 代码块内 "code line\n" 不计数
        # 关闭 ``` 后 \n 计数=1，再 \n 计数=2 → 触发
        full = " ".join(result)
        assert "code line" in full
        assert "End" in full
        # 代码块内容和 End 不应该分开（关键：code line 不因 \n 触发新段）
        # 至少代码块内容不被单独推出
        for seg in result:
            # 每段要么包含代码块内容，要么是末尾的 End
            assert seg.strip()  # 没有纯空白段

    def test_backticks_split_two_chunks(self):
        """``` 被分到连续两个 chunk：chunk1 = "``", chunk2 = "`\n" """
        chunks = ["``", "`\n", "inside code\n", "more code\n", "```\n", "After\n"]
        result = asyncio.run(simulate_streaming_chunks(chunks, 2))
        full_content = "".join(result)
        assert "inside code" in full_content
        assert "more code" in full_content
        assert "After" in full_content
        # 代码块内的两行不触发分段
        # 关闭 ``` 后 \n 计1，"After\n" 计2 → 触发
        # 所以代码块内容和 After 应该在同一段
        code_and_after_in_same = any("inside code" in s and "After" in s for s in result)
        assert code_and_after_in_same

    def test_code_block_newlines_not_counted_line_buffer(self):
        """单行级缓冲：代码块内换行符不计入阈值"""
        chunks = [
            "Intro\n\n",           # 2 \n → 触发第一段
            "```python\n",         # 进入代码块
            "def foo():\n",        # 代码块内，不计
            "    return 1\n",      # 代码块内，不计
            "```\n",               # 退出，计1
            "Outro\n",             # 计2 → 触发第二段
        ]
        result = asyncio.run(simulate_streaming_chunks(chunks, 2))
        assert len(result) == 2
        assert "Intro" in result[0]
        assert "```python" in result[1]
        assert "def foo" in result[1]
        assert "Outro" in result[1]

    def test_code_block_content_not_split(self):
        """代码块内即使有很多换行符，也不被分段"""
        chunks = [
            "```\n",
            "line1\n" * 10,  # 10 个换行符，全在代码块内
            "```\n",
            "\nEnd"
        ]
        result = asyncio.run(simulate_streaming_chunks(chunks, 2))
        full = "".join(result)
        # 所有 line1 都在同一个内容流中，不被拆分
        assert full.count("line1") == 10


# ---------------------------------------------------------------------------
# 测试：problem2 - qq_line_buffer 的余量包含在最终发送中
# ---------------------------------------------------------------------------

class TestLineBufferRemainder:
    def test_incomplete_last_line_included_in_remainder(self):
        """最后一行没有 \\n 时，qq_line_buffer 的内容作为余量发出"""
        # "Hello\nWorld" → \n 之前 "Hello" 触发（若 threshold=1）
        # "World" 无 \n → 在 qq_line_buffer → 作为余量发出
        result = asyncio.run(simulate_streaming_chunks(["Hello\n", "World"], 1))
        assert result[0] == "Hello"
        assert result[1] == "World"  # 来自 qq_line_buffer

    def test_all_content_in_line_buffer_no_newline(self):
        """全程没有 \n，内容全在 qq_line_buffer，最终作为一段发出"""
        result = asyncio.run(simulate_streaming_chunks(
            ["Hello", " beautiful", " World"], 2
        ))
        assert result == ["Hello beautiful World"]

    def test_line_buffer_content_stripped(self):
        """qq_line_buffer 中的内容 strip() 后才发出"""
        result = asyncio.run(simulate_streaming_chunks(["  spaces  "], 1))
        assert result == ["spaces"]


# ---------------------------------------------------------------------------
# 测试：Bug2 - Recorder 记录验证
# ---------------------------------------------------------------------------

class TestRecorderCalls:
    def test_threshold0_records_once(self):
        records = []
        asyncio.run(simulate_streaming_chunks(["Hello\nWorld\nEnd"], 0, records))
        assert len(records) == 1

    def test_threshold1_records_each_segment(self):
        records = []
        asyncio.run(simulate_streaming_chunks(["A\nB\nC"], 1, records))
        assert len(records) == 3
        assert records == ["A", "B", "C"]

    def test_no_whitespace_recorded(self):
        records = []
        asyncio.run(simulate_streaming_chunks(["\n\n\n"], 1, records))
        assert records == []


# ---------------------------------------------------------------------------
# 测试：边界情况
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_chunks(self):
        assert asyncio.run(simulate_streaming_chunks([], 1)) == []

    def test_all_whitespace_not_sent(self):
        result = asyncio.run(simulate_streaming_chunks(["\n\n\n"], 1))
        assert result == []

    def test_large_threshold(self):
        result = asyncio.run(simulate_streaming_chunks(["A\n" * 10], 100))
        assert len(result) == 1

    def test_exact_boundary_stripped(self):
        result = asyncio.run(simulate_streaming_chunks(["A\nB\n"], 2))
        assert result == ["A\nB"]  # 尾部 \n 被 strip
