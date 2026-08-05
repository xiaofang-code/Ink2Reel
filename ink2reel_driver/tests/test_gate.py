"""GatePolicy 单元测试（不打真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ink2reel_driver.gate import GatePolicy
from ink2reel_driver.qc.judge import JudgeResult
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager


@pytest.fixture()
def pm(tmp_path: Path) -> ProjectManager:
    root = tmp_path / "projects"
    root.mkdir()
    return ProjectManager(str(root))


def _seed_project(pm: ProjectManager, name: str = "demo") -> Path:
    pm.create_project(name, content_mode="narration")
    pm.create_project_metadata(
        name,
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )

    def _mutate(p: dict) -> None:
        p["episodes"] = [{"episode": 1, "title": "E1", "script_file": "scripts/episode_1.json"}]

    pm.update_project(name, _mutate)
    project_path = pm.get_project_path(name)
    drafts = project_path / "drafts" / "episode_1"
    drafts.mkdir(parents=True, exist_ok=True)
    step1 = {
        "segments": [
            {
                "segment_id": "E1S01",
                "novel_text": "测试旁白。",
                "duration_seconds": 4,
                "segment_break": False,
                "characters_in_segment": [],
                "scenes": [],
                "props": [],
            }
        ]
    }
    atomic_write_json(drafts / "step1_segments.json", step1)
    return project_path


def _write_script(pm: ProjectManager, name: str = "demo") -> Path:
    novel = "这是一段足够长度的旁白文本用于通过字数校验规则。" * 2
    script = {
        "title": "t",
        "segments": [
            {
                "segment_id": "E1S01",
                "novel_text": novel,
                "duration_seconds": 8,
                "image_prompt": {"scene": "unique store scene alpha"},
            },
            {
                "segment_id": "E1S02",
                "novel_text": novel,
                "duration_seconds": 10,
                "image_prompt": {"scene": "unique counter scene beta"},
            },
        ],
    }
    path = pm.get_project_path(name) / "scripts" / "episode_1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return path


def test_auto_confirms(pm: ProjectManager) -> None:
    _seed_project(pm)
    d = GatePolicy(stage="step1_review", mode="auto").decide_and_apply(
        pm=pm, project_name="demo", episode=1
    )
    assert d.approved is True
    assert d.approved_by == "auto"
    project = pm.load_project("demo")
    review = project["episodes"][0].get("step1_review")
    assert review and review.get("fingerprint")


def test_manual_blocks(pm: ProjectManager) -> None:
    _seed_project(pm)
    d = GatePolicy(stage="step1_review", mode="manual").decide_and_apply(
        pm=pm, project_name="demo", episode=1
    )
    assert d.approved is False
    assert d.exit_code == 2
    assert d.approved_by is None


@pytest.mark.asyncio
async def test_threshold_pass(pm: ProjectManager) -> None:
    _seed_project(pm)
    _write_script(pm)

    async def fake_judge(_path):
        return JudgeResult(
            status="ok",
            total=55,
            dims={
                "hook": 9,
                "predictability": 9,
                "homogenization": 9,
                "payoff_density": 9,
                "character_id": 9,
                "cliche": 10,
            },
        )

    d = await GatePolicy(
        stage="script_qc", mode="threshold", threshold=50, judge_fn=fake_judge
    ).decide_and_apply_async(pm=pm, project_name="demo", episode=1)
    assert d.approved is True
    assert d.approved_by == "threshold"
    assert d.escalated is False
    assert d.scores.get("total") == 55


@pytest.mark.asyncio
async def test_threshold_low_score_escalate(pm: ProjectManager) -> None:
    _seed_project(pm)
    _write_script(pm)

    async def fake_judge(_path):
        return JudgeResult(status="ok", total=30, dims={"hook": 5})

    d = await GatePolicy(
        stage="script_qc", mode="threshold", threshold=50, judge_fn=fake_judge
    ).decide_and_apply_async(pm=pm, project_name="demo", episode=1)
    assert d.approved is False
    assert d.escalated is True
    assert d.exit_code == 0
    assert d.exception_ids


@pytest.mark.asyncio
async def test_threshold_judge_unavailable_escalate(pm: ProjectManager) -> None:
    _seed_project(pm)
    _write_script(pm)

    async def fake_judge(_path):
        return JudgeResult(status="judge_unavailable", error="parse fail")

    d = await GatePolicy(
        stage="script_qc", mode="threshold", threshold=50, judge_fn=fake_judge
    ).decide_and_apply_async(pm=pm, project_name="demo", episode=1)
    assert d.escalated is True
    assert d.approved is False
    assert "judge_unavailable" in d.reason
