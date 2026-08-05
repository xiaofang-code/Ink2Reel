"""Driver 运行日志：JSONL，落盘 projects/{name}/driver_journal.jsonl。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class JournalEvent:
    ts: str
    stage: str
    event: str  # start | end | gate | error | info
    elapsed_s: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class DriverJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_starts: dict[str, float] = {}

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _write(self, event: JournalEvent) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

    def start(self, stage: str, **detail: Any) -> None:
        self._stage_starts[stage] = time.perf_counter()
        self._write(JournalEvent(ts=self._now(), stage=stage, event="start", detail=detail))

    def end(self, stage: str, **detail: Any) -> None:
        t0 = self._stage_starts.pop(stage, None)
        elapsed = (time.perf_counter() - t0) if t0 is not None else None
        self._write(
            JournalEvent(ts=self._now(), stage=stage, event="end", elapsed_s=elapsed, detail=detail)
        )

    def gate(self, stage: str, **detail: Any) -> None:
        self._write(JournalEvent(ts=self._now(), stage=stage, event="gate", detail=detail))

    def error(self, stage: str, **detail: Any) -> None:
        t0 = self._stage_starts.pop(stage, None)
        elapsed = (time.perf_counter() - t0) if t0 is not None else None
        self._write(
            JournalEvent(ts=self._now(), stage=stage, event="error", elapsed_s=elapsed, detail=detail)
        )

    def info(self, stage: str, **detail: Any) -> None:
        self._write(JournalEvent(ts=self._now(), stage=stage, event="info", detail=detail))
