r"""
Kill any stale .batch/run_batch.sh left running by a previous session
and its claude -p translator children.

Called from src/orchestrator.py during PREPARING when a new wake-up
detects that the previous batch is still alive (PID file + alive
process; see ARCHITECTURE.md §9.3). The new
session does NOT wait for the old batch — it terminates it, cleans
the .batch/ residue, and starts its own cycle from the current
progress.json state.

Detection (filtered by process Name first to avoid self-matching):
  - bash.exe whose CommandLine contains "run_batch"
  - claude.exe whose CommandLine has " -p " + "bypassPermissions"
    (the translator headless invocation)

Idempotent: prints which PIDs were killed; safe to run when nothing
matches.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
BATCH = ROOT / '.batch'

PS_CMD = (
    "$targets = Get-CimInstance Win32_Process | Where-Object { "
    "$_.CommandLine -ne $null -and ("
    "($_.Name -eq 'bash.exe' -and $_.CommandLine -match 'run_batch') "
    "-or ($_.Name -eq 'claude.exe' "
    "-and $_.CommandLine -match ' -p ' "
    "-and $_.CommandLine -match 'bypassPermissions')"
    ") }; "
    "if ($targets) { "
    "$targets | ForEach-Object { "
    "Write-Output ('killed PID={0} {1}' -f $_.ProcessId, $_.Name); "
    "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
    "} "
    "} else { Write-Output 'no stale processes' }"
)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    cp = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', PS_CMD],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if cp.stdout:
        print(cp.stdout.strip())
    if cp.stderr:
        print('STDERR:', cp.stderr.strip(), file=sys.stderr)

    pid_file = BATCH / 'run_batch.pid'
    if pid_file.exists():
        pid_file.unlink()


if __name__ == '__main__':
    main()
