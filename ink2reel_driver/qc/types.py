"""质检共用类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Verdict = Literal["pass", "warn", "fixed", "block"]


@dataclass
class Issue:
    code: str
    message: str
    segment_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.segment_id is not None:
            d["segment_id"] = self.segment_id
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class RuleResult:
    verdict: Verdict
    issues: list[Issue] = field(default_factory=list)
    fixed_text: str | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "fixed_text": self.fixed_text,
            "issues": [i.as_dict() for i in self.issues],
        }
