"""pipeline 状态机 mock 测试（不打真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ink2reel_driver.gate import GateDecision
from ink2reel_driver.pipeline import PipelineError, run
from ink2reel_driver.step1 import Step1Result
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager


@pytest.fixture()
def novel_file(tmp_path: Path) -> Path:
    p = tmp_path / "novel.txt"
    p.write_text("第一章\n林夏在便利店发现十七分钟空白。\n" * 20, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_pipeline_order_and_success(tmp_path: Path, novel_file: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    order: list[str] = []

    async def fake_plan(pm, project_name, episode, journal):
        order.append("plan")
        # 登记 episode
        def _mutate(p: dict) -> None:
            p["episodes"] = [{"episode": 1, "title": "E1", "script_file": "scripts/episode_1.json"}]

        pm.update_project(project_name, _mutate)
        ep = pm.get_project_path(project_name) / "source" / "episode_1.txt"
        ep.write_text("林夏在便利店发现十七分钟空白。", encoding="utf-8")

    async def fake_step1(*, pm, project_name, episode=1, **kwargs):
        order.append("step1")
        drafts = pm.get_project_path(project_name) / "drafts" / f"episode_{episode}"
        drafts.mkdir(parents=True, exist_ok=True)
        path = drafts / "step1_segments.json"
        atomic_write_json(
            path,
            {
                "segments": [
                    {
                        "segment_id": "E1S01",
                        "novel_text": "林夏在便利店发现十七分钟空白。",
                        "duration_seconds": 4,
                        "segment_break": False,
                        "characters_in_segment": [],
                        "scenes": [],
                        "props": [],
                    }
                ]
            },
        )
        return Step1Result(path=path, method="llm_boundary", retries=0, segment_count=1)

    async def fake_generate(self, episode, output_filename=None):
        order.append("script")
        out = Path(self.project_path) / "scripts" / f"episode_{episode}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        script = {
            "title": "测试",
            "content_mode": "narration",
            "duration_seconds": 4,
            "segments": [
                {
                    "segment_id": "E1S01",
                    "novel_text": "林夏在便利店发现十七分钟空白。",
                    "duration_seconds": 4,
                    "segment_break": False,
                    "characters_in_segment": [],
                    "scenes": [],
                    "props": [],
                    "image_prompt": {
                        "scene": "night convenience store",
                        "composition": {
                            "shot_type": "Medium Shot",
                            "lighting": "cold fluorescent",
                            "ambiance": "tense",
                        },
                    },
                    "video_prompt": {
                        "action": "stares at monitor",
                        "camera_motion": "Static",
                        "ambiance_audio": "fridge hum",
                        "dialogue": [],
                    },
                }
            ],
        }
        atomic_write_json(out, script)
        return out

    with (
        patch("ink2reel_driver.pipeline._plan_until_episode", side_effect=fake_plan),
        patch("ink2reel_driver.pipeline.split_narration_segments", side_effect=fake_step1),
        patch("ink2reel_driver.pipeline.ScriptGenerator.create", new_callable=AsyncMock) as create_gen,
        patch("ink2reel_driver.pipeline.GatePolicy.decide_and_apply") as gate_apply,
    ):
        gate_apply.return_value = GateDecision(
            stage="step1_review",
            mode="auto",
            approved=True,
            approved_by="auto",
            reason="mock",
        )
        gen_inst = MagicMock()
        gen_inst.generate = fake_generate
        gen_inst.project_path = None

        async def _create(path):
            order.append("script_create")
            inst = MagicMock()
            inst.project_path = path
            inst.generate = fake_generate.__get__(inst, type(inst))
            return inst

        create_gen.side_effect = _create

        summary = await run(
            novel_file,
            project_name="midnight",
            until="script",
            gate_mode="auto",
            projects_root=projects_root,
            export=True,
        )

    assert "script_path" in summary
    assert Path(summary["script_path"]).is_file()
    assert Path(summary["deliverables"]).is_dir()
    assert order == ["plan", "step1", "script_create", "script"]
    journal = projects_root / "midnight" / "driver_journal.jsonl"
    assert journal.is_file()
    text = journal.read_text(encoding="utf-8")
    assert "gate" in text
    assert "approved_by" in text or "auto" in text


@pytest.mark.asyncio
async def test_manual_gate_exit_2(tmp_path: Path, novel_file: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    async def fake_plan(pm, project_name, episode, journal):
        def _mutate(p: dict) -> None:
            p["episodes"] = [{"episode": 1, "title": "E1", "script_file": "scripts/episode_1.json"}]

        pm.update_project(project_name, _mutate)

    async def fake_step1(*, pm, project_name, episode=1, **kwargs):
        drafts = pm.get_project_path(project_name) / "drafts" / f"episode_{episode}"
        drafts.mkdir(parents=True, exist_ok=True)
        path = drafts / "step1_segments.json"
        atomic_write_json(
            path,
            {
                "segments": [
                    {
                        "segment_id": "E1S01",
                        "novel_text": "x",
                        "duration_seconds": 4,
                        "segment_break": False,
                        "characters_in_segment": [],
                        "scenes": [],
                        "props": [],
                    }
                ]
            },
        )
        return Step1Result(path=path, method="llm_boundary", retries=0, segment_count=1)

    with (
        patch("ink2reel_driver.pipeline._plan_until_episode", side_effect=fake_plan),
        patch("ink2reel_driver.pipeline.split_narration_segments", side_effect=fake_step1),
        patch("ink2reel_driver.pipeline.GatePolicy.decide_and_apply") as gate_apply,
    ):
        gate_apply.return_value = GateDecision(
            stage="step1_review",
            mode="manual",
            approved=False,
            approved_by=None,
            reason="pending human review",
            exit_code=2,
        )
        with pytest.raises(PipelineError) as ei:
            await run(
                novel_file,
                project_name="midnight",
                until="script",
                gate_mode="manual",
                projects_root=projects_root,
                export=False,
            )
        assert ei.value.exit_code == 2


@pytest.mark.asyncio
async def test_until_media_alias_runs_media_path(tmp_path: Path, novel_file: Path) -> None:
    """until=media 归一为 deliverables；无剧本时会走文本链（此处 mock 跳过并断言入口可达）。"""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    with (
        patch("ink2reel_driver.pipeline._run_text_chain", new_callable=AsyncMock) as text,
        patch("ink2reel_driver.pipeline.run_media_chain", new_callable=AsyncMock) as media,
        patch("ink2reel_driver.pipeline.export_episode") as exp,
    ):
        from ink2reel_driver.media import MediaRunResult

        text.return_value = Path("scripts/episode_1.json")
        media.return_value = MediaRunResult([], {"spent_usd": 0}, ["assets"])
        exp.return_value = Path("deliverables")

        # 先建空项目目录逻辑：run 会 create project
        summary = await run(
            novel_file,
            project_name="demo",
            until="media",
            projects_root=projects_root,
            start_worker=False,
            budget_usd=5,
            limit_segments=2,
        )
        assert summary["until"] == "deliverables"
        media.assert_awaited()
