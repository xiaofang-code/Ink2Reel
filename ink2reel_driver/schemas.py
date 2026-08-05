"""交付物 Schema（用户 codex 消费契约）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class TranscriptLine(BaseModel):
    segment_id: str
    kind: Literal["narration", "dialogue"] = "narration"
    text: str
    est_start_s: float = Field(ge=0)
    est_duration_s: float = Field(ge=0)


class TranscriptDocument(BaseModel):
    project: str
    episode: int
    schema_version: str = "1.0"
    lines: list[TranscriptLine]

    @field_validator("lines")
    @classmethod
    def _monotonic(cls, lines: list[TranscriptLine]) -> list[TranscriptLine]:
        prev_end = 0.0
        for line in lines:
            if line.est_start_s < 0:
                raise ValueError(f"{line.segment_id}: est_start_s < 0")
            if line.est_start_s + 1e-9 < prev_end:
                raise ValueError(
                    f"{line.segment_id}: est_start_s={line.est_start_s} not >= previous end={prev_end}"
                )
            prev_end = line.est_start_s + line.est_duration_s
        return lines


class CanvasSpec(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30


class TimelineItem(BaseModel):
    segment_id: str
    source: str
    start: float = Field(ge=0)
    est_duration_s: float = Field(ge=0)
    status: Literal["pending_media", "ready", "missing", "failed"] = "pending_media"


class EditManifest(BaseModel):
    project: str
    episode: int = 1
    schema_version: str = "1.0"
    canvas: CanvasSpec = Field(default_factory=CanvasSpec)
    timeline: list[TimelineItem]
    captions: str = "transcript/subtitles.srt"
    voiceover: Any | None = None
    music: Any | None = None


class StoryboardMeta(BaseModel):
    schema_version: str = "1.0"
    generated_at: str
    source_episode: int
    project: str
    content_mode: str = "narration"
