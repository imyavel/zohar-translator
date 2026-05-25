"""
Poll .batch/ directory for batch completion.

Exit code 0 if .batch/run_batch.done exists (batch finished).
Exit code 1 otherwise (still waiting).

Stdout: one-line human summary.

Used by the orchestrator in a monitoring loop:
    while ! python corpus_tools/batch_status.py; do sleep 60; done
"""
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
BATCH = ROOT / ".batch"


def main():
    done_file = BATCH / 'run_batch.done'
    if done_file.exists():
        content = done_file.read_text(encoding='utf-8', errors='replace').strip()
        print(f'DONE {content}')
        return 0
    results = len(list(BATCH.glob('result_*.json')))
    prompts = len(list(BATCH.glob('prompt_*.txt')))
    print(f'WAITING {results}/{prompts}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
