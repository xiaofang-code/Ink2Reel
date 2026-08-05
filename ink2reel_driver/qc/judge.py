"""LLM 六维剧本评审。

rubric 移植 OnlyShot ``references/critic-checklist.md``（MIT）：
钩子 / 可预测度 / 同质化 / 爽点密度 / 角色辨识度 / 老套度，各 0–10。
总分 = 六维之和（0–60）；threshold 默认 50。

失败语义：**judge 解析失败 → judge_unavailable → escalate，禁止默认 0 分或默认放行。**
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, Field, ValidationError

from lib.text_utils import strip_json_code_fences

logger = logging.getLogger(__name__)

DIM_KEYS = (
    "hook",  # 黄金 3 秒钩子
    "predictability",  # 可预测度（高分=难猜）
    "homogenization",  # 同质化风险（高分=差异大）
    "payoff_density",  # 爽点密度
    "character_id",  # 角色辨识度
    "cliche",  # 老套度（高分=原创）
)

JudgeLLM = Callable[[str], Awaitable[str]]


class JudgeDimsModel(BaseModel):
    hook: float = Field(ge=0, le=10)
    predictability: float = Field(ge=0, le=10)
    homogenization: float = Field(ge=0, le=10)
    payoff_density: float = Field(ge=0, le=10)
    character_id: float = Field(ge=0, le=10)
    cliche: float = Field(ge=0, le=10)
    issues: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class JudgeResult:
    status: str  # "ok" | "judge_unavailable"
    total: float | None = None
    dims: dict[str, float] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    raw: str | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "ok" and self.total is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "total": self.total,
            "dims": self.dims,
            "issues": self.issues,
            "error": self.error,
        }


def _build_prompt(episode: dict[str, Any]) -> str:
    segs = episode.get("segments") or []
    compact = []
    for s in segs[:20]:
        compact.append(
            {
                "segment_id": s.get("segment_id"),
                "novel_text": (s.get("novel_text") or "")[:400],
                "duration_seconds": s.get("duration_seconds"),
                "image_prompt": s.get("image_prompt"),
            }
        )
    body = json.dumps({"title": episode.get("title"), "segments": compact}, ensure_ascii=False)
    return f"""你是短剧剧本评审。按六维 0-10 打分（来源 OnlyShot critic-checklist）：
1. hook 黄金3秒钩子强度
2. predictability 可预测度（10=完全猜不到）
3. homogenization 同质化风险（10=高度差异化）
4. payoff_density 爽点密度
5. character_id 角色辨识度
6. cliche 老套度（10=原创）

只输出 JSON，不要 markdown：
{{"hook":0-10,"predictability":0-10,"homogenization":0-10,"payoff_density":0-10,"character_id":0-10,"cliche":0-10,"issues":[{{"segment_id":"...","message":"..."}}]}}

剧本：
{body}
"""


def _parse_judge_json(text: str) -> JudgeDimsModel:
    cleaned = strip_json_code_fences(text or "")
    data = json.loads(cleaned)
    return JudgeDimsModel.model_validate(data)


async def _default_llm(prompt: str, *, project_name: str | None) -> str:
    from lib.text_backends.base import DEFAULT_MAX_OUTPUT_TOKENS, TextGenerationRequest, TextTaskType
    from lib.text_generator import TextGenerator

    gen = await TextGenerator.create(TextTaskType.SCRIPT, project_name=project_name)
    result = await gen.generate(
        TextGenerationRequest(
            prompt=prompt,
            max_output_tokens=min(DEFAULT_MAX_OUTPUT_TOKENS, 2048),
        )
    )
    # TextGenerator 返回对象或 str
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None) or getattr(result, "content", None)
    if text:
        return str(text)
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), ensure_ascii=False)
    return str(result)


async def judge_script(
    episode_json: dict[str, Any] | str | Path,
    *,
    llm: JudgeLLM | None = None,
    project_name: str | None = None,
    repair_once: bool = True,
) -> JudgeResult:
    """六维评审。解析失败最多 1 次格式修复重试；仍失败 → judge_unavailable。"""
    if isinstance(episode_json, (str, Path)):
        episode = json.loads(Path(episode_json).read_text(encoding="utf-8"))
    else:
        episode = episode_json

    prompt = _build_prompt(episode)
    call = llm
    if call is None:

        async def _call(p: str) -> str:
            return await _default_llm(p, project_name=project_name)

        call = _call

    raw: str | None = None
    try:
        raw = await call(prompt)
        model = _parse_judge_json(raw)
    except Exception as exc1:  # noqa: BLE001
        if not repair_once:
            return JudgeResult(status="judge_unavailable", error=str(exc1), raw=raw)
        try:
            repair_prompt = (
                "将下列内容修正为合法 JSON，仅输出 JSON 对象：\n"
                f"{raw if raw else str(exc1)}"
            )
            raw2 = await call(repair_prompt)
            raw = raw2
            model = _parse_judge_json(raw2)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("judge_unavailable: %s / repair: %s", exc1, exc2)
            return JudgeResult(
                status="judge_unavailable",
                error=f"{exc1}; repair_failed: {exc2}",
                raw=raw,
            )

    dims = {k: float(getattr(model, k)) for k in DIM_KEYS}
    total = sum(dims.values())
    issues = list(model.issues or [])
    return JudgeResult(status="ok", total=total, dims=dims, issues=issues, raw=raw)
