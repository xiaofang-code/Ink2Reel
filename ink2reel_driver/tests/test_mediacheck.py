"""mediacheck 单测（不依赖真实 ffprobe 时用 monkeypatch）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ink2reel_driver import mediacheck


def test_image_missing(tmp_path: Path) -> None:
    r = mediacheck.check_image(tmp_path / "no.png")
    assert r.ok is False
    assert r.status == "missing"
    assert r.reason  # 非空


def test_image_too_small(tmp_path: Path) -> None:
    from PIL import Image

    p = tmp_path / "tiny.png"
    Image.new("RGB", (100, 100), color=(1, 2, 3)).save(p)
    r = mediacheck.check_image(p, min_short_side=512)
    assert r.ok is False
    assert r.status == "failed"
    assert "short side" in r.reason


def test_image_ok(tmp_path: Path) -> None:
    from PIL import Image

    p = tmp_path / "ok.png"
    Image.new("RGB", (720, 1280), color=(10, 20, 30)).save(p)
    r = mediacheck.check_image(p)
    assert r.ok is True
    assert r.status == "ready"


def test_video_missing(tmp_path: Path) -> None:
    r = mediacheck.check_video(tmp_path / "x.mp4", expected_duration_s=4)
    assert r.status == "missing"
    assert r.reason


def test_video_ffprobe_missing_reports_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3：ffprobe 缺失必须 failed + 非空 reason（ffprobe_not_found），禁止 soft-ok。"""
    p = tmp_path / "a.mp4"
    p.write_bytes(b"x" * 60_000)
    monkeypatch.setattr(mediacheck, "_ffprobe_bin", lambda: None)
    monkeypatch.setattr(mediacheck, "_ffmpeg_bin", lambda: None)
    r = mediacheck.check_video(p, expected_duration_s=4)
    assert r.ok is False
    assert r.status == "failed"
    assert r.reason == "ffprobe_not_found"


def test_video_ffprobe_missing_tiny_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "a.mp4"
    p.write_bytes(b"not-a-video")
    monkeypatch.setattr(mediacheck, "_ffprobe_bin", lambda: None)
    monkeypatch.setattr(mediacheck, "_ffmpeg_bin", lambda: None)
    r = mediacheck.check_video(p, expected_duration_s=4)
    assert r.ok is False
    assert r.reason == "ffprobe_not_found"


def test_video_aspect_mismatch_reason_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "v.mp4"
    p.write_bytes(b"fake")

    def landscape(_path: Path) -> dict:
        return {
            "format": {"duration": "4.0"},
            "streams": [{"codec_type": "video", "width": 848, "height": 480, "duration": "4.0"}],
        }

    monkeypatch.setattr(mediacheck, "ffprobe_json", landscape)
    r = mediacheck.check_video(p, expected_duration_s=4)
    assert r.ok is False
    assert r.reason == "aspect_mismatch_848x480_landscape"
    assert "aspect_mismatch" in r.reason


def test_video_aspect_and_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "v.mp4"
    p.write_bytes(b"fake")

    def fake_probe(_path: Path) -> dict:
        return {
            "format": {"duration": "4.0"},
            "streams": [{"codec_type": "video", "width": 1080, "height": 1920, "duration": "4.0"}],
        }

    monkeypatch.setattr(mediacheck, "ffprobe_json", fake_probe)
    r = mediacheck.check_video(p, expected_duration_s=4)
    assert r.ok is True

    def bad_aspect(_path: Path) -> dict:
        return {
            "format": {"duration": "4.0"},
            "streams": [{"codec_type": "video", "width": 100, "height": 100}],
        }

    monkeypatch.setattr(mediacheck, "ffprobe_json", bad_aspect)
    r2 = mediacheck.check_video(p, expected_duration_s=4)
    assert r2.ok is False
    assert "aspect_mismatch" in r2.reason

    # 16:9 仅在 allow_landscape=True 时通过；默认契约 9:16 拒绝
    def landscape(_path: Path) -> dict:
        return {
            "format": {"duration": "4.0"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        }

    monkeypatch.setattr(mediacheck, "ffprobe_json", landscape)
    assert mediacheck.check_video(p, expected_duration_s=4).ok is False
    assert mediacheck.check_video(p, expected_duration_s=4, allow_landscape=True).ok is True

    def bad_dur(_path: Path) -> dict:
        return {
            "format": {"duration": "20.0"},
            "streams": [{"codec_type": "video", "width": 1080, "height": 1920}],
        }

    monkeypatch.setattr(mediacheck, "ffprobe_json", bad_dur)
    r3 = mediacheck.check_video(p, expected_duration_s=4, duration_tol=0.5)
    assert r3.ok is False
    assert r3.reason.startswith("duration_deviation(")
    assert "%" in r3.reason


def test_format_video_status_label() -> None:
    assert mediacheck.format_video_status_label("ready") == "ready"
    assert (
        mediacheck.format_video_status_label("failed", "aspect_mismatch_848x480_landscape")
        == "failed(aspect_mismatch_848x480_landscape)"
    )
    assert mediacheck.format_video_status_label("failed", "") == "failed(unknown)"
