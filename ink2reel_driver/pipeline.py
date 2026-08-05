"""确定性状态机：文本链 + 媒体链。"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Literal

from lib.episode_planner import EpisodePlanner
from lib.project_manager import ProjectManager, get_project_manager
from lib.script_generator import ScriptGenerator
from lib.script_models import NarrationEpisodeScript

from ink2reel_driver.budget import BudgetExhaustedError
from ink2reel_driver.exporter import export_episode
from ink2reel_driver.gate import GateMode, GatePolicy
from ink2reel_driver.journal import DriverJournal
from ink2reel_driver.media import run_media_chain
from ink2reel_driver.step1 import split_narration_segments
from ink2reel_driver.textclean import clean_novel

logger = logging.getLogger(__name__)

UntilStage = Literal["script", "assets", "storyboard", "video", "deliverables", "media"]


class PipelineError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def _ensure_project(
    pm: ProjectManager,
    project_name: str,
    *,
    title: str | None = None,
) -> Path:
    project_name = pm.normalize_project_name(project_name)
    project_dir = pm.projects_root / project_name
    if project_dir.exists():
        project = pm.load_project(project_name)
        if project.get("content_mode") != "narration":
            raise PipelineError(
                f"project {project_name!r} content_mode={project.get('content_mode')!r}, expect narration"
            )
        if project.get("generation_mode") not in (None, "storyboard"):
            if project.get("generation_mode") != "storyboard":
                raise PipelineError(
                    f"project {project_name!r} generation_mode={project.get('generation_mode')!r}, "
                    "expect storyboard"
                )
        return project_dir

    pm.create_project(project_name, content_mode="narration")
    extras = {"generation_mode": "storyboard", "grid_storyboard": False}
    pm.create_project_metadata(
        project_name,
        title=title or project_name,
        content_mode="narration",
        aspect_ratio="9:16",
        extras=extras,
    )
    return project_dir


def _upload_novel(pm: ProjectManager, project_name: str, novel_path: Path) -> Path:
    if not novel_path.is_file():
        raise PipelineError(f"novel file not found: {novel_path}")
    source_dir = pm.get_project_path(project_name) / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    raw_dest = source_dir / f"raw_{novel_path.name}"
    shutil.copy2(novel_path, raw_dest)

    raw_text = novel_path.read_text(encoding="utf-8")
    cleaned = clean_novel(raw_text)
    dest = source_dir / novel_path.name
    dest.write_text(cleaned.cleaned_text + ("\n" if cleaned.cleaned_text else ""), encoding="utf-8")
    return dest


async def _plan_until_episode(pm: ProjectManager, project_name: str, episode: int, journal: DriverJournal) -> None:
    project_path = pm.get_project_path(project_name)
    planner = await EpisodePlanner.create(project_path)

    while True:
        project = pm.load_project(project_name)
        episodes = project.get("episodes") or []
        nums = {ep.get("episode") for ep in episodes if isinstance(ep, dict)}
        if episode in nums:
            journal.info("plan", message=f"episode {episode} already planned", episodes=sorted(n for n in nums if n))
            return

        result = await planner.plan(
            instructions="按章节自然切分；优先保证第 1 集覆盖开篇悬念（十七分钟/钥匙扣），体量适合 45–90 秒说书短片。"
        )
        journal.info(
            "plan",
            planned=len(result.episodes),
            source_exhausted=result.source_exhausted,
            total_planned=result.total_planned,
            cursor=str(result.cursor) if result.cursor is not None else None,
        )
        if result.source_exhausted and not result.episodes:
            project = pm.load_project(project_name)
            nums = {ep.get("episode") for ep in (project.get("episodes") or []) if isinstance(ep, dict)}
            if episode in nums:
                return
            raise PipelineError(
                f"episode planning exhausted source but episode {episode} missing; "
                f"have={sorted(n for n in nums if n)}"
            )
        if not result.episodes and not result.source_exhausted:
            raise PipelineError("EpisodePlanner.plan returned no episodes without source_exhausted")


def _normalize_until(until: str) -> UntilStage:
    if until == "media":
        return "deliverables"
    if until not in ("script", "assets", "storyboard", "video", "deliverables"):
        raise PipelineError(f"unknown --until {until!r}")
    return until  # type: ignore[return-value]


async def _run_text_chain(
    *,
    pm: ProjectManager,
    project_name: str,
    novel_path: Path,
    episode: int,
    title: str | None,
    gate_mode: GateMode,
    journal: DriverJournal,
    summary: dict[str, Any],
) -> Path:
    """小说 → 剧本；返回 script_path。"""
    journal.start("create_project", project=project_name)
    journal.end("create_project", path=str(pm.get_project_path(project_name)))

    journal.start("upload_novel", novel=str(novel_path))
    dest = _upload_novel(pm, project_name, novel_path)
    journal.end(
        "upload_novel",
        dest=str(dest),
        raw=str(dest.parent / f"raw_{novel_path.name}"),
        cleaned=True,
    )

    journal.start("plan")
    await _plan_until_episode(pm, project_name, episode, journal)
    journal.end("plan", episode=episode)

    journal.start("step1", episode=episode)
    step1 = await split_narration_segments(pm=pm, project_name=project_name, episode=episode)
    journal.end(
        "step1",
        path=str(step1.path),
        step1_method=step1.method,
        retries=step1.retries,
        segment_count=step1.segment_count,
    )
    journal.info(
        "step1",
        step1_method=step1.method,
        retries=step1.retries,
        segment_count=step1.segment_count,
    )
    summary["step1_path"] = str(step1.path)
    summary["step1_method"] = step1.method
    summary["step1_retries"] = step1.retries

    # step1 门：threshold 模式先 auto 确认 step1，剧本级质检放在 script 生成之后
    step1_mode = "auto" if gate_mode == "threshold" else gate_mode
    journal.start("gate", mode=step1_mode, episode=episode, gate_stage="step1_review")
    decision = GatePolicy(stage="step1_review", mode=step1_mode).decide_and_apply(
        pm=pm, project_name=project_name, episode=episode
    )
    journal.gate(
        "step1_review",
        mode=decision.mode,
        approved=decision.approved,
        approved_by=decision.approved_by,
        reason=decision.reason,
        exit_code=decision.exit_code,
    )
    journal.end("gate", approved=decision.approved)
    if not decision.approved and decision.exit_code != 0:
        raise PipelineError(decision.reason, exit_code=decision.exit_code)

    def _ensure_video_backend(p: dict) -> None:
        if not p.get("video_backend"):
            p["video_backend"] = "gemini-aistudio/veo-3.1-generate-preview"

    pm.update_project(project_name, _ensure_video_backend)
    journal.info("script", video_backend="gemini-aistudio/veo-3.1-generate-preview (duration tiers only)")

    journal.start("script", episode=episode)
    gen = await ScriptGenerator.create(pm.get_project_path(project_name))
    script_path = await gen.generate(episode=episode)
    data = json.loads(script_path.read_text(encoding="utf-8"))
    NarrationEpisodeScript.model_validate(data)
    journal.end("script", path=str(script_path))
    summary["script_path"] = str(script_path)

    if gate_mode == "threshold":
        thr = float(summary.get("_threshold") or summary.get("threshold") or 50.0)
        journal.start("gate", mode="threshold", episode=episode, gate_stage="script_qc")
        qc = await GatePolicy(stage="script_qc", mode="threshold", threshold=thr).decide_and_apply_async(
            pm=pm,
            project_name=project_name,
            episode=episode,
            script_path=script_path,
        )
        journal.gate(
            "script_qc",
            mode=qc.mode,
            approved=qc.approved,
            approved_by=qc.approved_by,
            reason=qc.reason,
            escalated=qc.escalated,
            scores=qc.scores,
            exception_ids=qc.exception_ids,
        )
        journal.end("gate", approved=qc.approved, escalated=qc.escalated)
        summary["gate"] = {
            "approved": qc.approved,
            "approved_by": qc.approved_by,
            "reason": qc.reason,
            "escalated": qc.escalated,
            "scores": qc.scores,
            "exception_ids": qc.exception_ids,
        }
        if qc.escalated:
            summary.setdefault("escalated", []).append(
                {"stage": qc.stage, "ids": qc.exception_ids, "reason": qc.reason}
            )
    else:
        summary["gate"] = {
            "approved": decision.approved,
            "approved_by": decision.approved_by,
            "reason": decision.reason,
        }

    return script_path


async def run(
    novel_path: str | Path,
    project_name: str = "midnight",
    *,
    until: UntilStage | str = "script",
    gate_mode: GateMode = "auto",
    episode: int = 1,
    title: str | None = None,
    projects_root: str | Path | None = None,
    export: bool = True,
    limit_segments: int | None = None,
    budget_usd: float = 5.0,
    start_worker: bool = True,
    threshold: float = 50.0,
) -> dict[str, Any]:
    until_n = _normalize_until(str(until))
    novel_path = Path(novel_path).resolve()
    if projects_root is not None:
        pm = ProjectManager(str(projects_root))
    else:
        pm = get_project_manager()

    project_name = pm.normalize_project_name(project_name)
    summary: dict[str, Any] = {
        "project": project_name,
        "episode": episode,
        "until": until_n,
        "limit_segments": limit_segments,
        "budget_usd": budget_usd,
        "threshold": threshold,
        "_threshold": threshold,
        "escalated": [],
    }
    journal: DriverJournal | None = None

    try:
        project_dir = _ensure_project(pm, project_name, title=title)
        journal = DriverJournal(Path(project_dir) / "driver_journal.jsonl")

        script_path = pm.get_project_path(project_name) / "scripts" / f"episode_{episode}.json"
        need_text = until_n == "script" or not script_path.is_file()

        if need_text:
            await _run_text_chain(
                pm=pm,
                project_name=project_name,
                novel_path=novel_path,
                episode=episode,
                title=title,
                gate_mode=gate_mode,
                journal=journal,
                summary=summary,
            )
            script_path = Path(summary.get("script_path") or script_path)
        else:
            journal.info("text", skipped=True, reason="script exists", path=str(script_path))
            summary["script_path"] = str(script_path)
            # 确保 video_backend 身份仍在
            def _ensure_vb(p: dict) -> None:
                if not p.get("video_backend"):
                    p["video_backend"] = "gemini-aistudio/veo-3.1-generate-preview"

            pm.update_project(project_name, _ensure_vb)

            # 复用剧本时仍可跑 threshold 质检门（T6 幂等路径）
            if gate_mode == "threshold":
                journal.start("gate", mode=gate_mode, episode=episode, path=str(script_path))
                decision = await GatePolicy(
                    stage="script_qc", mode="threshold", threshold=threshold
                ).decide_and_apply_async(
                    pm=pm,
                    project_name=project_name,
                    episode=episode,
                    script_path=script_path,
                )
                journal.gate(
                    "script_qc",
                    mode=decision.mode,
                    approved=decision.approved,
                    approved_by=decision.approved_by,
                    reason=decision.reason,
                    escalated=decision.escalated,
                    scores=decision.scores,
                    exception_ids=decision.exception_ids,
                )
                journal.end("gate", approved=decision.approved, escalated=decision.escalated)
                summary["gate"] = {
                    "approved": decision.approved,
                    "approved_by": decision.approved_by,
                    "reason": decision.reason,
                    "escalated": decision.escalated,
                    "scores": decision.scores,
                    "exception_ids": decision.exception_ids,
                }
                if decision.escalated:
                    summary["escalated"].append(
                        {
                            "stage": decision.stage,
                            "ids": decision.exception_ids,
                            "reason": decision.reason,
                        }
                    )

        if until_n == "script":
            if export:
                journal.start("export", episode=episode)
                out = export_episode(
                    pm=pm,
                    project_name=project_name,
                    episode=episode,
                    limit_segments=limit_segments,
                )
                journal.end("export", path=str(out))
                summary["deliverables"] = str(out)
            journal.info("pipeline", status="ok", summary=summary)
            return summary

        # 媒体链
        media = await run_media_chain(
            pm=pm,
            project_name=project_name,
            episode=episode,
            until=until_n,
            limit_segments=limit_segments,
            budget_usd=budget_usd,
            journal=journal,
            start_worker=start_worker,
            gate_mode=gate_mode,
        )
        summary["media_stages"] = media.stages
        summary["budget"] = media.budget
        summary["segment_statuses"] = [
            {
                "segment_id": s.segment_id,
                "storyboard": s.storyboard,
                "video": s.video,
                "audio": s.audio,
                "cost_estimate_usd": s.cost_estimate_usd,
                "notes": s.notes,
            }
            for s in media.segment_statuses
        ]
        for st in media.segment_statuses:
            if st.storyboard == "skipped" and any("escalated:" in n for n in st.notes):
                summary.setdefault("escalated", []).append(
                    {"stage": "storyboard_qc", "segment_id": st.segment_id, "notes": st.notes}
                )

        if until_n == "deliverables" or export:
            journal.start("export", episode=episode)
            out = export_episode(
                pm=pm,
                project_name=project_name,
                episode=episode,
                limit_segments=limit_segments,
                media_statuses=media.segment_statuses,
                budget_summary=media.budget,
            )
            journal.end("export", path=str(out))
            summary["deliverables"] = str(out)

        journal.info("pipeline", status="ok", summary=summary)
        return summary
    except BudgetExhaustedError as e:
        if journal is not None:
            journal.error("pipeline", error=str(e), exit_code=3, spent=e.spent, cap=e.cap)
        raise PipelineError(str(e), exit_code=3) from e
    except PipelineError as e:
        if journal is not None:
            journal.error("pipeline", error=str(e), exit_code=e.exit_code)
        raise
    except NotImplementedError as e:
        if journal is not None:
            journal.error("pipeline", error=str(e))
        raise
    except Exception as e:
        if journal is not None:
            journal.error("pipeline", error=f"{type(e).__name__}: {e}")
        raise
