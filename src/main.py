"""
ZoharTGBatch entry point — параллельный батч-вариант ZoharTG.

Runs orchestrator + Telegram bot in a single asyncio loop.

  python main.py            # auto-start in unlimited mode
  python main.py --usage 60 # auto-start in usage-60% mode
  python main.py --idle     # boot but don't auto-start a cycle
  python main.py --no-bot   # orchestrator-only mode
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from config import load_config
from orchestrator import Orchestrator
from state import Event


def _setup_logging(level: str, log_file) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)

    fh = RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    # Tame the Telegram lib's internal noise on INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


async def _stdout_event_logger(ev: Event) -> None:
    important = {
        "state_transition", "tool_invocation",
        "cycle_started", "cycle_finished",
        "hit_limit", "chapter_closed", "site_rebuilt",
        "error", "emergency_halt", "done",
        "orchestrator_started", "orchestrator_boot_recovery",
    }
    if ev.type in important:
        logging.getLogger("zohartgbatch.events").info(
            "EVENT %s %s", ev.type, ev.data,
        )


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usage", type=int, default=None,
                    help="usage_limit_pct for build_batch (default unlimited)")
    ap.add_argument("--idle", action="store_true",
                    help="boot but don't auto-start a cycle (manual /run)")
    ap.add_argument("--no-bot", action="store_true",
                    help="run orchestrator only, no Telegram bot")
    args = ap.parse_args()

    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.state_dir / "orchestrator.log")

    logger = logging.getLogger("zohartgbatch.main")
    logger.info(
        "ZoharTGBatch starting · heb_root=%s · log_level=%s · bot=%s · "
        "parallel=%d · primer=%s",
        cfg.heb_root, cfg.log_level, "off" if args.no_bot else "on",
        cfg.parallel_translators, cfg.parallel_cache_primer,
    )

    orch = Orchestrator(cfg)
    orch.subscribe(_stdout_event_logger)

    # Initialize start mode
    if args.idle:
        orch.run.next_wake_at = None
        orch.run.idle_reason = "manual"
    if args.usage is not None:
        orch.run.mode = "usage"
        orch.run.usage_limit_pct = args.usage
    else:
        orch.run.mode = "unlimited"
        orch.run.usage_limit_pct = None

    # --- bot setup (optional) ---
    bot = None
    if not args.no_bot:
        if cfg.tg_private_chat_id is None:
            logger.error(
                "TG_PRIVATE_CHAT_ID is empty in .env. "
                "Either fill it (send /start to bot, fetch via getUpdates) "
                "or use --no-bot. Aborting."
            )
            return 2
        from bot import ZoharBot
        bot = ZoharBot(cfg, orch)
        await bot.start()

    # --- signal handling ---
    loop = asyncio.get_running_loop()

    def _handle_signal(signame: str) -> None:
        logger.info("Got %s, requesting shutdown", signame)
        orch.request_shutdown()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal, sig.name)
    else:
        # Windows: asyncio loop.add_signal_handler is not available, so we
        # install a plain signal.signal() handler that hops onto the loop
        # via call_soon_threadsafe. SIGBREAK arrives when the GUI wrapper
        # sends CTRL_BREAK_EVENT to the process group (graceful X-close).
        # SIGINT covers Ctrl-C in a console. SIGTERM does NOT exist on
        # Windows in a useful way (TerminateProcess is unblockable).
        def _win_handler(sig_num, _frame) -> None:
            try:
                name = signal.Signals(sig_num).name
            except Exception:
                name = str(sig_num)
            loop.call_soon_threadsafe(_handle_signal, name)
        for sig in (signal.SIGINT, signal.SIGBREAK):
            try:
                signal.signal(sig, _win_handler)
            except (ValueError, OSError):
                # ValueError if not in main thread; OSError if signal
                # unsupported on this Windows build. Silent — best effort.
                pass

    # --- main loop ---
    try:
        await orch.run_forever()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        orch.request_shutdown()
    finally:
        if bot:
            await bot.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
