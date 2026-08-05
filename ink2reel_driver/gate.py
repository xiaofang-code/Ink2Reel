"""GatePolicy：auto / manual / threshold（质检评分 + 异常队列）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from lib import script_review
from lib.project_manager import ProjectManager

from ink2reel_driver.exceptions import append_exception
from ink2reel_driver.qc.judge import JudgeResult, judge_script
from ink2reel_driver.qc.rules_script import check_script

GateMode = Literal["auto", "threshold", "manual"]

DEFAULT_THRESHOLD = 50.0  # 六维总分 0–60，默认 50


@dataclass
class GateDecision:
    stage: str
    mode: GateMode
    approved: bool
    approved_by: str | None
    reason: str
    exit_code: int = 0  # manual 拒绝推进时为 2；escalate 为 0（不停机）
    escalated: bool = False
    scores: dict[str, Any] = field(default_factory=dict)
    exception_ids: list[str] = field(default_factory=list)


class GatePolicy:
    """审核门策略。

    - auto：调用 lib.script_review 确认逻辑放行，``approved_by="auto"``
    - manual：不自动确认，返回 exit_code=2
    - threshold：机械规则 + LLM 六维；≥阈值 auto-pass；否则 escalate（exit 0，进异常队列）
    """

    def __init__(
        self,
        stage: str,
        mode: GateMode = "auto",
        max_auto_retries: int = 0,
        threshold: float = DEFAULT_THRESHOLD,
        judge_fn: Any = None,
    ):
        self.stage = stage
        self.mode = mode
        self.max_auto_retries = max_auto_retries
        self.threshold = float(threshold)
        self.judge_fn = judge_fn  # async callable for tests

    def decide_and_apply(
        self,
        *,
        pm: ProjectManager,
        project_name: str,
        episode: int = 1,
    ) -> GateDecision:
        if self.mode == "threshold":
            # 同步包装：CLI/pipeline 对 threshold 用 async 入口 decide_and_apply_async
            raise RuntimeError(
                "threshold gate requires decide_and_apply_async (LLM judge); "
                "use await GatePolicy(...).decide_and_apply_async(...)"
            )

        project_path = pm.get_project_path(project_name)
        project = pm.load_project(project_name)
        status = script_review.review_status(project_path, project, episode)

        if self.mode == "manual":
            if status == "confirmed":
                return GateDecision(
                    stage=self.stage,
                    mode=self.mode,
                    approved=True,
                    approved_by="manual_prior",
                    reason=f"already confirmed (status={status})",
                )
            return GateDecision(
                stage=self.stage,
                mode=self.mode,
                approved=False,
                approved_by=None,
                reason=f"pending human review (status={status}); resume after confirm",
                exit_code=2,
            )

        # auto
        if status == "confirmed":
            return GateDecision(
                stage=self.stage,
                mode=self.mode,
                approved=True,
                approved_by="auto",
                reason=f"already confirmed (status={status})",
            )

        path = script_review.step1_path(project_path, project, episode)
        if path is None or not path.exists():
            return GateDecision(
                stage=self.stage,
                mode=self.mode,
                approved=False,
                approved_by=None,
                reason="no step1 file; cannot auto-confirm",
                exit_code=1,
            )

        fingerprint = script_review.content_fingerprint(path)
        if not fingerprint:
            return GateDecision(
                stage=self.stage,
                mode=self.mode,
                approved=False,
                approved_by=None,
                reason="step1 fingerprint unavailable",
                exit_code=1,
            )

        confirmed_at = datetime.now(UTC).isoformat()

        def _mutate(p: dict[str, Any]) -> None:
            ok = script_review.apply_confirmation(p, episode, fingerprint, confirmed_at)
            if not ok:
                raise RuntimeError(f"episode {episode} not found in project.json; cannot apply confirmation")

        pm.update_project(project_name, _mutate)

        return GateDecision(
            stage=self.stage,
            mode=self.mode,
            approved=True,
            approved_by="auto",
            reason=f"auto-confirmed fingerprint={fingerprint[:12]}…",
        )

    async def decide_and_apply_async(
        self,
        *,
        pm: ProjectManager,
        project_name: str,
        episode: int = 1,
        script_path: Path | None = None,
    ) -> GateDecision:
        """async 入口：threshold 走质检；其它模式委托同步 decide_and_apply。"""
        if self.mode != "threshold":
            return self.decide_and_apply(pm=pm, project_name=project_name, episode=episode)
        return await self._decide_threshold(
            pm=pm,
            project_name=project_name,
            episode=episode,
            script_path=script_path,
        )

    async def _decide_threshold(
        self,
        *,
        pm: ProjectManager,
        project_name: str,
        episode: int,
        script_path: Path | None,
    ) -> GateDecision:
        project_path = pm.get_project_path(project_name)
        if script_path is None:
            script_path = project_path / "scripts" / f"episode_{episode}.json"
        if not script_path.is_file():
            rec = append_exception(
                project_name=project_name,
                target_id=f"EP-{episode:03d}",
                stage=self.stage,
                reasons=["script_missing"],
                suggested_action="regenerate",
                pm=pm,
            )
            return GateDecision(
                stage=self.stage,
                mode="threshold",
                approved=False,
                approved_by=None,
                reason="script missing for threshold QC",
                escalated=True,
                exception_ids=[rec.id],
                exit_code=0,
            )

        mechanical = check_script(script_path)
        scores: dict[str, Any] = {"mechanical": mechanical.as_dict()}

        if mechanical.blocked:
            reasons = [f"{i.code}:{i.message}" for i in mechanical.issues]
            rec = append_exception(
                project_name=project_name,
                target_id=f"EP-{episode:03d}",
                stage=self.stage,
                reasons=reasons or ["mechanical_block"],
                suggested_action="regenerate",
                meta={"mechanical": mechanical.as_dict()},
                pm=pm,
            )
            return GateDecision(
                stage=self.stage,
                mode="threshold",
                approved=False,
                approved_by=None,
                reason=f"mechanical block: {'; '.join(reasons)[:200]}",
                escalated=True,
                scores=scores,
                exception_ids=[rec.id],
                exit_code=0,
            )

        # LLM judge
        if self.judge_fn is not None:
            judge: JudgeResult = await self.judge_fn(script_path)
        else:
            judge = await judge_script(script_path, project_name=project_name)
        scores["judge"] = judge.as_dict()

        if not judge.available:
            rec = append_exception(
                project_name=project_name,
                target_id=f"EP-{episode:03d}",
                stage=self.stage,
                reasons=["judge_unavailable", judge.error or "parse_failed"],
                suggested_action="review",
                meta={"judge": judge.as_dict()},
                pm=pm,
            )
            return GateDecision(
                stage=self.stage,
                mode="threshold",
                approved=False,
                approved_by=None,
                reason="judge_unavailable → escalate (no default pass/zero score)",
                escalated=True,
                scores=scores,
                exception_ids=[rec.id],
                exit_code=0,
            )

        total = float(judge.total or 0.0)
        scores["threshold"] = self.threshold
        scores["total"] = total
        if total >= self.threshold:
            # 可选：同步写 fingerprint 确认（与 auto 一致）
            project = pm.load_project(project_name)
            step1 = script_review.step1_path(project_path, project, episode)
            if step1 and step1.exists():
                fp = script_review.content_fingerprint(step1)
                if fp:
                    confirmed_at = datetime.now(UTC).isoformat()

                    def _mutate(p: dict[str, Any]) -> None:
                        script_review.apply_confirmation(p, episode, fp, confirmed_at)

                    try:
                        pm.update_project(project_name, _mutate)
                    except Exception:  # noqa: BLE001
                        pass
            return GateDecision(
                stage=self.stage,
                mode="threshold",
                approved=True,
                approved_by="threshold",
                reason=f"judge total={total:.1f} >= threshold={self.threshold} dims={judge.dims}",
                scores=scores,
                exit_code=0,
            )

        rec = append_exception(
            project_name=project_name,
            target_id=f"EP-{episode:03d}",
            stage=self.stage,
            reasons=[f"score_below_threshold:{total:.1f}<{self.threshold}"],
            suggested_action="regenerate",
            meta={"judge": judge.as_dict(), "threshold": self.threshold},
            pm=pm,
        )
        return GateDecision(
            stage=self.stage,
            mode="threshold",
            approved=False,
            approved_by=None,
            reason=f"judge total={total:.1f} < threshold={self.threshold} → escalate",
            escalated=True,
            scores=scores,
            exception_ids=[rec.id],
            exit_code=0,
        )
