"""judge 单测（mock LLM）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ink2reel_driver.qc.judge import judge_script


def _episode() -> dict:
    return {
        "title": "t",
        "segments": [
            {
                "segment_id": "E1S01",
                "novel_text": "足够长的旁白" * 10,
                "duration_seconds": 6,
                "image_prompt": {"scene": "store"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_judge_ok() -> None:
    payload = {
        "hook": 8,
        "predictability": 7,
        "homogenization": 8,
        "payoff_density": 7,
        "character_id": 8,
        "cliche": 9,
        "issues": [{"segment_id": "E1S01", "message": "ok"}],
    }

    async def llm(_p: str) -> str:
        return json.dumps(payload)

    r = await judge_script(_episode(), llm=llm)
    assert r.status == "ok"
    assert r.total == 47
    assert r.dims["hook"] == 8


@pytest.mark.asyncio
async def test_judge_repair_once() -> None:
    good = {
        "hook": 6,
        "predictability": 6,
        "homogenization": 6,
        "payoff_density": 6,
        "character_id": 6,
        "cliche": 6,
        "issues": [],
    }
    calls = {"n": 0}

    async def llm(p: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-json {{"
        return json.dumps(good)

    r = await judge_script(_episode(), llm=llm)
    assert r.status == "ok"
    assert r.total == 36
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_judge_unavailable() -> None:
    async def llm(_p: str) -> str:
        return "still broken"

    r = await judge_script(_episode(), llm=llm)
    assert r.status == "judge_unavailable"
    assert r.total is None
    assert r.error
