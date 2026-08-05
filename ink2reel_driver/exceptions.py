"""异常队列：escalate 写入 projects/{name}/exceptions.jsonl + CLI 操作。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from lib.project_manager import ProjectManager, get_project_manager

ResolveAction = Literal["approve", "regenerate", "skip"]


@dataclass
class ExceptionRecord:
    id: str
    project: str
    target_id: str
    stage: str
    reasons: list[str]
    suggested_action: str
    ts: str
    status: str = "open"  # open|resolved
    resolve_action: str | None = None
    resolved_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def exceptions_path(pm: ProjectManager, project_name: str) -> Path:
    return pm.get_project_path(project_name) / "exceptions.jsonl"


def append_exception(
    *,
    project_name: str,
    target_id: str,
    stage: str,
    reasons: list[str],
    suggested_action: str = "review",
    meta: dict[str, Any] | None = None,
    pm: ProjectManager | None = None,
) -> ExceptionRecord:
    pm = pm or get_project_manager()
    project_name = pm.normalize_project_name(project_name)
    rec = ExceptionRecord(
        id=str(uuid.uuid4()),
        project=project_name,
        target_id=target_id,
        stage=stage,
        reasons=[str(r) for r in reasons if r],
        suggested_action=suggested_action,
        ts=datetime.now(UTC).isoformat(),
        meta=meta or {},
    )
    path = exceptions_path(pm, project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec.as_dict(), ensure_ascii=False) + "\n")
    return rec


def list_exceptions(
    *,
    project_name: str,
    status: str | None = "open",
    pm: ProjectManager | None = None,
) -> list[ExceptionRecord]:
    pm = pm or get_project_manager()
    project_name = pm.normalize_project_name(project_name)
    path = exceptions_path(pm, project_name)
    if not path.is_file():
        return []
    out: list[ExceptionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            rec = ExceptionRecord(
                id=str(d.get("id") or ""),
                project=str(d.get("project") or project_name),
                target_id=str(d.get("target_id") or ""),
                stage=str(d.get("stage") or ""),
                reasons=list(d.get("reasons") or []),
                suggested_action=str(d.get("suggested_action") or "review"),
                ts=str(d.get("ts") or ""),
                status=str(d.get("status") or "open"),
                resolve_action=d.get("resolve_action"),
                resolved_at=d.get("resolved_at"),
                meta=dict(d.get("meta") or {}),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if status is not None and rec.status != status:
            continue
        out.append(rec)
    return out


def resolve_exception(
    exception_id: str,
    *,
    project_name: str,
    action: ResolveAction,
    pm: ProjectManager | None = None,
) -> ExceptionRecord:
    """将匹配 id 的记录标 resolved，并重写 jsonl（整文件 rewrite）。"""
    pm = pm or get_project_manager()
    project_name = pm.normalize_project_name(project_name)
    path = exceptions_path(pm, project_name)
    if not path.is_file():
        raise FileNotFoundError(f"no exceptions.jsonl for project {project_name}")

    lines = path.read_text(encoding="utf-8").splitlines()
    found: ExceptionRecord | None = None
    new_lines: list[str] = []
    now = datetime.now(UTC).isoformat()
    for line in lines:
        if not line.strip():
            continue
        d = json.loads(line)
        if str(d.get("id")) == exception_id:
            d["status"] = "resolved"
            d["resolve_action"] = action
            d["resolved_at"] = now
            found = ExceptionRecord(
                id=str(d["id"]),
                project=str(d.get("project") or project_name),
                target_id=str(d.get("target_id") or ""),
                stage=str(d.get("stage") or ""),
                reasons=list(d.get("reasons") or []),
                suggested_action=str(d.get("suggested_action") or "review"),
                ts=str(d.get("ts") or ""),
                status="resolved",
                resolve_action=action,
                resolved_at=now,
                meta=dict(d.get("meta") or {}),
            )
            # approve：人工放行回写 gate 标记
            if action == "approve":
                _apply_approve_gate(pm, project_name, found)
        new_lines.append(json.dumps(d, ensure_ascii=False))
    if found is None:
        raise KeyError(f"exception id not found: {exception_id}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return found


def _apply_approve_gate(pm: ProjectManager, project_name: str, rec: ExceptionRecord) -> None:
    """approve 时在 project.json 记一笔 manual gate 放行。"""

    def _mutate(p: dict[str, Any]) -> None:
        gates = p.setdefault("qc_approvals", {})
        if not isinstance(gates, dict):
            gates = {}
            p["qc_approvals"] = gates
        key = f"{rec.stage}:{rec.target_id}"
        gates[key] = {
            "exception_id": rec.id,
            "action": "approve",
            "resolved_at": rec.resolved_at,
        }

    pm.update_project(project_name, _mutate)
