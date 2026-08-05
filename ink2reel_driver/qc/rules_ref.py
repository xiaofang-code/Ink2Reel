"""参考资产完备性规则。

经验阈值（OnlyShot / 实战）：
- 单帧 ref >2 → warn（3+ 一致性 <50%，N=8 实测、换供应商需重校准）
- >4 → block
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ink2reel_driver.qc.types import Issue, RuleResult

REF_WARN_AT = 3  # inclusive: 3–4 warn
REF_BLOCK_AT = 5  # >=5 block


def _asset_exists(assets_dir: Path, kind: str, name: str) -> bool:
    """在 characters/ 或 scenes/ 下按 stem 匹配（支持 .png/.jpg/.webp）。"""
    folder = assets_dir / kind
    if not folder.is_dir():
        # 也允许扁平结构 assets_dir / name.png
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            if (assets_dir / f"{name}{ext}").is_file():
                return True
        return False
    for p in folder.iterdir():
        if p.is_file() and p.stem == name:
            return True
    # 宽松：文件名包含角色名
    for p in folder.iterdir():
        if p.is_file() and name in p.stem:
            return True
    return False


def _ref_names(segment: dict[str, Any]) -> tuple[list[str], list[str]]:
    chars = segment.get("characters_in_segment") or segment.get("characters") or []
    scenes = segment.get("scenes") or []
    if not isinstance(chars, list):
        chars = []
    if not isinstance(scenes, list):
        scenes = []
    return [str(c) for c in chars if c], [str(s) for s in scenes if s]


def check_refs(segment: dict[str, Any], assets_dir: str | Path) -> RuleResult:
    """检查段引用的角色/场景图是否存在，以及 ref 数量阈值。"""
    assets = Path(assets_dir)
    sid = str(segment.get("segment_id") or "")
    chars, scenes = _ref_names(segment)
    issues: list[Issue] = []

    missing_chars = [c for c in chars if not _asset_exists(assets, "characters", c)]
    missing_scenes = [s for s in scenes if not _asset_exists(assets, "scenes", s)]
    if missing_chars or missing_scenes:
        issues.append(
            Issue(
                code="ref_missing",
                message=f"missing assets characters={missing_chars} scenes={missing_scenes}",
                segment_id=sid or None,
                detail={"missing_characters": missing_chars, "missing_scenes": missing_scenes},
            )
        )
        return RuleResult(verdict="block", issues=issues)

    n_refs = len(chars) + len(scenes)
    if n_refs >= REF_BLOCK_AT:
        issues.append(
            Issue(
                code="ref_count_block",
                message=f"ref count {n_refs} >= {REF_BLOCK_AT} (N=8 实测、换供应商需重校准)",
                segment_id=sid or None,
                detail={"ref_count": n_refs},
            )
        )
        return RuleResult(verdict="block", issues=issues)

    if n_refs >= REF_WARN_AT:
        issues.append(
            Issue(
                code="ref_count_warn",
                message=f"ref count {n_refs} >= {REF_WARN_AT}; 3+ 一致性 <50% (N=8 实测)",
                segment_id=sid or None,
                detail={"ref_count": n_refs},
            )
        )
        return RuleResult(verdict="warn", issues=issues)

    return RuleResult(verdict="pass", issues=[])
