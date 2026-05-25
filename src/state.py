"""
State models for ZoharTGBatch orchestrator.

Difference from ZoharTG: `current_articles: list[CurrentArticle]` instead
of `current_article: Optional[CurrentArticle]` to support N parallel
translators in flight at once. Also adds `total_failures_without_success`
counter (renamed from sequential `consecutive_failures`).

Files in state/:
  current_run.json  — single-record current FSM state
  events.jsonl      — append-only event log
  last_session.json — snapshot of the last finalized cycle
"""
from __future__ import annotations

import datetime as dt
import json
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class State(str, Enum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    DONE = "DONE"
    ERROR = "ERROR"


IdleReason = Literal["fresh", "hit_limit_wait", "manual", "boot"]
ExitReason = Literal[
    "manifest_exhausted",
    "hit_limit",
    "stop",
    "kill",
    "post_hit_limit_loop",
    "fatal_translator_error",
    "consecutive_failures",
    "translator_timeout",
]
ReportStatus = Literal["ok", "hit_limit", "empty", "skip", "error"]


class CurrentArticle(BaseModel):
    batch_name: str
    started_at: dt.datetime
    expected_chars: int = 0


class CurrentRun(BaseModel):
    state: State = State.IDLE
    idle_reason: Optional[IdleReason] = "boot"
    next_wake_at: Optional[dt.datetime] = None

    mode: Literal["unlimited", "usage"] = "unlimited"
    usage_limit_pct: Optional[int] = None

    started_at: Optional[dt.datetime] = None
    manifest_path: Optional[Path] = None

    current_articles: list[CurrentArticle] = Field(default_factory=list)
    completed_in_run: int = 0
    failed_in_run: int = 0
    completed_in_run_batch_names: list[str] = Field(default_factory=list)

    # Resumable translation: count of articles that came back as
    # marker_rc=4 (partial .md kept on disk; next cycle resumes them).
    # Not counted as either completed or failed; reset on each new cycle.
    partial_in_run: int = 0
    partial_articles_in_run: list[str] = Field(default_factory=list)

    is_post_hit_limit_retry: bool = False
    exit_reason: Optional[ExitReason] = None
    last_report_status: Optional[ReportStatus] = None

    error_message: Optional[str] = None

    def write(self, path: Path) -> None:
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> "CurrentRun":
        if not path.exists():
            return cls()
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class Event(BaseModel):
    ts: dt.datetime = Field(default_factory=lambda: dt.datetime.now().replace(microsecond=0))
    type: str
    data: dict = Field(default_factory=dict)


def append_event(events_path: Path, event: Event) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(event.model_dump_json() + "\n")


def read_events_tail(events_path: Path, n: int = 50) -> list[Event]:
    if not events_path.exists():
        return []
    lines = events_path.read_text(encoding="utf-8").splitlines()
    return [Event.model_validate_json(line) for line in lines[-n:] if line.strip()]


class LastSession(BaseModel):
    ts: dt.datetime = Field(default_factory=lambda: dt.datetime.now().replace(microsecond=0))
    mode: str = "unlimited"
    started_at: Optional[dt.datetime] = None
    finished_at: Optional[dt.datetime] = None
    completed_in_run: int = 0
    failed_in_run: int = 0
    completed_in_run_batch_names: list[str] = Field(default_factory=list)
    partial_in_run: int = 0
    partial_articles_in_run: list[str] = Field(default_factory=list)
    chapters_touched: list[str] = Field(default_factory=list)
    chapters_done_this_cycle: list[str] = Field(default_factory=list)
    site_rebuilt: bool = False
    site_rebuilt_ts: Optional[str] = None
    report_status: Optional[ReportStatus] = None
    hit_limit_reset: Optional[str] = None
    next_cursor_after: Optional[str] = None
    done_articles_after: Optional[int] = None
    done_chapters_after: Optional[int] = None
    parallel_translators: Optional[int] = None
    parallel_cache_primer: Optional[bool] = None

    def write(self, path: Path) -> None:
        path.write_text(
            self.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )
