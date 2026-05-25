"""
Build a cross-chapter batch of articles to translate in one cycle.

Greedy selector starting from `--chapter`, walking the global chapter
order (sorted by book_index, chapter_order). Adds articles until
sum(cost_k) >= budget_k; allows one overshoot.

Cost model: cost_k = source_chars / DIVISOR (default 80).
Budget: budget_k = max(0, (TARGET - usage) / 100 * 1000).

Side effects:
  - Creates chapter_dir and skeleton progress.json for touched chapters.
  - Writes .batch/prompt_{NN:03d}_{Chapter}.txt for each selected article.
  - Writes .batch/manifest.json with batch metadata.

Usage:
    python corpus_tools/build_batch.py --chapter Vayeshev --usage 0
    python corpus_tools/build_batch.py --chapter Vayeshev --usage 17 --target 80 --divisor 80

Output: manifest JSON printed to stdout.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
CATALOG = ROOT / 'Source' / 'articles_catalog.json'
TRANSLATED = ROOT / 'Translated'
BATCH = ROOT / '.batch'
TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'translation_prompt.md'

# Feature flag: when set to 0, resumable behaviour is disabled — any pre-
# existing .md without done.flag is deleted before translation, exactly
# as build_batch behaved before the resumable refactor.
RESUMABLE = os.getenv('RESUMABLE_TRANSLATION', '1').strip() not in ('0', 'false', 'no', 'off')

sys.path.insert(0, str(Path(__file__).resolve().parent))
from partial_state import inspect_partial  # noqa: E402


def load_template():
    text = TEMPLATE.read_text(encoding='utf-8')
    m = re.search(r'---\n\n```\n(.*?)\n```\s*$', text, re.DOTALL)
    if not m:
        raise RuntimeError('translation_prompt.md: could not extract prompt block')
    return m.group(1)


def render_resume_block(art, partial_info, chunk_budget_chars=7500):
    """Render the `{{resume_block}}` insert for translation_prompt.md.

    The block warns the translator that a partial file already exists,
    tells them which paragraph to continue from, and forbids overwriting
    the existing prefix. Returns '' for state='absent'.
    """
    state = partial_info['state']
    nn = art['article_index']
    chapter_dir = f"Translated/{art['book']}/{art['chapter_ru']}"
    md_rel = f"{chapter_dir}/{nn:03d}.md"
    if state == 'absent':
        return ''
    if state == 'partial':
        next_start = partial_info['next_start']
        already = partial_info['translated_count']
        end = art['sulam_paragraphs'][1]
        return (
            f"\n## ВНИМАНИЕ: РЕЖИМ ДОПЕРЕВОДА (resumable)\n\n"
            f"Файл `{md_rel}` уже существует и содержит "
            f"{already} ранее переведённых параграфов "
            f"({art['sulam_paragraphs'][0]}..{next_start - 1}).\n\n"
            f"- НЕ перезаписывай файл и НЕ дублируй заголовок.\n"
            f"- Прочитай существующий файл и пропусти Шаг 4.1 (Write "
            f"заголовка) — заголовок уже там.\n"
            f"- Дописывай оставшиеся параграфы Bash heredoc append "
            f"(см. правила Шага 4), начиная с параграфа {next_start} "
            f"и до параграфа {end} включительно. Размер чанков выбирай "
            f"по правилу Шага 4 (budget {chunk_budget_chars} символов "
            f"источника) от ОСТАВШИХСЯ параграфов "
            f"({end - next_start + 1} шт.).\n"
            f"- В конце проверь, что в файле ровно "
            f"{end - art['sulam_paragraphs'][0] + 1} параграфов "
            f"(regex `^(\\d+)\\)`), и только тогда ставь done.flag.\n"
        )
    if state == 'complete':
        end = art['sulam_paragraphs'][1]
        return (
            f"\n## ВНИМАНИЕ: ФАЙЛ УЖЕ ПОЛНЫЙ (resumable)\n\n"
            f"Файл `{md_rel}` уже содержит все параграфы "
            f"{art['sulam_paragraphs'][0]}..{end} (предыдущий запуск "
            f"закончил перевод, но не успел поставить done.flag).\n\n"
            f"- НЕ перезаписывай файл.\n"
            f"- Прочитай файл, убедись что в нём ровно "
            f"{end - art['sulam_paragraphs'][0] + 1} параграфов, "
            f"и сразу ставь done.flag (Шаг 5).\n"
        )
    # 'corrupted' — caller deleted the .md already; treat as 'absent'.
    return ''


def render_prompt(tpl, art, partial_info=None, chunk_budget_chars=7500):
    resume_block = (
        render_resume_block(art, partial_info, chunk_budget_chars)
        if partial_info else ''
    )
    out = tpl.replace('{{NN:03d}}', f"{art['article_index']:03d}")
    subs = {
        'NN': str(art['article_index']),
        'Chapter': art['chapter'],
        'chapter_ru': art['chapter_ru'],
        'book': art['book'],
        'sulam_section': art['sulam_section'],
        'chapter_dir': f"Translated/{art['book']}/{art['chapter_ru']}",
        'start': str(art['sulam_paragraphs'][0]),
        'end': str(art['sulam_paragraphs'][1]),
        'he_title': art['he_title'],
        'resume_block': resume_block,
        'chunk_budget_chars': str(chunk_budget_chars),
        'HEB_ROOT': str(ROOT).replace('\\', '/'),
        'CORPUS_TOOLS': str(Path(__file__).resolve().parent).replace('\\', '/'),
        'REPO_ROOT': str(Path(__file__).resolve().parents[1]).replace('\\', '/'),
        'GLOSSARY_PATH': str(Path(__file__).resolve().parents[1] / 'glossary' / 'glossary.json').replace('\\', '/'),
    }
    for k, v in subs.items():
        out = out.replace('{{' + k + '}}', v)
    leftover = re.findall(r'\{\{[^}]+\}\}', out)
    if leftover:
        raise RuntimeError(f'unresolved placeholders: {leftover}')
    return out


def ensure_chapter_progress(book, chapter_ru, chapter_en, book_index, total):
    cdir = TRANSLATED / book / chapter_ru
    cdir.mkdir(parents=True, exist_ok=True)
    pfile = cdir / 'progress.json'
    if not pfile.exists():
        skeleton = {
            'book': book,
            'book_index': book_index,
            'chapter': chapter_en,
            'chapter_ru': chapter_ru,
            'total_articles': total,
            'completed': [],
            'failed': [],
        }
        pfile.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2),
                         encoding='utf-8')
        return skeleton
    return json.loads(pfile.read_text(encoding='utf-8'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--chapter', required=True, help='English chapter name, e.g. Vayeshev')
    ap.add_argument('--usage', type=float, default=0.0, help='Current session usage percent')
    ap.add_argument('--target', type=float, default=80.0)
    ap.add_argument('--divisor', type=float, default=95.6,
                    help='cost_k = chars / divisor (calibrated for headless Opus 4.7)')
    ap.add_argument('--unlimited', action='store_true',
                    help='No budget cap; pack all remaining articles of the chapter and beyond.')
    ap.add_argument('--chunk-budget-chars', type=int,
                    default=int(os.getenv('CHUNK_BUDGET_CHARS', '7500')),
                    help='Source-character budget per chunk in Шаг 4 of the prompt. '
                         'Default: CHUNK_BUDGET_CHARS env or 7500.')
    args = ap.parse_args()
    if not (1000 <= args.chunk_budget_chars <= 50000):
        raise SystemExit(
            f'--chunk-budget-chars must be in [1000, 50000], got {args.chunk_budget_chars}'
        )

    with CATALOG.open(encoding='utf-8') as f:
        cat = json.load(f)

    # Build unique ordered chapter list
    seen = {}
    for a in cat:
        key = (a['book_index'], a['chapter_order'])
        if key not in seen:
            seen[key] = a['chapter']
    ordered = sorted(seen.keys())
    chapter_order_list = [seen[k] for k in ordered]

    if args.chapter not in chapter_order_list:
        manifest = {'status': 'error',
                    'reason': f'Chapter {args.chapter} not found in catalog',
                    'articles': [], 'chapters_touched': []}
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 2

    start_idx = chapter_order_list.index(args.chapter)
    consider = chapter_order_list[start_idx:]

    if args.unlimited:
        budget_k = float('inf')
    else:
        budget_k = max(0.0, (args.target - args.usage) / 100.0 * 1000.0)
        if budget_k <= 0:
            manifest = {'status': 'skip',
                        'reason': f'usage {args.usage}% >= target {args.target}%',
                        'articles': [], 'chapters_touched': [],
                        'budget_k': budget_k}
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0

    BATCH.mkdir(parents=True, exist_ok=True)
    # Clean previous batch files
    for f in BATCH.glob('prompt_*.txt'):
        f.unlink()
    for f in BATCH.glob('result_*.json'):
        f.unlink()
    for f in BATCH.glob('stderr_*.log'):
        f.unlink()
    for f in BATCH.glob('done_*.flag'):
        f.unlink()
    for f in ['run_batch.sh', 'run_batch.log', 'run_batch.done',
              'manifest.json', 'order.txt']:
        p = BATCH / f
        if p.exists():
            p.unlink()

    tpl = load_template()
    articles_by_chapter = {}
    for a in cat:
        articles_by_chapter.setdefault(a['chapter'], []).append(a)

    selected = []
    chapters_touched = []
    cost_sum = 0.0
    stop = False
    for chap in consider:
        if stop:
            break
        articles = sorted(articles_by_chapter[chap],
                          key=lambda x: x['article_index'])
        meta = articles[0]
        prog = ensure_chapter_progress(meta['book'], meta['chapter_ru'],
                                       chap, meta['book_index'], len(articles))
        completed_set = set(prog.get('completed', []))
        remaining = [a for a in articles if a['article_index'] not in completed_set]
        if not remaining:
            # chapter already complete; move to next
            continue
        touched_this_chapter = False
        for a in remaining:
            # Resumable: check for an on-disk partial / complete-without-flag
            # before costing. If corrupted — wipe so translator starts fresh.
            partial_info = None
            cdir = TRANSLATED / a['book'] / a['chapter_ru']
            md_path = cdir / f"{a['article_index']:03d}.md"
            if RESUMABLE:
                start_p, end_p = a['sulam_paragraphs']
                partial_info = inspect_partial(
                    md_path, start_p, end_p,
                    expected_article_index=a['article_index'],
                    run_validator=False,
                )
                if partial_info['state'] == 'corrupted':
                    try:
                        md_path.unlink()
                    except FileNotFoundError:
                        pass
                    # After deletion the file is absent; treat as fresh.
                    partial_info = {
                        'state': 'absent', 'next_start': start_p,
                        'translated_count': 0, 'last': None,
                        'reason': f'cleaned_corrupted: {partial_info["reason"]}',
                    }
            else:
                # Feature flag off: legacy behaviour. If a stray .md exists
                # without done.flag, wipe it so translator restarts clean.
                flag_path = BATCH / f"done_{a['article_index']:03d}_{chap}.flag"
                if md_path.exists() and not flag_path.exists():
                    try:
                        md_path.unlink()
                    except FileNotFoundError:
                        pass
                partial_info = {
                    'state': 'absent',
                    'next_start': a['sulam_paragraphs'][0],
                    'translated_count': 0, 'last': None, 'reason': None,
                }

            cost_k = a['source_chars'] / args.divisor
            if cost_sum + cost_k > budget_k and selected:
                # Would exceed — stop after adding this "tail" is allowed;
                # but we stop BEFORE adding if we already have at least one.
                stop = True
                break
            selected.append({
                **a,
                'cost_k': round(cost_k, 2),
                '_partial_info': partial_info,
            })
            cost_sum += cost_k
            touched_this_chapter = True
            if cost_sum >= budget_k:
                stop = True
                break
        if touched_this_chapter:
            chapters_touched.append({
                'chapter': chap, 'book': meta['book'],
                'chapter_ru': meta['chapter_ru'],
                'book_index': meta['book_index'],
                'chapter_order': meta['chapter_order'],
                'total_articles': len(articles),
                'completed_before': len(completed_set),
            })

    # Write prompts
    for a in selected:
        prompt = render_prompt(
            tpl, a,
            partial_info=a.get('_partial_info'),
            chunk_budget_chars=args.chunk_budget_chars,
        )
        fname = f"prompt_{a['article_index']:03d}_{a['chapter']}.txt"
        (BATCH / fname).write_text(prompt, encoding='utf-8')

    manifest = {
        'status': 'ok' if selected else 'empty',
        'chapter_start': args.chapter,
        'usage_start': args.usage,
        'budget_k': None if args.unlimited else round(budget_k, 2),
        'unlimited': bool(args.unlimited),
        'cost_sum_k': round(cost_sum, 2),
        'divisor': args.divisor,
        'articles': [{
            'article_index': a['article_index'],
            'chapter': a['chapter'],
            'book': a['book'],
            'chapter_ru': a['chapter_ru'],
            'sulam_section': a['sulam_section'],
            'sulam_paragraphs': a['sulam_paragraphs'],
            'he_title': a['he_title'],
            'source_chars': a['source_chars'],
            'cost_k': a['cost_k'],
            'batch_name': f"{a['article_index']:03d}_{a['chapter']}",
            'partial_state': a.get('_partial_info', {}).get('state', 'absent'),
            'partial_next_start': a.get('_partial_info', {}).get(
                'next_start', a['sulam_paragraphs'][0]),
            'partial_translated_count': a.get('_partial_info', {}).get(
                'translated_count', 0),
        } for a in selected],
        'chapters_touched': chapters_touched,
    }
    (BATCH / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    # Print only a compact summary to stdout. The full manifest may be
    # huge (1327 articles ≈ 122k tokens in --unlimited mode), and the
    # orchestrator agent does not need the whole list — downstream
    # scripts (run_batch.sh, mark_article_done.py, process_results.py)
    # read manifest.json from disk directly. Keeping stdout small
    # protects the agent context window (esp. on Sonnet 200k).
    summary = {
        'status': manifest['status'],
        'chapter_start': manifest['chapter_start'],
        'unlimited': manifest['unlimited'],
        'budget_k': manifest['budget_k'],
        'cost_sum_k': manifest['cost_sum_k'],
        'articles_count': len(manifest['articles']),
        'first_batch_name': (manifest['articles'][0]['batch_name']
                             if manifest['articles'] else None),
        'last_batch_name': (manifest['articles'][-1]['batch_name']
                            if manifest['articles'] else None),
        'chapters_touched': [c['chapter'] for c in manifest['chapters_touched']],
    }
    if manifest['status'] in ('skip', 'empty', 'error'):
        summary['reason'] = manifest.get('reason')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
