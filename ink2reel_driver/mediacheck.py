"""媒体产物校验：图可解码/短边≥512；视频 ffprobe 时长与 9:16。"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

MediaStatus = Literal["ready", "failed", "skipped", "missing"]


@dataclass
class CheckResult:
    ok: bool
    status: MediaStatus
    reason: str = ""
    meta: dict[str, Any] | None = None


def _ffprobe_bin() -> str | None:
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def _ffmpeg_bin() -> str | None:
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    # 可选：imageio-ffmpeg 捆绑的 ffmpeg（无系统 PATH 时仍可解析宽高/时长）
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001
        pass
    return None


def check_image(path: Path, *, min_short_side: int = 512) -> CheckResult:
    if not path.is_file():
        return CheckResult(False, "missing", f"file not found: {path}")
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            w, h = im.size
        short = min(w, h)
        if short < min_short_side:
            return CheckResult(
                False,
                "failed",
                f"short side {short} < {min_short_side}",
                {"width": w, "height": h},
            )
        return CheckResult(True, "ready", reason="ok", meta={"width": w, "height": h})
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, "failed", f"decode error: {exc}")


def ffprobe_json(path: Path) -> dict[str, Any]:
    bin_ = _ffprobe_bin()
    if bin_:
        cmd = [
            bin_,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed ({proc.returncode}): {proc.stderr[:300]}")
        return json.loads(proc.stdout or "{}")

    # 回退：仅有 ffmpeg 时解析 stderr 中的 Duration / Video 行
    ff = _ffmpeg_bin()
    if not ff:
        # 稳定错误码：T3 要求 ffprobe 缺失时报清晰错误，禁止 soft-ok
        raise RuntimeError("ffprobe_not_found")
    proc = subprocess.run(
        [ff, "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    err = proc.stderr or ""
    dur = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w = h = 0
    m2 = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", err)
    if m2:
        w, h = int(m2.group(1)), int(m2.group(2))
    if not w or not h:
        raise RuntimeError(f"ffmpeg -i could not parse video stream: {err[-400:]}")
    return {
        "format": {"duration": str(dur) if dur is not None else None},
        "streams": [{"codec_type": "video", "width": w, "height": h, "duration": str(dur) if dur else None}],
    }


def check_video(
    path: Path,
    *,
    expected_duration_s: float | None = None,
    duration_tol: float = 0.5,
    aspect_w: int = 9,
    aspect_h: int = 16,
    aspect_tol: float = 0.10,
    allow_landscape: bool = False,
) -> CheckResult:
    """校验视频。契约默认 9:16 竖屏；失败 reason 永不为空。

    reason 约定：
    - ``ffprobe_not_found``
    - ``aspect_mismatch(WxH)`` / ``aspect_mismatch_WxH_landscape``
    - ``duration_deviation(xx%)``
    """
    if not path.is_file():
        return CheckResult(False, "missing", f"file not found: {path}")
    try:
        data = ffprobe_json(path)
    except RuntimeError as exc:
        msg = str(exc) or "ffprobe_error"
        if "ffprobe_not_found" in msg or "not found on PATH" in msg:
            return CheckResult(False, "failed", "ffprobe_not_found")
        return CheckResult(False, "failed", msg)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, "failed", f"ffprobe parse: {exc}")

    streams = data.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not vstream:
        return CheckResult(False, "failed", "no video stream")

    try:
        w = int(vstream.get("width") or 0)
        h = int(vstream.get("height") or 0)
    except (TypeError, ValueError):
        w, h = 0, 0
    if w <= 0 or h <= 0:
        return CheckResult(False, "failed", "invalid width/height")

    ratio = w / h
    targets = [aspect_w / aspect_h]
    if allow_landscape:
        targets.append(aspect_h / aspect_w)  # 16:9 when contract is 9:16
    if not any(abs(ratio - t) / t <= aspect_tol for t in targets):
        orient = "landscape" if w > h else ("portrait" if h > w else "square")
        # 主 reason：机器可读；兼容 grep aspect_mismatch
        reason = f"aspect_mismatch_{w}x{h}_{orient}"
        return CheckResult(
            False,
            "failed",
            reason,
            {"width": w, "height": h, "aspect_mismatch": f"{w}x{h}"},
        )

    dur = None
    fmt = data.get("format") or {}
    try:
        if fmt.get("duration") is not None:
            dur = float(fmt["duration"])
        elif vstream.get("duration") is not None:
            dur = float(vstream["duration"])
    except (TypeError, ValueError):
        dur = None

    if expected_duration_s is not None and dur is not None and expected_duration_s > 0:
        rel = abs(dur - expected_duration_s) / expected_duration_s
        if rel > duration_tol:
            pct = int(round(rel * 100))
            return CheckResult(
                False,
                "failed",
                f"duration_deviation({pct}%)",
                {"duration_s": dur, "expected_s": expected_duration_s, "width": w, "height": h},
            )

    return CheckResult(
        True,
        "ready",
        reason="ok",
        meta={"duration_s": dur, "width": w, "height": h},
    )


def probe_duration_s(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        data = ffprobe_json(path)
        d = (data.get("format") or {}).get("duration")
        return float(d) if d is not None else None
    except Exception:  # noqa: BLE001
        return None


def format_video_status_label(status: str, reason: str = "") -> str:
    """SUMMARY 用：failed + reason → ``failed(aspect_mismatch_...)``。"""
    if status != "failed":
        return status
    r = (reason or "").strip()
    if not r:
        return "failed(unknown)"
    # 已是 failed(...) 则原样
    if r.startswith("failed("):
        return r
    return f"failed({r})"
