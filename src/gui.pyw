"""
ZoharTGBatch GUI launcher.

Двойной клик по этому файлу запускает приложение через pythonw.exe (без
чёрной консоли). Внутри — две вкладки:
  1) Запуск: число параллельных переводчиков (auto-save в .env),
     кнопки Start/Stop и прокручиваемый лог с момента старта приложения.
  2) Настройки: остальные ключи .env, кнопки Save / Restore.

Бот стартует как subprocess (python.exe main.py, CREATE_NO_WINDOW),
stdout/stderr идут в виджет логов. Stop = terminate() (мягко — на
Windows это TerminateProcess, но активен только в IDLE-состоянии
оркестратора, поэтому никакая статья в полёте не страдает).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
STATE_DIR = PROJECT_ROOT / "state"
STATE_FILE = STATE_DIR / "current_run.json"
LOGS_DIR = PROJECT_ROOT / "logs"
MAIN_PY = PROJECT_ROOT / "main.py"

PARALLEL_KEY = "PARALLEL_TRANSLATORS"
PARALLEL_MIN = 1
PARALLEL_MAX = 50

POLL_LOG_MS = 100
POLL_STATE_MS = 1000

KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

class EnvLine:
    __slots__ = ("kind", "key", "value", "raw")

    def __init__(self, kind: str, raw: str, key: str = "", value: str = "") -> None:
        self.kind = kind  # 'kv' | 'other'
        self.key = key
        self.value = value
        self.raw = raw


def parse_env(text: str) -> list[EnvLine]:
    out: list[EnvLine] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            out.append(EnvLine("other", line))
            continue
        m = KV_RE.match(line)
        if m:
            out.append(EnvLine("kv", line, m.group(1), m.group(2)))
        else:
            out.append(EnvLine("other", line))
    return out


def render_env(env_lines: list[EnvLine], updates: dict[str, str]) -> str:
    """Re-emit .env keeping comments/order; replace values for keys in updates."""
    out_lines: list[str] = []
    for el in env_lines:
        if el.kind == "kv" and el.key in updates:
            out_lines.append(f"{el.key}={updates[el.key]}")
        else:
            out_lines.append(el.raw)
    return "\n".join(out_lines) + ("\n" if out_lines and not out_lines[-1].endswith("\n") else "")


def read_env_file() -> tuple[list[EnvLine], dict[str, str]]:
    text = ENV_PATH.read_text(encoding="utf-8")
    lines = parse_env(text)
    values = {el.key: el.value for el in lines if el.kind == "kv"}
    return lines, values


def write_env_file(updates: dict[str, str]) -> None:
    lines, _ = read_env_file()
    new_text = render_env(lines, updates)
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp, ENV_PATH)


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------

def _python_exe() -> str:
    """Return path to python.exe used to spawn main.py.

    Берём тот же Python, что запустил gui.pyw (sys.executable). Если он
    pythonw.exe — конвертируем в соседний python.exe (subprocess с
    PIPE стабильнее работает с консольным python.exe). Так гарантируем,
    что main.py использует тот же интерпретатор и тот же site-packages,
    куда _ensure_deps() поставил зависимости.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        cand = exe[: -len("pythonw.exe")] + "python.exe"
        if Path(cand).is_file():
            return cand
    return exe


CREATE_NO_WINDOW = 0x08000000  # win32 process creation flag
CREATE_NEW_PROCESS_GROUP = 0x00000200  # required to send CTRL_BREAK_EVENT


class BotProcess:
    """Owns the python.exe main.py subprocess + a stdout-reader thread."""

    def __init__(self, log_queue: "queue.Queue[str | None]") -> None:
        self.proc: subprocess.Popen | None = None
        self.log_queue = log_queue
        self._reader: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.is_running():
            return
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [_python_exe(), "-u", str(MAIN_PY)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            # CREATE_NEW_PROCESS_GROUP makes the child its own console group
            # so we can send signal.CTRL_BREAK_EVENT for graceful shutdown
            # without also signalling the GUI itself.
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="bot-stdout-reader", daemon=True,
        )
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.log_queue.put(line)
        except Exception as e:
            self.log_queue.put(f"[gui] reader error: {e!r}\n")
        finally:
            rc = self.proc.wait() if self.proc else "?"
            self.log_queue.put(f"\n[gui] процесс завершён, exit code = {rc}\n")
            self.log_queue.put(None)  # sentinel

    def stop(self, force: bool = False) -> None:
        if not self.is_running():
            return
        assert self.proc is not None
        if force:
            self.proc.kill()
        else:
            self.proc.terminate()

    def stop_graceful_windows(self) -> bool:
        """Send Ctrl-Break to the bot process group (Windows-only).

        Returns True if the signal was successfully posted; False if not
        running or the call failed. The caller is expected to poll
        `is_running()` afterwards and fall back to `stop(force=True)` on
        timeout. main.py installs a SIGBREAK handler (see _handle_signal)
        which triggers orchestrator shutdown — including kill of any
        running translator subprocesses.
        """
        if not self.is_running():
            return False
        assert self.proc is not None
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            return True
        except (ValueError, OSError):
            return False


