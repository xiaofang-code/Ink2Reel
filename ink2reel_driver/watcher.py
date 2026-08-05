"""queue 投喂闭环：扫描 inbox → 消费 pipeline → 三分箱 done/attention/failed。

目录（相对 ink2reel 或自定义 queue_root）：
  inbox/ processing/ done/ attention/ failed/ queue_journal.jsonl .lock
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 锁超过此时长且 PID 不存活 → 视作陈锁
LOCK_STALE_SECONDS = 2 * 3600
NOVEL_SUFFIXES = (".txt", ".md")
DEFAULT_SIDECAR: dict[str, Any] = {
    "until": "storyboard",
    "threshold": 50,
    "budget_usd": 5,
    "gate": "threshold",
}


@dataclass
class ScanResult:
    status: str  # consumed|empty|skipped|failed
    item: str | None = None
    project: str | None = None
    bin: str | None = None  # done|attention|failed
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_queue_root() -> Path:
    """ink2reel/queue（本文件位于 ink2reel_driver/）。"""
    return Path(__file__).resolve().parent.parent / "queue"


def ensure_queue_dirs(queue_root: Path) -> None:
    for name in ("inbox", "processing", "done", "attention", "failed"):
        (queue_root / name).mkdir(parents=True, exist_ok=True)


def _slugify(name: str) -> str:
    """ArcReel project 名仅允许 [a-z0-9-]；中文文件名 → item。"""
    base = Path(name).stem
    base = re.sub(r"\.retry\d+$", "", base)
    # 只留 ASCII 字母数字
    ascii_only = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    if not ascii_only or not any(c.isalpha() for c in ascii_only):
        ascii_only = "item"
    return ascii_only[:32]


def content_hash6(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:6]


def project_name_for(path: Path) -> str:
    return f"{_slugify(path.name)}-{content_hash6(path)}"


def sidecar_path_for(novel: Path) -> Path:
    return novel.with_suffix(".json")


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("json read failed %s: %s", path, exc)
        return None


def load_sidecar(novel: Path) -> dict[str, Any]:
    cfg = dict(DEFAULT_SIDECAR)
    sp = sidecar_path_for(novel)
    if sp.is_file():
        data = _read_json_file(sp)
        if data:
            cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_queue_journal(queue_root: Path, event: dict[str, Any]) -> None:
    path = queue_root / "queue_journal.jsonl"
    event = {**event, "ts": datetime.now(UTC).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ----- lock -----


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False


def lock_path(queue_root: Path) -> Path:
    return queue_root / ".lock"


def try_acquire_lock(queue_root: Path, *, now: float | None = None) -> bool:
    """获取单实例锁。成功 True；被活锁占用 False。陈锁会清除。"""
    ensure_queue_dirs(queue_root)
    lp = lock_path(queue_root)
    now = time.time() if now is None else now
    if lp.exists():
        try:
            meta = json.loads(lp.read_text(encoding="utf-8"))
            pid = int(meta.get("pid") or 0)
            ts = float(meta.get("ts") or 0)
            age = now - ts
            alive = _pid_alive(pid)
            if alive and pid == os.getpid():
                return True
            if alive and pid != os.getpid():
                return False
            # PID 不存活
            if age > LOCK_STALE_SECONDS or age > 60:
                append_queue_journal(
                    queue_root,
                    {"event": "stale_lock_cleared", "pid": pid, "age_s": age},
                )
                lp.unlink(missing_ok=True)
            else:
                # 刚死 <60s，防竞态
                return False
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            lp.unlink(missing_ok=True)

    payload = {"pid": os.getpid(), "ts": now}
    try:
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock(queue_root: Path) -> None:
    lp = lock_path(queue_root)
    try:
        if not lp.exists():
            return
        meta = json.loads(lp.read_text(encoding="utf-8"))
        if int(meta.get("pid") or 0) == os.getpid():
            lp.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # 仅当内容像本进程写的才删；否则尝试匹配
        try:
            lp.unlink(missing_ok=True)
        except OSError:
            pass


# ----- retry / self-heal -----


def retry_suffix_count(name: str) -> int:
    m = re.search(r"\.retry(\d+)(\.[^.]+)?$", name)
    if m:
        return int(m.group(1))
    return 0


def next_retry_name(name: str) -> str:
    n = retry_suffix_count(name)
    stem = re.sub(r"\.retry\d+$", "", Path(name).stem)
    suffix = Path(name).suffix
    return f"{stem}.retry{n + 1}{suffix}"


def heal_processing(queue_root: Path) -> list[str]:
    """processing/ 遗留 → inbox 加 .retryN；已 .retry2 仍在 processing → failed/。"""
    moved: list[str] = []
    proc = queue_root / "processing"
    if not proc.is_dir():
        return moved
    for p in list(proc.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in NOVEL_SUFFIXES and not p.name.endswith(NOVEL_SUFFIXES):
            # 也处理 .txt.retry1 等
            if not any(p.name.endswith(s) or f".retry" in p.name for s in NOVEL_SUFFIXES):
                if p.suffix.lower() not in (".txt", ".md", ".json"):
                    continue
        if p.suffix.lower() == ".json":
            continue  # sidecar 随主文件
        n = retry_suffix_count(p.name)
        if n >= 2:
            dest_dir = queue_root / "failed" / _slugify(p.name)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / p.name
            os.replace(p, dest)
            err = dest_dir / "error.txt"
            err.write_text(f"retry exhausted (found in processing as {p.name})\n", encoding="utf-8")
            # move sidecar if any
            sc = proc / (p.stem + ".json") if not re.search(r"\.retry\d+$", p.stem) else None
            # sidecar named like novel.with_suffix
            for cand in proc.glob(p.stem.split(".retry")[0] + "*.json"):
                if cand.is_file():
                    os.replace(cand, dest_dir / cand.name)
            moved.append(f"failed:{p.name}")
            append_queue_journal(
                queue_root, {"event": "heal_to_failed", "item": p.name, "retries": n}
            )
        else:
            new_name = next_retry_name(p.name)
            dest = queue_root / "inbox" / new_name
            os.replace(p, dest)
            # sidecar
            base_stem = re.sub(r"\.retry\d+$", "", p.stem)
            for cand in list(proc.glob(base_stem + "*.json")) + list(proc.glob(p.stem + ".json")):
                if cand.is_file() and cand.suffix == ".json":
                    # keep sidecar name aligned with novel stem if possible
                    sc_dest = queue_root / "inbox" / (Path(new_name).stem + ".json")
                    try:
                        os.replace(cand, sc_dest)
                    except OSError:
                        pass
            moved.append(f"inbox:{new_name}")
            append_queue_journal(
                queue_root, {"event": "heal_to_inbox", "item": p.name, "as": new_name}
            )
    return moved


def list_inbox_novels(inbox: Path) -> list[Path]:
    items: list[Path] = []
    if not inbox.is_dir():
        return items
    for p in inbox.iterdir():
        if not p.is_file():
            continue
        # novel: .txt/.md，允许 .retryN 插在 stem
        low = p.name.lower()
        if low.endswith(".txt") or low.endswith(".md"):
            items.append(p)
    items.sort(key=lambda x: (x.stat().st_mtime, x.name))
    return items


# ----- consume -----

PipelineRunner = Callable[..., dict[str, Any]]


def _default_run_pipeline(
    novel: Path,
    *,
    project_name: str,
    until: str,
    gate: str,
    threshold: float,
    budget_usd: float,
) -> dict[str, Any]:
    """进程内跑 pipeline；隔离性不足时可用子进程包装。"""
    from ink2reel_driver.pipeline import run

    # until 超出 script 时需进程内 worker 入队；幂等复用已有图可减少花费
    need_worker = until not in ("script",)
    return asyncio_run(
        run(
            novel,
            project_name=project_name,
            until=until,
            gate_mode=gate,  # type: ignore[arg-type]
            threshold=threshold,
            budget_usd=budget_usd,
            export=True,
            start_worker=need_worker,
            limit_segments=4,
        )
    )


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


def _exceptions_summary(project_name: str) -> list[dict[str, Any]]:
    try:
        from ink2reel_driver.exceptions import list_exceptions

        rows = list_exceptions(project_name=project_name, status=None)
        return [
            {
                "id": r.id,
                "stage": r.stage,
                "target_id": r.target_id,
                "reasons": r.reasons,
                "status": r.status,
            }
            for r in rows[-20:]
        ]
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]


def _bin_item(
    queue_root: Path,
    *,
    bin_name: str,
    item_key: str,
    novel: Path,
    sidecar: Path | None,
    result: dict[str, Any] | None,
    error: str | None,
) -> Path:
    dest_dir = queue_root / bin_name / item_key
    dest_dir.mkdir(parents=True, exist_ok=True)
    # move novel
    if novel.is_file():
        os.replace(novel, dest_dir / novel.name)
    if sidecar and sidecar.is_file():
        os.replace(sidecar, dest_dir / sidecar.name)
    if result is not None:
        atomic_write_json(dest_dir / "result.json", result)
    if error is not None:
        (dest_dir / "error.txt").write_text(error, encoding="utf-8")
    return dest_dir


def consume_one(
    novel: Path,
    queue_root: Path,
    *,
    run_pipeline: PipelineRunner | None = None,
) -> ScanResult:
    """消费 processing 中的一个 novel（调用方已 claim 移入）。"""
    runner = run_pipeline or _default_run_pipeline
    sc_path = sidecar_path_for(novel)
    if not sc_path.is_file():
        alt = novel.parent / (re.sub(r"\.retry\d+$", "", novel.stem) + ".json")
        if alt.is_file():
            sc_path = alt
    cfg = dict(DEFAULT_SIDECAR)
    if sc_path.is_file():
        data = _read_json_file(sc_path)
        if data:
            cfg.update({k: v for k, v in data.items() if v is not None})

    project = project_name_for(novel)
    item_key = project
    until = str(cfg.get("until") or "storyboard")
    gate = str(cfg.get("gate") or "threshold")
    threshold = float(cfg.get("threshold") or 50)
    budget = float(cfg.get("budget_usd") or 5)

    try:
        summary = runner(
            novel,
            project_name=project,
            until=until,
            gate=gate,
            threshold=threshold,
            budget_usd=budget,
        )
        escalated = summary.get("escalated") or []
        gate_info = summary.get("gate") or {}
        if gate_info.get("escalated"):
            escalated = escalated or [gate_info]
        has_esc = bool(escalated)

        result_body = {
            "project": project,
            "item": novel.name,
            "until": until,
            "gate": gate,
            "threshold": threshold,
            "budget_usd": budget,
            "summary": {k: v for k, v in summary.items() if not str(k).startswith("_")},
            "exceptions": _exceptions_summary(project) if has_esc else [],
            "deliverables": summary.get("deliverables"),
            "escalated": escalated,
        }
        bin_name = "attention" if has_esc else "done"
        dest = _bin_item(
            queue_root,
            bin_name=bin_name,
            item_key=item_key,
            novel=novel,
            sidecar=sc_path if sc_path.is_file() else None,
            result=result_body,
            error=None,
        )
        append_queue_journal(
            queue_root,
            {
                "event": "consumed",
                "item": novel.name,
                "project": project,
                "bin": bin_name,
                "dest": str(dest),
                "escalated": has_esc,
            },
        )
        return ScanResult(
            status="consumed",
            item=novel.name,
            project=project,
            bin=bin_name,
            detail={"dest": str(dest)},
        )
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        logger.exception("consume failed %s", novel)
        n = retry_suffix_count(novel.name)
        if n >= 2:
            dest = _bin_item(
                queue_root,
                bin_name="failed",
                item_key=item_key,
                novel=novel,
                sidecar=sc_path if sc_path.is_file() else None,
                result=None,
                error=err + "\n",
            )
            append_queue_journal(
                queue_root,
                {
                    "event": "failed",
                    "item": novel.name,
                    "project": project,
                    "bin": "failed",
                    "error": err,
                },
            )
            return ScanResult(
                status="failed",
                item=novel.name,
                project=project,
                bin="failed",
                detail={"error": err, "dest": str(dest)},
            )
        # 回 inbox 递增 retry
        new_name = next_retry_name(novel.name)
        dest_n = queue_root / "inbox" / new_name
        os.replace(novel, dest_n)
        if sc_path.is_file():
            os.replace(sc_path, queue_root / "inbox" / (Path(new_name).stem + ".json"))
        append_queue_journal(
            queue_root,
            {
                "event": "retry_requeue",
                "item": novel.name,
                "as": new_name,
                "error": err,
            },
        )
        return ScanResult(
            status="failed",
            item=novel.name,
            project=project,
            bin="inbox",
            detail={"error": err, "requeued": new_name},
        )


def claim_oldest(queue_root: Path) -> Path | None:
    """inbox 最旧 novel → processing（os.replace）。"""
    novels = list_inbox_novels(queue_root / "inbox")
    if not novels:
        return None
    novel = novels[0]
    proc = queue_root / "processing" / novel.name
    os.replace(novel, proc)
    # claim sidecar
    for cand in (
        queue_root / "inbox" / (novel.stem + ".json"),
        queue_root / "inbox" / (re.sub(r"\.retry\d+$", "", novel.stem) + ".json"),
    ):
        if cand.is_file():
            try:
                os.replace(cand, queue_root / "processing" / cand.name)
            except OSError:
                pass
            break
    return proc


def scan_once(
    queue_root: Path | None = None,
    *,
    run_pipeline: PipelineRunner | None = None,
    acquire_lock: bool = True,
) -> ScanResult:
    root = Path(queue_root) if queue_root else default_queue_root()
    ensure_queue_dirs(root)

    if acquire_lock:
        if not try_acquire_lock(root):
            append_queue_journal(root, {"event": "lock_busy", "status": "skipped"})
            return ScanResult(status="skipped", detail={"reason": "lock_held"})

    try:
        heal_processing(root)
        claimed = claim_oldest(root)
        if claimed is None:
            append_queue_journal(root, {"event": "empty", "status": "empty"})
            return ScanResult(status="empty")
        return consume_one(claimed, root, run_pipeline=run_pipeline)
    finally:
        if acquire_lock:
            release_lock(root)


def run_forever(
    interval_s: float = 300.0,
    queue_root: Path | None = None,
    *,
    run_pipeline: PipelineRunner | None = None,
) -> None:
    root = Path(queue_root) if queue_root else default_queue_root()
    ensure_queue_dirs(root)
    stop = {"flag": False}

    def _stop(*_a: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _stop)
        except (ValueError, OSError):
            pass

    logger.info("queue watcher start interval=%s root=%s", interval_s, root)
    while not stop["flag"]:
        try:
            r = scan_once(root, run_pipeline=run_pipeline)
            logger.info("scan_once %s", r.as_dict())
        except Exception:  # noqa: BLE001
            logger.exception("scan_once error")
        # 可中断 sleep
        end = time.time() + max(1.0, float(interval_s))
        while time.time() < end and not stop["flag"]:
            time.sleep(0.5)
    release_lock(root)
    logger.info("queue watcher stopped")
