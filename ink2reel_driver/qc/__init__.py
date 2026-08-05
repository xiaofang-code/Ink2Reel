"""质检层：OnlyShot 规则移植 + LLM 六维评审。

规则来源见各模块顶部注释；阈值（如 PROMPT_LEN_LIMIT=1500）为 N=8 实测，换供应商需重校准。
"""

from __future__ import annotations

from ink2reel_driver.qc.judge import JudgeResult, judge_script
from ink2reel_driver.qc.rules_prompt import check_prompt
from ink2reel_driver.qc.rules_ref import check_refs
from ink2reel_driver.qc.rules_script import check_script
from ink2reel_driver.qc.types import Issue, RuleResult

__all__ = [
    "Issue",
    "RuleResult",
    "check_prompt",
    "check_refs",
    "check_script",
    "JudgeResult",
    "judge_script",
]
