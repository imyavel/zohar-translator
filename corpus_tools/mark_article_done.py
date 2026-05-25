"""
Per-article progress.json updater. Called from run_batch.sh after each
`claude -p` returns, BEFORE the next article starts. This way if the
orchestrator dies mid-batch (session limit, OOM, etc.) progress.json
already reflects every article that finished cleanly.

Usage:
    python Scripts/mark_article_done.py <batch_name>

where <batch_name> is e.g. "019_Vayechi" — same as
manifest.articles[i].batch_name.

Success model: the translator MUST `touch .batch/done_<batch_name>.flag`
as the very last step of its protocol (translation_prompt.md §5),
ONLY after a successful write. Absence of the flag is the universal
failure signal — covers hit-limit, network errors, OOM, model refusal,
partial output, anything. We don't try to enumerate causes; we only
care whether the translator declared success.

The hit-limit regex is still consulted, but only to decide whether
to BREAK the whole batch (sibling translators will fail too) vs
log a per-article failure and continue.

Behaviour:
  - Read .batch/manifest.json to locate the article metadata.
  - If .batch/done_<batch_name>.flag exists → success branch:
      validate the .md (size + validate_translated_article.py +
      paragraph-count). On any anomaly → mark failed (rc=1).
      Otherwise → mark completed (rc=0).
  - Else (flag absent) → failure branch:
      look at result/stderr for "hit your limit". If present → rc=2
      (caller breaks loop). Otherwise → rc=1 (mark failed, continue).

Exit codes:
  0  ok (article added to completed)
  1  failed (added to failed; loop continues)
  2  hit-limit detected (loop should break)
  3  fatal (manifest missing, etc.)
  4  partial kept (valid partial .md on disk; nn stays in pending pool —
     not added to completed, not added to failed; next cycle resumes it)
"""
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
HIT_LIMIT_RX = re.compile(r"hit your limit", re.IGNORECASE)

