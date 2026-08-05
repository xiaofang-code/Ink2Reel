"""媒体阶段：进程内 GenerationWorker + batch_enqueue_and_wait。"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lib.generation_queue_client import (
    BatchTaskResult,
    TaskSpec,
    batch_enqueue_and_wait,
    is_worker_online,
)
from lib.generation_worker import GenerationWorker
from lib.project_manager import ProjectManager
from lib.prompt_builders import build_character_prompt, build_scene_prompt

from ink2reel_driver.budget import BudgetExhaustedError, BudgetGuard
from ink2reel_driver.exceptions import append_exception
from ink2reel_driver.journal import DriverJournal
from ink2reel_driver.mediacheck import check_image, check_video
from ink2reel_driver.qc.rules_prompt import check_prompt
from ink2reel_driver.qc.rules_ref import check_refs

logger = logging.getLogger(__name__)

SCRIPT_REL = "scripts/episode_{episode}.json"


@dataclass
class SegmentMediaStatus:
    segment_id: str
    storyboard: str = "missing"  # ready|failed|skipped|missing
    video: str = "missing"
    audio: str = "skipped"
    # 估算费用（BudgetGuard 常数），非真实账单
    cost_estimate_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        """兼容旧字段名；语义同 cost_estimate_usd。"""
        return self.cost_estimate_usd

    @cost_usd.setter
    def cost_usd(self, value: float) -> None:
        self.cost_estimate_usd = float(value or 0.0)


@dataclass
class MediaRunResult:
    segment_statuses: list[SegmentMediaStatus]
    budget: dict[str, Any]
    stages: list[str]


class WorkerHandle:
    def __init__(self, worker: GenerationWorker):
        self.worker = worker
        self._started = False

    async def start(self) -> None:
        await self.worker.start()
        self._started = True

    async def stop(self) -> None:
        if self._started:
            await self.worker.stop()
            self._started = False


def warn_if_server_port_open(host: str = "127.0.0.1", port: int = 1241) -> str | None:
    """检测常见 uvicorn 端口；双 worker 抢 default lease 会互斥。"""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            msg = (
                f"WARN: something listens on {host}:{port} (likely ArcReel server). "
                "Driver starts an in-process GenerationWorker with lease_name=default; "
                "only one worker can hold the lease. Stop the server OR stop the driver worker."
            )
            logger.warning(msg)
            return msg
    except OSError:
        return None


async def ensure_worker(
    *,
    journal: DriverJournal | None = None,
    lease_name: str = "default",
    ready_timeout_s: float = 30.0,
) -> WorkerHandle:
    import asyncio
    import time

    warn = warn_if_server_port_open()
    if journal and warn:
        journal.info("worker", warning=warn)
    online = await is_worker_online(lease_name)
    if online:
        msg = (
            f"worker lease '{lease_name}' already held by another process; "
            "driver will start anyway and contend for lease (tasks may wait)."
        )
        logger.warning(msg)
        if journal:
            journal.info("worker", warning=msg)
    worker = GenerationWorker(lease_name=lease_name)
    handle = WorkerHandle(worker)
    await handle.start()
    # enqueue_task_only 要求 worker online；等 lease 心跳就绪
    deadline = time.monotonic() + ready_timeout_s
    while time.monotonic() < deadline:
        if await is_worker_online(lease_name):
            break
        await asyncio.sleep(0.3)
    else:
        logger.warning("worker not online within %.1fs; continuing anyway", ready_timeout_s)
    if journal:
        journal.info(
            "worker",
            status="started",
            owner=worker.owner_id,
            lease=lease_name,
            online=await is_worker_online(lease_name),
        )
    return handle


def load_script_segments(
    pm: ProjectManager,
    project_name: str,
    episode: int,
    *,
    limit: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    script_file = SCRIPT_REL.format(episode=episode)
    script = pm.load_script(project_name, script_file)
    segs = list(script.get("segments") or [])
    if limit is not None and limit > 0:
        segs = segs[:limit]
    return script, segs, script_file


def _segment_id(seg: dict, i: int) -> str:
    return str(seg.get("segment_id") or f"E1S{i + 1:02d}")


async def generate_assets(
    *,
    pm: ProjectManager,
    project_name: str,
    budget: BudgetGuard,
    journal: DriverJournal,
    batch_fn: Callable = batch_enqueue_and_wait,
) -> list[BatchTaskResult]:
    project = pm.load_project(project_name)
    characters = project.get("characters") if isinstance(project.get("characters"), dict) else {}
    scenes = project.get("scenes") if isinstance(project.get("scenes"), dict) else {}
    style = str(project.get("style") or "")
    style_desc = str(project.get("style_description") or "")

    project_path = pm.get_project_path(project_name)
    specs: list[TaskSpec] = []
    for name, entry in characters.items():
        # 幂等跳过已有角色图
        char_dir = project_path / "characters"
        if char_dir.is_dir() and any(p.stem == str(name) or str(name) in p.stem for p in char_dir.iterdir() if p.is_file()):
            journal.info("assets", resource_id=str(name), status="reuse", kind="character")
            continue
        desc = ""
        if isinstance(entry, dict):
            desc = str(entry.get("description") or name)
        else:
            desc = str(name)
        prompt = build_character_prompt(name, desc, style=style, style_description=style_desc)
        est = budget.estimate_image(1)
        budget.check_can_enqueue(est, label=f"character:{name}")
        specs.append(
            TaskSpec.from_request(
                task_type="character",
                media_type="image",
                resource_id=str(name),
                prompt=prompt,
                source="skill",
            )
        )
        budget.commit(est, label=f"character:{name}", meta={"kind": "image_est"})

    for name, entry in scenes.items():
        scene_dir = project_path / "scenes"
        if scene_dir.is_dir() and any(p.stem == str(name) or str(name) in p.stem for p in scene_dir.iterdir() if p.is_file()):
            journal.info("assets", resource_id=str(name), status="reuse", kind="scene")
            continue
        desc = ""
        if isinstance(entry, dict):
            desc = str(entry.get("description") or name)
        else:
            desc = str(name)
        prompt = build_scene_prompt(name, desc, style=style, style_description=style_desc)
        est = budget.estimate_image(1)
        budget.check_can_enqueue(est, label=f"scene:{name}")
        specs.append(
            TaskSpec.from_request(
                task_type="scene",
                media_type="image",
                resource_id=str(name),
                prompt=prompt,
                source="skill",
            )
        )
        budget.commit(est, label=f"scene:{name}", meta={"kind": "image_est"})

    if not specs:
        journal.info("assets", message="no characters/scenes to generate (none registered or all reused)")
        return []

    journal.start("assets", count=len(specs))
    t0 = time.perf_counter()
    ok, fail = await batch_fn(project_name=project_name, specs=specs)
    elapsed = time.perf_counter() - t0
    for r in ok + fail:
        journal.info(
            "assets",
            resource_id=r.resource_id,
            task_id=r.task_id,
            status=r.status,
            error=r.error,
        )
    journal.end("assets", ok=len(ok), failed=len(fail), elapsed_s=elapsed)
    return ok + fail


def _image_prompt_text(img: Any) -> str:
    if isinstance(img, str):
        return img
    if isinstance(img, dict):
        import json as _json

        parts = []
        if img.get("scene"):
            parts.append(str(img["scene"]))
        if img.get("composition"):
            parts.append(_json.dumps(img["composition"], ensure_ascii=False))
        return "\n".join(parts)
    return ""


async def generate_storyboards(
    *,
    pm: ProjectManager,
    project_name: str,
    episode: int,
    limit: int | None,
    budget: BudgetGuard,
    journal: DriverJournal,
    batch_fn: Callable = batch_enqueue_and_wait,
    gate_mode: str = "auto",
    escalated_out: list | None = None,
) -> list[SegmentMediaStatus]:
    _script, segs, script_file = load_script_segments(pm, project_name, episode, limit=limit)
    statuses = [SegmentMediaStatus(segment_id=_segment_id(s, i)) for i, s in enumerate(segs)]
    project_path = pm.get_project_path(project_name)
    assets_dir = project_path
    specs: list[TaskSpec] = []
    for i, seg in enumerate(segs):
        sid = _segment_id(seg, i)
        img = seg.get("image_prompt")
        if not isinstance(img, dict) and not isinstance(img, str):
            statuses[i].storyboard = "failed"
            statuses[i].notes.append("missing image_prompt")
            continue

        # storyboard 门：threshold 下 prompt/ref 规则；block → escalate 跳过本段，不停机
        if gate_mode == "threshold":
            pr = check_prompt(_image_prompt_text(img))
            rr = check_refs(seg if isinstance(seg, dict) else {}, assets_dir)
            journal.info(
                "qc_storyboard",
                segment_id=sid,
                prompt=pr.as_dict(),
                refs=rr.as_dict(),
            )
            if pr.blocked or rr.blocked:
                reasons = [f"{x.code}:{x.message}" for x in (pr.issues + rr.issues)]
                rec = append_exception(
                    project_name=project_name,
                    target_id=sid,
                    stage="storyboard_qc",
                    reasons=reasons or ["storyboard_qc_block"],
                    suggested_action="regenerate",
                    meta={"prompt": pr.as_dict(), "refs": rr.as_dict()},
                    pm=pm,
                )
                statuses[i].storyboard = "skipped"
                statuses[i].notes.append(f"escalated:{rec.id}")
                if escalated_out is not None:
                    escalated_out.append({"segment_id": sid, "exception_id": rec.id, "reasons": reasons})
                journal.info(
                    "storyboard",
                    segment_id=sid,
                    status="escalated",
                    exception_id=rec.id,
                    reasons=reasons,
                )
                continue
            if pr.verdict == "fixed" and pr.fixed_text and isinstance(img, dict):
                img = {**img, "scene": pr.fixed_text}

        # 幂等：已有分镜图则跳过入队（质检棒不重烧图）
        existing = project_path / "storyboards" / f"scene_{sid}.png"
        if existing.is_file() and existing.stat().st_size > 1000:
            chk = check_image(existing)
            statuses[i].storyboard = "ready" if chk.ok else "failed"
            if not chk.ok:
                statuses[i].notes.append(chk.reason or "storyboard_check_failed")
            journal.info(
                "storyboard",
                segment_id=sid,
                status="reuse",
                path=str(existing),
                check=chk.reason or "ok",
            )
            continue

        est = budget.estimate_image(1)
        try:
            budget.check_can_enqueue(est, label=f"storyboard:{sid}")
        except BudgetExhaustedError:
            raise
        specs.append(
            TaskSpec.from_request(
                task_type="storyboard",
                media_type="image",
                resource_id=sid,
                prompt=img,
                script_file=script_file,
                source="skill",
            )
        )
        budget.commit(est, label=f"storyboard:{sid}", meta={"kind": "image_est", "segment_id": sid})

    if not specs:
        journal.info("storyboard", message="no storyboard specs (all reused or escalated)")
        return statuses

    journal.start("storyboard", count=len(specs))
    ok, fail = await batch_fn(project_name=project_name, specs=specs)
    by_id = {r.resource_id: r for r in ok + fail}
    project_path = pm.get_project_path(project_name)
    for st in statuses:
        r = by_id.get(st.segment_id)
        if r is None:
            continue
        journal.info(
            "storyboard",
            segment_id=st.segment_id,
            task_id=r.task_id,
            status=r.status,
            error=r.error,
        )
        if r.status != "succeeded":
            st.storyboard = "failed"
            st.notes.append(r.error or "storyboard failed")
            continue
        path = project_path / "storyboards" / f"scene_{st.segment_id}.png"
        chk = check_image(path)
        st.storyboard = "ready" if chk.ok else "failed"
        if not chk.ok:
            st.notes.append(chk.reason)
    journal.end("storyboard", ok=len(ok), failed=len(fail))
    return statuses


async def generate_videos(
    *,
    pm: ProjectManager,
    project_name: str,
    episode: int,
    limit: int | None,
    budget: BudgetGuard,
    journal: DriverJournal,
    statuses: list[SegmentMediaStatus] | None = None,
    batch_fn: Callable = batch_enqueue_and_wait,
) -> list[SegmentMediaStatus]:
    """逐段 I2V：每段只提交 1 次；入队前 BudgetGuard；失败标 media_failed 继续。"""
    _script, segs, script_file = load_script_segments(pm, project_name, episode, limit=limit)
    if statuses is None:
        statuses = [SegmentMediaStatus(segment_id=_segment_id(s, i)) for i, s in enumerate(segs)]
    status_by = {s.segment_id: s for s in statuses}
    project_path = pm.get_project_path(project_name)

    journal.start("video", count=len(segs))
    for i, seg in enumerate(segs):
        sid = _segment_id(seg, i)
        st = status_by.setdefault(sid, SegmentMediaStatus(segment_id=sid))
        # 无首帧则跳过视频
        sb_path = project_path / "storyboards" / f"scene_{sid}.png"
        if not sb_path.is_file():
            st.video = "failed"
            st.notes.append("no storyboard for i2v")
            journal.info("video", segment_id=sid, status="skipped", reason="no_storyboard")
            continue

        vprompt = seg.get("video_prompt")
        if not isinstance(vprompt, dict) and not isinstance(vprompt, str):
            st.video = "failed"
            st.notes.append("missing video_prompt")
            continue

        dur = int(seg.get("duration_seconds") or 4)
        est = budget.estimate_video(1, duration_seconds=dur)
        try:
            budget.check_can_enqueue(est, label=f"video:{sid}")
        except BudgetExhaustedError as e:
            journal.info("video", segment_id=sid, status="budget_stop", spent=e.spent, cap=e.cap)
            # 本段及后续标记 skipped；逐段 wait 无在途任务需取消
            for j in range(i, len(segs)):
                rid = _segment_id(segs[j], j)
                status_by.setdefault(rid, SegmentMediaStatus(segment_id=rid)).video = "skipped"
            raise

        budget.commit(est, label=f"video:{sid}", meta={"kind": "video_est", "segment_id": sid, "duration": dur})
        st.cost_estimate_usd += est

        spec = TaskSpec.from_request(
            task_type="video",
            media_type="video",
            resource_id=sid,
            prompt=vprompt,
            script_file=script_file,
            extra_payload={"duration_seconds": dur, "seed": None},
            source="skill",
        )
        t0 = time.perf_counter()
        ok, fail = await batch_fn(project_name=project_name, specs=[spec])
        elapsed = time.perf_counter() - t0
        r = (ok + fail)[0] if (ok or fail) else None
        if r is None or r.status != "succeeded":
            st.video = "failed"
            err = (r.error if r else None) or "video_generate_failed"
            if not str(err).strip():
                err = "video_generate_failed"
            st.notes.append(err)
            journal.info(
                "video",
                segment_id=sid,
                task_id=r.task_id if r else None,
                status="failed",
                error=err,
                elapsed_s=elapsed,
                cost_estimate_usd=est,
            )
            continue

        vpath = project_path / "videos" / f"scene_{sid}.mp4"
        chk = check_video(vpath, expected_duration_s=float(dur))
        st.video = "ready" if chk.ok else "failed"
        err_field: str | None = None
        if not chk.ok:
            reason = chk.reason or "media_check_failed"
            st.notes.append(reason)
            err_field = reason
        journal.info(
            "video",
            segment_id=sid,
            task_id=r.task_id,
            status=st.video,
            error=err_field if st.video == "failed" else None,
            elapsed_s=elapsed,
            cost_estimate_usd=est,
            check=chk.reason or "ok",
            meta=chk.meta,
        )

    journal.end("video")
    return list(status_by.values())


async def generate_tts(
    *,
    pm: ProjectManager,
    project_name: str,
    episode: int,
    limit: int | None,
    budget: BudgetGuard,
    journal: DriverJournal,
    statuses: list[SegmentMediaStatus],
    batch_fn: Callable = batch_enqueue_and_wait,
    audio_configured: bool | None = None,
) -> list[SegmentMediaStatus]:
    """有 audio provider 则入队；否则 journal skipped。"""
    if audio_configured is None:
        audio_configured = await _has_audio_provider(pm, project_name)

    if not audio_configured:
        journal.info("tts", status="skipped", reason="no_provider")
        for st in statuses:
            st.audio = "skipped"
        return statuses

    _script, segs, script_file = load_script_segments(pm, project_name, episode, limit=limit)
    status_by = {s.segment_id: s for s in statuses}
    specs: list[TaskSpec] = []
    for i, seg in enumerate(segs):
        sid = _segment_id(seg, i)
        text = str(seg.get("novel_text") or "").strip()
        if not text:
            status_by.setdefault(sid, SegmentMediaStatus(segment_id=sid)).audio = "skipped"
            continue
        est = budget.estimate_tts(1)
        try:
            budget.check_can_enqueue(est, label=f"tts:{sid}")
        except BudgetExhaustedError:
            journal.info("tts", status="budget_stop")
            break
        budget.commit(est, label=f"tts:{sid}", meta={"kind": "tts_est"})
        specs.append(
            TaskSpec.from_request(
                task_type="tts",
                media_type="audio",
                resource_id=sid,
                script_file=script_file,
                source="skill",
            )
        )

    if not specs:
        journal.info("tts", message="no tts specs")
        return statuses

    journal.start("tts", count=len(specs))
    ok, fail = await batch_fn(project_name=project_name, specs=specs)
    by_id = {r.resource_id: r for r in ok + fail}
    for sid, r in by_id.items():
        st = status_by.setdefault(sid, SegmentMediaStatus(segment_id=sid))
        st.audio = "ready" if r.status == "succeeded" else "failed"
        journal.info("tts", segment_id=sid, task_id=r.task_id, status=r.status, error=r.error)
    journal.end("tts", ok=len(ok), failed=len(fail))
    return list(status_by.values())


async def _has_audio_provider(pm: ProjectManager, project_name: str) -> bool:
    try:
        from lib.config.resolver import ConfigResolver
        from lib.db import async_session_factory

        project = pm.load_project(project_name)
        await ConfigResolver(async_session_factory).resolve_audio_backend(project, None)
        return True
    except Exception:  # noqa: BLE001
        return False


async def run_media_chain(
    *,
    pm: ProjectManager,
    project_name: str,
    episode: int = 1,
    until: str = "deliverables",
    limit_segments: int | None = 4,
    budget_usd: float = 5.0,
    journal: DriverJournal,
    batch_fn: Callable = batch_enqueue_and_wait,
    start_worker: bool = True,
    gate_mode: str = "auto",
) -> MediaRunResult:
    """assets → storyboard → video → tts（按 until 截断）。"""
    stages_done: list[str] = []
    budget = BudgetGuard(cap_usd=budget_usd)
    worker: WorkerHandle | None = None
    statuses: list[SegmentMediaStatus] = []
    escalated_segments: list[dict] = []

    try:
        if start_worker:
            worker = await ensure_worker(journal=journal)

        # assets
        if until in ("assets", "storyboard", "video", "deliverables"):
            await generate_assets(
                pm=pm, project_name=project_name, budget=budget, journal=journal, batch_fn=batch_fn
            )
            stages_done.append("assets")
            if until == "assets":
                return MediaRunResult(statuses, budget.summary(), stages_done)

        # storyboard
        if until in ("storyboard", "video", "deliverables"):
            statuses = await generate_storyboards(
                pm=pm,
                project_name=project_name,
                episode=episode,
                limit=limit_segments,
                budget=budget,
                journal=journal,
                batch_fn=batch_fn,
                gate_mode=gate_mode,
                escalated_out=escalated_segments,
            )
            stages_done.append("storyboard")
            journal.gate(
                "first_frame",
                mode=gate_mode,
                approved=True,
                approved_by="threshold" if gate_mode == "threshold" else "auto",
                ready=sum(1 for s in statuses if s.storyboard == "ready"),
                escalated=len(escalated_segments),
                escalated_segments=escalated_segments,
            )
            if until == "storyboard":
                return MediaRunResult(statuses, budget.summary(), stages_done)

        # video
        if until in ("video", "deliverables"):
            statuses = await generate_videos(
                pm=pm,
                project_name=project_name,
                episode=episode,
                limit=limit_segments,
                budget=budget,
                journal=journal,
                statuses=statuses or None,
                batch_fn=batch_fn,
            )
            stages_done.append("video")
            if until == "video":
                return MediaRunResult(statuses, budget.summary(), stages_done)

        # tts + (export 由 pipeline 做)
        if until == "deliverables":
            statuses = await generate_tts(
                pm=pm,
                project_name=project_name,
                episode=episode,
                limit=limit_segments,
                budget=budget,
                journal=journal,
                statuses=statuses,
                batch_fn=batch_fn,
            )
            stages_done.append("tts")

        return MediaRunResult(statuses, budget.summary(), stages_done)
    finally:
        if worker is not None:
            await worker.stop()
            journal.info("worker", status="stopped")
