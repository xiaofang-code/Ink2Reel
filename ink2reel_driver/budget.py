"""预算熔断：入队前估算，≥100% 停止新入队（exit 3）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 保守估算常数（标明"估算"：无 pricing 或 calculate 失败时用）
EST_IMAGE_USD = 0.05  # 每张图
EST_VIDEO_USD = 1.0  # 每段视频
EST_TTS_USD = 0.02  # 每段 TTS


class BudgetExhaustedError(RuntimeError):
    """预算耗尽：应 exit code 3，非 crash。"""

    exit_code = 3

    def __init__(self, message: str, *, spent: float, cap: float):
        super().__init__(message)
        self.spent = spent
        self.cap = cap


@dataclass
class BudgetGuard:
    """累计已承诺花费；入队前 check_can_enqueue。

    - ≥80% cap → warn（不阻断）
    - ≥100% → 拒绝新入队，抛 BudgetExhaustedError
    在途任务不取消（由调用方 wait 完）。
    """

    cap_usd: float
    spent_usd: float = 0.0
    warned_80: bool = False
    line_items: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        return self.cap_usd - self.spent_usd

    def ratio(self) -> float:
        if self.cap_usd <= 0:
            return 1.0
        return self.spent_usd / self.cap_usd

    def estimate_image(self, n: int = 1) -> float:
        return EST_IMAGE_USD * n

    def estimate_video(self, n: int = 1, *, duration_seconds: int | None = None) -> float:
        # 时长越长略增，但保底用常数
        base = EST_VIDEO_USD * n
        if duration_seconds and duration_seconds > 8:
            base *= duration_seconds / 8.0
        return base

    def estimate_tts(self, n: int = 1) -> float:
        return EST_TTS_USD * n

    def try_estimate_via_calculator(
        self,
        *,
        provider: str,
        model: str | None,
        call_type: str,
        duration_seconds: int | None = None,
    ) -> float | None:
        """优先 lib.cost_calculator；失败返回 None 走保守常数。"""
        try:
            from lib.cost_calculator import cost_calculator
            from lib.pricing.strategies import PricingParams

            amount, _cur = cost_calculator.calculate_cost(
                provider,
                PricingParams(
                    call_type=call_type,
                    model=model,
                    duration_seconds=duration_seconds or 8,
                ),
                estimate_only=True,
            )
            if amount and amount > 0:
                return float(amount)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cost_calculator estimate failed: %s", exc)
        return None

    def check_can_enqueue(self, estimated_usd: float, *, label: str = "") -> None:
        """若加入 estimated 后会 ≥100% cap，拒绝。"""
        projected = self.spent_usd + max(0.0, estimated_usd)
        if self.cap_usd > 0 and projected >= self.cap_usd:
            raise BudgetExhaustedError(
                f"budget_exhausted: projected ${projected:.3f} >= cap ${self.cap_usd:.3f} ({label})",
                spent=self.spent_usd,
                cap=self.cap_usd,
            )
        if self.cap_usd > 0 and projected >= 0.8 * self.cap_usd and not self.warned_80:
            self.warned_80 = True
            logger.warning(
                "budget warn ≥80%%: spent=%.3f projected=%.3f cap=%.3f (%s)",
                self.spent_usd,
                projected,
                self.cap_usd,
                label,
            )

    def commit(self, amount_usd: float, *, label: str, meta: dict[str, Any] | None = None) -> None:
        self.spent_usd += max(0.0, amount_usd)
        self.line_items.append(
            {"label": label, "usd": amount_usd, "spent_after": self.spent_usd, **(meta or {})}
        )
        if self.cap_usd > 0 and self.spent_usd >= 0.8 * self.cap_usd and not self.warned_80:
            self.warned_80 = True
            logger.warning("budget warn ≥80%% after commit: spent=%.3f cap=%.3f", self.spent_usd, self.cap_usd)

    def summary(self) -> dict[str, Any]:
        return {
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 4),
            "remaining_usd": round(self.remaining(), 4),
            "ratio": round(self.ratio(), 4),
            "line_items": list(self.line_items),
        }
