"""剧本机械检查（非 LLM）。

规则：
- 段时长合计 vs 目标 60s 偏差 ≤±15%
- 单段 novel_text 40–400 字
- 连续 ≥3 段同 duration → warn
- 相邻段 prompt difflib 相似度 >0.9 → warn
- prompt 非空
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from ink2reel_driver.qc.types import Issue, RuleResult

TARGET_DURATION_S = 60.0
DURATION_TOL = 0.15  # ±15%
MIN_NOVEL_CHARS = 40
MAX_NOVEL_CHARS = 400
SAME_DUR_STREAK = 3
PROMPT_SIM_WARN = 0.9


def _prompt_text(seg: dict[str, Any]) -> str:
    img = seg.get("image_prompt")
    if isinstance(img, str) and img.strip():
        return img.strip()
    if isinstance(img, dict):
        parts: list[str] = []
        if img.get("scene"):
            parts.append(str(img["scene"]))
        comp = img.get("composition")
        if isinstance(comp, dict):
            parts.append(json.dumps(comp, ensure_ascii=False, sort_keys=True))
        elif comp:
            parts.append(str(comp))
        return "\n".join(parts).strip()
    vid = seg.get("video_prompt")
    if isinstance(vid, str):
        return vid.strip()
    if isinstance(vid, dict) and vid.get("action"):
        return str(vid["action"]).strip()
    return ""


def _novel_len(seg: dict[str, Any]) -> int:
    t = seg.get("novel_text") or ""
    return sum(1 for c in str(t) if not c.isspace())


def check_script(episode: dict[str, Any] | str | Path) -> RuleResult:
    """对整集 episode JSON 做机械检查。任一 hard 问题 → block。"""
    if isinstance(episode, (str, Path)):
        data = json.loads(Path(episode).read_text(encoding="utf-8"))
    else:
        data = episode
    segs = list(data.get("segments") or [])
    issues: list[Issue] = []
    hard = False

    if not segs:
        issues.append(Issue(code="no_segments", message="episode has no segments"))
        return RuleResult(verdict="block", issues=issues)

    total_dur = 0.0
    for i, seg in enumerate(segs):
        sid = str(seg.get("segment_id") or f"S{i+1:02d}")
        try:
            dur = float(seg.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        total_dur += dur

        nlen = _novel_len(seg)
        if nlen < MIN_NOVEL_CHARS or nlen > MAX_NOVEL_CHARS:
            hard = True
            issues.append(
                Issue(
                    code="novel_text_length",
                    message=f"novel_text chars={nlen} not in [{MIN_NOVEL_CHARS},{MAX_NOVEL_CHARS}]",
                    segment_id=sid,
                    detail={"chars": nlen},
                )
            )

        ptxt = _prompt_text(seg)
        if not ptxt:
            hard = True
            issues.append(
                Issue(
                    code="prompt_empty",
                    message="image/video prompt empty",
                    segment_id=sid,
                )
            )

    # 总时长：满集（≥8 段或合计≥45s）硬校验 60s±15%；短样例/limit-segments 仅 warn
    if TARGET_DURATION_S > 0:
        rel = abs(total_dur - TARGET_DURATION_S) / TARGET_DURATION_S
        if rel > DURATION_TOL:
            issue = Issue(
                code="total_duration",
                message=f"total duration {total_dur:.1f}s vs target {TARGET_DURATION_S}s (rel={rel:.2f}>±{DURATION_TOL})",
                detail={"total_s": total_dur, "target_s": TARGET_DURATION_S, "rel": rel},
            )
            issues.append(issue)
            if len(segs) >= 8 or total_dur >= 45:
                hard = True

    # 连续同 duration
    streak = 1
    for i in range(1, len(segs)):
        d0 = segs[i - 1].get("duration_seconds")
        d1 = segs[i].get("duration_seconds")
        if d0 == d1:
            streak += 1
            if streak >= SAME_DUR_STREAK:
                issues.append(
                    Issue(
                        code="same_duration_streak",
                        message=f">={SAME_DUR_STREAK} consecutive segments with duration={d1}",
                        segment_id=str(segs[i].get("segment_id") or ""),
                        detail={"duration": d1, "streak": streak},
                    )
                )
        else:
            streak = 1

    # 相邻 prompt 相似度
    for i in range(1, len(segs)):
        a = _prompt_text(segs[i - 1])
        b = _prompt_text(segs[i])
        if not a or not b:
            continue
        sim = difflib.SequenceMatcher(None, a, b).ratio()
        if sim > PROMPT_SIM_WARN:
            issues.append(
                Issue(
                    code="prompt_similarity",
                    message=f"adjacent prompt similarity {sim:.3f} > {PROMPT_SIM_WARN}",
                    segment_id=str(segs[i].get("segment_id") or ""),
                    detail={
                        "prev": str(segs[i - 1].get("segment_id") or ""),
                        "similarity": sim,
                    },
                )
            )

    if hard:
        return RuleResult(verdict="block", issues=issues)
    if issues:
        return RuleResult(verdict="warn", issues=issues)
    return RuleResult(verdict="pass", issues=[])
