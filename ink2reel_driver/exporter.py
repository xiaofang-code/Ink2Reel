"""交付文件夹导出器：script / transcript(+句级 SRT) / edit_manifest + 空 images/videos。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.project_manager import ProjectManager, get_project_manager
from lib.script_models import NarrationEpisodeScript

from ink2reel_driver.mediacheck import (
    check_video,
    format_video_status_label,
    probe_duration_s,
)
from ink2reel_driver.schemas import (
    CanvasSpec,
    EditManifest,
    StoryboardMeta,
    TimelineItem,
    TranscriptDocument,
    TranscriptLine,
)

# transcript 段级时点：中文字数 ÷ 4.5 字/秒；最短 1.5s
CHARS_PER_SECOND = 4.5
MIN_DURATION_S = 1.5

# SRT 句级
SRT_MAX_CHARS = 22
SRT_MIN_S = 1.2
SRT_MAX_S = 7.0
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？…；\n])")
_COMMA_SPLIT = re.compile(r"(?<=[，、])")


def estimate_duration_s(text: str) -> float:
    n = sum(1 for c in text if not c.isspace())
    return max(MIN_DURATION_S, n / CHARS_PER_SECOND)


def _strip_md_prefix(text: str) -> str:
    """双保险：去掉行首 # / > 。"""
    lines = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            s = re.sub(r"^#{1,6}\s*", "", s)
        if s.startswith(">"):
            s = re.sub(r"^>\s?", "", s)
        lines.append(s)
    return "\n".join(lines).strip()


def build_transcript_lines(segments: list[dict[str, Any]]) -> list[TranscriptLine]:
    """逐 segment 一条（时点估算）；与 SRT 句级拆分独立。"""
    lines: list[TranscriptLine] = []
    t_ms = 0
    for seg in segments:
        text = _strip_md_prefix(str(seg.get("novel_text") or ""))
        dur = estimate_duration_s(text)
        dur_ms = max(int(round(dur * 1000)), int(MIN_DURATION_S * 1000))
        lines.append(
            TranscriptLine(
                segment_id=str(seg.get("segment_id") or f"S{len(lines) + 1:02d}"),
                kind="narration",
                text=text,
                est_start_s=t_ms / 1000.0,
                est_duration_s=dur_ms / 1000.0,
            )
        )
        t_ms += dur_ms
    return lines


