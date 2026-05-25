"""
ZoharTGBatch orchestrator — параллельный батч-вариант ZoharTG.

States: IDLE → PREPARING → RUNNING → FINALIZING → IDLE (+ DONE / ERROR).

Главное отличие от ZoharTG/orchestrator.py:
  _handle_running крутит до N translator'ов одновременно через
  asyncio.wait(FIRST_COMPLETED). На любом «жёстком» событии
  (hit_limit / fatal / timeout / consecutive_failures threshold)
  отменяет оставшиеся in-flight задачи и переходит в FINALIZING.

Anti-spam guards адаптированы для параллельного режима. Вместо
«N consecutive failures» считаем `total_failures_without_success`,
обнуляющийся на каждом успешном переводе. Threshold берётся из
.env (CONSECUTIVE_FAILURE_THRESHOLD, default 5).

mark_article_done.py вызовы из параллельных task'ов сериализованы
глобальным asyncio.Lock — у скрипта read-modify-write на
progress.json, без lock'а две одновременных main_done из одной
главы могут потерять запись.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from config import Config
from state import (
    CurrentArticle,
    CurrentRun,
    Event,
    LastSession,
    State,
    append_event,
)


logger = logging.getLogger("zohartgbatch.orchestrator")

CORPUS_TOOLS = Path(__file__).resolve().parents[1] / "corpus_tools"

HIT_LIMIT_RX = re.compile(r"hit your limit", re.IGNORECASE)
HIT_LIMIT_RESET_RX = re.compile(
    r"hit your limit.*?resets?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE | re.DOTALL,
)

FATAL_TRANSLATOR_RX = re.compile(
    r"\b(401|403|unauthorized|forbidden|invalid\s+api\s+key|"
    r"authentication\s+failed|api\s+key\s+(?:not\s+found|missing|invalid)|"
    r"insufficient\s+credit|quota\s+exhausted|account\s+(?:suspended|disabled)|"
    r"access\s+denied)\b",
    re.IGNORECASE,
)

# Match the mark_article_done.py print on closing: "{batch_name}: chapter
# {chapter_en} closed → rebuilding site". Group 1 = chapter_en (e.g. "Shemot").
CHAPTER_CLOSED_RX = re.compile(
    r"chapter\s+(\S+)\s+closed",
    re.IGNORECASE,
)


EventCallback = Callable[[Event], Awaitable[None]]


@dataclass
class ArticleResult:
    """Outcome of one parallel _process_one_article task."""
    batch_name: str
    translator_rc: int
    marker_rc: int
    fatal_match: Optional[str]
    timeout: bool


class Orchestrator:
    """Single-instance FSM driver for the parallel-batch variant."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state_file = cfg.state_dir / "current_run.json"
        self.events_file = cfg.state_dir / "events.jsonl"
        self.last_session_file = cfg.state_dir / "last_session.json"

        self.heb_root = cfg.heb_root
        self.batch_dir = self.heb_root / ".batch"

        self.run = CurrentRun.read(self.state_file)

        self._wake_event = asyncio.Event()
        self._stop_flag = False
        self._kill_flag = False
        self._shutdown_flag = False

        # Track all currently-running translator subprocesses. Keyed by
        # batch_name so /kill can iterate them all.
        self._current_procs: dict[str, asyncio.subprocess.Process] = {}

        # Set when ANY translator task exits via timeout — first one to
        # signal halts the wave. Used to populate ERROR message.
        self._timeout_translator_rc: Optional[int] = None
        self._timeout_batch_name: Optional[str] = None

        # Serializes calls to mark_article_done.py — see module docstring.
        self._mark_done_lock = asyncio.Lock()

        # Queued run request — set when user pressed «⚡ Run now» / «▶️ Run...»
        # while a cycle was still in flight (state in RUNNING/PREPARING/
        # FINALIZING). Applied at the end of FINALIZING (override of the
        # default IDLE-manual landing for stop/kill exit_reasons). Cleared
        # after apply, on /kill (force-stop), or on /resume from ERROR.
        self._pending_run: Optional[dict] = None

        # Set of chapter_en that were already published mid-cycle via
        # parsing of mark_article_done.py stderr ("chapter X closed →
        # rebuilding site"). FINALIZING skips them so we don't post a
        # duplicate zip per chapter. Reset at IDLE→PREPARING transition.
        self._published_chapters_in_run: set[str] = set()

        self._subscribers: list[EventCallback] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, cb: EventCallback) -> None:
        self._subscribers.append(cb)

    async def request_run(
        self,
        usage_limit_pct: Optional[int] = None,
        when: Optional[dt.datetime] = None,
    ) -> bool:
        """Schedule a manual run.

        Returns:
            True  — run scheduled immediately (state was IDLE/DONE/ERROR).
            False — cycle is in flight; request queued, will start
                    automatically as soon as the current cycle finalizes.
        """
        if self.run.state in (State.RUNNING, State.PREPARING, State.FINALIZING):
            self._pending_run = {
                "usage_limit_pct": usage_limit_pct,
                "when": when,
            }
            logger.info(
                "request_run queued (cycle in %s); will apply at FINALIZING tail",
                self.run.state.value,
            )
            await self._emit("run_queued", {
                "usage_limit_pct": usage_limit_pct,
                "when": when.isoformat() if when else None,
                "current_state": self.run.state.value,
            })
            return False
        self.run.next_wake_at = when or dt.datetime.now()
        self.run.idle_reason = "manual"
        self.run.usage_limit_pct = usage_limit_pct
        self.run.mode = "unlimited" if usage_limit_pct is None else "usage"
        self.run.is_post_hit_limit_retry = False
        if self.run.state == State.ERROR:
            self.run.state = State.IDLE
            self.run.error_message = None
        self._save("manual_run_requested")
        self._wake_event.set()
        return True

    async def clear_error(self) -> None:
        # /resume также сбрасывает pending run — переход из ERROR должен
        # требовать явного нового /run, не «достать» очередь годовой
        # давности из памяти.
        self._pending_run = None
        if self.run.state != State.ERROR:
            logger.info("clear_error ignored: state=%s", self.run.state)
            return
        self.run.state = State.IDLE
        self.run.idle_reason = "manual"
        self.run.next_wake_at = None
        self.run.error_message = None
        self.run.is_post_hit_limit_retry = False
        self.run.exit_reason = None
        self._save("error_cleared")
        self._wake_event.set()

    async def request_stop(self, graceful: bool = True) -> None:
        self._stop_flag = True
        if not graceful:
            self._kill_flag = True
            # Force stop = пользователь хочет жёсткий halt без авто-restart'а.
            # Если был pending run — отменяем его.
            self._pending_run = None
            await self._kill_all_procs()
        await self._emit("stop_requested", {"graceful": graceful})

    def request_shutdown(self) -> None:
        self._shutdown_flag = True
        self._stop_flag = True
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        if self.run.state in (State.RUNNING, State.PREPARING, State.FINALIZING):
            previous_state = self.run.state.value
            logger.error(
                "Boot recovery: orchestrator was in %s when last persisted; → ERROR",
                previous_state,
            )
            self.run.state = State.ERROR
            self.run.error_message = (
                f"Orchestrator restarted while in {previous_state}; "
                "manual /resume required"
            )
            self.run.current_articles = []
            self._save("boot_recovery_error")
            await self._emit("orchestrator_boot_recovery", {
                "previous_state": previous_state,
            })

        if self.run.state == State.IDLE and self.run.idle_reason is None:
            self.run.idle_reason = "boot"
            self.run.next_wake_at = dt.datetime.now()

        self._save("startup_persist")

        await self._emit("orchestrator_started", {
            "state": self.run.state.value,
            "parallel_translators": self.cfg.parallel_translators,
            "parallel_cache_primer": self.cfg.parallel_cache_primer,
        })

        while not self._shutdown_flag:
            try:
                if self.run.state == State.IDLE:
                    await self._handle_idle()
                elif self.run.state == State.PREPARING:
                    await self._handle_preparing()
                elif self.run.state == State.RUNNING:
                    await self._handle_running()
                elif self.run.state == State.FINALIZING:
                    await self._handle_finalizing()
                elif self.run.state == State.DONE:
                    await self._handle_done()
                elif self.run.state == State.ERROR:
                    await self._handle_error()
            except Exception as e:
                logger.exception("Unhandled exception in FSM loop")
                self.run.state = State.ERROR
                self.run.error_message = f"Unhandled: {type(e).__name__}: {e}"
                self._save("unhandled_exception")
                await self._emit("error", {"reason": self.run.error_message})

        await self._emit("orchestrator_stopped", {})
        logger.info("Orchestrator shutdown clean")

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    async def _handle_idle(self) -> None:
        now = dt.datetime.now()
        nw = self.run.next_wake_at

        if self.run.idle_reason == "manual" and nw is None:
            logger.info("IDLE — manual, waiting for /run")
            await self._wake_event.wait()
            self._wake_event.clear()
            return

        if nw is not None and nw > now:
            wait_seconds = (nw - now).total_seconds()
            logger.info(
                "IDLE — waiting until %s (%.0fs from now), reason=%s",
                nw.strftime("%H:%M:%S"),
                wait_seconds,
                self.run.idle_reason,
            )
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=wait_seconds)
                self._wake_event.clear()
                logger.info("IDLE — woken early by external request")
            except asyncio.TimeoutError:
                logger.info("IDLE — wake time reached")

        self.run.state = State.PREPARING
        self.run.started_at = dt.datetime.now()
        self.run.completed_in_run = 0
        self.run.failed_in_run = 0
        self.run.completed_in_run_batch_names = []
        self.run.partial_in_run = 0
        self.run.partial_articles_in_run = []
        self.run.current_articles = []
        self.run.exit_reason = None
        self.run.error_message = None
        self._stop_flag = False
        self._kill_flag = False
        self._timeout_translator_rc = None
        self._timeout_batch_name = None
        self._published_chapters_in_run = set()
        self._save("idle_to_preparing")
        await self._emit("state_transition", {
            "from": "IDLE",
            "to": "PREPARING",
            "reason": f"idle_reason={self.run.idle_reason}",
        })

    async def _handle_preparing(self) -> None:
        logger.info("PREPARING — checking next_cursor")
        rc, stdout, _stderr = await self._run_subprocess(
            [sys.executable, str(CORPUS_TOOLS / "next_cursor.py")], cwd=self.heb_root
        )
        if rc != 0:
            await self._goto_error("next_cursor.py failed")
            return
        try:
            nc = json.loads(stdout)
        except Exception as e:
            await self._goto_error(f"next_cursor.py output not JSON: {e}")
            return

        if nc.get("next_cursor") is None:
            logger.info("PREPARING — corpus complete, → DONE")
            self.run.state = State.DONE
            self._save("preparing_to_done")
            await self._emit("state_transition", {
                "from": "PREPARING", "to": "DONE",
                "reason": "next_cursor=null, corpus complete",
            })
            await self._emit("done", {})
            return

        chapter = nc["next_cursor"]
        bb_args = [
            sys.executable, str(CORPUS_TOOLS / "build_batch.py"), "--chapter", chapter,
            "--chunk-budget-chars", str(self.cfg.chunk_budget_chars),
        ]
        if self.run.mode == "unlimited":
            bb_args.append("--unlimited")
        else:
            # User-picked cap = target ceiling, не текущая usage. В UI
            # ConversationHandler'а кнопка «80 %» означает «остановиться
            # когда дойдём до 80% квоты», а не «текущий usage = 80%».
            # build_batch.py-семантика: budget_k = (target - usage)/100*1000.
            # Поэтому передаём --target N --usage 0 (мы не tracker'им
            # реальный usage, считаем квоту свежей; при необходимости
            # цикл оборвёт hit_limit-маркер на стороне translator'а).
            bb_args.extend([
                "--target", str(self.run.usage_limit_pct),
                "--usage", "0",
            ])

        logger.info("PREPARING — build_batch %s", " ".join(bb_args[2:]))
        await self._emit("tool_invocation", {
            "tool": "build_batch.py",
            "args": bb_args[2:],
            "phase": "started",
        })
        rc, stdout, stderr = await self._run_subprocess(bb_args, cwd=self.heb_root)
        if rc != 0:
            await self._emit("tool_invocation", {
                "tool": "build_batch.py", "phase": "failed", "rc": rc,
                "stderr_tail": stderr[-300:],
            })
            await self._goto_error(f"build_batch.py failed (rc={rc}): {stderr[:500]}")
            return
        try:
            bb = json.loads(stdout)
        except Exception as e:
            await self._goto_error(f"build_batch.py output not JSON: {e}")
            return
        await self._emit("tool_invocation", {
            "tool": "build_batch.py", "phase": "done", "rc": 0,
            "status": bb.get("status"),
            "articles_count": bb.get("articles_count"),
            "chapters_touched_count": len(bb.get("chapters_touched", []) or []),
        })

        status = bb.get("status")
        if status == "error":
            await self._goto_error(f"build_batch: {bb.get('reason')}")
            return
        if status in ("skip", "empty"):
            logger.info(
                "PREPARING — build_batch returned %s (%s) → IDLE",
                status, bb.get("reason"),
            )
            self.run.state = State.IDLE
            # 'empty' = глава кончилась, нет новых статей → можно сразу
            # идти на следующий цикл (next_cursor покажет другую главу
            # или null=DONE). 'skip' = бюджет исчерпан (usage>=target);
            # автоматический retry бесполезен, бюджет тот же. Останавливаемся
            # в IDLE-manual и ждём команды пользователя.
            if status == "empty":
                self.run.idle_reason = "fresh"
                self.run.next_wake_at = dt.datetime.now()
            else:  # status == "skip" — budget exhausted
                self.run.idle_reason = "manual"
                self.run.next_wake_at = None
                logger.warning(
                    "build_batch=skip (%s); usage-cap exhausted, "
                    "stopping in IDLE-manual to avoid infinite loop. "
                    "Re-run with bigger cap or unlimited mode.",
                    bb.get("reason"),
                )
            self._save(f"preparing_{status}_to_idle")
            await self._emit("state_transition", {
                "from": "PREPARING", "to": "IDLE",
                "reason": (
                    f"build_batch={status} ({bb.get('reason')}); "
                    f"idle_reason={self.run.idle_reason}"
                ),
            })
            return

        self.run.manifest_path = self.batch_dir / "manifest.json"
        self.run.state = State.RUNNING
        self._save("preparing_to_running")
        await self._emit("state_transition", {
            "from": "PREPARING", "to": "RUNNING",
            "reason": f"manifest ready, {bb.get('articles_count')} articles",
        })
        await self._emit("cycle_started", {
            "chapter_start": bb.get("chapter_start"),
            "articles_count": bb.get("articles_count"),
            "mode": self.run.mode,
            "usage_limit_pct": self.run.usage_limit_pct,
            "parallel_translators": self.cfg.parallel_translators,
            "parallel_cache_primer": self.cfg.parallel_cache_primer,
        })

    # ------------------------------------------------------------------
    # PARALLEL RUNNING
    # ------------------------------------------------------------------

    async def _handle_running(self) -> None:
        manifest = json.loads(self.run.manifest_path.read_text(encoding="utf-8"))
        articles: list[dict] = manifest["articles"]
        N = self.cfg.parallel_translators
        threshold = self.cfg.consecutive_failure_threshold
        primer = self.cfg.parallel_cache_primer
        logger.info(
            "RUNNING — %d articles in manifest, parallel=%d, primer=%s, threshold=%d",
            len(articles), N, primer, threshold,
        )

        queue: list[dict] = list(articles)
        in_flight: dict[str, asyncio.Task[ArticleResult]] = {}
        failures_without_success = 0
        hard_halt = False
        # Burst detector: timestamps of recent failures. If >=3 fail
        # within 30 seconds, halt early (faster than threshold=5).
        # Protects from systemic problem at first wave (mangled prompt,
        # broken claude CLI etc.) that would burn N parallel calls in
        # seconds before threshold catches.
        recent_fail_ts: list[dt.datetime] = []
        BURST_WINDOW_S = 30
        BURST_THRESHOLD = 3

        # ---- Optional primer pass: 1 article solo to warm cache. -----
        if primer and queue and not self._kill_flag and not self._stop_flag:
            primer_art = queue.pop(0)
            primer_name = primer_art["batch_name"]
            self.run.current_articles = [CurrentArticle(
                batch_name=primer_name,
                started_at=dt.datetime.now(),
                expected_chars=primer_art["source_chars"],
            )]
            self._save(f"primer_started_{primer_name}")
            await self._emit("article_started", {
                "batch_name": primer_name,
                "expected_chars": primer_art["source_chars"],
                "primer": True,
            })
            primer_result = await self._process_one_article(primer_art)
            self.run.current_articles = []
            failures_without_success, hard_halt = await self._apply_result(
                primer_result, failures_without_success, threshold,
            )
            if hard_halt:
                queue.clear()

        # ---- Parallel waves --------------------------------------------
        while not hard_halt and (queue or in_flight):
            # Refill in_flight up to N (unless stopping/killing).
            while (
                queue
                and len(in_flight) < N
                and not self._kill_flag
                and not self._stop_flag
            ):
                art = queue.pop(0)
                bn = art["batch_name"]
                self.run.current_articles.append(CurrentArticle(
                    batch_name=bn,
                    started_at=dt.datetime.now(),
                    expected_chars=art["source_chars"],
                ))
                task = asyncio.create_task(
                    self._process_one_article(art),
                    name=f"translate:{bn}",
                )
                in_flight[bn] = task
                await self._emit("article_started", {
                    "batch_name": bn,
                    "expected_chars": art["source_chars"],
                })
            if self.run.current_articles:
                self._save("wave_filled")

            if not in_flight:
                break  # nothing left to wait on

            # Hard /kill arrived while we were filling — cancel everything.
            if self._kill_flag:
                self.run.exit_reason = "kill"
                hard_halt = True
                break

            done, _pending = await asyncio.wait(
                in_flight.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in list(done):
                # Find batch_name for this task.
                bn = next((k for k, v in in_flight.items() if v is task), None)
                if bn is None:
                    continue
                in_flight.pop(bn, None)
                self.run.current_articles = [
                    c for c in self.run.current_articles if c.batch_name != bn
                ]
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    logger.info("article task %s was cancelled", bn)
                    continue
                except Exception as e:
                    logger.exception("article task %s crashed", bn)
                    self.run.failed_in_run += 1
                    failures_without_success += 1
                    await self._emit("article_failed", {
                        "batch_name": bn,
                        "marker_rc": -1,
                        "translator_rc": -1,
                        "consecutive_failures": failures_without_success,
                        "fatal_match": None,
                        "exception": f"{type(e).__name__}: {e}",
                    })
                    if failures_without_success >= threshold:
                        self.run.exit_reason = "consecutive_failures"
                        self.run.error_message = (
                            f"{failures_without_success} failures без success'а "
                            f"(threshold={threshold}). Last failed: {bn}."
                        )
                        hard_halt = True
                    continue
                # Detect a "real failure" (not success, not hit_limit,
                # not timeout, not partial-kept) for burst-counter
                # purposes. rc=4 = resumable partial preserved on disk,
                # which is progress, not a failure.
                is_real_failure = (
                    not result.timeout
                    and result.marker_rc != 0
                    and result.marker_rc != 2
                    and result.marker_rc != 4
                )
                if is_real_failure:
                    now = dt.datetime.now()
                    recent_fail_ts.append(now)
                    cutoff = now - dt.timedelta(seconds=BURST_WINDOW_S)
                    recent_fail_ts = [t for t in recent_fail_ts if t > cutoff]

                failures_without_success, was_hard_halt = await self._apply_result(
                    result, failures_without_success, threshold,
                )
                if was_hard_halt:
                    hard_halt = True

                # Burst guard: regardless of total threshold, if N
                # failures cluster in <BURST_WINDOW_S, halt early.
                # Protects from systemic burst (bad prompt / broken
                # CLI) that would otherwise burn ~threshold calls
                # in seconds.
                if (
                    not hard_halt
                    and is_real_failure
                    and len(recent_fail_ts) >= BURST_THRESHOLD
                ):
                    self.run.exit_reason = "consecutive_failures"
                    self.run.error_message = (
                        f"fast_fail_burst: {len(recent_fail_ts)} failures "
                        f"within {BURST_WINDOW_S}s — early halt to avoid "
                        f"hammering claude API. completed_in_run="
                        f"{self.run.completed_in_run}. "
                        f"Check .batch/stderr_*.log."
                    )
                    logger.error("EMERGENCY HALT: %s", self.run.error_message)
                    await self._emit("emergency_halt", {
                        "reason": "fast_fail_burst",
                        "window_seconds": BURST_WINDOW_S,
                        "failures_in_window": len(recent_fail_ts),
                    })
                    hard_halt = True

            if hard_halt:
                break

            # /stop graceful: stop refilling, but let in_flight finish.
            if self._stop_flag and self.run.exit_reason is None:
                # Don't set exit_reason yet — only after in_flight drains.
                queue.clear()

        # ---- Cleanup: cancel any remaining in_flight on hard halt ------
        if hard_halt and in_flight:
            logger.info(
                "hard halt — cancelling %d in-flight translators", len(in_flight),
            )
            await self._kill_all_procs()
            for t in in_flight.values():
                t.cancel()
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
            in_flight.clear()
            queue.clear()

        # ---- Determine exit_reason if not yet set ---------------------
        if self.run.exit_reason is None:
            if self._kill_flag:
                self.run.exit_reason = "kill"
            elif self._stop_flag:
                self.run.exit_reason = "stop"
            else:
                self.run.exit_reason = "manifest_exhausted"

        # post-hit-limit-loop guard. With resumable: partial progress
        # also counts — if at least one article advanced (completed OR
        # partial bytes on disk), the cycle wasn't a no-op.
        if (
            self.run.is_post_hit_limit_retry
            and self.run.exit_reason == "hit_limit"
            and self.run.completed_in_run == 0
            and self.run.partial_in_run == 0
        ):
            self.run.exit_reason = "post_hit_limit_loop"
            logger.error(
                "EMERGENCY HALT: post-hit-limit retry hit-limit'd again "
                "with 0 successes and 0 partials"
            )

        self.run.current_articles = []
        self.run.state = State.FINALIZING
        self._save(f"running_to_finalizing_{self.run.exit_reason}")
        await self._emit("state_transition", {
            "from": "RUNNING", "to": "FINALIZING",
            "reason": f"exit_reason={self.run.exit_reason}, "
                      f"completed={self.run.completed_in_run}, "
                      f"failed={self.run.failed_in_run}, "
                      f"partial={self.run.partial_in_run}",
        })

    async def _apply_result(
        self,
        result: ArticleResult,
        failures_without_success: int,
        threshold: int,
    ) -> tuple[int, bool]:
        """Process one ArticleResult; update state; return (new_failures_counter, hard_halt).

        hard_halt=True signals _handle_running to stop refilling and cancel
        all remaining in_flight (hit_limit / fatal / timeout / threshold).
        """
        bn = result.batch_name

        if result.timeout:
            self.run.exit_reason = "translator_timeout"
            self.run.error_message = (
                f"Агент-переводчик убит по таймауту "
                f"({self.cfg.translator_timeout_seconds}s) на статье "
                f"{self._timeout_batch_name or bn}, "
                f"translator_rc={self._timeout_translator_rc}. "
                f"См. .batch/stderr_{bn}.log."
            )
            logger.error("EMERGENCY HALT: %s", self.run.error_message)
            await self._emit("emergency_halt", {
                "reason": "translator_timeout",
                "batch_name": bn,
                "translator_rc": result.translator_rc,
                "timeout_seconds": self.cfg.translator_timeout_seconds,
            })
            return failures_without_success, True

        if result.marker_rc == 0:
            self.run.completed_in_run += 1
            self.run.completed_in_run_batch_names.append(bn)
            failures_without_success = 0
            if self.run.is_post_hit_limit_retry:
                self.run.is_post_hit_limit_retry = False
                logger.info("Cleared is_post_hit_limit_retry after first success")
            self._save(f"article_done_{bn}")
            await self._emit("article_done", {
                "batch_name": bn,
                "completed_in_run": self.run.completed_in_run,
            })
            return failures_without_success, False

        if result.marker_rc == 4:
            # Partial kept — neither success nor failure. Article stays in
            # the pending pool; next cycle will resume from next_start
            # (build_batch.py runs inspect_partial).
            self.run.partial_in_run += 1
            if bn not in self.run.partial_articles_in_run:
                self.run.partial_articles_in_run.append(bn)
            # Reset the post-hit-limit guard too — translated paragraphs
            # were added to disk, that's real progress even without a
            # closed article.
            if self.run.is_post_hit_limit_retry:
                self.run.is_post_hit_limit_retry = False
                logger.info(
                    "Cleared is_post_hit_limit_retry after partial progress",
                )
            self._save(f"article_partial_{bn}")
            await self._emit("article_partial", {
                "batch_name": bn,
                "partial_in_run": self.run.partial_in_run,
            })
            # Do NOT increment failures_without_success — this isn't a failure.
            return failures_without_success, False

        if result.marker_rc == 2:
            self.run.exit_reason = "hit_limit"
            self._save(f"hit_limit_{bn}")
            await self._emit("hit_limit_detected", {"batch_name": bn})
            return failures_without_success, True

        # Anything else is a failure.
        self.run.failed_in_run += 1
        failures_without_success += 1
        self._save(f"article_failed_{bn}")
        await self._emit("article_failed", {
            "batch_name": bn,
            "marker_rc": result.marker_rc,
            "translator_rc": result.translator_rc,
            "consecutive_failures": failures_without_success,
            "fatal_match": result.fatal_match,
        })

        if result.fatal_match:
            self.run.exit_reason = "fatal_translator_error"
            self.run.error_message = (
                f"Fatal translator error on {bn}: {result.fatal_match!r} "
                f"(translator_rc={result.translator_rc}, marker_rc={result.marker_rc}). "
                f"Check .batch/stderr_{bn}.log."
            )
            logger.error("EMERGENCY HALT: %s", self.run.error_message)
            await self._emit("emergency_halt", {
                "reason": "fatal_translator_error",
                "batch_name": bn,
                "match": result.fatal_match,
            })
            return failures_without_success, True

        if failures_without_success >= threshold:
            self.run.exit_reason = "consecutive_failures"
            self.run.error_message = (
                f"{failures_without_success} failures без success'а "
                f"(threshold={threshold}). Last failed: {bn}. "
                f"Check .batch/stderr_*.log и .batch/result_*.json."
            )
            logger.error("EMERGENCY HALT: %s", self.run.error_message)
            await self._emit("emergency_halt", {
                "reason": "consecutive_failures",
                "consecutive": failures_without_success,
                "last_batch_name": bn,
            })
            return failures_without_success, True

        return failures_without_success, False

    async def _process_one_article(self, art: dict) -> ArticleResult:
        """Run translator + mark_article_done for one article. Concurrent-safe."""
        bn = art["batch_name"]
        translator_rc, marker_rc, timeout = await self._translate_one(bn)
        fatal_match = self._scan_for_fatal_error(bn)
        return ArticleResult(
            batch_name=bn,
            translator_rc=translator_rc,
            marker_rc=marker_rc,
            fatal_match=fatal_match,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # FINALIZING — mostly identical to ZoharTG (one section adapted)
    # ------------------------------------------------------------------

    async def _handle_finalizing(self) -> None:
        logger.info("FINALIZING — process_results.py")
        await self._emit("tool_invocation", {
            "tool": "process_results.py", "phase": "started",
        })
        rc, stdout, stderr = await self._run_subprocess(
            [sys.executable, str(CORPUS_TOOLS / "process_results.py")], cwd=self.heb_root
        )
        report: dict = {}
        process_results_failed = False
        if rc == 0 and stdout:
            try:
                report = json.loads(stdout)
            except Exception as e:
                logger.error(
                    "process_results.py stdout not JSON: %s; treating as fatal",
                    e,
                )
                process_results_failed = True
        else:
            logger.error(
                "process_results.py rc=%d (expected 0); stderr=%s",
                rc, stderr[:500],
            )
            process_results_failed = True

        await self._emit("tool_invocation", {
            "tool": "process_results.py", "phase": "done", "rc": rc,
            "status": report.get("status"),
            "articles_translated": report.get("articles_translated"),
            "articles_failed_count": report.get("articles_failed_count"),
            "site_rebuilt": report.get("site_rebuilt"),
            "stderr_tail": stderr[-300:] if stderr else None,
        })

        if process_results_failed:
            await self._goto_error(
                f"process_results.py failed (rc={rc}). "
                f"stderr-tail: {stderr[-300:] if stderr else '(empty)'}"
            )
            return

        nc_rc, nc_out, _ = await self._run_subprocess(
            [sys.executable, str(CORPUS_TOOLS / "next_cursor.py")], cwd=self.heb_root
        )
        nc = json.loads(nc_out) if nc_rc == 0 and nc_out else {}

        site_index = self.heb_root / "Translated" / "Site" / "index.html"
        site_ts = (
            dt.datetime.fromtimestamp(site_index.stat().st_mtime)
            .replace(microsecond=0)
            .isoformat()
            if site_index.exists()
            else None
        )

        snap = LastSession(
            ts=dt.datetime.now().replace(microsecond=0),
            mode=self.run.mode,
            started_at=self.run.started_at,
            finished_at=dt.datetime.now().replace(microsecond=0),
            completed_in_run=self.run.completed_in_run,
            failed_in_run=self.run.failed_in_run,
            completed_in_run_batch_names=list(self.run.completed_in_run_batch_names),
            partial_in_run=self.run.partial_in_run,
            partial_articles_in_run=list(self.run.partial_articles_in_run),
            chapters_touched=report.get("chapters_touched", []) or [],
            chapters_done_this_cycle=report.get("chapters_done_this_cycle", []) or [],
            site_rebuilt=bool(report.get("site_rebuilt")),
            site_rebuilt_ts=site_ts,
            report_status=report.get("status"),
            hit_limit_reset=report.get("hit_limit_reset"),
            next_cursor_after=nc.get("next_cursor"),
            done_articles_after=nc.get("done_articles"),
            done_chapters_after=nc.get("done_chapters"),
            parallel_translators=self.cfg.parallel_translators,
            parallel_cache_primer=self.cfg.parallel_cache_primer,
        )
        snap.write(self.last_session_file)
        self.run.last_report_status = report.get("status")

        # Emit site_rebuilt and chapter_closed FIRST, before any
        # error-routing return. Реально закрытые главы и пересобранный
        # сайт — выполненная работа на disk'е, она должна быть опубликована
        # независимо от exit_reason'а цикла.
        # Дедуп: главы, для которых уже сработал mid-cycle-emit
        # (см. _emit_mid_cycle_chapter_closed в _translate_one), пропускаем,
        # иначе пост в канал и chapter_closed-уведомление будут двойными.
        all_done = list(report.get("chapters_done_this_cycle") or [])
        chapters_to_emit = [
            c for c in all_done if c not in self._published_chapters_in_run
        ]
        already_published = [
            c for c in all_done if c in self._published_chapters_in_run
        ]
        if already_published:
            logger.info(
                "FINALIZING: skipping site_rebuilt+chapter_closed for "
                "already-published mid-cycle chapters: %s",
                already_published,
            )
        if chapters_to_emit and report.get("site_rebuilt"):
            await self._emit("site_rebuilt", {
                "chapters_done_this_cycle": chapters_to_emit,
                "site_ts": site_ts,
            })
        for chapter in chapters_to_emit:
            await self._emit("chapter_closed", {"chapter": chapter})

        if self.run.exit_reason == "post_hit_limit_loop":
            await self._goto_error(
                "post_hit_limit_loop: после ожидания reset'а первый же translator снова хитнулся"
            )
            await self._emit("emergency_halt", {
                "reason": "post_hit_limit_loop",
                "completed_in_run": self.run.completed_in_run,
            })
            return

        if self.run.exit_reason == "fatal_translator_error":
            await self._goto_error(
                self.run.error_message or "fatal_translator_error"
            )
            return

        if self.run.exit_reason == "consecutive_failures":
            await self._goto_error(
                self.run.error_message or "consecutive_failures threshold reached"
            )
            return

        if self.run.exit_reason == "translator_timeout":
            await self._goto_error(
                self.run.error_message or "translator killed by timeout"
            )
            return

        if report.get("status") == "error":
            await self._goto_error(f"process_results error: {report.get('reason')}")
            return

        if report.get("status") == "hit_limit":
            await self._emit("hit_limit", {
                "reset": report.get("hit_limit_reset"),
                "completed_in_run": self.run.completed_in_run,
                "completed_in_run_batch_names": list(self.run.completed_in_run_batch_names),
                "next_cursor": nc.get("next_cursor"),
                "completed_count": nc.get("completed_count"),
                "total_articles": nc.get("total_articles"),
            })
            self.run.next_wake_at = self._compute_next_wake_after_hit_limit(
                report.get("hit_limit_reset")
            )
            self.run.idle_reason = "hit_limit_wait"
            self.run.is_post_hit_limit_retry = True
        elif self.run.exit_reason in ("stop", "kill"):
            self.run.next_wake_at = None
            self.run.idle_reason = "manual"
            self.run.is_post_hit_limit_retry = False
        else:
            # === TEMPORARY ONE-SHOT TEST MODE (2026-04-26 18:04) ===
            # User asked: после первого успешного батча НЕ авто-стартовать
            # следующий цикл, а уйти в IDLE-manual и ждать команды /run.
            # Цель: проверить полную цепочку (PREPARING → 10 параллельных
            # translator'ов → FINALIZING → process_results → site_rebuilt
            # → zip → канал) на одном батче, без auto-loop'а.
            # Revert: заменить блок ниже на:
            #   self.run.next_wake_at = dt.datetime.now() + dt.timedelta(seconds=2)
            #   self.run.idle_reason = "fresh"
            #   self.run.is_post_hit_limit_retry = False
            self.run.next_wake_at = None
            self.run.idle_reason = "manual"
            self.run.is_post_hit_limit_retry = False
            logger.info(
                "ONE-SHOT TEST MODE: остановка после FINALIZING вместо "
                "auto-continue. См. orchestrator.py:_handle_finalizing."
            )
            # === END TEMPORARY ===

        # Pending-run override: если пока шёл цикл пользователь нажал
        # ⚡ Run now / ▶️ Run..., запрос был поставлен в очередь
        # (см. request_run). Применяем его сейчас, поверх default
        # IDLE-manual / IDLE-fresh / IDLE-hit_limit_wait.
        # Hit_limit-сценарий тоже override'им — пользователь явно сказал
        # «начни новый цикл», даже если квота не reset'нулась (тогда
        # цикл схлопнется на первом translator'е, что нормально).
        if self._pending_run is not None:
            pending = self._pending_run
            self._pending_run = None
            self.run.next_wake_at = pending["when"] or dt.datetime.now()
            self.run.idle_reason = "manual"
            self.run.usage_limit_pct = pending["usage_limit_pct"]
            self.run.mode = (
                "unlimited" if pending["usage_limit_pct"] is None else "usage"
            )
            self.run.is_post_hit_limit_retry = False
            logger.info(
                "Pending run applied at FINALIZING tail: usage=%s, when=%s",
                pending["usage_limit_pct"],
                pending["when"].isoformat() if pending["when"] else "now",
            )
            await self._emit("pending_run_applied", {
                "usage_limit_pct": pending["usage_limit_pct"],
                "when": (pending["when"].isoformat()
                         if pending["when"] else None),
            })

        self.run.state = State.IDLE
        self._save(f"finalizing_to_idle_{self.run.idle_reason}")
        await self._emit("state_transition", {
            "from": "FINALIZING", "to": "IDLE",
            "reason": f"idle_reason={self.run.idle_reason}, "
                      f"next_wake_at={self.run.next_wake_at.strftime('%H:%M') if self.run.next_wake_at else 'manual'}",
        })
        await self._emit("cycle_finished", {
            "exit_reason": self.run.exit_reason,
            "report_status": report.get("status"),
            "completed_in_run": self.run.completed_in_run,
            "failed_in_run": self.run.failed_in_run,
            "partial_in_run": self.run.partial_in_run,
            "partial_articles_in_run": list(self.run.partial_articles_in_run),
            "next_wake_at": self.run.next_wake_at.isoformat() if self.run.next_wake_at else None,
        })

    async def _handle_done(self) -> None:
        logger.info("DONE — corpus translated, blocking until external /run")
        await self._wake_event.wait()
        self._wake_event.clear()
        if self.run.state == State.DONE:
            self.run.state = State.IDLE

    async def _handle_error(self) -> None:
        logger.error("ERROR — %s; blocking until /resume", self.run.error_message)
        await self._wake_event.wait()
        self._wake_event.clear()
        if self.run.state == State.ERROR:
            pass

    # ------------------------------------------------------------------
    # Translation primitive (cancellation-safe)
    # ------------------------------------------------------------------

    async def _translate_one(self, batch_name: str) -> tuple[int, int, bool]:
        """Run claude -p for one article; then mark_article_done.py.

        Returns (translator_rc, marker_rc, timed_out).
          marker_rc semantics: 0=ok, 1=no_flag, 2=hit_limit, 3=missing_files,
          -1=skipped (kill_flag), other=unusual.
        Cancellation: if the surrounding task is cancelled (hard halt),
        we kill the proc and re-raise CancelledError.
        """
        prompt_file = self.batch_dir / f"prompt_{batch_name}.txt"
        result_file = self.batch_dir / f"result_{batch_name}.json"
        stderr_file = self.batch_dir / f"stderr_{batch_name}.log"

        if not prompt_file.exists():
            logger.error("prompt file missing: %s", prompt_file)
            return -1, 3, False

        prompt_text = prompt_file.read_text(encoding="utf-8")

        cmd = [
            self.cfg.claude_bin,
            "-p", prompt_text,
            "--model", "claude-opus-4-7",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(self.heb_root),
            "--output-format", "json",
        ]
        if self.cfg.translator_effort:
            cmd.extend(["--effort", self.cfg.translator_effort])
        logger.info("translator → %s", batch_name)
        t0 = dt.datetime.now()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.heb_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._current_procs[batch_name] = proc
        timed_out = False
        timeout_s = self.cfg.translator_timeout_seconds
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "translator TIMEOUT after %ds on %s — killing",
                    timeout_s, batch_name,
                )
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stdout, stderr = await proc.communicate()
            except asyncio.CancelledError:
                logger.info("translator task cancelled on %s — killing proc", batch_name)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                try:
                    stderr_file.write_bytes(
                        b"[orchestrator] translator task cancelled (hard halt)\n"
                    )
                except Exception:
                    pass
                # Атомарно подчищаем все артефакты: cancel'нутый translator
                # мог успеть записать частичный/полный .md и/или флаг до того
                # как мы его прибили. Чтобы статья гарантированно
                # пере-перевелась в следующем цикле, удаляем .md, log_*.txt,
                # done-flag и убираем номер из progress.json/completed.
                async with self._mark_done_lock:
                    self._cleanup_partial_artifacts(batch_name)
                raise
        finally:
            self._current_procs.pop(batch_name, None)

        translator_rc = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            translator_rc = -1
            stderr = (stderr or b"") + (
                f"\n[orchestrator] killed after {timeout_s}s timeout\n"
            ).encode("utf-8")
            self._timeout_translator_rc = translator_rc
            self._timeout_batch_name = batch_name
        result_file.write_bytes(stdout or b"")
        stderr_file.write_bytes(stderr or b"")
        dur = (dt.datetime.now() - t0).total_seconds()
        logger.info(
            "translator done %s rc=%d duration=%.1fs out=%dB%s",
            batch_name, translator_rc, dur, len(stdout or b""),
            " TIMEOUT" if timed_out else "",
        )

        if timed_out:
            async with self._mark_done_lock:
                self._cleanup_partial_artifacts(batch_name)
            return translator_rc, -100, True

        if self._kill_flag:
            async with self._mark_done_lock:
                self._cleanup_partial_artifacts(batch_name)
            return translator_rc, 1, False

        # External kill detection: hard-halt path (_kill_all_procs)
        # invokes proc.kill() directly без cancel'a задачи, поэтому
        # proc.communicate() возвращает пустой stdout и выполнение идёт
        # сюда «обычной веткой» — но envelope парсить нечего, статья
        # реально оборвана. Без явной обработки preserve-лог не пишется,
        # хотя partial-файл на диске остался (как у 030_Bereshit в hit-
        # limit'е 2026-05-22 12:12). Чиним: при rc!=0 и пустом stdout
        # дёргаем preserve, чтобы был лог `kept_partial/complete/wiped_*`.
        if translator_rc != 0 and not (stdout or b""):
            logger.info(
                "translator %s exited rc=%d with empty stdout — "
                "treating as external kill, running preserve",
                batch_name, translator_rc,
            )
            async with self._mark_done_lock:
                self._cleanup_partial_artifacts(batch_name)
            return translator_rc, 1, False

        # Inspect envelope BEFORE delegating to mark_article_done.
        # Translator может успеть атомарно записать .md + done.flag
        # на ранней успешной итерации, а на финальном turn'е поймать
        # api_error_status=429 / is_error=true (rate-limit на закрывающем
        # сообщении). Тогда mark_article_done увидит флаг + валидный .md
        # и зачтёт статью как ok — а реально она может быть неполной.
        # Защита: парсим envelope. Если is_error=true — подчищаем
        # все артефакты и возвращаем fail (или hit_limit, если в тексте
        # есть «hit your limit»).
        envelope_issue = self._envelope_indicates_error(batch_name)
        if envelope_issue is not None:
            logger.warning(
                "translator %s envelope error: %s — wiping partial artefacts",
                batch_name, envelope_issue,
            )
            async with self._mark_done_lock:
                self._cleanup_partial_artifacts(batch_name)
            # Если envelope содержит hit-limit-маркер, эскалируем как
            # marker_rc=2 (так _apply_result поднимет hit_limit halt).
            if HIT_LIMIT_RX.search(envelope_issue):
                return translator_rc, 2, False
            return translator_rc, 1, False

        # Serialize mark_article_done across parallel translators —
        # the script does read-modify-write on per-chapter progress.json.
        async with self._mark_done_lock:
            rc, _so, se = await self._run_subprocess(
                [sys.executable, str(CORPUS_TOOLS / "mark_article_done.py"), batch_name],
                cwd=self.heb_root,
            )
        if rc not in (0, 1, 2, 3, 4):
            logger.warning("mark_article_done unusual rc=%d stderr=%s", rc, se[:300])

        # Mid-cycle chapter closure detection. mark_article_done.py prints
        # "{batch}: chapter {chapter_en} closed → rebuilding site" to stderr
        # right before re-running build_site.py. Catch it and emit
        # chapter_closed + site_rebuilt events НЕ дожидаясь FINALIZING,
        # чтобы бот опубликовал zip в канал сразу. Дедуп через
        # self._published_chapters_in_run; FINALIZING пропустит уже
        # опубликованные главы.
        if rc == 0 and se:
            m = CHAPTER_CLOSED_RX.search(se)
            if m:
                chapter_en = m.group(1)
                if chapter_en not in self._published_chapters_in_run:
                    self._published_chapters_in_run.add(chapter_en)
                    await self._emit_mid_cycle_chapter_closed(chapter_en)
        return translator_rc, rc, False

    async def _emit_mid_cycle_chapter_closed(self, chapter_en: str) -> None:
        """Mid-cycle emit pair (chapter_closed + site_rebuilt) for a chapter
        that just closed inside _translate_one (build_site.py was triggered
        by mark_article_done's own hook). Bot will publish the zip in
        response to site_rebuilt event."""
        site_index = self.heb_root / "Translated" / "Site" / "index.html"
        site_ts = (
            dt.datetime.fromtimestamp(site_index.stat().st_mtime)
            .replace(microsecond=0).isoformat()
            if site_index.exists() else None
        )
        logger.info(
            "Mid-cycle chapter closed: %s — emitting site_rebuilt + chapter_closed",
            chapter_en,
        )
        await self._emit("chapter_closed", {
            "chapter": chapter_en,
            "mid_cycle": True,
        })
        await self._emit("site_rebuilt", {
            "chapters_done_this_cycle": [chapter_en],
            "site_ts": site_ts,
            "mid_cycle": True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run_subprocess(
        self, cmd: list[str], cwd: Path
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            (stdout or b"").decode("utf-8", errors="replace"),
            (stderr or b"").decode("utf-8", errors="replace"),
        )

    def _envelope_indicates_error(self, batch_name: str) -> Optional[str]:
        """Inspect result_<batch>.json envelope. If translator ended in an
        API error (is_error=true / api_error_status set) — return a short
        diagnostic string (for log + hit_limit detection). Else None.

        Resumable refinement: if the envelope reports an error BUT the
        on-disk .md is a complete file and the done.flag exists, trust
        the disk and return None — the error likely arrived on the
        closing turn after a successful write.
        """
        result_file = self.batch_dir / f"result_{batch_name}.json"
        if not result_file.exists():
            return None
        try:
            d = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not (d.get("is_error") is True or d.get("api_error_status")):
            return None

        flag_path = self.batch_dir / f"done_{batch_name}.flag"
        art = self._resolve_article(batch_name)
        if art is not None and flag_path.exists():
            info = self._inspect_md(art, run_validator=True)
            if info is not None and info["state"] == "complete":
                logger.info(
                    "envelope error on %s but .md complete + flag set — "
                    "trusting disk, ignoring envelope",
                    batch_name,
                )
                return None

        api_status = d.get("api_error_status")
        result_msg = str(d.get("result", ""))[:300]
        return (
            f"is_error={d.get('is_error')} api_status={api_status} "
            f"result={result_msg!r}"
        )

    def _resolve_article(self, batch_name: str) -> Optional[dict]:
        """Look up article meta in the current batch manifest."""
        if self.run.manifest_path is None or not self.run.manifest_path.exists():
            return None
        try:
            manifest = json.loads(
                self.run.manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            logger.exception("manifest unreadable for %s", batch_name)
            return None
        return next(
            (a for a in manifest.get("articles", [])
             if a.get("batch_name") == batch_name),
            None,
        )

    def _inspect_md(
        self, art: dict, run_validator: bool = False,
    ) -> Optional[dict]:
        """Run corpus_tools/partial_state.inspect_partial for the article's
        on-disk .md file. Returns None on infrastructure error."""
        try:
            sys.path.insert(0, str(CORPUS_TOOLS))
            from partial_state import inspect_partial  # type: ignore
        except Exception:
            logger.exception("could not import partial_state for inspection")
            return None
        nn = art["article_index"]
        cdir = self.heb_root / "Translated" / art["book"] / art["chapter_ru"]
        md_path = cdir / f"{nn:03d}.md"
        start_p, end_p = art["sulam_paragraphs"]
        try:
            return inspect_partial(
                md_path, start_p, end_p,
                expected_article_index=nn,
                run_validator=run_validator,
            )
        except Exception:
            logger.exception("inspect_partial crashed for %s", art.get("batch_name"))
            return None

    def _preserve_partial_artifacts(self, batch_name: str) -> str:
        """Keep on-disk partial .md if it looks resumable; otherwise wipe.

        Called from cancel / timeout / envelope-error / hit-limit-kill paths
        in _translate_one (replacement for the old _cleanup_partial_artifacts).
        Caller MUST hold self._mark_done_lock.

        Always removes the .batch/done_<batch>.flag (translator never gets
        to confirm success on a half-write). Leaves the .md alone when
        partial_state says 'partial' or 'complete'. Wipes .md (and log)
        when 'corrupted' or 'absent'.

        Always strips `nn` from progress.json completed/failed — slot must
        be free for the next cycle's build_batch to re-pick the article.

        Returns one of: 'kept_partial', 'kept_complete', 'wiped_corrupted',
        'wiped_absent', 'unknown'.
        """
        art = self._resolve_article(batch_name)
        if art is None:
            return "unknown"

        nn = art["article_index"]
        cdir = self.heb_root / "Translated" / art["book"] / art["chapter_ru"]
        md_path = cdir / f"{nn:03d}.md"
        log_path = cdir / f"log_{nn:03d}.txt"
        prog_path = cdir / "progress.json"
        flag_path = self.batch_dir / f"done_{batch_name}.flag"

        try:
            if flag_path.exists():
                flag_path.unlink()
                logger.info("preserve: removed stale flag %s", flag_path.name)
        except Exception:
            logger.exception("preserve: failed to unlink flag %s", flag_path)

        info = self._inspect_md(art, run_validator=False)
        outcome = "unknown"
        if info is None:
            outcome = "unknown"
        elif info["state"] in ("partial", "complete"):
            outcome = (
                "kept_partial" if info["state"] == "partial" else "kept_complete"
            )
            logger.info(
                "preserve: %s for %s (translated=%d, next_start=%d)",
                outcome, batch_name, info["translated_count"], info["next_start"],
            )
        else:
            try:
                if md_path.exists():
                    md_path.unlink()
                    logger.info("preserve: wiped %s (%s: %s)",
                                md_path.name, info["state"], info.get("reason"))
            except Exception:
                logger.exception("preserve: failed to unlink %s", md_path)
            try:
                if log_path.exists() and log_path.stat().st_size < 200:
                    log_path.unlink()
            except Exception:
                pass
            outcome = (
                "wiped_corrupted" if info["state"] == "corrupted"
                else "wiped_absent"
            )

        if prog_path.exists():
            try:
                prog = json.loads(prog_path.read_text(encoding="utf-8"))
                completed = list(prog.get("completed", []))
                failed = list(prog.get("failed", []))
                changed = False
                if nn in completed:
                    completed.remove(nn)
                    prog["completed"] = sorted(completed)
                    changed = True
                if nn in failed:
                    failed.remove(nn)
                    prog["failed"] = sorted(failed)
                    changed = True
                if changed:
                    prog_path.write_text(
                        json.dumps(prog, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    logger.info(
                        "preserve: removed %d from %s completed/failed",
                        nn, prog_path.name,
                    )
            except Exception:
                logger.exception("preserve: progress.json edit failed for %s",
                                 batch_name)

        return outcome

    # Backwards-compat alias: old name kept until all call sites move over.
    def _cleanup_partial_artifacts(self, batch_name: str) -> None:
        self._preserve_partial_artifacts(batch_name)

    def _scan_for_fatal_error(self, batch_name: str) -> Optional[str]:
        result_file = self.batch_dir / f"result_{batch_name}.json"
        stderr_file = self.batch_dir / f"stderr_{batch_name}.log"
        for f in (stderr_file, result_file):
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")[:32_000]
            except Exception:
                continue
            m = FATAL_TRANSLATOR_RX.search(txt)
            if m:
                return m.group(0)
        return None

    async def _kill_all_procs(self) -> None:
        """Kill every active translator subprocess (used by /kill and hard halt)."""
        if not self._current_procs:
            return
        names = list(self._current_procs.keys())
        for bn, proc in list(self._current_procs.items()):
            if proc.returncode is None:
                try:
                    proc.kill()
                    logger.info("Killed translator subprocess for %s", bn)
                except ProcessLookupError:
                    pass
        # Drain so they reap; don't wait forever.
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(p.wait() for p in self._current_procs.values()),
                    return_exceptions=True,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Some translators didn't exit within 10s after kill: %s", names,
            )

    async def _goto_error(self, msg: str) -> None:
        old = self.run.state.value
        logger.error("→ ERROR: %s", msg)
        self.run.state = State.ERROR
        self.run.error_message = msg
        self.run.current_articles = []
        self._save("error_state")
        await self._emit("state_transition", {
            "from": old, "to": "ERROR", "reason": msg[:200],
        })
        await self._emit("error", {"reason": msg})

    def _compute_next_wake_after_hit_limit(
        self, reset: Optional[str]
    ) -> dt.datetime:
        """Parse reset-string from process_results.py:
           - ISO datetime "YYYY-MM-DDTHH:MM:SS" → weekly-limit format
             (далеко в будущем: дни/недели). Используем как есть + 5min.
           - "HH:MM" → 5-часовой session-limit. Если время уже прошло
             сегодня — переносим на завтра. + 5min.
           - None / unparseable → fallback now+1h5min.
        """
        now = dt.datetime.now()
        if not reset or reset == "??:??":
            return now + dt.timedelta(hours=1, minutes=5)
        # ISO datetime form has 'T' separator (weekly).
        if "T" in reset:
            try:
                cand = dt.datetime.fromisoformat(reset)
                # Если по какой-то причине ISO в прошлом — fallback.
                if cand <= now:
                    return now + dt.timedelta(hours=1, minutes=5)
                return cand + dt.timedelta(minutes=5)
            except Exception:
                return now + dt.timedelta(hours=1, minutes=5)
        # HH:MM (session) form.
        try:
            h, m = reset.split(":")
            cand = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if cand <= now:
                cand += dt.timedelta(days=1)
            return cand + dt.timedelta(minutes=5)
        except Exception:
            return now + dt.timedelta(hours=1, minutes=5)

    def _save(self, reason: str) -> None:
        self.run.write(self.state_file)
        logger.debug("state saved (%s): %s", reason, self.run.state.value)

    async def _emit(self, event_type: str, data: Optional[dict] = None) -> None:
        ev = Event(type=event_type, data=data or {})
        append_event(self.events_file, ev)
        for cb in list(self._subscribers):
            try:
                await cb(ev)
            except Exception:
                logger.exception("subscriber callback failed for %s", event_type)