RESUMABLE = os.getenv('RESUMABLE_TRANSLATION', '1').strip() not in (
    '0', 'false', 'no', 'off',
)
sys.path.insert(0, str(ROOT / 'Scripts'))
from partial_state import inspect_partial  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print('usage: mark_article_done.py <batch_name>', file=sys.stderr)
        return 3

    batch_name = sys.argv[1]
    manifest_path = BATCH / 'manifest.json'
    if not manifest_path.exists():
        print(f'manifest.json missing', file=sys.stderr)
        return 3

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    art = next((a for a in manifest['articles']
                if a['batch_name'] == batch_name), None)
    if art is None:
        print(f'article {batch_name} not in manifest', file=sys.stderr)
        return 3

    nn = art['article_index']
    expected = art['sulam_paragraphs'][1] - art['sulam_paragraphs'][0] + 1
    cdir = TRANSLATED / art['book'] / art['chapter_ru']
    md_path = cdir / f"{nn:03d}.md"
    log_path = cdir / f"log_{nn:03d}.txt"
    pfile = cdir / 'progress.json'
    flag_path = BATCH / f'done_{batch_name}.flag'

    # Success is signalled positively: translator must touch the flag
    # as its very last step (translation_prompt.md §5). Absence of the
    # flag = failure of some kind, regardless of cause.
    start_p, end_p = art['sulam_paragraphs']
    if not flag_path.exists():
        # Distinguish hit-limit (break the whole batch) from a one-off
        # local failure (just continue).
        signal_text = ''
        result_path = BATCH / f'result_{batch_name}.json'
        stderr_path = BATCH / f'stderr_{batch_name}.log'
        if result_path.exists():
            try:
                r = json.loads(result_path.read_text(encoding='utf-8'))
                signal_text += str(r.get('result', '')) + ' '
                signal_text += json.dumps(r.get('iterations', []),
                                          ensure_ascii=False)
            except Exception:
                try:
                    signal_text += result_path.read_text(encoding='utf-8',
                                                         errors='replace')
                except Exception:
                    pass
        if stderr_path.exists():
            try:
                signal_text += stderr_path.read_text(encoding='utf-8',
                                                     errors='replace')
            except Exception:
                pass

        hit_limit_seen = bool(HIT_LIMIT_RX.search(signal_text))

        # Resumable: inspect the .md before any destructive action.
        partial_info = None
        if RESUMABLE:
            partial_info = inspect_partial(
                md_path, start_p, end_p,
                expected_article_index=nn, run_validator=True,
            )

        # On hit-limit we DON'T touch progress.json — translator slot
        # stays in pending pool. With resumable=on we ALSO keep the
        # partial .md so the next cycle resumes from where we stopped.
        if hit_limit_seen:
            if RESUMABLE and partial_info and partial_info['state'] == 'partial':
                print(
                    f'{batch_name}: NO-FLAG + hit-limit + partial '
                    f'({partial_info["translated_count"]} pars kept) → break',
                    file=sys.stderr,
                )
            elif RESUMABLE and partial_info and partial_info['state'] == 'corrupted':
                if md_path.exists():
                    md_path.unlink()
                print(
                    f'{batch_name}: NO-FLAG + hit-limit + corrupted '
                    f'({partial_info["reason"]}) → wiped, break',
                    file=sys.stderr,
                )
            else:
                # Legacy fallback when resumable is off.
                if md_path.exists() and md_path.stat().st_size < 1000:
                    md_path.unlink()
                if log_path.exists() and log_path.stat().st_size < 200:
                    log_path.unlink()
                print(f'NO-FLAG + hit-limit on {batch_name} → break',
                      file=sys.stderr)
            return 2

        # No hit-limit. Resumable: keep valid partials, treat corrupted
        # as a real failure.
        if RESUMABLE and partial_info is not None:
            ps = partial_info['state']
            if ps == 'partial':
                print(
                    f'{batch_name}: NO-FLAG, partial kept '
                    f'({partial_info["translated_count"]} pars, '
                    f'resume from {partial_info["next_start"]}) → rc=4',
                    file=sys.stderr,
                )
                return 4
            if ps == 'complete':
                # File covers full range, only flag is missing. Promote
                # to success path below (mark completed).
                print(
                    f'{batch_name}: NO-FLAG but file complete '
                    f'({partial_info["translated_count"]} pars) → promote ok',
                    file=sys.stderr,
                )
                status = 'ok'
                # Skip the flag-present branch — jump directly to the
                # progress.json update below.
                # (We fall through past the else: clause via a flag.)
                goto_progress = True
            elif ps == 'corrupted':
                if md_path.exists():
                    md_path.unlink()
                if log_path.exists() and log_path.stat().st_size < 200:
                    log_path.unlink()
                print(
                    f'{batch_name}: NO-FLAG + corrupted '
                    f'({partial_info["reason"]}) → failed',
                    file=sys.stderr,
                )
                status = 'no_flag'
                goto_progress = True
            else:  # absent
                if log_path.exists() and log_path.stat().st_size < 200:
                    log_path.unlink()
                print(
                    f'{batch_name}: NO-FLAG, no .md → failed',
                    file=sys.stderr,
                )
                status = 'no_flag'
                goto_progress = True
        else:
            # Legacy mode: wipe small partials, mark failed.
            if md_path.exists() and md_path.stat().st_size < 1000:
                md_path.unlink()
            if log_path.exists() and log_path.stat().st_size < 200:
                log_path.unlink()
            print(f'NO-FLAG on {batch_name} → failed (continue)',
                  file=sys.stderr)
            status = 'no_flag'
            goto_progress = True
    else:
        goto_progress = True
        # Flag present → validate the actual artefact for sanity.
        status = 'ok'
        if not md_path.exists() or md_path.stat().st_size == 0:
            status = 'missing'
        else:
            cp = subprocess.run(
                ['python', 'Scripts/validate_translated_article.py', str(md_path)],
                cwd=ROOT, capture_output=True, text=True,
                encoding='utf-8', errors='replace')
            if cp.returncode != 0:
                status = 'invalid'
            else:
                try:
                    text = md_path.read_text(encoding='utf-8')
                    n = len(re.findall(r'(?m)^(\d+)\)', text))
                    if n < expected:
                        status = 'partial'
                except UnicodeDecodeError:
                    status = 'invalid'

        # Resumable: a partial file with flag set is a translator bug
        # (it touched the flag prematurely). Don't wipe — keep the
        # partial bytes, just don't mark completed. Return rc=4 so
        # orchestrator counts it as "kept for next cycle".
        if RESUMABLE and status == 'partial':
            try:
                flag_path.unlink()
            except FileNotFoundError:
                pass
            print(
                f'{batch_name}: flag set but partial '
                f'({n}/{expected}) → keep partial, rc=4',
                file=sys.stderr,
            )
            return 4

    if not goto_progress:
        # Defensive: should not happen — every branch sets goto_progress.
        return 1

    # Load existing progress.json (or skeleton)
    if pfile.exists():
        prog = json.loads(pfile.read_text(encoding='utf-8'))
    else:
        prog = {
            'book': art['book'],
            'book_index': manifest.get('chapters_touched', [{}])[0].get(
                'book_index', 0),
            'chapter': art['chapter'],
            'chapter_ru': art['chapter_ru'],
            'total_articles': next(
                (c['total_articles'] for c in manifest['chapters_touched']
                 if c['chapter'] == art['chapter']), 0),
            'completed': [],
            'failed': [],
        }

    completed = set(prog.get('completed', []))
    failed = set(prog.get('failed', []))

    total = prog.get('total_articles', 0)
    completed_before = len(completed)

    if status == 'ok':
        completed.add(nn)
        failed.discard(nn)
        rc = 0
    else:
        if md_path.exists():
            md_path.unlink()
        failed.add(nn)
        rc = 1

    prog['completed'] = sorted(completed)
    prog['failed'] = sorted(failed)
    import datetime as dt
    prog['last_session'] = dt.datetime.now().replace(
        microsecond=0).isoformat()
    pfile.write_text(json.dumps(prog, ensure_ascii=False, indent=2),
                     encoding='utf-8')
    print(f'{batch_name}: {status} ({len(completed)}/{total})')

    # If this article just closed the chapter — rebuild the site and
    # master_progress.md right now. This way a mid-batch death never
    # leaves the site stale w.r.t. completed chapters.
    chapter_just_closed = (
        status == 'ok'
        and total > 0
        and completed_before < total
        and len(completed) >= total
    )
    if chapter_just_closed:
        print(f'{batch_name}: chapter {art["chapter"]} closed → rebuilding site',
              file=sys.stderr)
        subprocess.run(['python', 'Scripts/build_master_progress.py'],
                       cwd=ROOT, check=False)
        subprocess.run(['python', 'Scripts/build_site.py'],
                       cwd=ROOT, check=False)
    return rc


if __name__ == '__main__':
    sys.exit(main())
