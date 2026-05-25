"""
Inspect a (possibly partial) translated .md file and report whether the
translator can resume from it, must restart from scratch, or already
finished.

Used by:
  - corpus_tools/build_batch.py — render `{{resume_block}}` into prompt.
  - corpus_tools/mark_article_done.py — decide between rc=4 (partial_kept)
    and rc=1 (corrupted → delete + failed).
  - src/orchestrator.py — `_preserve_partial_artifacts` keeps .md only if
    state == 'partial' or 'complete'.

State semantics:
  absent     — file does not exist or is empty.
  partial    — first paragraph is `start`, paragraphs continue without
               gaps/duplicates, last paragraph < end. Resume from
               `next_start = last + 1`.
  complete   — paragraphs cover the full [start..end] range. The
               translator can just `touch` the done.flag (validation
               will be re-run by mark_article_done).
  corrupted  — file exists but doesn't satisfy partial/complete contract:
               wrong starting number, gaps, duplicates, out-of-range
               numbers, validator failure, etc. Caller MUST delete the
               file and restart translation.

The returned dict has keys:
  state             — one of the above
  next_start        — int, only meaningful for state='partial'
  translated_count  — number of paragraphs already on disk
  last              — highest paragraph number found (or None)
  reason            — short string explaining corruption (or None)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
CORPUS_TOOLS = Path(__file__).resolve().parent

_PARA_RX = re.compile(r'(?m)^(\d+)\)')
_TITLE_RX = re.compile(r'^#\s+Статья\s+(\d+)\.', re.MULTILINE)


def inspect_partial(
    md_path: Path,
    start: int,
    end: int,
    *,
    expected_article_index: Optional[int] = None,
    run_validator: bool = True,
) -> dict:
    """Inspect a translated .md file vs the expected paragraph range.

    Args:
        md_path:                Path to {NN:03d}.md.
        start, end:             1-based inclusive sulam paragraph range.
        expected_article_index: If given, the file's `# Статья N.`
                                header must match this number.
        run_validator:          If True, also runs
                                corpus_tools/validate_translated_article.py.
                                Slower but catches formatting violations.

    Returns:
        dict with keys: state, next_start, translated_count, last, reason.
    """
    if not md_path.exists() or md_path.stat().st_size == 0:
        return {
            'state': 'absent',
            'next_start': start,
            'translated_count': 0,
            'last': None,
            'reason': None,
        }

    try:
        text = md_path.read_text(encoding='utf-8')
    except UnicodeDecodeError as e:
        return {
            'state': 'corrupted',
            'next_start': start,
            'translated_count': 0,
            'last': None,
            'reason': f'unicode_decode_error: {e}',
        }

    if not text.strip():
        return {
            'state': 'absent',
            'next_start': start,
            'translated_count': 0,
            'last': None,
            'reason': None,
        }

    if expected_article_index is not None:
        m = _TITLE_RX.search(text)
        if not m:
            return _corrupted(start, 'missing_article_header')
        if int(m.group(1)) != expected_article_index:
            return _corrupted(
                start,
                f'article_header_mismatch: got {m.group(1)}, '
                f'expected {expected_article_index}',
            )

    nums = [int(x) for x in _PARA_RX.findall(text)]
    if not nums:
        return _corrupted(start, 'no_paragraph_markers')

    if nums[0] != start:
        return _corrupted(
            start,
            f'wrong_first_paragraph: got {nums[0]}, expected {start}',
        )

    expected_seq = list(range(start, start + len(nums)))
    if nums != expected_seq:
        return _corrupted(
            start,
            f'non_continuous_or_duplicate: got {nums[:5]}..{nums[-3:]}',
        )

    last = nums[-1]
    if last > end:
        return _corrupted(
            start,
            f'overshoot: last={last} > end={end}',
        )

    translated_count = len(nums)
    expected_count = end - start + 1

    if last == end and translated_count == expected_count:
        if not text.endswith('\n'):
            return _corrupted(start, 'complete_but_no_trailing_newline')
        if run_validator:
            v = _run_validator(md_path)
            if v is not None:
                return _corrupted(start, f'validator_failed: {v}')
        return {
            'state': 'complete',
            'next_start': end + 1,
            'translated_count': translated_count,
            'last': last,
            'reason': None,
        }

    if not _ends_cleanly(text):
        return _corrupted(start, 'partial_file_truncated_mid_block')

    return {
        'state': 'partial',
        'next_start': last + 1,
        'translated_count': translated_count,
        'last': last,
        'reason': None,
    }


def _corrupted(start: int, reason: str) -> dict:
    return {
        'state': 'corrupted',
        'next_start': start,
        'translated_count': 0,
        'last': None,
        'reason': reason,
    }


def _ends_cleanly(text: str) -> bool:
    """Heuristic: a partial file should end after a fully-written paragraph
    block, not mid-sentence. We require the file to end with a newline,
    and the last paragraph body (after `N)`) to be non-empty.
    """
    if not text.endswith('\n'):
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    last_para_idx = None
    for i, ln in enumerate(lines):
        if _PARA_RX.match(ln):
            last_para_idx = i
    if last_para_idx is None:
        return False
    body = '\n'.join(lines[last_para_idx:])
    head_match = re.match(r'^(\d+)\)\s*(.*)', body, re.DOTALL)
    if not head_match:
        return False
    after_marker = head_match.group(2).strip()
    return bool(after_marker)


def _run_validator(md_path: Path) -> Optional[str]:
    """Return None on validator pass, else a short stderr tail."""
    try:
        cp = subprocess.run(
            [sys.executable,
             str(CORPUS_TOOLS / 'validate_translated_article.py'),
             str(md_path)],
            cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
    except Exception as e:
        return f'validator_invocation_failed: {e}'
    if cp.returncode == 0:
        return None
    tail = (cp.stderr or cp.stdout or '').strip().splitlines()
    return tail[-1] if tail else f'rc={cp.returncode}'


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument('md_path')
    ap.add_argument('start', type=int)
    ap.add_argument('end', type=int)
    ap.add_argument('--article-index', type=int, default=None)
    ap.add_argument('--no-validator', action='store_true')
    args = ap.parse_args()
    res = inspect_partial(
        Path(args.md_path), args.start, args.end,
        expected_article_index=args.article_index,
        run_validator=not args.no_validator,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0)
