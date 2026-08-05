"""media 编排 mock 单测（不打真实 API）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ink2reel_driver.budget import BudgetExhaustedError, BudgetGuard
from ink2reel_driver.journal import DriverJournal
from ink2reel_driver.media import (
    generate_tts,
    generate_videos,
    run_media_chain,
)
from lib.generation_queue_client import BatchTaskResult as RealBatch
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager


def _pm(tmp_path: Path) -> ProjectManager:
    root = tmp_path / "projects"
    root.mkdir()
    pm = ProjectManager(str(root))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    return pm


def _seed_script(pm: ProjectManager, n: int = 3) -> None:
    segs = []
    for i in range(1, n + 1):
        segs.append(
            {
                "segment_id": f"E1S{i:02d}",
                "novel_text": f"旁白段落{i}。" * 10,
                "duration_seconds": 4,
                "segment_break": False,
                "characters_in_segment": ["林夏"] if i == 1 else [],
                "scenes": ["便利店"] if i == 1 else [],
                "props": [],
                "image_prompt": {
                    "scene": f"scene {i}",
                    "composition": {
                        "shot_type": "Medium Shot",
                        "lighting": "cold",
                        "ambiance": "tense",
                    },
                },
                "video_prompt": {
                    "action": "action",
                    "camera_motion": "Static",
                    "ambiance_audio": "hum",
                    "dialogue": [],
                },
            }
        )
    path = pm.get_project_path("demo") / "scripts" / "episode_1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {"title": "t", "content_mode": "narration", "duration_seconds": 4 * n, "segments": segs},
    )

    def _assets(p: dict) -> None:
        p["characters"] = {"林夏": {"description": "女店员"}}
        p["scenes"] = {"便利店": {"description": "夜店"}}

    pm.update_project("demo", _assets)


@pytest.mark.asyncio
async def test_stage_order_and_limit(tmp_path: Path) -> None:
    pm = _pm(tmp_path)
    _seed_script(pm, 5)
    journal = DriverJournal(tmp_path / "j.jsonl")
    order: list[str] = []

    async def fake_batch(*, project_name, specs):
        order.append(specs[0].task_type if specs else "empty")
        oks = []
        for s in specs:
            oks.append(
                RealBatch(
                    resource_id=s.resource_id,
                    task_id=f"t-{s.resource_id}",
                    status="succeeded",
                    result={},
                )
            )
            # touch files for storyboard/video checks
            proj = pm.get_project_path(project_name)
            if s.task_type == "storyboard":
                p = proj / "storyboards" / f"scene_{s.resource_id}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                from PIL import Image

                Image.new("RGB", (720, 1280)).save(p)
            if s.task_type == "video":
                p = proj / "videos" / f"scene_{s.resource_id}.mp4"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            if s.task_type in ("character", "scene"):
                sub = "characters" if s.task_type == "character" else "scenes"
                p = proj / sub / f"{s.resource_id}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                from PIL import Image

                Image.new("RGB", (720, 1280)).save(p)
        return oks, []

    # monkeypatch video check to ok without ffprobe
    from ink2reel_driver import media as media_mod
    from ink2reel_driver.mediacheck import CheckResult

    media_mod.check_video = lambda *a, **k: CheckResult(True, "ready", meta={"duration_s": 4})  # type: ignore
    media_mod.check_image = lambda *a, **k: CheckResult(True, "ready")  # type: ignore

    result = await run_media_chain(
        pm=pm,
        project_name="demo",
        episode=1,
        until="video",
        limit_segments=2,
        budget_usd=50,
        journal=journal,
        batch_fn=fake_batch,
        start_worker=False,
    )
    assert "assets" in result.stages
    assert "storyboard" in result.stages
    assert "video" in result.stages
    # limit 2 storyboards + 2 videos
    assert sum(1 for s in result.segment_statuses if s.segment_id.startswith("E1S")) <= 2
    assert result.segment_statuses[0].storyboard in ("ready", "failed")


@pytest.mark.asyncio
async def test_segment_failure_continues(tmp_path: Path) -> None:
    pm = _pm(tmp_path)
    _seed_script(pm, 2)
    journal = DriverJournal(tmp_path / "j.jsonl")
    # create only first storyboard file
    proj = pm.get_project_path("demo")
    (proj / "storyboards").mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (720, 1280)).save(proj / "storyboards" / "scene_E1S01.png")

    call_n = {"n": 0}

    async def fake_batch(*, project_name, specs):
        call_n["n"] += 1
        outs = []
        fails = []
        for s in specs:
            if s.resource_id == "E1S01":
                outs.append(
                    RealBatch(resource_id=s.resource_id, task_id="t1", status="succeeded", result={})
                )
                p = proj / "videos" / f"scene_{s.resource_id}.mp4"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            else:
                fails.append(
                    RealBatch(resource_id=s.resource_id, task_id="t2", status="failed", error="boom")
                )
        return outs, fails

    from ink2reel_driver import media as media_mod
    from ink2reel_driver.mediacheck import CheckResult

    media_mod.check_video = lambda *a, **k: CheckResult(True, "ready")  # type: ignore

    statuses = [
        __import__("ink2reel_driver.media", fromlist=["SegmentMediaStatus"]).SegmentMediaStatus(
            segment_id="E1S01", storyboard="ready"
        ),
        __import__("ink2reel_driver.media", fromlist=["SegmentMediaStatus"]).SegmentMediaStatus(
            segment_id="E1S02", storyboard="failed"
        ),
    ]
    out = await generate_videos(
        pm=pm,
        project_name="demo",
        episode=1,
        limit=2,
        budget=BudgetGuard(100),
        journal=journal,
        statuses=statuses,
        batch_fn=fake_batch,
    )
    by = {s.segment_id: s for s in out}
    assert by["E1S01"].video == "ready"
    assert by["E1S02"].video == "failed"  # no storyboard → failed, didn't block


@pytest.mark.asyncio
async def test_tts_skipped_no_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pm = _pm(tmp_path)
    _seed_script(pm, 1)
    journal = DriverJournal(tmp_path / "j.jsonl")
    from ink2reel_driver.media import SegmentMediaStatus

    statuses = [SegmentMediaStatus(segment_id="E1S01")]
    out = await generate_tts(
        pm=pm,
        project_name="demo",
        episode=1,
        limit=1,
        budget=BudgetGuard(10),
        journal=journal,
        statuses=statuses,
        audio_configured=False,
    )
    assert out[0].audio == "skipped"
    text = journal.path.read_text(encoding="utf-8")
    assert "no_provider" in text or "skipped" in text


@pytest.mark.asyncio
async def test_budget_stops_video_enqueue(tmp_path: Path) -> None:
    pm = _pm(tmp_path)
    _seed_script(pm, 3)
    journal = DriverJournal(tmp_path / "j.jsonl")
    proj = pm.get_project_path("demo")
    (proj / "storyboards").mkdir(parents=True, exist_ok=True)
    from PIL import Image

    for i in range(1, 4):
        Image.new("RGB", (720, 1280)).save(proj / "storyboards" / f"scene_E1S{i:02d}.png")

    from ink2reel_driver.media import SegmentMediaStatus

    statuses = [
        SegmentMediaStatus(segment_id=f"E1S{i:02d}", storyboard="ready") for i in range(1, 4)
    ]
    # cap only enough for ~1 video est ($1)
    g = BudgetGuard(cap_usd=1.5)
    g.commit(0.6, label="prior")

    async def fake_batch(*, project_name, specs):
        return [
            RealBatch(resource_id=specs[0].resource_id, task_id="t", status="succeeded", result={})
        ], []

    from ink2reel_driver import media as media_mod
    from ink2reel_driver.mediacheck import CheckResult

    media_mod.check_video = lambda *a, **k: CheckResult(True, "ready")  # type: ignore

    with pytest.raises(BudgetExhaustedError):
        # first video commits ~1.0 → spent 1.6? check before second
        # actually first check: 0.6+1.0=1.6 >= 1.5 → fails on first segment
        await generate_videos(
            pm=pm,
            project_name="demo",
            episode=1,
            limit=3,
            budget=g,
            journal=journal,
            statuses=statuses,
            batch_fn=fake_batch,
        )
