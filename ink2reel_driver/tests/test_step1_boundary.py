"""step1 边界切分单测（mock LLM，不打真实 API）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ink2reel_driver.step1 import (
    assemble_from_boundary_draft,
    split_narration_segments,
)
from lib.project_manager import ProjectManager


def _novel() -> str:
    # 构造可被 end_quote 精确命中的多段正文（每段 ≥40 非空白字）
    parts = [
        "凌晨两点十七分，林夏把监控回放往前拖了四十分钟，画面里的自己正蹲在货架前补饮料，动作很慢。",
        "收银台的蓝光映在她脸上，冷白荧光灯嗡嗡地响，像有只苍蝇贴着灯管飞个不停，令人心烦。",
        "她按了暂停，盯着屏幕右下角的时间戳，数字一格一格跳动得不对劲，她又拖了一次进度条。",
        "十七分钟像被人用剪刀剪掉，两头缝在一起，后巷排水管的声控灯坏了，她摸着黑扔垃圾袋。",
        "一个穿深色外套的男人站在收银台前，掏出红色旧钥匙扣放在台面上，反光刺得她眯起眼。",
    ]
    return "".join(parts)


def test_assemble_exact_coverage() -> None:
    novel = _novel()
    # 每段 end_quote 取段尾
    quotes = [
        "动作很慢。",
        "令人心烦。",
        "进度条。",
        "扔垃圾袋。",
        "眯起眼。",
    ]
    draft = {
        "segments": [
            {
                "end_quote": q,
                "characters": ["林夏"] if i == 0 else [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": i > 0,
            }
            for i, q in enumerate(quotes)
        ]
    }
    segs, problems = assemble_from_boundary_draft(
        novel, draft, episode=1, supported_durations=[4, 6, 8]
    )
    assert problems == []
    assert segs
    assert "".join(s["novel_text"] for s in segs) == novel
    assert all(s["duration_seconds"] in (4, 6, 8) for s in segs)


def test_assemble_missing_quote() -> None:
    novel = _novel()
    draft = {
        "segments": [
            {
                "end_quote": "这段话原文里根本没有XYZ",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 4,
                "segment_break": False,
            }
        ]
    }
    segs, problems = assemble_from_boundary_draft(
        novel, draft, episode=1, supported_durations=[4, 6, 8]
    )
    assert segs == []
    assert any("找不到" in p for p in problems)


@pytest.mark.asyncio
async def test_split_happy_path_mock(tmp_path: Path) -> None:
    pm = ProjectManager(str(tmp_path / "projects"))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    novel = _novel()
    ep = pm.get_project_path("demo") / "source" / "episode_1.txt"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(novel, encoding="utf-8")

    quotes = [
        "动作很慢。",
        "令人心烦。",
        "进度条。",
        "扔垃圾袋。",
        "眯起眼。",
    ]
    payload = {
        "segments": [
            {
                "end_quote": q,
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            }
            for q in quotes
        ]
    }
    import json

    gen = MagicMock()
    gen.generate = AsyncMock(return_value=MagicMock(text=json.dumps(payload, ensure_ascii=False)))

    result = await split_narration_segments(
        pm=pm, project_name="demo", episode=1, generator=gen, max_attempts=3
    )
    assert result.method == "llm_boundary"
    assert result.retries == 0
    assert result.path.is_file()
    data = json.loads(result.path.read_text(encoding="utf-8"))
    assert "".join(s["novel_text"] for s in data["segments"]) == novel


@pytest.mark.asyncio
async def test_split_retry_then_success(tmp_path: Path) -> None:
    pm = ProjectManager(str(tmp_path / "projects"))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    novel = _novel()
    ep = pm.get_project_path("demo") / "source" / "episode_1.txt"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(novel, encoding="utf-8")

    bad = {
        "segments": [
            {
                "end_quote": "不存在的引用句ABC",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 4,
                "segment_break": False,
            }
        ]
    }
    good = {
        "segments": [
            {
                "end_quote": "动作很慢。",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            },
            {
                "end_quote": "令人心烦。",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            },
            {
                "end_quote": "进度条。",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            },
            {
                "end_quote": "扔垃圾袋。",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            },
            {
                "end_quote": "眯起眼。",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 6,
                "segment_break": False,
            },
        ]
    }
    import json

    gen = MagicMock()
    gen.generate = AsyncMock(
        side_effect=[
            MagicMock(text=json.dumps(bad, ensure_ascii=False)),
            MagicMock(text=json.dumps(good, ensure_ascii=False)),
        ]
    )
    result = await split_narration_segments(
        pm=pm, project_name="demo", episode=1, generator=gen, max_attempts=3
    )
    assert result.method == "llm_boundary"
    assert result.retries == 1
    assert gen.generate.await_count == 2


@pytest.mark.asyncio
async def test_split_fallback_after_3(tmp_path: Path) -> None:
    pm = ProjectManager(str(tmp_path / "projects"))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    novel = _novel()
    ep = pm.get_project_path("demo") / "source" / "episode_1.txt"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(novel, encoding="utf-8")

    bad = {
        "segments": [
            {
                "end_quote": "完全找不到的引用",
                "characters": [],
                "scenes": [],
                "props": [],
                "duration_tier": 4,
                "segment_break": False,
            }
        ]
    }
    import json

    gen = MagicMock()
    gen.generate = AsyncMock(return_value=MagicMock(text=json.dumps(bad, ensure_ascii=False)))
    result = await split_narration_segments(
        pm=pm, project_name="demo", episode=1, generator=gen, max_attempts=3
    )
    assert result.method == "mechanical_fallback"
    assert result.retries == 2  # attempts 1..3, last retries=2
    data = json.loads(result.path.read_text(encoding="utf-8"))
    assert "".join(s["novel_text"] for s in data["segments"]) == novel
    assert gen.generate.await_count == 3
