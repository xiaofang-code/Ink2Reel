"""Prompt 敏感词 / 长度规则。

来源：
- 词表：OnlyShot ``scripts/生成分镜图.py:41-56``（MIT）
- 替换映射：OnlyShot ``references/jimeng-failure-modes.md:65-114``（MIT）
- ``PROMPT_LEN_LIMIT = 1500``：即梦 5.0 实测上限（N=8 实测、换供应商需重校准）
"""

from __future__ import annotations

import re
from typing import Iterable

from ink2reel_driver.qc.types import Issue, RuleResult

# 来源 OnlyShot scripts/生成分镜图.py:41-56
SENSITIVE_VILLAIN: list[str] = [
    "sinister",
    "menacingly",
    "dark intent",
    "possessive",
    "dark cyan",
    "dark teal",
    "dark particles",
    "dark shadows engulf",
    "cold smirk",
    "harsh shadow",
    "dark wood",
    "sinister spotlight",
]
SENSITIVE_INTIMATE: list[str] = [
    "blush on cheeks",
    "pink blush",
    "extreme close-up upper portion",
    "extreme close-up face filling frame",
    "internal warm glow",
    "subtle internal glow",
    "pulsing softly",
    "blooms with",
]
SENSITIVE_CN: list[str] = ["反派", "不死心"]

# N=8 实测、换供应商需重校准
PROMPT_LEN_LIMIT = 1500
PROMPT_LEN_WARN = 1200

# 来源 jimeng-failure-modes.md:65-114（有映射可 auto-fix）
_REPLACEMENTS: dict[str, str] = {
    "sinister spotlight": "moody spotlight",
    "sinister": "moody",
    "menacingly": "forward",
    "dark intent": "focused",
    "possessive": "focused",
    "dark cyan": "deep cyan",
    "dark teal": "deep teal",
    "dark particles": "floating motes",
    "dark shadows engulf": "shadows along edges",
    "cold smirk": "subtle smirk",
    "harsh shadow": "soft shadow",
    "dark wood": "deep brown wood",
    "blush on cheeks": "rose tint on surface",
    "pink blush": "pink glow",
    "extreme close-up upper portion": "medium shot full body",
    "extreme close-up face filling frame": "medium close-up",
    "internal warm glow": "",
    "subtle internal glow": "",
    "pulsing softly": "glowing steadily",
    "blooms with": "shows",
    "反派": "这个角色",
    "不死心": "还在惦记",
}


def _all_sensitive() -> list[str]:
    # 长词优先替换，避免短词抢匹配
    words = list(dict.fromkeys(SENSITIVE_VILLAIN + SENSITIVE_INTIMATE + SENSITIVE_CN))
    words.sort(key=len, reverse=True)
    return words


def _apply_replacements(text: str) -> tuple[str, list[tuple[str, str]]]:
    out = text
    applied: list[tuple[str, str]] = []
    for word in _all_sensitive():
        if word not in _REPLACEMENTS:
            continue
        repl = _REPLACEMENTS[word]
        if not word:
            continue
        # 英文词用边界；中文直接子串
        if re.fullmatch(r"[A-Za-z0-9 \-']+", word):
            pattern = re.compile(re.escape(word), re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(word))
        if not pattern.search(out):
            continue
        out2, n = pattern.subn(repl, out)
        if n:
            applied.append((word, repl if repl else "<deleted>"))
            out = out2
    # 清理多余空格
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" ,", ",", out)
    return out, applied


def _unmapped_hits(text: str, mapped: Iterable[str]) -> list[str]:
    hits: list[str] = []
    lower = text.lower()
    mapped_set = {m.lower() for m in mapped}
    for word in _all_sensitive():
        if word.lower() in mapped_set:
            continue
        if re.fullmatch(r"[A-Za-z0-9 \-']+", word):
            if re.search(re.escape(word), lower, re.IGNORECASE):
                hits.append(word)
        else:
            if word in text:
                hits.append(word)
    return hits


def check_prompt(text: str) -> RuleResult:
    """检查单条 prompt 文本。

    - 长度 >1500 → block；1200–1500 → warn
    - 敏感词有映射 → fixed + 清单；无映射 → block
    - 替换后再次超长 → block
    """
    if text is None:
        text = ""
    issues: list[Issue] = []
    n = len(text)

    if n > PROMPT_LEN_LIMIT:
        issues.append(
            Issue(
                code="prompt_too_long",
                message=f"prompt length {n} > {PROMPT_LEN_LIMIT} (N=8 实测、换供应商需重校准)",
                detail={"length": n, "limit": PROMPT_LEN_LIMIT},
            )
        )
        return RuleResult(verdict="block", issues=issues)

    if n >= PROMPT_LEN_WARN:
        issues.append(
            Issue(
                code="prompt_length_warn",
                message=f"prompt length {n} in [{PROMPT_LEN_WARN},{PROMPT_LEN_LIMIT}]",
                detail={"length": n},
            )
        )

    fixed, applied = _apply_replacements(text)
    if applied:
        issues.append(
            Issue(
                code="sensitive_auto_fixed",
                message=f"replaced {len(applied)} sensitive term(s)",
                detail={"replacements": [{"from": a, "to": b} for a, b in applied]},
            )
        )

    unmapped = _unmapped_hits(fixed if applied else text, [a for a, _ in applied])
    if unmapped:
        issues.append(
            Issue(
                code="sensitive_unmapped",
                message=f"sensitive terms without mapping: {unmapped}",
                detail={"terms": unmapped},
            )
        )
        return RuleResult(verdict="block", issues=issues, fixed_text=fixed if applied else None)

    if applied:
        if len(fixed) > PROMPT_LEN_LIMIT:
            issues.append(
                Issue(
                    code="prompt_too_long_after_fix",
                    message=f"after fix length {len(fixed)} > {PROMPT_LEN_LIMIT}",
                    detail={"length": len(fixed)},
                )
            )
            return RuleResult(verdict="block", issues=issues, fixed_text=fixed)
        return RuleResult(verdict="fixed", issues=issues, fixed_text=fixed)

    if issues:
        return RuleResult(verdict="warn", issues=issues)
    return RuleResult(verdict="pass", issues=[])