# ---------------------------------------------------------------------------
# Clipboard support: Ctrl+C/V/X/A + context menu в Entry/Text.
#
# Tkinter ловит Ctrl+letter по keysym, поэтому при русской раскладке
# (Ctrl+С/М/Ч/Ф) штатные шорткаты НЕ срабатывают. Ниже — bind по
# event.keycode (физическая клавиша), он не зависит от раскладки.
# ---------------------------------------------------------------------------

_KEYCODE_VIRTUAL = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>"}  # C / V / X
_KEYCODE_SELECT_ALL = 65  # A


def _widget_is_text(w: tk.Misc) -> bool:
    try:
        return w.winfo_class() == "Text"
    except tk.TclError:
        return False


def _widget_is_entry(w: tk.Misc) -> bool:
    try:
        return w.winfo_class() in ("TEntry", "Entry")
    except tk.TclError:
        return False


def _widget_is_editable(w: tk.Misc) -> bool:
    try:
        state = str(w.cget("state"))
    except tk.TclError:
        return True
    return state not in ("disabled", "readonly")


def _select_all(w: tk.Misc) -> None:
    try:
        if _widget_is_entry(w):
            w.select_range(0, "end")
            w.icursor("end")
        elif _widget_is_text(w):
            w.tag_add("sel", "1.0", "end-1c")
            w.mark_set("insert", "end-1c")
        w.focus_set()
    except tk.TclError:
        pass


def _on_ctrl_keypress(event: tk.Event) -> str | None:
    virtual = _KEYCODE_VIRTUAL.get(event.keycode)
    if virtual:
        try:
            event.widget.event_generate(virtual)
        except tk.TclError:
            return None
        return "break"
    if event.keycode == _KEYCODE_SELECT_ALL:
        _select_all(event.widget)
        return "break"
    return None


class ContextMenu:
    """Right-click меню Cut/Copy/Paste/Select All для Entry и Text."""

    def __init__(self, root: tk.Misc) -> None:
        self.target: tk.Misc | None = None
        m = tk.Menu(root, tearoff=False)
        m.add_command(label="Вырезать",     command=lambda: self._gen("<<Cut>>"))
        m.add_command(label="Копировать",   command=lambda: self._gen("<<Copy>>"))
        m.add_command(label="Вставить",     command=lambda: self._gen("<<Paste>>"))
        m.add_separator()
        m.add_command(label="Выделить всё", command=self._select_all)
        self.menu = m

    def _gen(self, virtual: str) -> None:
        if self.target is None:
            return
        try:
            self.target.event_generate(virtual)
        except tk.TclError:
            pass

    def _select_all(self) -> None:
        if self.target is not None:
            _select_all(self.target)

    def show(self, event: tk.Event) -> None:
        w = event.widget
        self.target = w
        try:
            w.focus_set()
        except tk.TclError:
            pass
        editable = _widget_is_editable(w)
        self.menu.entryconfig(0, state=("normal" if editable else "disabled"))  # Cut
        self.menu.entryconfig(1, state="normal")                                # Copy
        self.menu.entryconfig(2, state=("normal" if editable else "disabled"))  # Paste
        self.menu.entryconfig(4, state="normal")                                # Select All
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