def split_sentences_for_srt(text: str) -> list[str]:
    """按句切分，单条 ≤22 字；超长在逗号处再切。"""
    text = _strip_md_prefix(text)
    if not text:
        return []

    raw_parts = [p for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    # 若没有句读，整段作为一句
    if not raw_parts:
        raw_parts = [text]

    pieces: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= SRT_MAX_CHARS:
            pieces.append(part)
            continue
        # 逗号再切
        sub = [s for s in _COMMA_SPLIT.split(part) if s and s.strip()]
        if len(sub) <= 1:
            # 硬切
            for i in range(0, len(part), SRT_MAX_CHARS):
                chunk = part[i : i + SRT_MAX_CHARS].strip()
                if chunk:
                    pieces.append(chunk)
        else:
            buf = ""
            for s in sub:
                s = s.strip()
                if not s:
                    continue
                if not buf:
                    buf = s
                elif len(buf) + len(s) <= SRT_MAX_CHARS:
                    buf += s
                else:
                    pieces.append(buf)
                    buf = s
            if buf:
                if len(buf) <= SRT_MAX_CHARS:
                    pieces.append(buf)
                else:
                    for i in range(0, len(buf), SRT_MAX_CHARS):
                        chunk = buf[i : i + SRT_MAX_CHARS].strip()
                        if chunk:
                            pieces.append(chunk)
    return pieces


def _allocate_sentence_durations(sentences: list[str], total_s: float) -> list[float]:
    """按字数比例分配段时长；单条 clamp 到 [1.2, 7]，超出则均分拆条后的时长再归一。"""
    if not sentences:
        return []
    weights = [max(1, sum(1 for c in s if not c.isspace())) for s in sentences]
    wsum = sum(weights)
    raw = [total_s * w / wsum for w in weights]

    # 先 clamp 到 [min, max]；若总和偏离则按比例微调（在 clamp 内尽量）
    clamped = [min(SRT_MAX_S, max(SRT_MIN_S, d)) for d in raw]
    # 若段总时长 < n*min，抬升 total
    min_total = SRT_MIN_S * len(sentences)
    target = max(total_s, min_total)

    # 对仍 > max 的（理论上 clamp 后不会）——若字数极不均，均分超过 max 的部分
    # 重新：若任何一条在比例分配后 > max，拆成多条已在 split 阶段处理；此处只保证时长
    s = sum(clamped)
    if s <= 0:
        return [target / len(sentences)] * len(sentences)
    scale = target / s
    out = [d * scale for d in clamped]
    # 再次 clamp 并修正总和
    out = [min(SRT_MAX_S, max(SRT_MIN_S, d)) for d in out]
    # 若 clamp 后总和不等于 target，把差额摊到还有空间的条目
    diff = target - sum(out)
    if abs(diff) > 1e-6:
        for _ in range(len(out) * 3):
            if abs(diff) < 1e-6:
                break
            for i in range(len(out)):
                if abs(diff) < 1e-6:
                    break
                if diff > 0 and out[i] < SRT_MAX_S:
                    add = min(diff, SRT_MAX_S - out[i])
                    out[i] += add
                    diff -= add
                elif diff < 0 and out[i] > SRT_MIN_S:
                    sub = min(-diff, out[i] - SRT_MIN_S)
                    out[i] -= sub
                    diff += sub
    return out


def expand_srt_cues(
    segments: list[dict[str, Any]],
    segment_timings: list[TranscriptLine] | None = None,
) -> list[dict[str, Any]]:
    """将 segments 展开为句级 SRT cue：{text, start, duration}。"""
    if segment_timings is None:
        segment_timings = build_transcript_lines(segments)

    cues: list[dict[str, Any]] = []
    for seg, timing in zip(segments, segment_timings, strict=False):
        text = str(seg.get("novel_text") or "")
        sentences = split_sentences_for_srt(text)
        if not sentences:
            continue
        # 段内总时长用 transcript 段时长
        durs = _allocate_sentence_durations(sentences, timing.est_duration_s)
        t = timing.est_start_s
        for sent, dur in zip(sentences, durs, strict=True):
            # 若仍 > 7s（极端），均分拆条
            if dur > SRT_MAX_S + 1e-9:
                n = int(dur // SRT_MAX_S) + (1 if dur % SRT_MAX_S > 1e-9 else 0)
                n = max(2, n)
                # 文本硬切
                chunk_len = max(1, (len(sent) + n - 1) // n)
                parts = [sent[i : i + chunk_len] for i in range(0, len(sent), chunk_len)]
                sub_durs = _allocate_sentence_durations(parts, dur)
                for p, sd in zip(parts, sub_durs, strict=False):
                    cues.append({"text": _strip_md_prefix(p), "start": t, "duration": sd})
                    t += sd
            else:
                cues.append({"text": _strip_md_prefix(sent), "start": t, "duration": dur})
                t += dur
    return cues


def lines_to_srt(lines: list[TranscriptLine]) -> str:
    """兼容旧接口：按 TranscriptLine 直接出 SRT（段级）。句级请用 cues_to_srt。"""
    return cues_to_srt(
        [{"text": ln.text, "start": ln.est_start_s, "duration": ln.est_duration_s} for ln in lines]
    )


def cues_to_srt(cues: list[dict[str, Any]]) -> str:
    def _ts(seconds: float) -> str:
        if seconds < 0:
            seconds = 0.0
        ms_total = int(round(seconds * 1000))
        h, rem = divmod(ms_total, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        start = float(cue["start"])
        end = start + float(cue["duration"])
        text = _strip_md_prefix(str(cue["text"]))
        blocks.append(f"{i}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
    return "\n".join(blocks)


def parse_srt(srt_text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    blocks = [b.strip() for b in srt_text.strip().split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        idx = lines[0].strip()
        timing = lines[1].strip()
        text = "\n".join(lines[2:]).strip()
        if "-->" not in timing:
            continue
        start, end = [p.strip() for p in timing.split("-->", 1)]
        entries.append({"index": int(idx), "start": start, "end": end, "text": text})
    return entries


def srt_timestamp_to_seconds(ts: str) -> float:
    # HH:MM:SS,mmm
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def _render_episode_md(script: dict[str, Any], project_name: str, episode: int) -> str:
    title = script.get("title") or f"EP-{episode:03d}"
    segments = script.get("segments") or []
    lines = [
        f"# {title}",
        "",
        f"- project: `{project_name}`",
        f"- episode: {episode}",
        f"- content_mode: {script.get('content_mode', 'narration')}",
        f"- segments: {len(segments)}",
        "",
    ]
    for i, seg in enumerate(segments, start=1):
        sid = seg.get("segment_id", f"S{i:02d}")
        dur = seg.get("duration_seconds", "?")
        brk = " [场景切换]" if seg.get("segment_break") else ""
        lines.append(f"## {i}. {sid}（{dur}s）{brk}")
        lines.append("")
        lines.append("### 旁白")
        lines.append("")
        lines.append(str(seg.get("novel_text") or "").strip() or "_(空)_")
        lines.append("")
        img = seg.get("image_prompt")
        if isinstance(img, dict):
            img = img.get("prompt") or img.get("text") or json.dumps(img, ensure_ascii=False)
        vid = seg.get("video_prompt")
        if isinstance(vid, dict):
            vid = vid.get("prompt") or vid.get("text") or json.dumps(vid, ensure_ascii=False)
        if img:
            lines.append("### 画面（image_prompt）")
            lines.append("")
            lines.append(str(img).strip())
            lines.append("")
        if vid:
            lines.append("### 视频（video_prompt）")
            lines.append("")
            lines.append(str(vid).strip())
            lines.append("")
    return "\n".join(lines)


def _safe_copy(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _copy_media_tree(
    project_path: Path,
    ep_dir: Path,
    project: dict[str, Any],
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """复制（不搬移）角色/场景/首帧/视频/音频到交付目录。"""
    chars_dir = ep_dir / "images" / "characters"
    locs_dir = ep_dir / "images" / "locations"
    ff_dir = ep_dir / "images" / "first_frames"
    vid_dir = ep_dir / "videos"
    audio_dir = ep_dir / "audio"
    for d in (chars_dir, locs_dir, ff_dir, vid_dir, audio_dir):
        d.mkdir(parents=True, exist_ok=True)

    copied: dict[str, Any] = {"characters": [], "locations": [], "first_frames": [], "videos": [], "audio": []}

    characters = project.get("characters") if isinstance(project.get("characters"), dict) else {}
    scenes = project.get("scenes") if isinstance(project.get("scenes"), dict) else {}
    for name in characters:
        src = project_path / "characters" / f"{name}.png"
        if _safe_copy(src, chars_dir / f"{name}.png"):
            copied["characters"].append(name)
    for name in scenes:
        src = project_path / "scenes" / f"{name}.png"
        if _safe_copy(src, locs_dir / f"{name}.png"):
            copied["locations"].append(name)

    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        if not sid:
            continue
        sb = project_path / "storyboards" / f"scene_{sid}.png"
        if _safe_copy(sb, ff_dir / f"{sid}.png"):
            copied["first_frames"].append(sid)
        vp = project_path / "videos" / f"scene_{sid}.mp4"
        if _safe_copy(vp, vid_dir / f"{sid}.mp4"):
            copied["videos"].append(sid)
        # TTS 常见路径
        for ap in (
            project_path / "audio" / f"{sid}.wav",
            project_path / "audio" / f"scene_{sid}.wav",
            project_path / "audios" / f"{sid}.wav",
        ):
            if _safe_copy(ap, audio_dir / f"{sid}.wav"):
                copied["audio"].append(sid)
                break
    return copied


def write_summary_md(
    ep_dir: Path,
    *,
    project_name: str,
    episode: int,
    segment_rows: list[dict[str, Any]],
    cost_total: float | None = None,
    budget: dict[str, Any] | None = None,
) -> Path:
    lines = [
        f"# Deliverable SUMMARY — {project_name} EP-{episode:03d}",
        "",
        "| segment_id | first_frame | video | audio | notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in segment_rows:
        lines.append(
            f"| {row.get('segment_id','')} | {row.get('storyboard','')} | "
            f"{row.get('video','')} | {row.get('audio','')} | "
            f"{';'.join(row.get('notes') or [])} |"
        )
    lines.append("")
    if cost_total is not None:
        lines.append(f"- **cost_total_usd (est)**: {cost_total:.4f}")
    if budget:
        lines.append(f"- **budget**: `{json.dumps(budget, ensure_ascii=False)}`")
    lines.append("")
    path = ep_dir / "SUMMARY.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def export_episode(
    *,
    project_name: str,
    episode: int = 1,
    pm: ProjectManager | None = None,
    script_path: Path | None = None,
    limit_segments: int | None = None,
    media_statuses: list[Any] | None = None,
    budget_summary: dict[str, Any] | None = None,
) -> Path:
    """导出 deliverables/EP-XXX/，返回交付根目录。"""
    pm = pm or get_project_manager()
    project_name = pm.normalize_project_name(project_name)
    project_path = pm.get_project_path(project_name)
    project = pm.load_project(project_name)

    if script_path is None:
        script_path = project_path / "scripts" / f"episode_{episode}.json"
    if not script_path.is_file():
        raise FileNotFoundError(f"script not found: {script_path}")

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    NarrationEpisodeScript.model_validate(raw)
    segments = raw.get("segments") or []
    if not isinstance(segments, list) or not segments:
        raise ValueError("script has no segments")
    if limit_segments is not None and limit_segments > 0:
        segments = segments[:limit_segments]

    ep_dir = project_path / "deliverables" / f"EP-{episode:03d}"
    script_dir = ep_dir / "script"
    transcript_dir = ep_dir / "transcript"
    images_dir = ep_dir / "images"
    videos_dir = ep_dir / "videos"
    for d in (script_dir, transcript_dir, images_dir, videos_dir):
        d.mkdir(parents=True, exist_ok=True)
    (images_dir / ".gitkeep").write_text("", encoding="utf-8")
    (videos_dir / ".gitkeep").write_text("", encoding="utf-8")

    # 复制媒体
    copied = _copy_media_tree(project_path, ep_dir, project, segments)

    meta = StoryboardMeta(
        generated_at=datetime.now(UTC).isoformat(),
        source_episode=episode,
        project=project_name,
        content_mode=str(project.get("content_mode") or raw.get("content_mode") or "narration"),
    )
    storyboard = {
        "meta": meta.model_dump(),
        "title": raw.get("title"),
        "segments": segments,
        "source_script": str(script_path.relative_to(project_path)).replace("\\", "/"),
        "copied_media": copied,
    }
    (script_dir / "storyboard.json").write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (script_dir / "episode_script.md").write_text(
        _render_episode_md(raw, project_name, episode),
        encoding="utf-8",
    )

    t_lines = build_transcript_lines(segments)
    t_doc = TranscriptDocument(project=project_name, episode=episode, lines=t_lines)
    (transcript_dir / "transcript.json").write_text(
        t_doc.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    cues = expand_srt_cues(segments, t_lines)
    srt = cues_to_srt(cues)
    (transcript_dir / "subtitles.srt").write_text(srt, encoding="utf-8")

    # timeline：真实文件 + ffprobe 时长
    status_map: dict[str, Any] = {}
    if media_statuses:
        for st in media_statuses:
            sid = getattr(st, "segment_id", None) or (st.get("segment_id") if isinstance(st, dict) else None)
            if sid:
                status_map[str(sid)] = st

    timeline: list[TimelineItem] = []
    t_cursor = 0.0
    voiceover_paths: list[str] = []
    summary_rows: list[dict[str, Any]] = []
    cost_total = 0.0

    for line in t_lines:
        sid = line.segment_id
        v_rel = f"videos/{sid}.mp4"
        v_abs = ep_dir / v_rel
        a_rel = f"audio/{sid}.wav"
        a_abs = ep_dir / a_rel
        ff_abs = ep_dir / "images" / "first_frames" / f"{sid}.png"

        st = status_map.get(sid)
        notes: list[str] = []
        if st is not None:
            if hasattr(st, "video"):
                v_status = st.video
                a_status = st.audio
                s_status = st.storyboard
                notes = list(st.notes or [])
                cost_total += float(
                    getattr(st, "cost_estimate_usd", None)
                    or getattr(st, "cost_usd", 0)
                    or 0
                )
            else:
                v_status = st.get("video", "missing")
                a_status = st.get("audio", "skipped")
                s_status = st.get("storyboard", "missing")
                notes = list(st.get("notes") or [])
                cost_total += float(
                    st.get("cost_estimate_usd") or st.get("cost_usd") or 0
                )
        else:
            v_status = "missing"
            a_status = "skipped"
            s_status = "ready" if ff_abs.is_file() else "missing"
            notes = []

        # 以 mediacheck 为准重验交付视频：文件在也不等于 ready
        v_reason = ""
        if v_abs.is_file():
            exp_dur = float(line.est_duration_s) if line.est_duration_s else None
            chk = check_video(v_abs, expected_duration_s=exp_dur)
            if chk.ok:
                v_status = "ready"
            else:
                v_status = "failed"
                v_reason = chk.reason or "media_check_failed"
                if v_reason and v_reason not in notes:
                    notes.append(v_reason)
        else:
            if v_status == "ready":
                v_status = "missing"
            if v_status not in ("failed", "skipped", "missing"):
                v_status = "missing"

        if s_status == "ready" and not ff_abs.is_file():
            s_status = "missing"

        dur = line.est_duration_s
        if v_abs.is_file():
            probed = probe_duration_s(v_abs)
            if probed is not None and probed > 0:
                dur = probed

        # timeline：ready 仅当 mediacheck 通过；failed 文件保留路径供对照
        if v_status == "ready" and v_abs.is_file():
            tl_status: str = "ready"
        elif v_status == "failed" and v_abs.is_file():
            tl_status = "failed"
        else:
            tl_status = "missing"
        timeline.append(
            TimelineItem(
                segment_id=sid,
                source=v_rel,
                start=t_cursor,
                est_duration_s=dur,
                status=tl_status,  # type: ignore[arg-type]
            )
        )
        t_cursor += dur
        if a_abs.is_file():
            voiceover_paths.append(a_rel)
            a_status = "ready"

        summary_rows.append(
            {
                "segment_id": sid,
                "storyboard": s_status,
                "video": format_video_status_label(v_status, v_reason or " ".join(notes)),
                "audio": a_status,
                "notes": notes,
            }
        )

    voiceover = {"tracks": voiceover_paths} if voiceover_paths else None
    manifest = EditManifest(
        project=project_name,
        episode=episode,
        canvas=CanvasSpec(width=1080, height=1920, fps=30),
        timeline=timeline,
        captions="transcript/subtitles.srt",
        voiceover=voiceover,
        music=None,
    )
    (ep_dir / "edit_manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_md(
        ep_dir,
        project_name=project_name,
        episode=episode,
        segment_rows=summary_rows,
        cost_total=cost_total if cost_total else (budget_summary or {}).get("spent_usd"),
        budget=budget_summary,
    )
    return ep_dir
