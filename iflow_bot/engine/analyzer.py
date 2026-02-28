"""Result Analyzer - Smart analysis of iflow CLI output.

Ported from feishu-iflow-bridge/src/modules/ResultAnalyzer.js
Original author: Wuguoshuo

Features:
- Extract NEXT_PHASE from output
- Detect completion/error/input-needed states
- Extract file paths (images, audio, video, docs)
- Calculate confidence for continuation
- Determine if human intervention is needed
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


# File extension categories
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".ogg", ".flac", ".m4a", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".webm"}
DOC_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt", ".csv", ".md"}

# File path pattern
FILE_PATH_PATTERN = re.compile(r"(?:[a-zA-Z]:\\|/)?[\w\-\\/\.]+\.\w+")


@dataclass
class AnalysisResult:
    """Result of analyzing iflow CLI output."""
    can_continue: bool = False
    next_phase: Optional[str] = None
    is_complete: bool = False
    has_error: bool = False
    needs_input: bool = False
    has_progress: bool = False
    summary: str = ""
    confidence: float = 0.0
    # Extracted files by category
    image_files: list[str] = field(default_factory=list)
    audio_files: list[str] = field(default_factory=list)
    video_files: list[str] = field(default_factory=list)
    doc_files: list[str] = field(default_factory=list)
    all_files: list[str] = field(default_factory=list)


class ResultAnalyzer:
    """Analyzes iflow CLI execution output for actionable insights."""

    # NEXT_PHASE patterns
    NEXT_PHASE_PATTERNS = [
        re.compile(r"(?:下一阶段|next\s*phase|next\s*step)[：:]\s*([^\n]+)", re.IGNORECASE),
        re.compile(r"(?:阶段目标|phase\s*goal|step\s*goal)[：:]\s*([^\n]+)", re.IGNORECASE),
        re.compile(r"(?:继续|continue)[：:]\s*([^\n]+)", re.IGNORECASE),
        re.compile(r"NEXT_PHASE:\s*([^\n]+)", re.IGNORECASE),
        re.compile(r"NEXT_GOAL:\s*([^\n]+)", re.IGNORECASE),
    ]

    # Completion patterns
    COMPLETION_PATTERNS = [
        re.compile(r"(?:完成|completed|done|finished|success)", re.IGNORECASE),
        re.compile(r"(?:任务结束|task\s+completed|task\s+done)", re.IGNORECASE),
        re.compile(r"(?:没有下一阶段|no\s+next\s+phase|no\s+next\s+step)", re.IGNORECASE),
    ]

    # Error patterns
    ERROR_PATTERNS = [
        re.compile(r"(?:错误|error|failed|failure)", re.IGNORECASE),
        re.compile(r"(?:异常|exception|crash)", re.IGNORECASE),
    ]

    # Waiting for input patterns
    INPUT_PATTERNS = [
        re.compile(r"(?:请输入|please\s+input|enter\s+your)", re.IGNORECASE),
        re.compile(r"(?:等待|waiting|awaiting)", re.IGNORECASE),
        re.compile(r"\?$"),
    ]

    def analyze(self, result: dict) -> AnalysisResult:
        """Analyze an iflow execution result.

        Args:
            result: Dict with keys 'success', 'output', 'error', 'command', etc.

        Returns:
            AnalysisResult with all analysis fields populated.
        """
        analysis = AnalysisResult()

        output = result.get("output", "") or ""
        if not output:
            return analysis

        # Check states
        analysis.is_complete = self._check_completion(output)
        analysis.has_error = self._check_error(output)
        analysis.needs_input = self._check_needs_input(output)

        # Extract next phase
        if not analysis.is_complete:
            next_phase = self._extract_next_phase(output)
            if next_phase:
                analysis.next_phase = next_phase.strip()
                analysis.can_continue = True
                analysis.has_progress = True
                analysis.confidence = self._calculate_confidence(output, next_phase)

        # Extract files
        self._extract_files(output, analysis)

        # Generate summary
        analysis.summary = self._generate_summary(result, analysis)

        logger.debug(
            f"Analysis: can_continue={analysis.can_continue}, "
            f"next_phase={analysis.next_phase}, complete={analysis.is_complete}, "
            f"error={analysis.has_error}, files={len(analysis.all_files)}, "
            f"confidence={analysis.confidence:.2f}"
        )

        return analysis

    def _check_completion(self, output: str) -> bool:
        return any(p.search(output) for p in self.COMPLETION_PATTERNS)

    def _check_error(self, output: str) -> bool:
        return any(p.search(output) for p in self.ERROR_PATTERNS)

    def _check_needs_input(self, output: str) -> bool:
        return any(p.search(output) for p in self.INPUT_PATTERNS)

    def _extract_next_phase(self, output: str) -> Optional[str]:
        """Extract next phase from output using pattern matching."""
        for pattern in self.NEXT_PHASE_PATTERNS:
            match = pattern.search(output)
            if match and match.group(1):
                return match.group(1)

        # Fallback: use last non-status line
        lines = [l.strip() for l in output.split("\n") if l.strip()]
        if lines:
            last_line = lines[-1]
            if not any(p.search(last_line) for p in self.COMPLETION_PATTERNS + self.ERROR_PATTERNS):
                return last_line

        return None

    def _calculate_confidence(self, output: str, next_phase: str) -> float:
        """Calculate confidence score (0.0 - 1.0) for continuation."""
        confidence = 0.0

        if any(p.search(output) for p in self.NEXT_PHASE_PATTERNS):
            confidence += 0.6

        if next_phase and len(next_phase) > 10:
            confidence += 0.2

        if re.search(r"继续|下一步|next|continue", output, re.IGNORECASE):
            confidence += 0.2

        return min(confidence, 1.0)

    def _extract_files(self, output: str, analysis: AnalysisResult) -> None:
        """Extract file paths from output and categorize them."""
        matches = FILE_PATH_PATTERN.findall(output)
        seen = set()

        for file_path in matches:
            if file_path in seen:
                continue
            seen.add(file_path)

            # Only include files that actually exist on disk
            if not Path(file_path).is_file():
                continue

            ext = Path(file_path).suffix.lower()
            analysis.all_files.append(file_path)

            if ext in IMAGE_EXTENSIONS:
                analysis.image_files.append(file_path)
            elif ext in AUDIO_EXTENSIONS:
                analysis.audio_files.append(file_path)
            elif ext in VIDEO_EXTENSIONS:
                analysis.video_files.append(file_path)
            elif ext in DOC_EXTENSIONS:
                analysis.doc_files.append(file_path)

    def _generate_summary(self, result: dict, analysis: AnalysisResult) -> str:
        """Generate a human-readable summary of the analysis."""
        parts = []

        if analysis.has_error:
            parts.append("❌ 执行出现错误")
        elif analysis.is_complete:
            parts.append("✅ 任务已完成")
        else:
            parts.append("⏳ 执行中")

        command = result.get("command", "")
        if command:
            parts.append(f"命令: {command}")

        if analysis.next_phase:
            parts.append(f"下一阶段: {analysis.next_phase}")

        if analysis.all_files:
            parts.append(f"检测到 {len(analysis.all_files)} 个文件")

        output = result.get("output", "")
        if output:
            output_preview = output.replace("\n", " ")[:200]
            parts.append(f"输出: {output_preview}...")

        return "\n".join(parts)

    def needs_intervention(self, analysis: AnalysisResult, loop_depth: int = 0, max_loop_depth: int = 100) -> bool:
        """Determine if human intervention is needed."""
        if analysis.has_error:
            return True
        if analysis.needs_input:
            return True
        if loop_depth >= max_loop_depth:
            logger.warning(f"Exceeded max loop depth: {loop_depth}/{max_loop_depth}")
            return True
        if analysis.can_continue and analysis.confidence < 0.3:
            logger.warning(f"Low confidence: {analysis.confidence:.2f}")
            return True
        return False


# Singleton instance
result_analyzer = ResultAnalyzer()