def _install_clipboard_support(root: tk.Misc) -> None:
    """Прописать Ctrl+C/V/X/A + right-click меню для всех Entry и Text окна."""
    for cls in ("TEntry", "Entry", "Text"):
        root.bind_class(cls, "<Control-KeyPress>", _on_ctrl_keypress)
    ctx = ContextMenu(root)
    for cls in ("TEntry", "Entry", "Text"):
        root.bind_class(cls, "<Button-3>", ctx.show)
    # держим ссылку на меню, чтобы GC не сожрал
    root._zohar_ctx_menu = ctx  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("zohar-translator")
        root.geometry("960x680")
        root.minsize(720, 480)

        self.log_queue: queue.Queue[str | None] = queue.Queue()
        self.bot = BotProcess(self.log_queue)
        self.current_state: str = "—"  # parsed from state/current_run.json

        # logs/gui_YYMMDD.log — точная копия того, что показывается в виджете
        # логов. При каждой строке открываем/пишем/закрываем — чтобы файл не
        # был залочен и его можно было читать другими редакторами на лету.
        # Файл за день append-ится между запусками GUI, поэтому история не
        # теряется при перезапуске.
        log_file = LOGS_DIR / f"gui_{time.strftime('%y%m%d')}.log"
        self.log_path: Path | None = log_file
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    f"[gui] === ZoharTGBatch GUI started "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
        except Exception as e:
            self.log_path = None
            messagebox.showwarning(
                "zohar-translator",
                f"Не могу открыть {log_file} для записи:\n{e}\n"
                "Лог в виджете работает, но в файл писаться не будет.",
            )

        _install_clipboard_support(root)
        self._build_ui()
        self._load_settings_into_ui()
        self._refresh_buttons()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_LOG_MS, self._pump_log)
        root.after(POLL_STATE_MS, self._poll_state)

    # ----- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_run = ttk.Frame(nb)
        self.tab_cfg = ttk.Frame(nb)
        nb.add(self.tab_run, text=" Запуск ")
        nb.add(self.tab_cfg, text=" Настройки .env ")

        self._build_run_tab(self.tab_run)
        self._build_cfg_tab(self.tab_cfg)

    def _build_run_tab(self, frm: ttk.Frame) -> None:
        # Top: parallel translators + state label + buttons
        top = ttk.Frame(frm)
        top.pack(fill="x", padx=4, pady=4)

        ttk.Label(top, text="Параллельных переводчиков:").grid(
            row=0, column=0, sticky="w", padx=(0, 6),
        )
        self.parallel_var = tk.StringVar()
        self.parallel_entry = ttk.Entry(top, textvariable=self.parallel_var, width=6)
        self.parallel_entry.grid(row=0, column=1, sticky="w")
        self.parallel_entry.bind("<FocusOut>", lambda _e: self._commit_parallel())
        self.parallel_entry.bind("<Return>", lambda _e: self._commit_parallel())
        ttk.Label(top, text=f"({PARALLEL_MIN}–{PARALLEL_MAX})").grid(
            row=0, column=2, sticky="w", padx=(4, 16),
        )

        ttk.Label(top, text="Состояние оркестратора:").grid(
            row=0, column=3, sticky="e", padx=(8, 4),
        )
        self.state_var = tk.StringVar(value="—")
        self.state_label = ttk.Label(
            top, textvariable=self.state_var, width=14, anchor="w",
        )
        self.state_label.grid(row=0, column=4, sticky="w")

        self.btn_start = ttk.Button(top, text="Start", command=self._on_start, width=10)
        self.btn_start.grid(row=0, column=5, padx=(16, 4))
        self.btn_stop = ttk.Button(top, text="Stop", command=self._on_stop, width=10)
        self.btn_stop.grid(row=0, column=6, padx=(0, 0))

        top.columnconfigure(3, weight=1)

        # Log area
        log_frm = ttk.LabelFrame(frm, text="Логи (с момента запуска приложения)")
        log_frm.pack(fill="both", expand=True, padx=4, pady=(4, 4))

        mono = tkfont.nametofont("TkFixedFont").actual()
        self.log_text = tk.Text(
            log_frm,
            wrap="none",
            state="disabled",
            font=(mono["family"], 9),
            background="#1e1e1e",
            foreground="#e0e0e0",
            insertbackground="#e0e0e0",
        )
        ysb = ttk.Scrollbar(log_frm, orient="vertical", command=self.log_text.yview)
        xsb = ttk.Scrollbar(log_frm, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        log_frm.rowconfigure(0, weight=1)
        log_frm.columnconfigure(0, weight=1)

        # Status bar (process state)
        bar = ttk.Frame(frm)
        bar.pack(fill="x", padx=4, pady=(0, 2))
        self.proc_var = tk.StringVar(value="Бот не запущен")
        ttk.Label(bar, textvariable=self.proc_var, foreground="#888").pack(
            side="left",
        )

    def _build_cfg_tab(self, frm: ttk.Frame) -> None:
        # Scrollable area for arbitrary number of keys
        container = ttk.Frame(frm)
        container.pack(fill="both", expand=True, padx=4, pady=4)

        canvas = tk.Canvas(container, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.cfg_inner = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=self.cfg_inner, anchor="nw")

        def _on_inner_config(_e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
        self.cfg_inner.bind("<Configure>", _on_inner_config)

        def _on_canvas_config(e: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=e.width)
        canvas.bind("<Configure>", _on_canvas_config)

        # mousewheel scrolling
        def _on_wheel(e: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        # Buttons bar
        btn_bar = ttk.Frame(frm)
        btn_bar.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(btn_bar, text="Save", command=self._on_save_env, width=12).pack(
            side="right", padx=(4, 0),
        )
        ttk.Button(btn_bar, text="Restore", command=self._on_restore_env, width=12).pack(
            side="right",
        )
        self.cfg_status_var = tk.StringVar(value="")
        ttk.Label(btn_bar, textvariable=self.cfg_status_var, foreground="#888").pack(
            side="left",
        )

        # field vars filled in _load_settings_into_ui
        self.cfg_vars: dict[str, tk.StringVar] = {}

    # ----- settings I/O ---------------------------------------------------

    def _load_settings_into_ui(self) -> None:
        try:
            _, values = read_env_file()
        except Exception as e:
            messagebox.showerror("zohar-translator", f"Не могу прочитать .env:\n{e}")
            return

        # Tab 1: parallel translators
        self.parallel_var.set(values.get(PARALLEL_KEY, "10"))

        # Tab 2: all other keys (preserve order from .env)
        for w in self.cfg_inner.winfo_children():
            w.destroy()
        self.cfg_vars.clear()

        lines, _ = read_env_file()
        row = 0
        for el in lines:
            if el.kind != "kv":
                continue
            if el.key == PARALLEL_KEY:
                continue
            ttk.Label(self.cfg_inner, text=el.key).grid(
                row=row, column=0, sticky="w", padx=(2, 8), pady=2,
            )
            var = tk.StringVar(value=el.value)
            self.cfg_vars[el.key] = var
            ent = ttk.Entry(self.cfg_inner, textvariable=var)
            ent.grid(row=row, column=1, sticky="ew", padx=(0, 2), pady=2)
            row += 1
        self.cfg_inner.columnconfigure(1, weight=1)

    def _commit_parallel(self) -> None:
        raw = self.parallel_var.get().strip()
        try:
            n = int(raw)
        except ValueError:
            messagebox.showerror(
                "zohar-translator",
                f"PARALLEL_TRANSLATORS должно быть целым в [{PARALLEL_MIN},{PARALLEL_MAX}]",
            )
            self._reload_parallel_from_env()
            return
        if not (PARALLEL_MIN <= n <= PARALLEL_MAX):
            messagebox.showerror(
                "zohar-translator",
                f"PARALLEL_TRANSLATORS должно быть в [{PARALLEL_MIN},{PARALLEL_MAX}], получено {n}",
            )
            self._reload_parallel_from_env()
            return
        # Read current value from .env to avoid noop writes
        try:
            _, values = read_env_file()
            if values.get(PARALLEL_KEY, "") == str(n):
                self.parallel_var.set(str(n))  # normalize
                return
            write_env_file({PARALLEL_KEY: str(n)})
            self.parallel_var.set(str(n))
            self._append_log(f"[gui] .env: {PARALLEL_KEY}={n}\n")
        except Exception as e:
            messagebox.showerror("zohar-translator", f"Не могу записать .env:\n{e}")

    def _reload_parallel_from_env(self) -> None:
        try:
            _, values = read_env_file()
            self.parallel_var.set(values.get(PARALLEL_KEY, "10"))
        except Exception:
            pass

    def _on_save_env(self) -> None:
        updates = {k: v.get() for k, v in self.cfg_vars.items()}
        try:
            write_env_file(updates)
            self.cfg_status_var.set(f"Сохранено ({time.strftime('%H:%M:%S')})")
            self._append_log("[gui] .env: настройки сохранены\n")
        except Exception as e:
            messagebox.showerror("zohar-translator", f"Не могу записать .env:\n{e}")

    def _on_restore_env(self) -> None:
        self._load_settings_into_ui()
        self.cfg_status_var.set(f"Перечитано из .env ({time.strftime('%H:%M:%S')})")

    # ----- process control ------------------------------------------------

    def _on_start(self) -> None:
        if self.bot.is_running():
            return
        # auto-commit parallel first (in case user typed but didn't blur)
        self._commit_parallel()
        try:
            self.bot.start()
        except Exception as e:
            messagebox.showerror("zohar-translator", f"Не могу запустить main.py:\n{e}")
            return
        self._append_log(f"[gui] запуск: {_python_exe()} {MAIN_PY}\n")
        self._refresh_buttons()

    def _on_stop(self) -> None:
        if not self.bot.is_running():
            return
        if self.current_state != "IDLE":
            messagebox.showwarning(
                "zohar-translator",
                "Stop доступен только когда оркестратор в состоянии IDLE.\n"
                "Сначала нажми Stop или Force stop в Telegram-боте.",
            )
            return
        self._append_log("[gui] Stop → terminate()\n")
        self.bot.stop(force=False)
        self._refresh_buttons()

    def _on_close(self) -> None:
        if not self.bot.is_running():
            self._close_log_file()
            self.root.destroy()
            return
        ok = messagebox.askyesno(
            "zohar-translator",
            "Только завершить все работающие процессы?\n\n"
            "Бот сейчас работает. Закрытие окна отправит ему\n"
            "Ctrl-Break (graceful shutdown), подождёт до 30 сек,\n"
            "затем при необходимости убьёт принудительно.",
        )
        if not ok:
            return
        # Step 1: try graceful shutdown via Ctrl-Break to the bot process
        # group. main.py's SIGBREAK handler runs orchestrator shutdown,
        # which kills any translator subprocesses (claude.exe) cleanly.
        self._append_log("[gui] закрытие окна → Ctrl-Break (graceful, до 30s)\n")
        sent = False
        try:
            sent = self.bot.stop_graceful_windows()
        except Exception:
            pass
        if not sent:
            # Fallback path (signal post failed): go straight to kill.
            self._append_log("[gui] Ctrl-Break не доставлен → force kill\n")
            try:
                self.bot.stop(force=True)
            except Exception:
                pass
        else:
            # Poll for graceful exit. _read_loop will pump remaining
            # stdout; we just wait for the process to actually exit.
            deadline = time.time() + 30.0
            while time.time() < deadline and self.bot.is_running():
                time.sleep(0.2)
            if self.bot.is_running():
                self._append_log(
                    "[gui] 30s истекли, бот всё ещё жив → force kill\n"
                )
                try:
                    self.bot.stop(force=True)
                except Exception:
                    pass
        # Brief drain after kill (covers either branch).
        deadline = time.time() + 1.5
        while time.time() < deadline and self.bot.is_running():
            time.sleep(0.05)
        self._close_log_file()
        self.root.destroy()

    def _close_log_file(self) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    f"[gui] === GUI closed "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
        except Exception:
            pass
        self.log_path = None

    def _refresh_buttons(self) -> None:
        running = self.bot.is_running()
        idle = self.current_state == "IDLE"
        self.btn_start.configure(state=("disabled" if running else "normal"))
        self.btn_stop.configure(state=("normal" if (running and idle) else "disabled"))
        if running:
            self.proc_var.set(f"Бот запущен (PID {self.bot.proc.pid})")
        else:
            self.proc_var.set("Бот не запущен")

    # ----- background pumps ----------------------------------------------

    def _pump_log(self) -> None:
        drained = 0
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item is None:
                    # subprocess ended; clear handle so buttons update
                    self._refresh_buttons()
                    continue
                self._append_log(item)
                drained += 1
                if drained >= 500:  # avoid GUI stalls on bursty output
                    break
        except queue.Empty:
            pass
        # If process died but no sentinel arrived (shouldn't happen, but…),
        # still refresh buttons periodically.
        if not self.bot.is_running() and self.btn_start.cget("state") == "disabled":
            self._refresh_buttons()
        self.root.after(POLL_LOG_MS, self._pump_log)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        # Always autoscroll to the bottom on new lines (operator preference,
        # 2026-05-20). Previously was bounded to "only if already near
        # bottom" via last > 0.98, but that occasionally felt sticky.
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # mirror to gui.log — open/write/close на каждую запись, чтобы
        # файл не был залочен и читался редакторами в реальном времени
        if self.log_path is not None:
            try:
                with open(self.log_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            except Exception:
                pass

    def _poll_state(self) -> None:
        new_state = self._read_state_file()
        if new_state != self.current_state:
            self.current_state = new_state
            self.state_var.set(new_state)
            self._refresh_buttons()
        self.root.after(POLL_STATE_MS, self._poll_state)

    def _read_state_file(self) -> str:
        if not self.bot.is_running():
            return "—"
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return str(data.get("state", "—"))
        except Exception:
            return "—"


REQUIRED_IMPORTS = ("dotenv", "pydantic", "telegram")  # как в requirements.txt


def _try_imports(names: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    for name in names:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    return missing


def _ensure_deps(root: tk.Tk) -> bool:
    """Если main.py требует чего-то, чего нет в sys.executable —
    тихо ставим через `python -m pip install -r requirements.txt`,
    показывая прогресс."""
    missing = _try_imports(REQUIRED_IMPORTS)
    if not missing:
        return True

    req = PROJECT_ROOT / "requirements.txt"
    if not req.is_file():
        messagebox.showerror(
            "zohar-translator",
            f"Не хватает зависимостей: {', '.join(missing)}\n"
            f"И файл {req} отсутствует — не из чего ставить.",
        )
        return False

    dlg = tk.Toplevel(root)
    dlg.title("zohar-translator — установка зависимостей")
    dlg.geometry("560x180")
    dlg.transient(root)
    dlg.resizable(False, False)
    dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # нельзя закрыть пока ставится

    ttk.Label(
        dlg,
        text=(
            f"Не хватает зависимостей: {', '.join(missing)}.\n\n"
            f"Запускаю pip install -r requirements.txt в:\n{sys.executable}"
        ),
        wraplength=520,
        justify="left",
    ).pack(padx=12, pady=(12, 8), anchor="w")

    pb = ttk.Progressbar(dlg, mode="indeterminate", length=520)
    pb.pack(padx=12, pady=4)
    pb.start(50)

    status_var = tk.StringVar(value="Запуск pip…")
    ttk.Label(dlg, textvariable=status_var, foreground="#666").pack(
        padx=12, pady=(4, 12), anchor="w",
    )
    dlg.update_idletasks()

    result: dict[str, object] = {"ok": None, "output": ""}

    def runner() -> None:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
                cwd=str(PROJECT_ROOT),
            )
            result["ok"] = (r.returncode == 0)
            result["output"] = r.stdout or ""
        except Exception as e:
            result["ok"] = False
            result["output"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=runner, name="pip-install", daemon=True)
    th.start()

    def poll() -> None:
        if th.is_alive():
            # показываем последнюю строку вывода, если уже есть
            dlg.after(150, poll)
        else:
            pb.stop()
            status_var.set("Готово, проверяю импорты…" if result["ok"] else "Ошибка")
            dlg.update_idletasks()
            dlg.after(200, dlg.destroy)

    poll()
    root.wait_window(dlg)

    if not result["ok"]:
        tail = str(result["output"])[-3000:]
        messagebox.showerror(
            "zohar-translator",
            "pip install завершился с ошибкой.\n\nПоследний вывод:\n\n" + tail,
        )
        return False

    # перепроверим, что зависимости теперь действительно импортятся
    import importlib
    importlib.invalidate_caches()
    still_missing = _try_imports(REQUIRED_IMPORTS)
    if still_missing:
        messagebox.showerror(
            "zohar-translator",
            "pip отчитался об успехе, но импорт всё равно падает: "
            + ", ".join(still_missing)
            + f"\n\nЗапускайте установку зависимостей вручную:\n"
            + f'  "{sys.executable}" -m pip install -r requirements.txt',
        )
        return False
    return True


def main() -> int:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    root.withdraw()  # не показываем основное окно, пока не проверим deps
    if not _ensure_deps(root):
        root.destroy()
        return 1
    root.deiconify()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
