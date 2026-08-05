"""queue watcher 单测（全 mock，不打真实 pipeline）。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ink2reel_driver import watcher as W


@pytest.fixture()
def qroot(tmp_path: Path) -> Path:
    root = tmp_path / "queue"
    W.ensure_queue_dirs(root)
    return root


def _put_inbox(qroot: Path, name: str, text: str = "hello novel", sidecar: dict | None = None) -> Path:
    p = qroot / "inbox" / name
    p.write_text(text, encoding="utf-8")
    if sidecar is not None:
        (qroot / "inbox" / (Path(name).stem + ".json")).write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
        )
    return p


def test_mtime_order(qroot: Path) -> None:
    a = _put_inbox(qroot, "b_second.txt", "b")
    time.sleep(0.02)
    b = _put_inbox(qroot, "a_first.txt", "a")
    # make a older
    older = time.time() - 100
    os.utime(a, (older, older))
    novels = W.list_inbox_novels(qroot / "inbox")
    assert novels[0].name == "b_second.txt"


def test_lock_mutex(qroot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert W.try_acquire_lock(qroot) is True
    # second acquire same pid ok
    assert W.try_acquire_lock(qroot) is True
    # fake other live pid
    lp = W.lock_path(qroot)
    lp.write_text(json.dumps({"pid": 1, "ts": time.time()}), encoding="utf-8")
    monkeypatch.setattr(W, "_pid_alive", lambda pid: pid == 1)
    assert W.try_acquire_lock(qroot) is False
    W.release_lock(qroot)  # won't remove other pid
    # force clear for cleanup
    lp.unlink(missing_ok=True)


def test_stale_lock_cleared(qroot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lp = W.lock_path(qroot)
    lp.write_text(
        json.dumps({"pid": 99999, "ts": time.time() - W.LOCK_STALE_SECONDS - 10}),
        encoding="utf-8",
    )
    monkeypatch.setattr(W, "_pid_alive", lambda pid: False)
    assert W.try_acquire_lock(qroot) is True
    W.release_lock(qroot)


def test_live_lock_kept(qroot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lp = W.lock_path(qroot)
    lp.write_text(json.dumps({"pid": 42, "ts": time.time()}), encoding="utf-8")
    monkeypatch.setattr(W, "_pid_alive", lambda pid: pid == 42)
    assert W.try_acquire_lock(qroot) is False


def test_atomic_write_json(qroot: Path) -> None:
    path = qroot / "done" / "x" / "result.json"
    W.atomic_write_json(path, {"ok": True})
    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True


def test_sidecar_defaults_and_override(qroot: Path) -> None:
    p = _put_inbox(qroot, "n.txt", "body")
    assert W.load_sidecar(p)["until"] == "storyboard"
    p2 = _put_inbox(qroot, "m.txt", "body", sidecar={"until": "deliverables", "threshold": 40})
    cfg = W.load_sidecar(p2)
    assert cfg["until"] == "deliverables"
    assert cfg["threshold"] == 40
    assert cfg["gate"] == "threshold"


def test_three_bins_done_attention_failed(qroot: Path) -> None:
    def runner_done(novel, **kw):
        return {"project": kw["project_name"], "escalated": [], "gate": {"escalated": False}}

    def runner_att(novel, **kw):
        return {
            "project": kw["project_name"],
            "escalated": [{"stage": "script_qc"}],
            "gate": {"escalated": True},
            "deliverables": "projects/x/deliverables/EP-001",
        }

    def runner_fail(novel, **kw):
        raise RuntimeError("boom")

    _put_inbox(qroot, "ok.txt", "novel ok")
    r = W.scan_once(qroot, run_pipeline=runner_done)
    assert r.status == "consumed"
    assert r.bin == "done"
    assert list((qroot / "done").iterdir())

    _put_inbox(qroot, "esc.txt", "novel esc")
    r2 = W.scan_once(qroot, run_pipeline=runner_att)
    assert r2.bin == "attention"
    dest = next((qroot / "attention").iterdir())
    assert (dest / "result.json").is_file()
    body = json.loads((dest / "result.json").read_text(encoding="utf-8"))
    assert body["escalated"]

    # retry1 then fail on retry2
    _put_inbox(qroot, "bad.txt", "bad")
    r3 = W.scan_once(qroot, run_pipeline=runner_fail)
    assert r3.status == "failed"
    assert r3.bin == "inbox"  # requeued
    assert any(p.name.startswith("bad.retry1") for p in (qroot / "inbox").iterdir())

    r4 = W.scan_once(qroot, run_pipeline=runner_fail)
    assert r4.bin == "inbox"
    assert any("retry2" in p.name for p in (qroot / "inbox").iterdir())

    r5 = W.scan_once(qroot, run_pipeline=runner_fail)
    assert r5.bin == "failed"
    assert list((qroot / "failed").iterdir())
    err = next((qroot / "failed").rglob("error.txt"))
    assert "boom" in err.read_text(encoding="utf-8")


def test_heal_processing_to_inbox_and_failed(qroot: Path) -> None:
    proc = qroot / "processing"
    (proc / "stuck.txt").write_text("x", encoding="utf-8")
    moved = W.heal_processing(qroot)
    assert any(m.startswith("inbox:") for m in moved)
    assert any(p.name.startswith("stuck.retry1") for p in (qroot / "inbox").iterdir())

    (proc / "stuck.retry2.txt").write_text("y", encoding="utf-8")
    moved2 = W.heal_processing(qroot)
    assert any(m.startswith("failed:") for m in moved2)
    assert list((qroot / "failed").iterdir())


def test_empty_inbox(qroot: Path) -> None:
    r = W.scan_once(qroot, run_pipeline=lambda *a, **k: {})
    assert r.status == "empty"


def test_lock_busy_skipped(qroot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(W, "try_acquire_lock", lambda *a, **k: False)
    r = W.scan_once(qroot)
    assert r.status == "skipped"


def test_project_name_hash(qroot: Path) -> None:
    p = _put_inbox(qroot, "午夜便利店.txt", "same-content")
    n1 = W.project_name_for(p)
    assert n1.startswith("item-")
    assert re.fullmatch(r"[a-z0-9\-]+", n1)
    assert len(n1.split("-")[-1]) == 6


import re  # noqa: E402  — used by test_project_name_hash


def test_retry_name() -> None:
    assert W.next_retry_name("a.txt") == "a.retry1.txt"
    assert W.next_retry_name("a.retry1.txt") == "a.retry2.txt"
    assert W.retry_suffix_count("a.retry2.txt") == 2
