"""BudgetGuard 单测。"""

from __future__ import annotations

import pytest

from ink2reel_driver.budget import BudgetExhaustedError, BudgetGuard, EST_VIDEO_USD


def test_warn_at_80_percent() -> None:
    g = BudgetGuard(cap_usd=10.0)
    g.commit(7.0, label="a")
    assert g.warned_80 is False
    g.check_can_enqueue(1.5, label="b")  # projected 8.5 = 85%
    assert g.warned_80 is True


def test_exhaust_at_100_exit_semantics() -> None:
    g = BudgetGuard(cap_usd=5.0)
    g.commit(4.0, label="v1")
    with pytest.raises(BudgetExhaustedError) as ei:
        g.check_can_enqueue(1.5, label="v2")  # 5.5 >= 5
    assert ei.value.exit_code == 3
    assert ei.value.spent == 4.0
    assert ei.value.cap == 5.0
    # 在途不取消：guard 本身不修改 line_items 以外的状态
    assert len(g.line_items) == 1


def test_estimate_constants() -> None:
    g = BudgetGuard(cap_usd=100)
    assert g.estimate_image(2) == pytest.approx(0.1)
    assert g.estimate_video(1) == EST_VIDEO_USD


def test_summary() -> None:
    g = BudgetGuard(cap_usd=5)
    g.commit(1.0, label="x")
    s = g.summary()
    assert s["spent_usd"] == 1.0
    assert s["remaining_usd"] == 4.0
