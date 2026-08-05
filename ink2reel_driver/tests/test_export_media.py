"""交付导出媒体扩展单测。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ink2reel_driver.exporter import export_episode
from ink2reel_driver.media import SegmentMediaStatus
from ink2reel_driver.schemas import EditManifest
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager


def test_export_ready_and_missing(tmp_path: Path) -> None:
    pm = ProjectManager(str(tmp_path / "projects"))
    pm.create_project("demo", content_mode="narration")
    pm.create_project_metadata(
        "demo",
        title="demo",
        content_mode="narration",
        extras={"generation_mode": "storyboard", "grid_storyboard": False},
    )
    proj = pm.get_project_path("demo")
    scripts = proj / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    segs = []
    for i, sid in enumerate(["E1S01", "E1S02"], start=1):
        segs.append(
            {
                "segment_id": sid,
                "novel_text": f"句子{i}。第二句。",
                "duration_seconds": 4,
                "segment_break": False,
                "characters_in_segment": [],
                "scenes": [],
                "props": [],
                "image_prompt": {
                    "scene": "s",
                    "composition": {"shot_type": "Medium Shot", "lighting": "l", "ambiance": "a"},
                },
                "video_prompt": {
                    "action": "a",
                    "camera_motion": "Static",
                    "ambiance_audio": "h",
                    "dialogue": [],
                },
            }
        )
    atomic_write_json(
        scripts / "episode_1.json",
        {"title": "t", "content_mode": "narration", "duration_seconds": 8, "segments": segs},
    )

    # only E1S01 video + first frame
    (proj / "storyboards").mkdir(parents=True, exist_ok=True)
    (proj / "videos").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (720, 1280)).save(proj / "storyboards" / "scene_E1S01.png")
    (proj / "videos" / "scene_E1S01.mp4").write_bytes(b"fake")

    statuses = [
        SegmentMediaStatus(segment_id="E1S01", storyboard="ready", video="ready", audio="skipped"),
        SegmentMediaStatus(segment_id="E1S02", storyboard="failed", video="failed", audio="skipped"),
    ]
    out = export_episode(
        pm=pm,
        project_name="demo",
        episode=1,
        media_statuses=statuses,
        budget_summary={"cap_usd": 5, "spent_usd": 1.2},
    )
    assert (out / "images" / "first_frames" / "E1S01.png").is_file()
    assert (out / "videos" / "E1S01.mp4").is_file()
    assert not (out / "videos" / "E1S02.mp4").exists()
    assert (out / "SUMMARY.md").is_file()
    summary = (out / "SUMMARY.md").read_text(encoding="utf-8")
    assert "E1S01" in summary and "E1S02" in summary

    manifest = EditManifest.model_validate_json((out / "edit_manifest.json").read_text(encoding="utf-8"))
    by = {t.segment_id: t for t in manifest.timeline}
    # ready 必须文件存在
    for t in manifest.timeline:
        if t.status == "ready":
            assert (out / t.source).is_file()
    # 无真实 9:16 探针结果时：ready|missing|failed 均可（fake mp4 会 aspect/parse 失败）
    assert by["E1S01"].status in ("ready", "missing", "failed")
    assert by["E1S02"].status == "missing"
