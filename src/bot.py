"""
Telegram bot frontend. Phase 2 MVP.

Lives in the same asyncio loop as the Orchestrator (see main.py).

Responsibilities:
  1. Single-user command interface in the private chat.
  2. Forward trace-level events from orchestrator to private chat.
  3. Auto-deploy site to GitHub Pages on article_done (debounced).

What's deliberately out of scope for Phase 2 MVP (Phase 2.1):
  - ConversationHandler for /run with date/limit pickers.
  - Live dashboard pinned message with rate-limited edits.
  - InlineKeyboard for /kill confirmation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

CORPUS_TOOLS = Path(__file__).resolve().parents[1] / "corpus_tools"

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    constants,
)
from telegram.error import NetworkError, TelegramError, BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config
from gh_deploy import DeployTarget, deploy_site_to_pages
from orchestrator import Orchestrator
from state import Event, State, LastSession, read_events_tail


logger = logging.getLogger("zohartgbatch.bot")


# Events that should be **forwarded as Telegram messages** to the
# owner's private chat. Everything else still lands in events.jsonl.
TRACE_EVENT_TYPES = {
    "state_transition",
    "tool_invocation",
    "cycle_started",
    "cycle_finished",
    "hit_limit",
    "chapter_closed",
    "site_rebuilt",
    "error",
    "emergency_halt",
    "done",
    "orchestrator_boot_recovery",
    "run_queued",
    "pending_run_applied",
}

REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("⚡ Run now"), KeyboardButton("▶️ Run..."),
         KeyboardButton("📊 Статус")],
        [KeyboardButton("🛠 Пересобрать"), KeyboardButton("🌐 Push"),
         KeyboardButton("📋 Лог")],
        [KeyboardButton("⏹ Stop"), KeyboardButton("💀 Force stop"),
         KeyboardButton("🔄 Resume")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# ConversationHandler states for "▶️ Run..." multi-step dialog.
ASK_WHEN = 1
ASK_LIMIT = 2
ASK_CONFIRM = 3
AWAIT_CUSTOM_TIME = 4
AWAIT_CUSTOM_LIMIT = 5


class ZoharBot:
    def __init__(self, cfg: Config, orchestrator: Orchestrator) -> None:
        self.cfg = cfg
        self.orch = orchestrator
        self.app: Optional[Application] = None
        self._owner_id = cfg.tg_private_chat_id
        if self._owner_id is None:
            raise RuntimeError(
                "TG_PRIVATE_CHAT_ID is empty in .env — "
                "send /start to the bot once and resolve via getUpdates"
            )
        # Live dashboard — single pinned message, edited in place on events.
        self._dashboard_msg_id: Optional[int] = None
        self._dashboard_path = cfg.state_dir / "dashboard_msg.json"
        self._dashboard_lock = asyncio.Lock()
        self._last_dashboard_text: Optional[str] = None
        # Cached global progress from next_cursor.py — refreshed per-event.
        self._progress: dict = {}
        # Dashboard debounce: collapse bursts of article_started/done events
        # into a single edit. State transitions / errors / hit_limit bypass.
        self._dashboard_debounce_task: Optional[asyncio.Task] = None
        self._dashboard_dirty = False
        # GitHub Pages auto-deploy: serialize concurrent pushes (one at a
        # time), debounce article_done bursts (one push per wave). Pending
        # batches accumulate between debounce ticks; the post-deploy
        # channel announcement uses the last one (most recently closed).
        #
        # Two-phase scheduler (fixed 2026-05-20):
        #   * `_gh_deploy_sleep_task` — the debounce timer. Cancellable freely
        #     when a new article_done arrives during the wait window.
        #   * `_gh_deploy_running` — True while `_run_gh_deploy` is actually
        #     pushing. CANNOT be cancelled (was the bug; cancelling left
        #     orphan build_site/pagefind subprocesses).
        #   * `_gh_deploy_dirty` — set if article_done arrives while the
        #     run is in flight. After the run finishes, scheduler starts a
        #     fresh debounce cycle so the queued batches still ship.
        self._gh_deploy_lock = asyncio.Lock()
        self._gh_deploy_sleep_task: Optional[asyncio.Task] = None
        self._gh_deploy_running: bool = False
        self._gh_deploy_dirty: bool = False
        self._gh_pending_batches: list[str] = []
        # Single-flight mutex for `build_site.py`. The same script also gets
        # invoked from mark_article_done.py (chapter close) and
        # process_results.py (FINALIZING). Those are separate Python
        # processes so this lock only protects bot-initiated builds.
        # Cross-process collisions are avoided by Step 2's contract:
        # gh_deploy no longer triggers build_site itself; only bot does
        # (with `--no-pagefind` for fast auto-deploys).
        self._site_build_lock = asyncio.Lock()
        # Timestamp of the last successful auto-deploy (manual /push and
        # /rebuild also update it). Used by _schedule_gh_deploy to enforce
        # cfg.gh_deploy_min_interval_seconds — at most one auto-push per
        # interval, however many article_done events arrive in between.
        # Pending debounce task survives cycle_finished and fires during
        # the next IDLE wake-wait so a queued push isn't lost.
        self._last_gh_deploy_at: Optional[dt.datetime] = None
        # Lazy-loaded articles catalog cache (<HEB_ROOT>/Source/articles_catalog.json),
        # used to resolve chapter_ru and articles-per-chapter for the
        # auto-deploy announcement.
        self._catalog_cache: Optional[list[dict]] = None
        if not (cfg.gh_token and cfg.gh_repo):
            logger.warning(
                "GH_TOKEN or GH_REPO not set — GitHub Pages auto-deploy is OFF",
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.app = (
            Application.builder()
            .token(self.cfg.tg_bot_token)
            .build()
        )
        self._register_handlers()
        await self._set_bot_commands()

        # Subscribe to orchestrator events.
        self.orch.subscribe(self._on_orch_event)

        await self.app.initialize()
        await self.app.start()
        if self.app.updater is None:
            raise RuntimeError("Telegram updater not initialized")
        await self.app.updater.start_polling(drop_pending_updates=False)
        await self._send_owner(
            f"✅ zohar-translator bot online (parallel={self.cfg.parallel_translators}"
            f"{', primer' if self.cfg.parallel_cache_primer else ''}). "
            "/status или кнопки внизу.",
            reply_markup=REPLY_KEYBOARD,
        )
        # Pull initial progress before rendering the pinned dashboard.
        await self._refresh_progress()
        await self._init_dashboard()
        logger.info("Telegram bot online; polling started")

    async def stop(self) -> None:
        if self.app is None:
            return
        try:
            if (self._dashboard_debounce_task is not None
                    and not self._dashboard_debounce_task.done()):
                self._dashboard_debounce_task.cancel()
                try:
                    await self._dashboard_debounce_task
                except (asyncio.CancelledError, Exception):
                    pass
            # Cancel sleep-phase auto-deploy if waiting. Running deploys
            # are NOT cancelled — we let them finish to avoid orphan
            # build_site/pagefind subprocesses.
            if (self._gh_deploy_sleep_task is not None
                    and not self._gh_deploy_sleep_task.done()):
                self._gh_deploy_sleep_task.cancel()
                try:
                    await self._gh_deploy_sleep_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            logger.exception("Error during bot shutdown")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        assert self.app is not None
        owner_filter = filters.User(user_id=self._owner_id)

        # Multi-step Run-with-options ConversationHandler. Registered FIRST
        # so it claims the "▶️ Run..." button before generic message handlers.
        run_conv = ConversationHandler(
            entry_points=[
                CommandHandler("run_advanced", self._run_conv_entry, owner_filter),
                MessageHandler(
                    owner_filter & filters.Regex(r"^▶️\s*Run\.\.\.$"),
                    self._run_conv_entry,
                ),
            ],
            states={
                ASK_WHEN: [CallbackQueryHandler(self._run_conv_when, pattern=r"^when:")],
                AWAIT_CUSTOM_TIME: [
                    MessageHandler(
                        owner_filter & filters.TEXT & ~filters.COMMAND,
                        self._run_conv_custom_time,
                    ),
                ],
                ASK_LIMIT: [CallbackQueryHandler(self._run_conv_limit, pattern=r"^lim:")],
                AWAIT_CUSTOM_LIMIT: [
                    MessageHandler(
                        owner_filter & filters.TEXT & ~filters.COMMAND,
                        self._run_conv_custom_limit,
                    ),
                ],
                ASK_CONFIRM: [CallbackQueryHandler(self._run_conv_confirm, pattern=r"^conf:")],
            },
            fallbacks=[
                CommandHandler("cancel", self._run_conv_cancel, owner_filter),
                CallbackQueryHandler(self._run_conv_cancel, pattern=r"^cancel$"),
            ],
            allow_reentry=True,
        )
        self.app.add_handler(run_conv)

        self.app.add_handler(CommandHandler("start", self._cmd_start, owner_filter))
        self.app.add_handler(CommandHandler("help", self._cmd_help, owner_filter))
        self.app.add_handler(CommandHandler("status", self._cmd_status, owner_filter))
        self.app.add_handler(CommandHandler("run", self._cmd_run, owner_filter))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop, owner_filter))
        self.app.add_handler(CommandHandler("kill", self._cmd_kill, owner_filter))
        self.app.add_handler(CommandHandler("site", self._cmd_site, owner_filter))
        self.app.add_handler(CommandHandler("rebuild", self._cmd_rebuild, owner_filter))
        self.app.add_handler(CommandHandler("push", self._cmd_push, owner_filter))
        self.app.add_handler(CommandHandler("log", self._cmd_log, owner_filter))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume, owner_filter))

        # ReplyKeyboard buttons → plain text → map to handlers.
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^(📊\s+)?Статус$"), self._cmd_status,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^⚡\s*Run now$"), self._cmd_run,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^⏹\s*Stop$"), self._cmd_stop,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^💀\s*Force\s*stop$"), self._cmd_kill,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^📦\s*Сайт$"), self._cmd_site,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^🛠\s*Пересобрать(\s+сайт)?$"), self._cmd_rebuild,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^🌐\s*Push$"), self._cmd_push,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^📋\s*Лог$"), self._cmd_log,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^🔄\s*Resume$"), self._cmd_resume,
        ))
        self.app.add_handler(MessageHandler(
            owner_filter & filters.Regex(r"^❓\s*Help$"), self._cmd_help,
        ))

    async def _set_bot_commands(self) -> None:
        assert self.app is not None
        await self.app.bot.set_my_commands([
            BotCommand("status", "Текущее состояние"),
            BotCommand("run", "Запустить цикл сейчас (unlimited)"),
            BotCommand("run_advanced", "Запустить с выбором времени и лимита"),
            BotCommand("stop", "Graceful stop после текущей статьи"),
            BotCommand("kill", "Hard stop, убить translator в полёте"),
            BotCommand("site", "Прислать ссылку на сайт в GitHub Pages"),
            BotCommand("rebuild", "Пересобрать сайт и запушить в GitHub Pages"),
            BotCommand("push", "Пересобрать сайт и запушить в GitHub Pages"),
            BotCommand("log", "Последние события"),
            BotCommand("resume", "Возобновить из ERROR-state"),
            BotCommand("help", "Справка"),
        ])

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, _ctx) -> None:
        await update.effective_message.reply_text(
            "Привет. Я zohar-translator — параллельный батч-оркестратор.\n"
            f"Запускаю {self.cfg.parallel_translators} translator'ов одновременно "
            f"в одной волне. Жми кнопки внизу или /-команды.",
            reply_markup=REPLY_KEYBOARD,
        )

    async def _cmd_help(self, update: Update, _ctx) -> None:
        text = (
            "<b>Команды</b>\n"
            "/status — текущее состояние\n"
            "/run [N] — запуск цикла. <code>N</code> в процентах для usage-режима, иначе unlimited.\n"
            "/stop — graceful stop, доделает текущую статью.\n"
            "/kill — hard stop, убьёт текущий translator.\n"
            "/site — прислать ссылку на сайт в GitHub Pages.\n"
            "/rebuild — пересобрать сайт и запушить в GitHub Pages.\n"
            "/push — то же, что /rebuild "
            "(после каждой статьи делается автоматически).\n"
            "/log [N] — последние N (по умолчанию 20) событий.\n"
            "/resume — выйти из ERROR-state и попробовать снова.\n"
            "/help — это сообщение."
        )
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def _cmd_status(self, update: Update, _ctx) -> None:
        await self._refresh_progress()
        r = self.orch.run
        p = self._progress
        nw = r.next_wake_at.strftime("%Y-%m-%d %H:%M") if r.next_wake_at else "manual"

        cur = ""
        if r.current_articles:
            lines = []
            for ca in r.current_articles:
                started = ca.started_at.strftime("%H:%M:%S")
                lines.append(
                    f"  • <code>{ca.batch_name}</code> "
                    f"({ca.expected_chars} chars, started {started})"
                )
            cur = "\n⏳ В работе ({} шт):\n{}".format(
                len(r.current_articles), "\n".join(lines),
            )

        # Progress lines (chapter + corpus + current chapter).
        if p.get("next_cursor"):
            chap_line = (
                f"📚 Главы: <b>{p.get('done_chapters')}</b>/"
                f"{p.get('total_chapters')}\n"
                f"📜 Статьи: <b>{p.get('done_articles')}</b>/"
                f"{p.get('total_articles_corpus')}\n"
                f"📍 В <b>{p.get('next_cursor')}</b>: "
                f"{p.get('completed_count')}/{p.get('total_articles')}\n"
            )
        elif p:
            chap_line = "🎉 Все главы готовы.\n"
        else:
            chap_line = ""

        text = (
            f"<b>{r.state.value}</b>"
            f"{f' · {r.idle_reason}' if r.state == State.IDLE else ''}\n"
            f"Режим: {r.mode}{f' ({r.usage_limit_pct}%)' if r.usage_limit_pct else ''} "
            f"· parallel={self.cfg.parallel_translators}"
            f"{' · primer' if self.cfg.parallel_cache_primer else ''}\n\n"
            f"{chap_line}\n"
            f"За цикл: ✅ {r.completed_in_run} · ❌ {r.failed_in_run}{cur}\n"
            f"next_wake_at: {nw}\n"
            f"is_post_hit_limit_retry: {r.is_post_hit_limit_retry}\n"
            f"last_report_status: {r.last_report_status}\n"
        )
        if r.error_message:
            text += f"\n<b>error_message:</b>\n<pre>{_escape(r.error_message)}</pre>"
        await update.effective_message.reply_text(text, parse_mode=constants.ParseMode.HTML)

    async def _cmd_run(self, update: Update, ctx) -> None:
        usage: Optional[int] = None
        if ctx.args:
            arg = ctx.args[0].rstrip("%")
            try:
                n = int(arg)
                if 1 <= n <= 99:
                    usage = n
            except ValueError:
                pass
        scheduled_now = await self.orch.request_run(usage_limit_pct=usage)
        mode_label = "unlimited" if usage is None else f"usage {usage}%"
        if scheduled_now:
            await update.effective_message.reply_text(
                f"⚡ Run requested: {mode_label}.",
            )
        else:
            await update.effective_message.reply_text(
                f"⏳ Run queued: {mode_label}. Текущий цикл "
                f"({self.orch.run.state.value.lower()}) сначала завершится "
                f"— потом стартанёт новый. /kill чтобы отменить очередь.",
            )

    async def _cmd_stop(self, update: Update, _ctx) -> None:
        await self.orch.request_stop(graceful=True)
        await update.effective_message.reply_text(
            "⏹ Stop requested · graceful (доделает текущую статью).",
        )

    async def _cmd_kill(self, update: Update, _ctx) -> None:
        await self.orch.request_stop(graceful=False)
        await update.effective_message.reply_text(
            "☠️ Kill requested · убил translator в полёте.",
        )

    async def _cmd_site(self, update: Update, _ctx) -> None:
        urls: list[str] = []
        if self.cfg.gh_repo:
            owner, name = self.cfg.gh_repo.split("/", 1)
            urls.append(f"https://{owner}.github.io/{name}/")
        if not urls:
            await update.effective_message.reply_text(
                "🌐 GitHub Pages не настроен (GH_REPO в .env пуст).",
            )
            return
        await update.effective_message.reply_text(
            "🌐 Сайт публикуется на GitHub Pages:\n" + "\n".join(urls),
            disable_web_page_preview=True,
        )

    async def _cmd_push(self, update: Update, _ctx) -> None:
        """Manual GitHub Pages push: build_site.py → copy → git push.

        Follows the same path as auto-deploy after every article_done event,
        but invoked synchronously by user request. Cancels any pending
        auto-deploy debounce timer (no point waiting if user wants it now).
        """
        if not (self.cfg.gh_token and self.cfg.gh_repo):
            await update.effective_message.reply_text(
                "⚠️ GitHub auto-deploy выключен (GH_TOKEN / GH_REPO в .env пусты).",
            )
            return

        msg = await update.effective_message.reply_text(
            "🌐 Собираю сайт и пушу в GitHub...",
        )
        # Cancel pending sleep-phase only; if a run is in flight, the
        # _run_gh_deploy lock below will serialize behind it.
        if self._gh_deploy_sleep_task and not self._gh_deploy_sleep_task.done():
            self._gh_deploy_sleep_task.cancel()
            self._gh_deploy_sleep_task = None

        try:
            await self._run_build_site(reason="push", with_pagefind=False)
            results = await self._run_gh_deploy(commit_msg_prefix="Manual push")
            self._last_gh_deploy_at = dt.datetime.now()
        except Exception as e:
            await msg.edit_text(
                f"❌ Push провалился: {type(e).__name__}: {e}\n"
                f"<code>{_escape(str(e))[:300]}</code>",
                parse_mode=constants.ParseMode.HTML,
            )
            return

        if not any(r.changed for r in results):
            await msg.edit_text(
                "🌐 Сайт уже актуален в GitHub — push не потребовался "
                "(нет изменений с последнего коммита).",
            )
            return

        lines = ["🌐 <b>Push ОК</b>"]
        for r in results:
            if r.changed:
                commit_url = f"https://github.com/{r.repo}/commit/{r.commit_sha}"
                lines.append(
                    f"  • <code>{_escape(r.repo)}</code>: "
                    f"<a href=\"{commit_url}\">{r.commit_sha[:7]}</a>, "
                    f"{r.files_changed} файлов"
                )
            else:
                lines.append(
                    f"  • <code>{_escape(r.repo)}</code>: без изменений"
                )
        # Public URL derived from config.
        owner, name = self.cfg.gh_repo.split("/", 1)
        primary_url = f"https://{owner}.github.io/{name}/"
        lines.append("Через 1–2 мин обновится: " + primary_url)
        await msg.edit_text(
            "\n".join(lines),
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    def _schedule_gh_deploy(self) -> None:
        """Debounced + rate-limited trigger for GitHub Pages auto-deploy.

        Called from _on_orch_event on `article_done`. Two-phase design
        (fixed 2026-05-20) — the sleep/debounce phase is cancellable, the
        run phase is NOT. Previously a single guard cancelled BOTH, which
        killed deploys mid-flight and left orphan build_site/pagefind
        subprocesses (→ PermissionError on next deploy).

        Behaviour:
          * If a run is currently in flight → set `_gh_deploy_dirty` so
            the post-run scheduler kicks off a fresh debounce when done.
          * If a sleep task is already waiting → cancel it and start a
            new one (normal debounce — collapses bursts to one push).
          * Otherwise → start a new sleep task.

        Rate-limit: at most one auto-push per `gh_deploy_min_interval_seconds`
        (default 600 s = 10 min). If the previous deploy was less than
        `min_interval` ago, the debounce extends to (last_deploy + interval).
        """
        if not (self.cfg.gh_token and self.cfg.gh_repo):
            return  # auto-deploy disabled

        # A run is in flight — don't disturb it. Just mark dirty; the
        # post-run scheduler in _wait_and_run will start a fresh debounce.
        if self._gh_deploy_running:
            self._gh_deploy_dirty = True
            logger.info(
                "gh_deploy: run in flight, marking dirty for follow-up debounce"
            )
            return

        # Cancel the in-progress sleep (debounce reset). Safe — sleep
        # has no side effects.
        if self._gh_deploy_sleep_task and not self._gh_deploy_sleep_task.done():
            self._gh_deploy_sleep_task.cancel()
            self._gh_deploy_sleep_task = None

        self._gh_deploy_sleep_task = asyncio.create_task(
            self._gh_deploy_sleep_and_run()
        )

    def _compute_gh_deploy_wait(self) -> float:
        """Return seconds to wait before next auto-deploy.

        max(debounce_seconds, rate_limit_remaining).
        """
        debounce_s = self.cfg.gh_deploy_debounce_seconds
        min_interval_s = self.cfg.gh_deploy_min_interval_seconds
        last = self._last_gh_deploy_at
        if last is not None:
            since_last = (dt.datetime.now() - last).total_seconds()
            wait_for_rate_limit = max(0.0, min_interval_s - since_last)
        else:
            wait_for_rate_limit = 0.0
        wait_s = max(debounce_s, wait_for_rate_limit)
        logger.info(
            "gh_deploy debounce: wait %.1fs "
            "(debounce=%.1f, rate_limit_remaining=%.1f, last_deploy=%s)",
            wait_s, debounce_s, wait_for_rate_limit,
            last.strftime("%H:%M:%S") if last else "never",
        )
        return wait_s

    async def _gh_deploy_sleep_and_run(self) -> None:
        """Sleep (cancellable) → run deploy (NOT cancellable) → reschedule if dirty."""
        wait_s = self._compute_gh_deploy_wait()
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            return  # debounce reset by a fresh article_done

        # Transition from sleep → run. From here on, cancelling this task
        # is a no-op for the subprocess pipeline; we run to completion.
        self._gh_deploy_sleep_task = None
        self._gh_deploy_running = True
        # Reset dirty BEFORE starting work — new article_done events
        # arriving from this point will set it again and trigger a
        # follow-up cycle.
        self._gh_deploy_dirty = False
        started_at = dt.datetime.now()
        try:
            # Snapshot pending batches; new ones will accumulate during/after.
            pending = list(self._gh_pending_batches)
            self._gh_pending_batches.clear()

            # Build first (no pagefind — fast), then deploy. mark_article_done
            # already builds with pagefind at chapter close, so search-index
            # is refreshed there; we only need the HTML to be current here.
            await self._run_build_site(reason="auto", with_pagefind=False)
            results = await self._run_gh_deploy(commit_msg_prefix="Auto-deploy")
            self._last_gh_deploy_at = dt.datetime.now()

            took = (self._last_gh_deploy_at - started_at).total_seconds()
            changed = any(r.changed for r in results)
            n_files = sum(r.files_changed for r in results)
            sha = next(
                (r.commit_sha[:7] for r in results if r.changed and r.commit_sha),
                "-",
            )
            logger.info(
                "gh_deploy ok: %d targets, changed=%s, files=%d, sha=%s, took=%.1fs",
                len(results), changed, n_files, sha, took,
            )

            # Short owner-chat heartbeat for successful auto-deploys with
            # actual changes — makes the debounce/deploy pipeline visible
            # in TG instead of being silent-success in the log only.
            if changed:
                try:
                    await self._send_owner(
                        f"🌐 auto-deploy ok: <code>{sha}</code>, "
                        f"{n_files} файл(ов), {took:.0f}s",
                        parse_mode=constants.ParseMode.HTML,
                    )
                except Exception:
                    logger.exception("owner-notify after auto-deploy failed")

            if changed and pending:
                last_bn = pending[-1]
                meta = self._resolve_batch_meta(last_bn)
                progress = self._progress or {}
                done_chap = progress.get("done_chapters") or "?"
                total_chap = progress.get("total_chapters") or "?"
                if meta:
                    text = (
                        f"🌐 Сайт обновлён. "
                        f"Статья {meta['chapter_articles_done']} / "
                        f"{meta['chapter_articles_total']} в главе "
                        f"{meta['chapter_ru']}, "
                        f"завершено глав {done_chap} / {total_chap}."
                    )
                else:
                    text = (
                        f"🌐 Сайт обновлён. "
                        f"Завершено глав {done_chap} / {total_chap}."
                    )
                if self.cfg.tg_public_channel:
                    try:
                        await self.app.bot.send_message(
                            chat_id=self.cfg.tg_public_channel,
                            text=text,
                        )
                    except Exception:
                        logger.exception(
                            "auto-deploy channel announcement failed"
                        )
        except Exception:
            logger.exception("gh auto-deploy failed")
            short = traceback.format_exception_only(*sys.exc_info()[:2])
            short_msg = "".join(short).strip()[:300]
            await self._send_owner(
                f"❌ GitHub auto-deploy упал:\n<code>{_escape(short_msg)}</code>\n"
                f"Подробности: state/orchestrator.log",
                parse_mode=constants.ParseMode.HTML,
            )
        finally:
            self._gh_deploy_running = False
            # If new article_done events arrived while we were deploying,
            # start a fresh debounce cycle now.
            if self._gh_deploy_dirty or self._gh_pending_batches:
                self._gh_deploy_dirty = False
                logger.info(
                    "gh_deploy: dirty after run, scheduling follow-up debounce"
                )
                self._gh_deploy_sleep_task = asyncio.create_task(
                    self._gh_deploy_sleep_and_run()
                )

    def _load_catalog(self) -> list[dict]:
        """Lazy-load articles_catalog.json once per process."""
        if self._catalog_cache is None:
            cat_path = self.cfg.heb_root / "Source" / "articles_catalog.json"
            self._catalog_cache = json.loads(cat_path.read_text(encoding="utf-8"))
        return self._catalog_cache

    def _resolve_batch_meta(self, batch_name: str) -> Optional[dict]:
        """Decode batch_name like '017_Bo' into a metadata dict.

        Returns dict with keys: article_index, chapter_en, chapter_ru, book,
        chapter_articles_total, chapter_articles_done.
        Returns None if batch_name doesn't parse or the chapter isn't in the
        catalog (defensive — batch_name format is stable).
        """
        # Format from build_batch.py: f"{article_index:03d}_{chapter_en}"
        m = re.match(r"^(\d+)_(.+)$", batch_name)
        if not m:
            return None
        article_index = int(m.group(1))
        chapter_en = m.group(2)

        catalog = self._load_catalog()
        chap_articles = [a for a in catalog if a.get("chapter") == chapter_en]
        if not chap_articles:
            return None
        meta = chap_articles[0]
        chapter_ru = meta.get("chapter_ru", chapter_en)
        book = meta.get("book", "")

        # Count completed .md files in this chapter's translation dir.
        chap_dir = self.cfg.heb_root / "Translated" / book / chapter_ru
        articles_done = (
            sum(1 for p in chap_dir.glob("*.md") if p.stem.isdigit())
            if chap_dir.is_dir() else 0
        )
        return {
            "article_index": article_index,
            "chapter_en": chapter_en,
            "chapter_ru": chapter_ru,
            "book": book,
            "chapter_articles_total": len(chap_articles),
            "chapter_articles_done": articles_done,
        }

    def _gh_targets(self) -> list[DeployTarget]:
        """Build the list of deploy targets from config (single primary)."""
        return [DeployTarget(
            repo=self.cfg.gh_repo,
            deploy_dir=self.cfg.gh_deploy_dir,
            cname=None,
        )]

    async def _run_gh_deploy(self, commit_msg_prefix: str):
        """Run gh_deploy.deploy_site_to_pages under a lock.

        Lock prevents two concurrent pushes (e.g. /push pressed while a
        debounce-triggered auto-deploy is already running).

        Returns list[DeployResult] — one per target.
        """
        async with self._gh_deploy_lock:
            return await asyncio.to_thread(
                deploy_site_to_pages,
                self.cfg.heb_root,
                self._gh_targets(),
                self.cfg.gh_token,
                commit_msg_prefix,
            )

    async def _run_build_site(self, *, reason: str, with_pagefind: bool) -> None:
        """Run corpus_tools/build_site.py under a mutex.

        Called BEFORE `_run_gh_deploy` (auto-deploy, /push, /rebuild) so
        the deploy syncs the freshly built `Translated/Site/`.

        Args:
            reason: short tag for the log line (e.g. "auto", "push", "rebuild").
            with_pagefind: True only for /rebuild (full build). Auto-deploy
                and /push pass False to skip the slow search-index rebuild;
                the existing index stays on disk and gets refreshed at the
                next chapter close via mark_article_done.py.
        """
        async with self._site_build_lock:
            script = CORPUS_TOOLS / "build_site.py"
            if not script.is_file():
                raise FileNotFoundError(f"build_site.py not found at {script}")
            cmd = [sys.executable, str(script)]
            if not with_pagefind:
                cmd.append("--no-pagefind")
            started = dt.datetime.now()
            logger.info(
                "build_site start (reason=%s, with_pagefind=%s)",
                reason, with_pagefind,
            )
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=str(self.cfg.heb_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            took = (dt.datetime.now() - started).total_seconds()
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-2000:]
                logger.error(
                    "build_site failed (reason=%s, rc=%d, took=%.1fs):\n%s",
                    reason, proc.returncode, took, tail,
                )
                raise RuntimeError(
                    f"build_site.py failed (rc={proc.returncode}, reason={reason})"
                )
            logger.info(
                "build_site ok (reason=%s, took=%.1fs)", reason, took,
            )

    async def _cmd_rebuild(self, update: Update, _ctx) -> None:
        """Manual rebuild + GitHub Pages push.

        Full build (with pagefind) followed by git push to GH Pages.
        Cancels any pending auto-deploy debounce timer (no point waiting
        if user wants it now).
        """
        if not (self.cfg.gh_token and self.cfg.gh_repo):
            await update.effective_message.reply_text(
                "⚠️ GitHub auto-deploy выключен (GH_TOKEN / GH_REPO в .env пусты).",
            )
            return

        msg = await update.effective_message.reply_text(
            "🛠 Пересобираю сайт и пушу в GitHub...",
        )
        # Cancel pending sleep-phase only; if a run is in flight, the
        # _run_gh_deploy lock below will serialize behind it.
        if self._gh_deploy_sleep_task and not self._gh_deploy_sleep_task.done():
            self._gh_deploy_sleep_task.cancel()
            self._gh_deploy_sleep_task = None

        try:
            # Full build with pagefind — this is the explicit "rebuild" path.
            await self._run_build_site(reason="rebuild", with_pagefind=True)
            results = await self._run_gh_deploy(commit_msg_prefix="Manual rebuild")
            self._last_gh_deploy_at = dt.datetime.now()
        except Exception as e:
            await msg.edit_text(
                f"❌ Rebuild провалился: {type(e).__name__}: {e}\n"
                f"<code>{_escape(str(e))[:300]}</code>",
                parse_mode=constants.ParseMode.HTML,
            )
            return

        if not any(r.changed for r in results):
            await msg.edit_text(
                "🌐 Сайт уже актуален в GitHub — push не потребовался "
                "(нет изменений с последнего коммита).",
            )
            return

        lines = ["🌐 <b>Rebuild ОК</b>"]
        for r in results:
            if r.changed:
                commit_url = f"https://github.com/{r.repo}/commit/{r.commit_sha}"
                lines.append(
                    f"• <a href=\"{commit_url}\">{r.repo}@{r.commit_sha[:7]}</a>"
                )
        owner, name = self.cfg.gh_repo.split("/", 1)
        site_url = f"https://{owner}.github.io/{name}/"
        lines.append("Через 1–2 мин обновится: " + site_url)
        await msg.edit_text(
            "\n".join(lines),
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def _cmd_log(self, update: Update, ctx) -> None:
        n = 20
        if ctx.args:
            try:
                n = max(1, min(100, int(ctx.args[0])))
            except ValueError:
                pass
        events = read_events_tail(self.orch.events_file, n=n)
        if not events:
            await update.effective_message.reply_text("Лог пустой.")
            return
        lines = [
            f"{ev.ts.strftime('%H:%M:%S')} {ev.type}"
            + (f" {_short(ev.data)}" if ev.data else "")
            for ev in events
        ]
        body = "\n".join(lines)
        # Telegram message limit ≈ 4096 chars. Trim if needed.
        if len(body) > 3800:
            body = body[-3800:]
            body = "…\n" + body[body.find("\n") + 1:]
        await update.effective_message.reply_text(
            f"<pre>{_escape(body)}</pre>",
            parse_mode=constants.ParseMode.HTML,
        )

    async def _cmd_resume(self, update: Update, _ctx) -> None:
        if self.orch.run.state != State.ERROR:
            await update.effective_message.reply_text(
                f"State = {self.orch.run.state.value}, /resume не нужен.",
            )
            return
        await self.orch.clear_error()
        await update.effective_message.reply_text(
            "✅ /resume: ERROR cleared. State → IDLE (manual).\n"
            "Чтобы запустить цикл — жми <b>⚡ Run now</b>, "
            "или <b>▶️ Run...</b> для отложенного запуска.",
            parse_mode=constants.ParseMode.HTML,
        )

    # ------------------------------------------------------------------
    # ConversationHandler for "▶️ Run..." (multi-step run setup)
    # ------------------------------------------------------------------

    async def _run_conv_entry(self, update: Update, ctx) -> int:
        # Reset any leftover user_data from previous cancel.
        ctx.user_data.pop("run_when", None)
        ctx.user_data.pop("run_limit", None)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Сейчас", callback_data="when:now"),
             InlineKeyboardButton("🕐 Через 1ч", callback_data="when:1h")],
            [InlineKeyboardButton("🕔 Через 5ч", callback_data="when:5h"),
             InlineKeyboardButton("🌅 Завтра 09:00", callback_data="when:tomorrow_9")],
            [InlineKeyboardButton("📝 Указать вручную...", callback_data="when:manual")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="cancel")],
        ])
        await update.effective_message.reply_text(
            "🕐 <b>Когда запустить?</b>",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=kb,
        )
        return ASK_WHEN

    async def _run_conv_when(self, update: Update, ctx) -> int:
        q = update.callback_query
        await q.answer()
        choice = q.data.split(":", 1)[1]
        now = dt.datetime.now()

        if choice == "now":
            ctx.user_data["run_when"] = now
        elif choice == "1h":
            ctx.user_data["run_when"] = now + dt.timedelta(hours=1)
        elif choice == "5h":
            ctx.user_data["run_when"] = now + dt.timedelta(hours=5)
        elif choice == "tomorrow_9":
            tomorrow = now.date() + dt.timedelta(days=1)
            ctx.user_data["run_when"] = dt.datetime.combine(tomorrow, dt.time(9, 0))
        elif choice == "manual":
            await q.edit_message_text(
                "📝 Введи время: <code>HH:MM</code> (для сегодня) или "
                "<code>DD-HH:MM</code> (для конкретного дня этого месяца). "
                "Например: <code>14:30</code> или <code>27-09:00</code>.",
                parse_mode=constants.ParseMode.HTML,
            )
            return AWAIT_CUSTOM_TIME
        else:
            return ConversationHandler.END

        await q.edit_message_text(
            f"🕐 Старт: <b>{ctx.user_data['run_when'].strftime('%Y-%m-%d %H:%M')}</b>",
            parse_mode=constants.ParseMode.HTML,
        )
        return await self._run_conv_show_limit(update, ctx)

    async def _run_conv_custom_time(self, update: Update, ctx) -> int:
        text = (update.message.text or "").strip()
        parsed = self._parse_custom_time(text)
        if parsed is None:
            await update.message.reply_text(
                "❌ Не распознал формат. Попробуй ещё раз: HH:MM или DD-HH:MM. "
                "Или /cancel для выхода."
            )
            return AWAIT_CUSTOM_TIME
        ctx.user_data["run_when"] = parsed
        await update.message.reply_text(
            f"🕐 Старт: <b>{parsed.strftime('%Y-%m-%d %H:%M')}</b>",
            parse_mode=constants.ParseMode.HTML,
        )
        return await self._run_conv_show_limit(update, ctx)

    async def _run_conv_show_limit(self, update: Update, ctx) -> int:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("♾ Unlimited", callback_data="lim:unlim"),
             InlineKeyboardButton("80 %", callback_data="lim:80")],
            [InlineKeyboardButton("60 %", callback_data="lim:60"),
             InlineKeyboardButton("30 %", callback_data="lim:30")],
            [InlineKeyboardButton("✏️ Свой N%...", callback_data="lim:manual")],
            [InlineKeyboardButton("✖️ Отмена", callback_data="cancel")],
        ])
        target = update.effective_message
        await target.reply_text(
            "🎚 <b>Лимит квоты на цикл?</b>\n\n"
            "<i>«Unlimited» — крутить до hit-limit. "
            "Процент = верхняя граница usage; цикл соберёт ровно столько "
            "статей, чтобы предполагаемая стоимость не превысила оставшийся "
            "бюджет до 80% target'а.</i>",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=kb,
        )
        return ASK_LIMIT

    async def _run_conv_limit(self, update: Update, ctx) -> int:
        q = update.callback_query
        await q.answer()
        choice = q.data.split(":", 1)[1]

        if choice == "unlim":
            ctx.user_data["run_limit"] = None
        elif choice in ("80", "60", "30"):
            ctx.user_data["run_limit"] = int(choice)
        elif choice == "manual":
            await q.edit_message_text(
                "✏️ Введи целое число <code>N</code> от 1 до 99 — "
                "процент usage-cap'а. Например: <code>45</code>. "
                "Или /cancel.",
                parse_mode=constants.ParseMode.HTML,
            )
            return AWAIT_CUSTOM_LIMIT
        else:
            return ConversationHandler.END

        lim_label = "♾ Unlimited" if ctx.user_data["run_limit"] is None \
            else f"{ctx.user_data['run_limit']}%"
        await q.edit_message_text(
            f"🎚 Лимит: <b>{lim_label}</b>",
            parse_mode=constants.ParseMode.HTML,
        )
        return await self._run_conv_show_confirm(update, ctx)

    async def _run_conv_custom_limit(self, update: Update, ctx) -> int:
        text = (update.message.text or "").strip().rstrip("%")
        try:
            n = int(text)
            if not (1 <= n <= 99):
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "❌ Нужно целое 1..99. Попробуй ещё раз или /cancel."
            )
            return AWAIT_CUSTOM_LIMIT
        ctx.user_data["run_limit"] = n
        await update.message.reply_text(f"🎚 Лимит: <b>{n}%</b>",
                                        parse_mode=constants.ParseMode.HTML)
        return await self._run_conv_show_confirm(update, ctx)

    async def _run_conv_show_confirm(self, update: Update, ctx) -> int:
        when: dt.datetime = ctx.user_data["run_when"]
        lim = ctx.user_data["run_limit"]
        wait = when - dt.datetime.now()
        wait_str = (
            "сейчас" if wait.total_seconds() <= 60
            else f"через {int(wait.total_seconds() // 60)} мин"
        )
        lim_label = "♾ Unlimited" if lim is None else f"{lim}%"
        text = (
            "<b>Подтверди:</b>\n"
            f"⚡ Старт: {when.strftime('%Y-%m-%d %H:%M')} ({wait_str})\n"
            f"🎚 Лимит:  {lim_label}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data="conf:yes"),
             InlineKeyboardButton("✖️ Отмена", callback_data="cancel")],
        ])
        target = update.effective_message
        await target.reply_text(
            text, parse_mode=constants.ParseMode.HTML, reply_markup=kb,
        )
        return ASK_CONFIRM

    async def _run_conv_confirm(self, update: Update, ctx) -> int:
        q = update.callback_query
        await q.answer()
        if q.data != "conf:yes":
            return await self._run_conv_cancel(update, ctx)

        when: dt.datetime = ctx.user_data["run_when"]
        lim: Optional[int] = ctx.user_data["run_limit"]
        scheduled_now = await self.orch.request_run(usage_limit_pct=lim, when=when)
        wait = when - dt.datetime.now()
        wait_str = (
            "сейчас" if wait.total_seconds() <= 60
            else f"через {int(wait.total_seconds() // 60)} мин ({when.strftime('%H:%M')})"
        )
        lim_label = "unlimited" if lim is None else f"{lim}%"
        if scheduled_now:
            await q.edit_message_text(
                f"✅ Запрос принят: старт {wait_str}, лимит {lim_label}.",
            )
        else:
            await q.edit_message_text(
                f"⏳ Запрос принят, но в очередь: текущий цикл "
                f"({self.orch.run.state.value.lower()}) сначала завершится. "
                f"Параметры: старт {wait_str}, лимит {lim_label}. "
                f"/kill чтобы отменить очередь.",
            )
        ctx.user_data.clear()
        return ConversationHandler.END

    async def _run_conv_cancel(self, update: Update, ctx) -> int:
        ctx.user_data.clear()
        msg = "✖️ Отменено."
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.edit_message_text(msg)
            except Exception:
                pass
        elif update.message:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    @staticmethod
    def _parse_custom_time(text: str) -> Optional[dt.datetime]:
        """Parse 'HH:MM' (today, tomorrow if past) or 'DD-HH:MM' (this month)."""
        text = text.strip()
        now = dt.datetime.now()
        try:
            if "-" in text:
                day_str, hm = text.split("-", 1)
                day = int(day_str)
                h, m = map(int, hm.split(":"))
                cand = now.replace(day=day, hour=h, minute=m,
                                   second=0, microsecond=0)
                if cand <= now:
                    # Next month if day already passed.
                    if now.month == 12:
                        cand = cand.replace(year=now.year + 1, month=1)
                    else:
                        cand = cand.replace(month=now.month + 1)
                return cand
            else:
                h, m = map(int, text.split(":"))
                cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if cand <= now:
                    cand += dt.timedelta(days=1)
                return cand
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Event subscriber (orchestrator → bot → user)
    # ------------------------------------------------------------------

    async def _on_orch_event(self, ev: Event) -> None:
        # Trace events forwarded to private chat.
        if ev.type in TRACE_EVENT_TYPES:
            try:
                text = self._format_trace(ev)
                if text:
                    await self._send_owner(text, parse_mode=constants.ParseMode.HTML)
            except Exception:
                logger.exception("Failed to forward event %s", ev.type)

        # GitHub Pages auto-deploy: trigger after every closed article.
        # Debounced — bursts of article_done within debounce window
        # collapse into a single push.
        if ev.type == "article_done":
            bn = (ev.data or {}).get("batch_name")
            if bn:
                self._gh_pending_batches.append(bn)
            self._schedule_gh_deploy()

        # Refresh global progress (chapter+corpus counts) before
        # dashboard render — per-event.
        try:
            await self._refresh_progress()
        except Exception:
            logger.exception("refresh_progress failed for event %s", ev.type)

        # Live dashboard refresh.
        # Article-level events (article_started/done/failed) come in bursts
        # of N=parallel_translators and would saturate Telegram's edit
        # rate-limit (~30/min). Coalesce them via debounce. Important
        # control events (state, error, hit_limit, halt, lifecycle) edit
        # the dashboard immediately.
        IMMEDIATE_TYPES = {
            "state_transition", "cycle_started", "cycle_finished",
            "hit_limit", "hit_limit_detected", "error",
            "emergency_halt", "done", "site_rebuilt",
            "orchestrator_started", "orchestrator_boot_recovery",
            "orchestrator_stopped", "stop_requested",
        }
        try:
            if ev.type in IMMEDIATE_TYPES:
                await self._update_dashboard()
            else:
                self._schedule_dashboard_debounce()
        except Exception:
            logger.exception("dashboard update failed for event %s", ev.type)

    def _format_trace(self, ev: Event) -> Optional[str]:
        ts = ev.ts.strftime("%H:%M:%S")
        d = ev.data
        t = ev.type

        if t == "state_transition":
            return (f"🔁 {ts} <b>{d.get('from')} → {d.get('to')}</b>"
                    + (f"\n  {_escape(d.get('reason',''))}" if d.get("reason") else ""))
        if t == "tool_invocation":
            phase = d.get("phase", "?")
            tool = d.get("tool", "?")
            head = f"🛠 {ts} {tool} · {phase}"
            extras = []
            for k in ("rc", "status", "articles_count", "articles_translated",
                      "articles_failed_count", "site_rebuilt",
                      "chapters_touched_count"):
                if k in d:
                    extras.append(f"{k}={d[k]}")
            if d.get("phase") == "failed" and d.get("stderr_tail"):
                extras.append(f"stderr_tail={_escape(d['stderr_tail'][-200:])}")
            return head + ("\n  " + " ".join(extras) if extras else "")
        if t == "cycle_started":
            return (f"▶️ {ts} cycle_started · {d.get('chapter_start')} · "
                    f"{d.get('articles_count')} статей · mode={d.get('mode')}")
        if t == "cycle_finished":
            return (f"⏸ {ts} cycle_finished · "
                    f"exit_reason={d.get('exit_reason')} · "
                    f"report_status={d.get('report_status')} · "
                    f"+{d.get('completed_in_run')} статей · "
                    f"next_wake_at={d.get('next_wake_at') or 'manual'}")
        if t == "hit_limit":
            names = d.get("completed_in_run_batch_names") or []
            range_str = (
                f"{names[0]} → {names[-1]}" if len(names) >= 2
                else (names[0] if names else "—")
            )
            cur = (f"{d.get('next_cursor')} ({d.get('completed_count')}/"
                   f"{d.get('total_articles')})")
            reset = d.get("reset") or "??"
            # ISO datetime (содержит 'T') = недельный лимит, days-scale wait.
            # HH:MM = 5-часовой session-лимит.
            if isinstance(reset, str) and "T" in reset:
                kind = "<b>weekly</b>"
                # Показать в человечном виде "Apr 30 11:30"
                try:
                    cand = dt.datetime.fromisoformat(reset)
                    reset_h = cand.strftime("%b %d %H:%M")
                    delta = cand - dt.datetime.now()
                    days = delta.days
                    if days >= 1:
                        wait_h = f"~{days} дн."
                    else:
                        wait_h = f"~{int(delta.total_seconds() / 3600)} ч."
                    reset_h = f"{reset_h} ({wait_h})"
                except Exception:
                    reset_h = reset
            else:
                kind = "session"
                reset_h = reset
            return (f"⏳ {ts} <b>Hit-limit</b> ({kind}) · reset {reset_h}\n"
                    f"  ✅ За цикл: +{d.get('completed_in_run')} статей ({range_str})\n"
                    f"  📍 Курсор: {cur}\n"
                    f"  Продолжу автоматически после reset+5min.")
        if t == "chapter_closed":
            return f"✅ {ts} chapter_closed · <b>{d.get('chapter')}</b>"
        if t == "site_rebuilt":
            return (f"🌐 {ts} site_rebuilt · "
                    f"{', '.join(d.get('chapters_done_this_cycle') or [])} · "
                    f"ts={d.get('site_ts')}")
        if t == "done":
            return f"🎉 {ts} <b>DONE</b> · корпус полностью переведён."
        if t == "error":
            return f"❌ {ts} ERROR · <code>{_escape(d.get('reason',''))}</code>"
        if t == "emergency_halt":
            return (f"🚨 {ts} <b>EMERGENCY HALT</b> · reason={d.get('reason')}\n"
                    + _escape(str({k: v for k, v in d.items() if k != 'reason'})))
        if t == "orchestrator_boot_recovery":
            return f"⚠️ {ts} orchestrator boot recovery · prev={d.get('previous_state')}"
        if t == "run_queued":
            usage = d.get("usage_limit_pct")
            mode = "unlimited" if usage is None else f"usage {usage}%"
            return (f"⏳ {ts} run_queued · режим: {mode} · "
                    f"current_state={d.get('current_state')} · "
                    f"стартанёт после завершения текущего цикла")
        if t == "pending_run_applied":
            usage = d.get("usage_limit_pct")
            mode = "unlimited" if usage is None else f"usage {usage}%"
            return f"▶️ {ts} pending_run применён · режим: {mode}"
        return None

    # ------------------------------------------------------------------
    # Live dashboard pin
    # ------------------------------------------------------------------

    async def _init_dashboard(self) -> None:
        """Always send a fresh dashboard on bot start.

        Reason: a saved msg_id can edit-succeed silently when the user has
        cleared chat history (server still has the message, user doesn't
        see it). Telegram Bot API offers no reliable way to detect this.
        Predictable behaviour: always create new on start.

        Перед созданием нового — открепляем ВСЕ старые pinned-сообщения
        в приватном чате (через unpin_all_chat_messages). Иначе после
        каждого рестарта бота копится список «застрявших» дашбордов в
        списке pinned (Telegram держит до 50, потом старые вытесняются).
        Старые сообщения не удаляются — просто перестают быть pinned.

        Mid-session, _update_dashboard self-heals if the active dashboard
        gets deleted by the user.
        """
        async with self._dashboard_lock:
            # Discard any leftover saved id from previous runs.
            self._discard_saved_dashboard()
            await self._unpin_all_owner_messages()
            await self._create_dashboard()

    async def _unpin_all_owner_messages(self) -> None:
        """Unpin every currently-pinned message in the private owner chat.

        Used on bot startup to clean up dashboards from previous sessions.
        Telegram's `unpin_all_chat_messages` is idempotent and safe: if
        nothing is pinned, it just returns True.
        """
        try:
            await self.app.bot.unpin_all_chat_messages(chat_id=self._owner_id)
            logger.info("Unpinned all messages in owner chat (cleanup)")
        except (TelegramError, NetworkError, BadRequest) as e:
            # Non-fatal: bot may have lost pin permission, or chat may
            # be temporarily unavailable. Log and continue — new dashboard
            # will be pinned anyway.
            logger.warning("unpin_all_chat_messages failed: %s", e)

    async def _try_recover_dashboard(self) -> bool:
        """Read saved msg_id and try a single edit. True if message exists
        and was edited successfully. False otherwise — caller will create
        new dashboard."""
        if not self._dashboard_path.exists():
            return False
        try:
            data = json.loads(self._dashboard_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("dashboard_msg.json parse failed; discarding")
            self._discard_saved_dashboard()
            return False
        if data.get("chat_id") != self._owner_id:
            self._discard_saved_dashboard()
            return False
        saved_id = data.get("message_id")
        if not saved_id:
            self._discard_saved_dashboard()
            return False

        text = self._render_dashboard()
        try:
            await self.app.bot.edit_message_text(
                chat_id=self._owner_id,
                message_id=int(saved_id),
                text=text,
                parse_mode=constants.ParseMode.HTML,
            )
            self._dashboard_msg_id = int(saved_id)
            self._last_dashboard_text = text
            return True
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                self._dashboard_msg_id = int(saved_id)
                self._last_dashboard_text = text
                return True
            if ("message to edit not found" in msg
                    or "message_id_invalid" in msg
                    or "chat not found" in msg):
                logger.info("Saved dashboard msg %d is gone, will recreate",
                            saved_id)
                self._discard_saved_dashboard()
                return False
            logger.warning("dashboard recover edit failed: %s", e)
            self._discard_saved_dashboard()
            return False
        except (TelegramError, NetworkError) as e:
            logger.warning("dashboard recover network error: %s", e)
            return False  # don't discard — might be transient

    async def _create_dashboard(self) -> None:
        """Send a fresh dashboard message and pin it."""
        text = self._render_dashboard()
        try:
            msg = await self.app.bot.send_message(
                chat_id=self._owner_id,
                text=text,
                parse_mode=constants.ParseMode.HTML,
                disable_notification=True,
            )
        except Exception:
            logger.exception("create_dashboard send_message failed")
            return
        self._dashboard_msg_id = msg.message_id
        self._last_dashboard_text = text
        await self._try_pin(msg.message_id)
        self._save_dashboard_id()
        logger.info("Dashboard created (msg_id=%d)", msg.message_id)

    def _discard_saved_dashboard(self) -> None:
        self._dashboard_msg_id = None
        self._last_dashboard_text = None
        try:
            if self._dashboard_path.exists():
                self._dashboard_path.unlink()
        except Exception:
            logger.exception("could not delete dashboard_msg.json")

    async def _try_pin(self, msg_id: int) -> None:
        """Pin msg in private chat, idempotent. Logged at info-level."""
        try:
            await self.app.bot.pin_chat_message(
                chat_id=self._owner_id,
                message_id=msg_id,
                disable_notification=True,
            )
            logger.info("Dashboard pinned (msg_id=%d)", msg_id)
        except BadRequest as e:
            msg = str(e).lower()
            if "message is already pinned" in msg or "already pinned" in msg:
                logger.info("Dashboard already pinned (msg_id=%d)", msg_id)
            else:
                logger.warning(
                    "pin_chat_message failed for msg_id=%d: %s", msg_id, e,
                )
        except (TelegramError, NetworkError) as e:
            logger.warning("pin_chat_message network error: %s", e)

    def _schedule_dashboard_debounce(self) -> None:
        """Coalesce article-level events into a single edit per N seconds.

        Sets a dirty flag and (if not already running) starts a sleep-then-
        edit task. Multiple events arriving during the sleep just keep
        the dirty flag set; the task does ONE edit at the end.
        """
        self._dashboard_dirty = True
        if (self._dashboard_debounce_task is not None
                and not self._dashboard_debounce_task.done()):
            return
        self._dashboard_debounce_task = asyncio.create_task(
            self._debounce_dashboard_loop()
        )

    async def _debounce_dashboard_loop(self) -> None:
        try:
            await asyncio.sleep(self.cfg.dashboard_debounce_seconds)
            self._dashboard_dirty = False
            await self._update_dashboard()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("debounce dashboard task crashed")

    async def _update_dashboard(self, force: bool = False) -> bool:
        """Edit the pinned dashboard in place. If the message is gone
        (user deleted it / cleared chat history), auto-recreate.
        Non-recursive — recreate path goes through _create_dashboard()."""
        if self._dashboard_msg_id is None:
            # No dashboard yet — initialize one.
            await self._init_dashboard()
            return self._dashboard_msg_id is not None
        async with self._dashboard_lock:
            text = self._render_dashboard()
            if not force and text == self._last_dashboard_text:
                return True
            try:
                await self.app.bot.edit_message_text(
                    chat_id=self._owner_id,
                    message_id=self._dashboard_msg_id,
                    text=text,
                    parse_mode=constants.ParseMode.HTML,
                )
                self._last_dashboard_text = text
                return True
            except BadRequest as e:
                msg = str(e).lower()
                if "message is not modified" in msg:
                    return True
                if ("message to edit not found" in msg
                        or "message_id_invalid" in msg):
                    logger.info("Dashboard message gone, recreating")
                    self._discard_saved_dashboard()
                    await self._create_dashboard()
                    return self._dashboard_msg_id is not None
                logger.warning("edit_message_text failed: %s", e)
                return False
            except (TelegramError, NetworkError) as e:
                logger.warning("dashboard edit network/tg error: %s", e)
                return False

    def _render_dashboard(self) -> str:
        r = self.orch.run
        p = self._progress
        last = self._read_last_session()
        ts_now = dt.datetime.now().strftime("%H:%M:%S")

        state_emoji = {
            State.IDLE: "⏸",
            State.PREPARING: "🟡",
            State.RUNNING: "🟢",
            State.FINALIZING: "🔵",
            State.DONE: "🎉",
            State.ERROR: "🚨",
        }.get(r.state, "·")
        head = f"🌳 <b>zohar-translator</b> · {ts_now}\n\n"
        head += f"{state_emoji} <b>{r.state.value}</b>"
        if r.state == State.IDLE and r.idle_reason:
            head += f" · {r.idle_reason}"
        head += f"\nrežim: {r.mode}"
        if r.usage_limit_pct:
            head += f" ({r.usage_limit_pct}%)"
        head += f" · parallel={self.cfg.parallel_translators}"
        if self.cfg.parallel_cache_primer:
            head += " · primer"
        head += "\n"

        # Global progress block — chapter / corpus / in-chapter.
        if p.get("next_cursor"):
            head += (
                f"\n📚 Главы: <b>{p.get('done_chapters')}</b>/"
                f"{p.get('total_chapters')}\n"
                f"📜 Статьи: <b>{p.get('done_articles')}</b>/"
                f"{p.get('total_articles_corpus')}\n"
                f"📍 В <b>{p.get('next_cursor')}</b>: "
                f"{p.get('completed_count')}/{p.get('total_articles')}\n"
            )
        elif p:
            head += "\n🎉 Все главы готовы.\n"

        # Currently in-flight articles (parallel batch).
        if r.current_articles:
            head += (
                f"\n⏳ <b>В работе ({len(r.current_articles)}):</b>\n"
            )
            for ca in r.current_articles:
                started = ca.started_at.strftime("%H:%M:%S")
                head += (
                    f"  • <code>{ca.batch_name}</code> "
                    f"({ca.expected_chars} ch, started {started})\n"
                )

        # Cycle counter
        head += (f"\n✅ За цикл: {r.completed_in_run} статей "
                 f"· ❌ {r.failed_in_run}\n")
        if r.completed_in_run_batch_names:
            names = r.completed_in_run_batch_names
            if len(names) <= 5:
                head += f"  ({', '.join(names)})\n"
            else:
                head += (f"  ({names[0]} → {names[-1]}, "
                         f"всего {len(names)})\n")

        # Site
        if last and last.site_rebuilt_ts:
            head += f"\n🌐 Сайт: {last.site_rebuilt_ts}\n"

        # Wait info
        if r.next_wake_at:
            wait = r.next_wake_at - dt.datetime.now()
            wait_str = (
                r.next_wake_at.strftime("%H:%M")
                + (f" (через {int(wait.total_seconds()//60)} мин)"
                   if wait.total_seconds() > 0 else " (сейчас)")
            )
            head += f"\n⏰ next_wake_at: {wait_str}\n"

        # Error
        if r.error_message:
            head += f"\n🚨 <b>error:</b>\n<pre>{_escape(r.error_message[:500])}</pre>\n"

        if r.is_post_hit_limit_retry:
            head += "\n⚠️ post-hit-limit retry (следующий цикл — критический)\n"

        return head.rstrip()

    async def _refresh_progress(self) -> None:
        """Run corpus_tools/next_cursor.py and cache result."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(CORPUS_TOOLS / "next_cursor.py"),
                cwd=str(self.cfg.heb_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            if proc.returncode == 0 and stdout:
                self._progress = json.loads(stdout.decode("utf-8"))
        except Exception:
            logger.exception("_refresh_progress failed")

    def _read_last_session(self) -> Optional[LastSession]:
        path = self.cfg.state_dir / "last_session.json"
        if not path.exists():
            return None
        try:
            return LastSession.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_dashboard_id(self) -> None:
        if self._dashboard_msg_id is None:
            return
        import json
        self._dashboard_path.write_text(
            json.dumps({
                "chat_id": self._owner_id,
                "message_id": self._dashboard_msg_id,
            }, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send_owner(self, text: str, **kwargs) -> None:
        if self.app is None or self._owner_id is None:
            return
        try:
            await self.app.bot.send_message(chat_id=self._owner_id, text=text, **kwargs)
        except Exception:
            logger.exception("send to owner failed")


def _escape(s: str) -> str:
    """HTML-escape for Telegram parse_mode=HTML."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(d: dict) -> str:
    """Truncate event data dict for /log line."""
    s = ", ".join(f"{k}={v}" for k, v in d.items())
    return s if len(s) <= 200 else s[:197] + "..."


