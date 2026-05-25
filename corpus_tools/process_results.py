"""
Process headless translator results in .batch/ after run_batch.sh finishes.

For each article in manifest.json:
  - Parse result_{name}.json (headless stdout)
  - Detect hit-limit (in result.result text or stderr log)
  - Validate .md file via corpus_tools/validate_translated_article.py
  - Check paragraph count (regex ^(\d+)\))
  - Update per-chapter progress.json (completed/failed/last_session)
After all articles:
  - Rebuild the mini-site via build_site.py IF any chapter closed this cycle
  - Compute next_cursor via next_cursor.py logic

Output: orchestrator report (JSON) to stdout — matches the schema
consumed by src/orchestrator.py (status, chapters_touched,
chapters_done_this_cycle, articles_translated, articles_failed,
site_rebuilt, site_summary, next_cursor, hit_limit_reset, reason).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
TRANSLATED = ROOT / 'Translated'
BATCH = ROOT / '.batch'

RESUMABLE = os.getenv('RESUMABLE_TRANSLATION', '1').strip() not in (
    '0', 'false', 'no', 'off',
)
CORPUS_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(CORPUS_TOOLS))
from partial_state import inspect_partial  # noqa: E402
# Two reset-message variants emitted by claude-code:
#   5-hour (session) limit: "hit your limit · resets 6:40pm (Europe/Moscow)"
#   Weekly (Max plan)     : "hit your limit - resets Feb 4, 9pm (Africa/Johannesburg)"
# Weekly format ALSO contains time, but is preceded by month+day. We try
# weekly regex first (more specific). If it doesn't match — fall through
# to session regex.
HIT_LIMIT_RX_WEEKLY = re.compile(
    r"hit your limit.*?resets?\s+(\w+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE | re.DOTALL,
)
HIT_LIMIT_RX = re.compile(
    r"hit your limit.*?resets?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE | re.DOTALL,
)
_MONTH_ABBR = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def parse_hit_limit_reset(text):
    """Return reset-time as either:
       - ISO datetime "YYYY-MM-DDTHH:MM:SS" if message had date+time (weekly)
       - "HH:MM" string if message had only time (5-hour session)
       - None if neither pattern matched
    Orchestrator-side `_compute_next_wake_after_hit_limit` recognises both.
    """
    if not text:
        return None
    import datetime as _dt
    # Try weekly format first.
    m = HIT_LIMIT_RX_WEEKLY.search(text)
    if m:
        month_str = m.group(1).lower()[:3]
        if month_str in _MONTH_ABBR:
            month = _MONTH_ABBR[month_str]
            day = int(m.group(2))
            hour = int(m.group(3))
            minute = int(m.group(4)) if m.group(4) else 0
            ap = (m.group(5) or '').lower()
            if ap == 'pm' and hour < 12:
                hour += 12
            if ap == 'am' and hour == 12:
                hour = 0
            now = _dt.datetime.now()
            year = now.year
            try:
                cand = _dt.datetime(year, month, day, hour, minute)
                if cand <= now:
                    cand = _dt.datetime(year + 1, month, day, hour, minute)
                return cand.replace(microsecond=0).isoformat(timespec='seconds')
            except ValueError:
                pass
    # Fall through to session-format (HH:MM only).
    m = HIT_LIMIT_RX.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ap = (m.group(3) or '').lower()
    if ap == 'pm' and hour < 12:
        hour += 12
    if ap == 'am' and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def validate_md(md_path, expected_parags):
    if not md_path.exists() or md_path.stat().st_size == 0:
        return 'missing'
    cp = subprocess.run(
        [sys.executable,
         str(CORPUS_TOOLS / 'validate_translated_article.py'),
         str(md_path)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if cp.returncode != 0:
        return 'invalid'
    try:
        text = md_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return 'invalid'
    n = len(re.findall(r'(?m)^(\d+)\)', text))
    if n < expected_parags:
        return 'partial'
    return 'ok'


def load_json(p):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', default=str(BATCH / 'manifest.json'))
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        # Graceful exit so meta can call this unconditionally.
        print(json.dumps({'status': 'error',
                          'reason': 'manifest.json missing',
                          'articles_translated': 0,
                          'articles_failed': [],
                          'chapters_touched': []},
                         ensure_ascii=False, indent=2))
        return
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    articles = manifest['articles']
    touched_meta = {c['chapter']: c for c in manifest['chapters_touched']}

    hit_limit_reset = None
    articles_translated = 0
    articles_failed = []
    articles_partial = []  # resumable: kept on disk for next cycle
    now_iso = dt.datetime.now().replace(microsecond=0).isoformat()

    per_chapter_updates = {}  # chapter_en -> {completed: set, failed: set}

    articles_skipped_not_run = 0
    for a in articles:
        name = a['batch_name']
        chap = a['chapter']
        nn = a['article_index']
        expected = a['sulam_paragraphs'][1] - a['sulam_paragraphs'][0] + 1
        cdir = TRANSLATED / a['book'] / a['chapter_ru']
        md_path = cdir / f"{nn:03d}.md"
        result_path = BATCH / f'result_{name}.json'
        stderr_path = BATCH / f'stderr_{name}.log'
        flag_path = BATCH / f'done_{name}.flag'

        # If the article never started — no result_*.json AND no flag —
        # it's "pending, not yet attempted". Skip entirely so we don't
        # mark thousands of un-run articles as failures (typical case
        # in --unlimited mode after an early hit-limit break).
        if not flag_path.exists() and not result_path.exists():
            articles_skipped_not_run += 1
            continue

        updates = per_chapter_updates.setdefault(chap, {
            'completed': set(), 'failed': set(),
            'book': a['book'], 'chapter_ru': a['chapter_ru'],
        })

        start_p, end_p = a['sulam_paragraphs']
        partial_info = None
        if RESUMABLE:
            partial_info = inspect_partial(
                md_path, start_p, end_p,
                expected_article_index=nn, run_validator=False,
            )

        # Positive success signal: translator must have touched the
        # done flag in its final step. Absence = failure of any kind.
        if flag_path.exists():
            status = validate_md(md_path, expected)
            if status == 'ok':
                updates['completed'].add(nn)
                articles_translated += 1
            else:
                # Resumable: a partial file with flag set is kept for
                # next cycle (translator touched flag prematurely).
                if RESUMABLE and status == 'partial' and partial_info and \
                        partial_info['state'] == 'partial':
                    articles_partial.append({
                        'chapter': chap, 'nn': nn,
                        'translated': partial_info['translated_count'],
                        'next_start': partial_info['next_start'],
                        'reason': 'flag_set_but_partial_kept',
                    })
                    try:
                        flag_path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    if md_path.exists():
                        md_path.unlink()
                    updates['failed'].add(nn)
                    articles_failed.append({'chapter': chap, 'nn': nn,
                                            'reason': f'flag_set_but_{status}'})
            continue

        # Flag absent → some kind of failure. Try to differentiate
        # hit-limit (sets the global reset) from local failure.
        signal_text = ''
        result = load_json(result_path)
        if result:
            signal_text += str(result.get('result', '')) + ' '
            signal_text += json.dumps(result.get('iterations', []),
                                      ensure_ascii=False)
        if stderr_path.exists():
            try:
                signal_text += stderr_path.read_text(encoding='utf-8',
                                                     errors='replace')
            except Exception:
                pass

        reset = parse_hit_limit_reset(signal_text)
        if reset:
            hit_limit_reset = hit_limit_reset or reset
            # Hit-limit: leave article slot untouched in progress.json so
            # next cycle retries. With resumable=on, preserve any valid
            # partial .md (it'll be resumed next cycle); with resumable=off,
            # do the legacy small-file cleanup.
            if RESUMABLE and partial_info and \
                    partial_info['state'] in ('partial', 'complete'):
                articles_partial.append({
                    'chapter': chap, 'nn': nn,
                    'translated': partial_info['translated_count'],
                    'next_start': partial_info['next_start'],
                    'reason': 'hit_limit_partial_kept',
                })
            elif RESUMABLE and partial_info and \
                    partial_info['state'] == 'corrupted':
                if md_path.exists():
                    md_path.unlink()
            else:
                if md_path.exists() and md_path.stat().st_size < 1000:
                    md_path.unlink()
                log_path = cdir / f'log_{nn:03d}.txt'
                if log_path.exists() and log_path.stat().st_size < 200:
                    log_path.unlink()
            continue

        # No flag, no hit-limit signal. With resumable=on, valid partial
        # bytes are kept; only corrupted/absent become failures.
        if RESUMABLE and partial_info and partial_info['state'] == 'partial':
            articles_partial.append({
                'chapter': chap, 'nn': nn,
                'translated': partial_info['translated_count'],
                'next_start': partial_info['next_start'],
                'reason': 'no_flag_partial_kept',
            })
            continue
        if RESUMABLE and partial_info and partial_info['state'] == 'complete':
            # File covers full range but flag missing — validate and
            # promote to completed (parity with mark_article_done.py).
            v = validate_md(md_path, expected)
            if v == 'ok':
                updates['completed'].add(nn)
                articles_translated += 1
                continue
            # else: fall through to failure
        if md_path.exists():
            md_path.unlink()
        updates['failed'].add(nn)
        articles_failed.append({'chapter': chap, 'nn': nn,
                                'reason': 'no_flag'})

    # Merge per-chapter updates back into progress.json
    chapters_done_this_cycle = []
    chapters_touched_list = []
    for chap_en, meta in touched_meta.items():
        cdir = TRANSLATED / meta['book'] / meta['chapter_ru']
        pfile = cdir / 'progress.json'
        prog = load_json(pfile) or {
            'book': meta['book'],
            'book_index': meta['book_index'],
            'chapter': chap_en,
            'chapter_ru': meta['chapter_ru'],
            'total_articles': meta['total_articles'],
            'completed': [],
            'failed': [],
        }
        completed_before = meta['completed_before']
        total = meta['total_articles']

        upd = per_chapter_updates.get(chap_en, {'completed': set(), 'failed': set()})
        completed = set(prog.get('completed', [])) | upd['completed']
        failed = set(prog.get('failed', [])) - upd['completed']  # passing overrides old fail
        failed |= upd['failed']
        prog['completed'] = sorted(completed)
        prog['failed'] = sorted(failed)
        prog['last_session'] = now_iso
        pfile.write_text(json.dumps(prog, ensure_ascii=False, indent=2),
                         encoding='utf-8')

        chapters_touched_list.append({
            'chapter': chap_en,
            'book_index': meta['book_index'],
            'chapter_order': meta['chapter_order'],
        })
        if len(completed) >= total and completed_before < total:
            chapters_done_this_cycle.append({
                'chapter': chap_en,
                'book_index': meta['book_index'],
                'chapter_order': meta['chapter_order'],
            })

    # Sort outputs
    chapters_touched_list.sort(key=lambda x: (x['book_index'], x['chapter_order']))
    chapters_done_this_cycle.sort(key=lambda x: (x['book_index'], x['chapter_order']))

    # NOTE: master_progress.md and the static site are rebuilt
    # incrementally by mark_article_done.py whenever a chapter closes,
    # so we no longer rebuild them here. site_rebuilt reflects whether
    # any chapter closed this cycle (which means mark_article_done.py
    # already ran build_site.py).
    site_rebuilt = bool(chapters_done_this_cycle)
    site_summary = (f"Chapters done this cycle: "
                    f"{[c['chapter'] for c in chapters_done_this_cycle]}"
                    if chapters_done_this_cycle else None)

    # Compute next_cursor using the same logic as next_cursor.py
    nc_cp = subprocess.run([sys.executable,
                            str(CORPUS_TOOLS / 'next_cursor.py')],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
    nc = json.loads(nc_cp.stdout or '{}') if nc_cp.returncode == 0 else {}

    status = 'hit_limit' if hit_limit_reset else 'ok'
    report = {
        'status': status,
        'chapters_touched': [c['chapter'] for c in chapters_touched_list],
        'chapters_done_this_cycle': [c['chapter'] for c in chapters_done_this_cycle],
        'articles_translated': articles_translated,
        'articles_failed': articles_failed,
        'articles_partial': articles_partial,
        'articles_partial_count': len(articles_partial),
        'articles_skipped_not_run': articles_skipped_not_run,
        'site_rebuilt': site_rebuilt,
        'site_summary': site_summary,
        'next_cursor': nc.get('next_cursor'),
        'done_chapters': nc.get('done_chapters'),
        'total_chapters': nc.get('total_chapters'),
        'done_articles': nc.get('done_articles'),
        'total_articles_corpus': nc.get('total_articles_corpus'),
        'hit_limit_reset': hit_limit_reset,
        'reason': None,
    }
    (BATCH / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    # Compact stdout summary. The full report (with potentially long
    # articles_failed list) is on disk; the agent only needs the
    # essentials in its context.
    print(json.dumps({
        'status': status,
        'chapters_touched': report['chapters_touched'],
        'chapters_done_this_cycle': report['chapters_done_this_cycle'],
        'articles_translated': articles_translated,
        'articles_failed_count': len(articles_failed),
        'articles_partial_count': len(articles_partial),
        'articles_skipped_not_run': articles_skipped_not_run,
        'site_rebuilt': site_rebuilt,
        'next_cursor': nc.get('next_cursor'),
        'done_chapters': nc.get('done_chapters'),
        'total_chapters': nc.get('total_chapters'),
        'done_articles': nc.get('done_articles'),
        'total_articles_corpus': nc.get('total_articles_corpus'),
        'hit_limit_reset': hit_limit_reset,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
