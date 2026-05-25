"""
Config loader for ZoharTGBatch (parallel-batch variant of ZoharTG).

Adds parallel_translators, parallel_cache_primer, consecutive_failure_threshold,
dashboard_debounce_seconds. Otherwise mirrors ZoharTG/config.py.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class Config(BaseModel):
    tg_bot_token: str
    tg_public_channel: Optional[str] = None
    tg_private_chat_id: Optional[int]

    heb_root: Path
    log_level: str = "INFO"

    state_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "state")

    # Hard timeout for a single translator subprocess; zombie hangs are
    # cut at this mark. Reduced from 3600→1800 on 2026-05-20 — typical
    # article translates in ~5-15 min, so a 30-min cap is generous and
    # still gives operator a chance to react before a full hour stalls.
    # Override via TRANSLATOR_TIMEOUT_SECONDS env if you want the old value.
    translator_timeout_seconds: int = 1800
    claude_bin: str = "claude"
    translator_effort: str = ""

    # Parallel-mode knobs.
    parallel_translators: int = 10
    parallel_cache_primer: bool = False
    consecutive_failure_threshold: int = 5
    dashboard_debounce_seconds: float = 3.0
    # Burst-mode failure detection in orchestrator parallel waves: if
    # `burst_threshold` translators fail within `burst_window_seconds`,
    # halt the cycle early before the full consecutive_failure_threshold
    # is exhausted. Moved from orchestrator.py hardcoded constants on
    # 2026-05-20.
    burst_window_seconds: int = 30
    burst_threshold: int = 3

    # Chunk budget for Шаг 4 of translation_prompt.md. Source-character
    # budget for one chunk: translator accumulates paragraphs until the
    # sum exceeds CHUNK_BUDGET_CHARS, then closes the chunk and starts
    # a new one. Single paragraph > budget → its own chunk (no splitting).
    # Passed to build_batch.py via --chunk-budget-chars; rendered into
    # the prompt as {{chunk_budget_chars}}.
    chunk_budget_chars: int = 7500

    # GitHub Pages auto-deploy (optional). If gh_token is empty, the bot
    # logs a one-time warning at startup and skips all push attempts.
    gh_token: Optional[str] = None
    gh_repo: Optional[str] = None        # primary repo, served at username.github.io/repo
    gh_mirror_repo: Optional[str] = None  # optional mirror repo with custom domain
    gh_custom_domain: Optional[str] = None  # CNAME target for the mirror repo
    gh_deploy_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "state" / "site_git")
    gh_mirror_deploy_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "state" / "site_git_mirror")
    gh_deploy_debounce_seconds: float = 30.0
    # Minimum gap between two consecutive auto-deploys, in seconds. With
    # N parallel translators each emitting article_done, this caps the
    # auto-push rate at 1 / interval, regardless of how many articles
    # land in a wave. Manual /push and /rebuild bypass this throttle.
    # Pending pushes survive cycle_finished and fire during IDLE wait.
    gh_deploy_min_interval_seconds: float = 600.0

    @field_validator("log_level")
    @classmethod
    def _level_valid(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR, got {v!r}")
        return v

    @field_validator("parallel_translators")
    @classmethod
    def _parallel_valid(cls, v: int) -> int:
        if not (1 <= v <= 50):
            raise ValueError(f"PARALLEL_TRANSLATORS must be in [1,50], got {v}")
        return v

    @field_validator("consecutive_failure_threshold")
    @classmethod
    def _cft_valid(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"CONSECUTIVE_FAILURE_THRESHOLD must be >=1, got {v}")
        return v

    @field_validator("chunk_budget_chars")
    @classmethod
    def _chunk_budget_valid(cls, v: int) -> int:
        if not (1000 <= v <= 50000):
            raise ValueError(
                f"CHUNK_BUDGET_CHARS must be in [1000, 50000], got {v}"
            )
        return v


def load_config() -> Config:
    chat_raw = os.getenv("TG_PRIVATE_CHAT_ID", "").strip()
    chat_id: Optional[int] = int(chat_raw) if chat_raw else None

    timeout_raw = os.getenv("TRANSLATOR_TIMEOUT_SECONDS", "").strip()
    timeout_s = int(timeout_raw) if timeout_raw else 1800

    claude_bin_raw = os.getenv("CLAUDE_BIN", "").strip()
    if claude_bin_raw:
        claude_bin = claude_bin_raw
    else:
        resolved = shutil.which("claude")
        if resolved is None:
            raise RuntimeError(
                "Could not find `claude` CLI on PATH. "
                "Either install it (npm install -g @anthropic-ai/claude-code) "
                "or set CLAUDE_BIN in .env to the absolute path."
            )
        if resolved.lower().endswith((".cmd", ".bat")):
            npm_exe = (Path(resolved).parent
                       / "node_modules" / "@anthropic-ai" / "claude-code"
                       / "bin" / "claude.exe")
            if npm_exe.is_file():
                resolved = str(npm_exe)
        claude_bin = resolved

    effort_raw = os.getenv("TRANSLATOR_EFFORT", "").strip().lower()
    if effort_raw and effort_raw not in ("low", "medium", "high", "xhigh", "max"):
        raise RuntimeError(
            f"TRANSLATOR_EFFORT={effort_raw!r} invalid. "
            "Допустимы: '' (default) | low | medium | high | xhigh | max"
        )

    parallel_raw = os.getenv("PARALLEL_TRANSLATORS", "").strip()
    parallel_n = int(parallel_raw) if parallel_raw else 10

    primer_raw = os.getenv("PARALLEL_CACHE_PRIMER", "").strip().lower()
    primer = primer_raw in ("1", "true", "yes", "on")

    cft_raw = os.getenv("CONSECUTIVE_FAILURE_THRESHOLD", "").strip()
    cft = int(cft_raw) if cft_raw else 5

    debounce_raw = os.getenv("DASHBOARD_DEBOUNCE_SECONDS", "").strip()
    debounce = float(debounce_raw) if debounce_raw else 3.0

    burst_window_raw = os.getenv("BURST_WINDOW_SECONDS", "").strip()
    burst_window = int(burst_window_raw) if burst_window_raw else 30
    burst_thr_raw = os.getenv("BURST_THRESHOLD", "").strip()
    burst_thr = int(burst_thr_raw) if burst_thr_raw else 3

    chunk_budget_raw = os.getenv("CHUNK_BUDGET_CHARS", "").strip()
    chunk_budget = int(chunk_budget_raw) if chunk_budget_raw else 7500

    gh_deploy_debounce_raw = os.getenv("GH_DEPLOY_DEBOUNCE_SECONDS", "").strip()
    gh_deploy_debounce = (
        float(gh_deploy_debounce_raw) if gh_deploy_debounce_raw else 30.0
    )
    gh_deploy_min_interval_raw = os.getenv(
        "GH_DEPLOY_MIN_INTERVAL_SECONDS", "",
    ).strip()
    gh_deploy_min_interval = (
        float(gh_deploy_min_interval_raw) if gh_deploy_min_interval_raw else 600.0
    )
    gh_token = os.getenv("GH_TOKEN", "").strip() or None
    gh_repo = os.getenv("GH_REPO", "").strip() or None
    gh_mirror_repo = os.getenv("GH_MIRROR_REPO", "").strip() or None
    gh_custom_domain = os.getenv("GH_CUSTOM_DOMAIN", "").strip() or None

    cfg = Config(
        tg_bot_token=_required("TG_BOT_TOKEN"),
        tg_public_channel=(os.getenv("TG_PUBLIC_CHANNEL", "").strip() or None),
        tg_private_chat_id=chat_id,
        heb_root=Path(_required("HEB_ROOT")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        translator_timeout_seconds=timeout_s,
        claude_bin=claude_bin,
        translator_effort=effort_raw,
        parallel_translators=parallel_n,
        parallel_cache_primer=primer,
        consecutive_failure_threshold=cft,
        dashboard_debounce_seconds=debounce,
        burst_window_seconds=burst_window,
        burst_threshold=burst_thr,
        chunk_budget_chars=chunk_budget,
        gh_token=gh_token,
        gh_repo=gh_repo,
        gh_mirror_repo=gh_mirror_repo,
        gh_custom_domain=gh_custom_domain,
        gh_deploy_debounce_seconds=gh_deploy_debounce,
        gh_deploy_min_interval_seconds=gh_deploy_min_interval,
    )

    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.heb_root.is_dir():
        raise RuntimeError(f"HEB_ROOT does not exist or is not a directory: {cfg.heb_root}")

    return cfg


def _required(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v:
        raise RuntimeError(f"Required env var {key!r} is empty or missing in .env")
    return v
