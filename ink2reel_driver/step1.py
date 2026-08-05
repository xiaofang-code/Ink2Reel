"""narration step1：边界切分方案。

LLM 只输出段尾句引用（end_quote）+ 元数据；driver 文本匹配定位 offset，
从原文切片生成 novel_text——逐字覆盖由构造保证。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from lib.custom_provider.duration_presets import DEFAULT_FALLBACK
from lib.episode_paths import STEP1_FILENAMES, episode_drafts_dir
from lib.json_io import atomic_write_json
from lib.project_manager import ProjectManager
from lib.script_models import NarrationStep1Draft
from lib.text_backends.base import DEFAULT_MAX_OUTPUT_TOKENS, TextGenerationRequest, TextTaskType
from lib.text_generator import TextGenerator
from lib.text_utils import strip_json_code_fences

logger = logging.getLogger(__name__)

Step1Method = Literal["llm_boundary", "mechanical_fallback"]

# 边界方案允许的时长档（handoff）；落盘前 snap 到 supported_durations
BOUNDARY_DURATION_TIERS = (4, 6, 8, 10)
MIN_SEG_CHARS = 40
MAX_SEG_CHARS = 400


class BoundarySegmentDraft(BaseModel):
    end_quote: str = Field(min_length=1, description="段尾句原文一字不差，建议 8–20 字")
    characters: list[str] = Field(default_factory=list)
    scenes: list[str] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)
    duration_tier: int = Field(description="时长档 4|6|8|10")
    segment_break: bool = False


class BoundaryDraft(BaseModel):
    segments: list[BoundarySegmentDraft] = Field(min_length=1)


@dataclass
class Step1Result:
    path: Path
    method: Step1Method
    retries: int
    segment_count: int


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_QUOTE_MAP = str.maketrans(
    {
        "\u201c": '"',  # “
        "\u201d": '"',  # ”
        "\u2018": "'",  # ‘
        "\u2019": "'",  # ’
        "\u300c": '"',  # 「
        "\u300d": '"',  # 」
        "\u300e": '"',  # 『
        "\u300f": '"',  # 』
    }
)


def _normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_MAP)


def _parse_json_obj(response_text: str) -> dict:
    text = strip_json_code_fences(response_text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"step1 边界 JSON 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("step1 边界结构异常：顶层应为对象")
    return data


def _load_episode_source(project_path: Path, episode: int) -> tuple[str, str]:
    ep_path = project_path / "source" / f"episode_{episode}.txt"
    if ep_path.is_file():
        text = ep_path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"集源文件为空: {ep_path}")
        return text, f"source/episode_{episode}.txt"

    source_dir = project_path / "source"
    if not source_dir.exists():
        raise ValueError(f"source/ 目录不存在: {source_dir}")
    texts = [
        f.read_text(encoding="utf-8")
        for f in sorted(source_dir.iterdir())
        if f.is_file()
        and f.suffix.lower() in (".txt", ".md", ".text")
        and not f.name.startswith("episode_")
        and not f.name.startswith("raw_")
    ]
    novel_text = "\n\n".join(texts)
    if not novel_text.strip():
        raise ValueError("小说原文为空")
    return novel_text, "source/*"


def _char_count(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def _target_segment_range(n_chars: int) -> tuple[int, int]:
    """段数目标 = 字数÷120 ± 30%。"""
    mid = max(1, round(n_chars / 120))
    lo = max(1, int(mid * 0.7))
    hi = max(lo, int(mid * 1.3 + 0.999))
    return lo, hi


def _build_boundary_prompt(novel_text: str, *, episode: int, lo: int, hi: int) -> str:
    n = _char_count(novel_text)
    return f"""你是说书短视频分镜架构师。任务：把下方「第 {episode} 集原文」按朗读节奏切成若干片段的**边界表**。

# 硬规则
1. **不要输出 novel_text 全文**。每个片段只输出该段**最后一句在原文中一字不差的短引用** `end_quote`。
2. **end_quote 长度 8–20 字**，必须从原文**复制粘贴**，禁止同义改写、禁止换主语、禁止增删标点。
3. 片段按原文顺序；第 i 段 end_quote 必须出现在第 i-1 段 end_quote **之后**。
4. 最后一段 end_quote 尽量靠近文末（代码会延伸到文末）。
5. 目标段数约 **{lo}–{hi}** 段（原文约 {n} 字，每段约 80–150 字）。
6. 单段正文（切片后）理想 40–400 字；在场景切换/时间跳跃处切。
7. `duration_tier` 只能是 4 / 6 / 8 / 10。
8. `characters` / `scenes` / `props`：该段出现的名称；无则 []。
9. `segment_break=true` 仅真正场景切换。

# 输出 JSON（不要 markdown 围栏外的解释）
{{
  "segments": [
    {{
      "end_quote": "原文一字不差的段尾短句",
      "characters": [],
      "scenes": [],
      "props": [],
      "duration_tier": 6,
      "segment_break": false
    }}
  ]
}}

# 第 {episode} 集原文
<<<SOURCE
{novel_text}
SOURCE
"""


def _find_quote_end(novel_text: str, quote: str, cursor: int) -> int | None:
    """在 cursor 之后定位 quote 结束 offset（不含）；失败返回 None。

    策略（仍保证切片取自原文，不改写正文）：
    1. 精确 find
    2. 引号归一化后 find（弯引号/直角引号 → ASCII）
    3. 空白折叠 + 引号归一化
    4. 取 quote 末尾 8–20 字后缀（归一化后）在剩余原文中找
    """
    q = quote.strip()
    if not q:
        return None
    pos = novel_text.find(q, cursor)
    if pos >= 0:
        return pos + len(q)

    rest = novel_text[cursor:]
    qn = _normalize_quotes(q)
    rest_nq = _normalize_quotes(rest)
    pos2 = rest_nq.find(qn)
    if pos2 >= 0:
        # 归一化不改变长度（仅替换字符），offset 一致
        return cursor + pos2 + len(qn)

    # 空白折叠 + 引号归一
    nq = _normalize_ws(qn)
    nr = _normalize_ws(rest_nq)
    if nq:
        j = nr.find(nq)
        if j >= 0:
            ti = 0
            ni = 0
            while ti < len(rest_nq) and ni < len(nr):
                if rest_nq[ti].isspace():
                    ti += 1
                    continue
                if ni == j:
                    start_t = ti
                if ni == j + len(nq) - 1:
                    return cursor + ti + 1
                ni += 1
                ti += 1

    # 后缀回退（去空白 + 引号归一）
    plain = re.sub(r"\s+", "", qn)
    compact: list[str] = []
    idx_map: list[int] = []
    for ti, ch in enumerate(rest_nq):
        if not ch.isspace():
            compact.append(ch)
            idx_map.append(ti)
    compact_s = "".join(compact)
    for L in range(min(20, len(plain)), 7, -1):
        suffix = plain[-L:]
        k = compact_s.find(suffix)
        if k >= 0:
            end_local = idx_map[k + len(suffix) - 1] + 1
            return cursor + end_local
    return None


def _locate_boundaries(novel_text: str, quotes: list[str]) -> tuple[list[int], list[str]]:
    """返回每段 end offset（不含）列表与问题清单。"""
    problems: list[str] = []
    ends: list[int] = []
    cursor = 0
    for i, q in enumerate(quotes):
        q = q.strip()
        if not q:
            problems.append(f"段{i + 1}: end_quote 为空")
            ends.append(-1)
            continue
        end = _find_quote_end(novel_text, q, cursor)
        if end is None:
            neigh = novel_text[max(0, cursor) : cursor + 200]
            problems.append(
                f"段{i + 1}: end_quote 在原文 cursor={cursor} 之后找不到。"
                f" quote={q!r}；邻近原文≈{neigh[:60]!r}"
            )
            ends.append(-1)
            continue
        if ends and ends[-1] >= 0 and end <= ends[-1]:
            problems.append(
                f"段{i + 1}: end_quote 乱序/未前进（end={end} <= prev={ends[-1]}）。quote={q!r}"
            )
            ends.append(-1)
            continue
        ends.append(end)
        cursor = end
    return ends, problems


def _slice_segments(
    novel_text: str,
    ends: list[int],
    drafts: list[BoundarySegmentDraft],
    *,
    episode: int,
    supported_durations: list[int],
) -> tuple[list[dict], list[str]]:
    """按 ends 切片；最后一段延伸到文末。返回 segments 与问题。"""
    problems: list[str] = []
    if any(e < 0 for e in ends):
        return [], problems  # 定位失败已在上层记

    # 强制最后一段到文末
    ends = list(ends)
    ends[-1] = len(novel_text)

    segments: list[dict] = []
    start = 0
    for i, end in enumerate(ends):
        if end < start:
            problems.append(f"段{i + 1}: end={end} < start={start} 乱序")
            continue
        # 若中间有空隙（LLM 跳过了字），归入本段（从 start 切到 end）——保证拼接覆盖
        text = novel_text[start:end]
        n = _char_count(text)
        if n < MIN_SEG_CHARS and i < len(ends) - 1:
            problems.append(
                f"段{i + 1}: 切片过短 ({n}字 < {MIN_SEG_CHARS})；"
                f"邻文={text[:60]!r}"
            )
        if n > MAX_SEG_CHARS:
            problems.append(
                f"段{i + 1}: 切片过长 ({n}字 > {MAX_SEG_CHARS})；"
                f"开头={text[:40]!r}…结尾={text[-40:]!r}"
            )
        d = drafts[i]
        tier = int(d.duration_tier)
        if tier not in supported_durations:
            tier = min(supported_durations, key=lambda x: abs(x - tier))
        segments.append(
            {
                "segment_id": f"E{episode}S{i + 1:02d}",
                "novel_text": text,
                "duration_seconds": tier,
                "segment_break": bool(d.segment_break),
                "characters_in_segment": list(d.characters or []),
                "scenes": list(d.scenes or []),
                "props": list(d.props or []),
            }
        )
        start = end

    # 覆盖断言（严格字符拼接）
    joined = "".join(s["novel_text"] for s in segments)
    if joined != novel_text:
        # 允许仅首尾空白差异时修正：否则报问题
        if joined.strip() == novel_text.strip():
            # 把缺失的首尾空白补到首/末段
            if novel_text.startswith(joined):
                pass
            problems.append("段拼接与原文不完全一致（空白差异）")
        else:
            problems.append(
                f"段拼接未覆盖原文：joined_len={len(joined)} src_len={len(novel_text)}"
            )
    return segments, problems


def _assert_exact_coverage(segments: list[dict], novel_text: str) -> None:
    joined = "".join(str(s.get("novel_text") or "") for s in segments)
    if joined != novel_text:
        raise ValueError(
            f"novel_text 拼接 != 原文 (joined={len(joined)}, src={len(novel_text)})"
        )


def _collect_asset_names(segments: list[dict]) -> tuple[set[str], set[str], set[str]]:
    chars: set[str] = set()
    scenes: set[str] = set()
    props: set[str] = set()
    for segment in segments:
        for name in segment.get("characters_in_segment") or []:
            if str(name).strip():
                chars.add(str(name).strip())
        for name in segment.get("scenes") or []:
            if str(name).strip():
                scenes.add(str(name).strip())
        for name in segment.get("props") or []:
            if str(name).strip():
                props.add(str(name).strip())
    return chars, scenes, props


def _ensure_assets_registered(
    pm: ProjectManager,
    project_name: str,
    segments: list[dict],
) -> dict[str, Any]:
    chars, scenes, props = _collect_asset_names(segments)
    project = pm.load_project(project_name)
    existing_c = project.get("characters") if isinstance(project.get("characters"), dict) else {}
    existing_s = project.get("scenes") if isinstance(project.get("scenes"), dict) else {}
    existing_p = project.get("props") if isinstance(project.get("props"), dict) else {}

    new_c = {n: {"description": f"（driver 自 step1 登记）{n}"} for n in chars if n not in existing_c}
    new_s = {n: {"description": f"（driver 自 step1 登记）{n}"} for n in scenes if n not in existing_s}
    new_p = {n: {"description": f"（driver 自 step1 登记）{n}"} for n in props if n not in existing_p}

    if new_c:
        pm.upsert_assets(project_name, "characters", new_c)
    if new_s:
        pm.upsert_assets(project_name, "scenes", new_s)
    if new_p:
        pm.upsert_assets(project_name, "props", new_p)
    return pm.load_project(project_name)


def _validate_ids_and_durations(segments: list[dict], supported_durations: list[int]) -> None:
    ids = [s.get("segment_id") for s in segments]
    dupes = sorted(str(sid) for sid, count in Counter(ids).items() if count > 1)
    if dupes:
        raise ValueError(f"segment_id 重复: {dupes}")
    allowed = {int(d) for d in supported_durations}
    bad = sorted(
        {int(s["duration_seconds"]) for s in segments if int(s.get("duration_seconds") or 0) not in allowed}
    )
    if bad:
        raise ValueError(f"duration_seconds 非法（不在 {sorted(allowed)} 内）: {bad}")


def _pick_duration(char_count: int, supported_durations: list[int]) -> int:
    est = max(1.5, char_count / 4.5)
    return min(supported_durations, key=lambda d: abs(d - est))


def _mechanical_split(
    novel_text: str,
    *,
    episode: int,
    supported_durations: list[int],
    target_chars: int = 120,
) -> dict[str, Any]:
    """3 次边界切分失败后的确定性回退。"""
    paragraphs = [p for p in re.split(r"\n\s*\n", novel_text) if p.strip()]
    if not paragraphs:
        paragraphs = [novel_text]

    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= int(target_chars * 1.5):
            chunks.append(para)
            continue
        start = 0
        while start < len(para):
            end = min(len(para), start + target_chars)
            if end < len(para):
                window = para[start:end]
                cut = max((window.rfind(ch) for ch in "。！？；…\n"), default=-1)
                if cut >= target_chars // 3:
                    end = start + cut + 1
            piece = para[start:end]
            if piece:
                chunks.append(piece)
            start = end

    # 若拼接失败，固定窗口
    if "".join(chunks) != novel_text:
        chunks = []
        i = 0
        while i < len(novel_text):
            j = min(len(novel_text), i + target_chars)
            chunks.append(novel_text[i:j])
            i = j

    segments = []
    for i, text in enumerate(chunks, start=1):
        segments.append(
            {
                "segment_id": f"E{episode}S{i:02d}",
                "novel_text": text,
                "duration_seconds": _pick_duration(_char_count(text), supported_durations),
                "segment_break": i > 1 and text[:1] in ("\n",) or False,
                "characters_in_segment": [],
                "scenes": [],
                "props": [],
            }
        )
    _assert_exact_coverage(segments, novel_text)
    _validate_ids_and_durations(segments, supported_durations)
    return {"segments": segments}


def assemble_from_boundary_draft(
    novel_text: str,
    draft: BoundaryDraft | dict,
    *,
    episode: int,
    supported_durations: list[int],
) -> tuple[list[dict], list[str]]:
    """纯函数：边界 draft → segments + problems（供单测）。"""
    if isinstance(draft, dict):
        draft = BoundaryDraft.model_validate(draft)
    quotes = [s.end_quote for s in draft.segments]
    ends, loc_problems = _locate_boundaries(novel_text, quotes)
    if loc_problems or any(e < 0 for e in ends):
        return [], loc_problems
    segments, slice_problems = _slice_segments(
        novel_text,
        ends,
        list(draft.segments),
        episode=episode,
        supported_durations=supported_durations,
    )
    problems = loc_problems + slice_problems
    # 硬失败：覆盖或致命乱序
    hard = [p for p in problems if "找不到" in p or "乱序" in p or "未覆盖" in p or "不完全一致" in p]
    if hard:
        return [], problems
    # 过短/过长也视为需重试
    if any("过短" in p or "过长" in p for p in problems):
        return [], problems
    try:
        _assert_exact_coverage(segments, novel_text)
        _validate_ids_and_durations(segments, supported_durations)
    except ValueError as e:
        return [], problems + [str(e)]
    return segments, problems


def _feedback_message(problems: list[str], novel_text: str) -> str:
    lines = ["# 上一次边界表有问题，请只修正问题段后重新输出完整 JSON。", "问题清单："]
    for p in problems:
        lines.append(f"- {p}")
    lines.append(
        "要求：end_quote 必须是原文连续子串、一字不差；顺序前进；"
        f"单段目标 {MIN_SEG_CHARS}–{MAX_SEG_CHARS} 字。"
    )
    return "\n".join(lines)


async def split_narration_segments(
    *,
    pm: ProjectManager,
    project_name: str,
    episode: int = 1,
    supported_durations: list[int] | None = None,
    default_duration: int | None = None,
    max_attempts: int = 3,
    generator: TextGenerator | None = None,
) -> Step1Result:
    """边界切分 step1；返回路径与 method/retries。"""
    project_path = pm.get_project_path(project_name)
    novel_text, source_label = _load_episode_source(project_path, episode)

    durations = list(supported_durations) if supported_durations else list(DEFAULT_FALLBACK)
    # 边界档与 registry 并集，优先保留 4/6/8
    for t in BOUNDARY_DURATION_TIERS:
        if t not in durations and t in (4, 6, 8):
            durations.append(t)
    durations = sorted(set(int(d) for d in durations))
    if not durations:
        durations = list(DEFAULT_FALLBACK)

    lo, hi = _target_segment_range(_char_count(novel_text))
    base_prompt = _build_boundary_prompt(novel_text, episode=episode, lo=lo, hi=hi)

    if generator is None:
        generator = await TextGenerator.create(TextTaskType.SCRIPT, project_name=project_name)

    last_problems: list[str] = []
    segments: list[dict] | None = None
    retries = 0  # 反馈重试次数（不含首次）

    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt
        if last_problems:
            prompt = base_prompt + "\n\n" + _feedback_message(last_problems, novel_text)
            retries = attempt - 1

        try:
            result = await generator.generate(
                TextGenerationRequest(
                    prompt=prompt,
                    response_schema=BoundaryDraft,
                    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                ),
                project_name=project_name,
            )
            raw = _parse_json_obj(result.text)
            try:
                draft = BoundaryDraft.model_validate(raw)
            except ValidationError as e:
                last_problems = [f"schema 校验失败: {e}"]
                logger.warning("step1 boundary attempt %d/%d schema: %s", attempt, max_attempts, e)
                continue

            segs, problems = assemble_from_boundary_draft(
                novel_text,
                draft,
                episode=episode,
                supported_durations=durations,
            )
            if segs and not problems:
                segments = segs
                retries = attempt - 1
                break
            if segs and problems:
                # 仅有非硬问题仍可接受？handoff：过短过长要重试；assemble 已把它们当失败
                pass
            last_problems = problems or ["未知边界问题"]
            logger.warning(
                "step1 boundary attempt %d/%d failed: %s",
                attempt,
                max_attempts,
                last_problems[:5],
            )
            retries = attempt - 1
        except Exception as e:  # noqa: BLE001
            last_problems = [f"{type(e).__name__}: {e}"]
            logger.warning("step1 boundary attempt %d/%d error: %s", attempt, max_attempts, e)
            retries = attempt - 1

    method: Step1Method
    if segments is None:
        logger.warning(
            "step1 boundary failed %d times; mechanical_fallback. last=%s",
            max_attempts,
            last_problems[:3],
        )
        content = _mechanical_split(novel_text, episode=episode, supported_durations=durations)
        segments = content["segments"]
        method = "mechanical_fallback"
    else:
        method = "llm_boundary"

    _assert_exact_coverage(segments, novel_text)
    _validate_ids_and_durations(segments, durations)
    _ensure_assets_registered(pm, project_name, segments)

    # 重新校验 assets 引用（登记后应全通过）
    project = pm.load_project(project_name)
    characters = project.get("characters") if isinstance(project.get("characters"), dict) else {}
    scenes = project.get("scenes") if isinstance(project.get("scenes"), dict) else {}
    props = project.get("props") if isinstance(project.get("props"), dict) else {}
    for seg in segments:
        for field, bucket in (
            ("characters_in_segment", characters),
            ("scenes", scenes),
            ("props", props),
        ):
            bad = [n for n in (seg.get(field) or []) if n not in bucket]
            if bad:
                # 清理未登记（防御）
                seg[field] = [n for n in (seg.get(field) or []) if n in bucket]

    content = {"segments": segments}
    # 过 NarrationStep1Draft
    NarrationStep1Draft.model_validate(content)

    drafts_dir = episode_drafts_dir(project_path, episode)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    step1_path = drafts_dir / STEP1_FILENAMES["narration"]
    atomic_write_json(step1_path, content)
    logger.info(
        "step1 saved %s source=%s segments=%d method=%s retries=%d",
        step1_path,
        source_label,
        len(segments),
        method,
        retries,
    )
    return Step1Result(
        path=step1_path,
        method=method,
        retries=retries,
        segment_count=len(segments),
    )
