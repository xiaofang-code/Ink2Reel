"""源文清洗：去 markdown 标题、引用标记、压缩空行。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


@dataclass
class CleanResult:
    cleaned_text: str
    chapters: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)


def clean_novel(text: str) -> CleanResult:
    """清洗小说原文。

    - 去掉 markdown 标题行（``# …``），标题文本进入 ``chapters``
    - 去掉行首 ``>`` 引用标记
    - 连续 ≥3 空行压为 1 个空行
    """
    chapters: list[str] = []
    removed: list[str] = []
    out_lines: list[str] = []

    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip()) if line.strip() else None
        if m:
            title = m.group(2).strip()
            if title:
                chapters.append(title)
            removed.append(line)
            continue
        # 行首引用标记
        cleaned_line = _BLOCKQUOTE_RE.sub("", line) if line.lstrip().startswith(">") else line
        # 若整行只剩空白且原行是纯引用，仍保留空行语义由后续压缩处理
        out_lines.append(cleaned_line)

    joined = "\n".join(out_lines)
    # 连续 ≥3 空行 → 1 空行（即两个 \n）
    joined = _MULTI_BLANK_RE.sub("\n\n", joined)
    # 去掉首尾多余空白行，但保留内部段落结构
    cleaned = joined.strip("\n") + ("\n" if joined.strip("\n") else "")
    # strip trailing: handoff 未强制末尾换行；统一为 strip 后的文本
    cleaned = joined.strip()
    if text.endswith("\n") and cleaned and not cleaned.endswith("\n"):
        # 不强制保留；幂等以 strip 语义为准
        pass
    cleaned = cleaned.strip()

    return CleanResult(cleaned_text=cleaned, chapters=chapters, removed_lines=removed)
