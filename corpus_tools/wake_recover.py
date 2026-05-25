"""
Recover the .batch/ state at the start of a meta wake-up.

Two cases this handles:

  (a) The previous session left run_batch.sh still alive (pid file
      exists, process responds to OpenProcess). The new session does
      NOT wait for it — it kills the stale tree and clears residue
      so its own cycle can start fresh.

  (b) run_batch.sh already finished cleanly (.done present) but the
      previous orchestrator never ran process_results.py (e.g. it
      died on a hit-limit retry-loop). Re-run process_results.py so
      report.json reflects reality before the wake-up message goes
      out.

Outcome printed to stdout (one line):
  STALE_KILLED    — stale .sh was alive and was terminated; residue cleared.
  SELF_HEAL       — orphan .done detected; process_results.py was run.
  OK              — nothing to do.

Exit code: always 0 (information is in stdout). Errors during cleanup
are logged to stderr but don't fail the script.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
BATCH = ROOT / '.batch'

PS_DETECT_KILL = (
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
    "}; exit 1 "
    "} else { exit 0 }"
)


def detect_and_kill_stale():
    """Returns True if any stale processes were killed."""
    cp = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command',
         PS_DETECT_KILL],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if cp.stdout:
        for line in cp.stdout.strip().splitlines():
            print(f'  {line}', file=sys.stderr)
    if cp.stderr:
        print(f'  PS-stderr: {cp.stderr.strip()}', file=sys.stderr)
    return cp.returncode == 1


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    # PID file is the cheap pre-check. If it doesn't exist, no stale .sh.
    pid_file = BATCH / 'run_batch.pid'
    done_file = BATCH / 'run_batch.done'
    manifest_file = BATCH / 'manifest.json'

    if pid_file.exists():
        if detect_and_kill_stale():
            # Clear residue so process_results.py self-heal below
            # doesn't pick up a half-done batch.
            for f in (done_file, pid_file, manifest_file):
                try:
                    if f.exists():
                        f.unlink()
                except Exception as e:
                    print(f'  could not remove {f.name}: {e}',
                          file=sys.stderr)
            print('STALE_KILLED')
            return 0
        # PID file existed but no live process — stale stale, just remove.
        try:
            pid_file.unlink()
        except Exception:
            pass

    # No alive .sh. Did a previous batch finish without process_results?
    if done_file.exists() and manifest_file.exists():
        cp = subprocess.run(
            ['python', 'Scripts/process_results.py'],
            cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
        if cp.stdout:
            print(cp.stdout.strip(), file=sys.stderr)
        if cp.stderr:
            print(cp.stderr.strip(), file=sys.stderr)
        print('SELF_HEAL')
        return 0

    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
