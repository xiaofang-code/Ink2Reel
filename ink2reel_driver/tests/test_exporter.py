"""exporter / Schema / SRT 测试（不打真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ink2reel_driver.exporter import (
    build_transcript_lines,
    cues_to_srt,
    estimate_duration_s,
    expand_srt_cues,
    export_episode,
    lines_to_srt,
    parse_srt,
    srt_timestamp_to_seconds,
)
from ink2reel_driver.schemas import EditManifest, TranscriptDocument
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager


def test_estimate_min_duration() -> None:
    assert estimate_duration_s("") >= 1.5
    assert estimate_duration_s("短") >= 1.5
    long = "字" * 45
    assert abs(estimate_duration_s(long) - 10.0) < 0.01


def test_transcript_monotonic() -> None:
    segs = [
        {"segment_id": "E1S01", "novel_text": "甲" * 9},
        {"segment_id": "E1S02", "novel_text": "乙" * 18},
    ]
    lines = build_transcript_lines(segs)
    doc = TranscriptDocument(project="p", episode=1, lines=lines)
    assert doc.lines[0].est_start_s == 0
    assert doc.lines[1].est_start_s >= doc.lines[0].est_start_s
    assert all(ln.est_start_s >= 0 and ln.est_duration_s >= 0 for ln in doc.lines)


def test_srt_parse_count() -> None:
    segs = [
        {"segment_id": "E1S01", "novel_text": "第一段旁白内容。"},
        {"segment_id": "E1S02", "novel_text": "第二段旁白内容更长一些。"},
    ]
    lines = build_transcript_lines(segs)
    srt = lines_to_srt(lines)
    entries = parse_srt(srt)
    assert len(entries) == len(lines) == 2
    assert "-->" in srt


def test_export_tree(tmp_path: Path) -> None:
    pm = ProjectManager(str(tmp_path / "projects"))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    project_path = pm.get_project_path("demo")
    scripts = project_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    def _seg(sid: str, text: str, *, brk: bool = False) -> dict:
        return {
            "segment_id": sid,
            "novel_text": text,
            "duration_seconds": 4,
            "segment_break": brk,
            "characters_in_segment": [],
            "scenes": [],
            "props": [],
            "image_prompt": {
                "scene": "convenience store at night, cold fluorescent light",
                "composition": {
                    "shot_type": "Medium Shot",
                    "lighting": "cold fluorescent",
                    "ambiance": "tense night",
                },
            },
            "video_prompt": {
                "action": "woman stares at monitor",
                "camera_motion": "Static",
                "ambiance_audio": "fridge hum",
                "dialogue": [],
            },
        }

    script = {
        "title": "测试集",
        "content_mode": "narration",
        "duration_seconds": 8,
        "segments": [
            _seg("E1S01", "凌晨两点，林夏盯着监控。"),
            _seg("E1S02", "十七分钟的空白像刀口。", brk=True),
        ],
    }
    atomic_write_json(scripts / "episode_1.json", script)

    out = export_episode(pm=pm, project_name="demo", episode=1)
    assert out.name == "EP-001"
    assert (out / "script" / "episode_script.md").is_file()
    assert (out / "script" / "storyboard.json").is_file()
    assert (out / "transcript" / "transcript.json").is_file()
    assert (out / "transcript" / "subtitles.srt").is_file()
    assert (out / "images" / ".gitkeep").is_file()
    assert (out / "videos" / ".gitkeep").is_file()
    assert (out / "edit_manifest.json").is_file()

    manifest = EditManifest.model_validate_json((out / "edit_manifest.json").read_text(encoding="utf-8"))
    assert manifest.canvas.width == 1080
    assert manifest.canvas.height == 1920
    assert len(manifest.timeline) == 2
    assert all(t.status in ("pending_media", "ready", "missing", "failed") for t in manifest.timeline)

    tdoc = TranscriptDocument.model_validate_json(
        (out / "transcript" / "transcript.json").read_text(encoding="utf-8")
    )
    assert len(tdoc.lines) == 2
    srt_entries = parse_srt((out / "transcript" / "subtitles.srt").read_text(encoding="utf-8"))
    # 句级 SRT：条数 ≥ segment 数
    assert len(srt_entries) >= len(tdoc.lines)


def test_srt_sentence_split_long_segment() -> None:
    """24s 量级长段应切出 ≥3 条，单条 ≤7s，无 #，时间轴连续。"""
    # ~108 字 → transcript 段时长约 24s
    text = (
        "凌晨两点，林夏盯着监控屏幕上的时间戳，数字跳得不对劲。"
        "十七分钟像被人用剪刀剪掉，货架与收银台之间出现了空白。"
        "一个穿深色外套的男人放下红色钥匙扣，转身走向后门消失。"
        "她把回放拖回去，又确认了一遍断层，心跳得很快。"
    )
    segs = [
        {
            "segment_id": "E1S01",
            "novel_text": text,
            "duration_seconds": 24,
            "segment_break": False,
            "characters_in_segment": [],
            "scenes": [],
            "props": [],
        }
    ]
    lines = build_transcript_lines(segs)
    assert lines[0].est_duration_s >= 20  # 接近 24s 量级
    cues = expand_srt_cues(segs, lines)
    assert len(cues) >= 3
    assert all(c["duration"] <= 7.0 + 1e-6 for c in cues)
    assert all(not str(c["text"]).lstrip().startswith("#") for c in cues)
    # 时间轴连续无重叠
    for i in range(1, len(cues)):
        prev_end = cues[i - 1]["start"] + cues[i - 1]["duration"]
        assert cues[i]["start"] + 1e-6 >= prev_end
    # 段内总时长一致 ±0.1s
    total = sum(c["duration"] for c in cues)
    assert abs(total - lines[0].est_duration_s) <= 0.1

    srt = cues_to_srt(cues)
    assert "#" not in "".join(e["text"] for e in parse_srt(srt))
    entries = parse_srt(srt)
    for e in entries:
        start = srt_timestamp_to_seconds(e["start"])
        end = srt_timestamp_to_seconds(e["end"])
        assert end - start <= 7.0 + 0.05


def test_srt_strips_hash_prefix() -> None:
    segs = [
        {
            "segment_id": "E1S01",
            "novel_text": "# 不该出现\n正常旁白句子。",
            "duration_seconds": 4,
            "segment_break": False,
            "characters_in_segment": [],
            "scenes": [],
            "props": [],
        }
    ]
    cues = expand_srt_cues(segs)
    assert all(not c["text"].lstrip().startswith("#") for c in cues)
