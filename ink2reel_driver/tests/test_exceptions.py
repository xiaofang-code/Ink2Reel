"""异常队列 + CLI round-trip。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ink2reel_driver.__main__ import main
from ink2reel_driver.exceptions import append_exception, list_exceptions, resolve_exception
from lib.project_manager import ProjectManager


def _pm(tmp_path: Path) -> ProjectManager:
    root = tmp_path / "projects"
    root.mkdir()
    return ProjectManager(str(root))


def _seed(pm: ProjectManager, name: str = "qc-probe") -> None:
    pm.create_project(name, content_mode="narration")
    pm.create_project_metadata(name, title=name, content_mode="narration")


def test_append_list_resolve(tmp_path: Path) -> None:
    pm = _pm(tmp_path)
    _seed(pm)
    rec = append_exception(
        project_name="qc-probe",
        target_id="E1S01",
        stage="storyboard_qc",
        reasons=["prompt_too_long"],
        suggested_action="regenerate",
        pm=pm,
    )
    assert rec.id
    opens = list_exceptions(project_name="qc-probe", pm=pm)
    assert len(opens) == 1
    assert opens[0].id == rec.id

    resolved = resolve_exception(rec.id, project_name="qc-probe", action="skip", pm=pm)
    assert resolved.status == "resolved"
    assert resolved.resolve_action == "skip"
    assert list_exceptions(project_name="qc-probe", pm=pm) == []
    assert len(list_exceptions(project_name="qc-probe", status=None, pm=pm)) == 1


def test_cli_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pm = _pm(tmp_path)
    _seed(pm)

    monkeypatch.setattr("lib.project_manager.get_project_manager", lambda: pm)
    code = main(["exceptions", "list", "--project", "qc-probe"])
    assert code == 0


def test_cli_resolve_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pm = _pm(tmp_path)
    _seed(pm)
    rec = append_exception(
        project_name="qc-probe",
        target_id="EP-001",
        stage="script_qc",
        reasons=["low_score"],
        pm=pm,
    )
    monkeypatch.setattr("lib.project_manager.get_project_manager", lambda: pm)
    code = main(
        ["exceptions", "resolve", rec.id, "--project", "qc-probe", "--action", "approve"]
    )
    assert code == 0
    proj = pm.load_project("qc-probe")
    assert "qc_approvals" in proj
