"""
Compute next_cursor = first chapter (by global book/chapter order) that
is not fully translated yet. Used by both controller (meta_orchestration
§2) and orchestrator (§5.4).

Output: JSON to stdout with fields:
{
  "next_cursor": "Vayeshev" | null,
  "book": "Берешит (Бытие)" | null,
  "chapter_ru": "Ваешев (И поселился)" | null,
  "book_index": 1,
  "chapter_order": 9,
  "total_articles": 25,
  "completed_count": 23,
  "done_chapters": 9,
  "total_chapters": 52,
  "done_articles": 417,
  "total_articles_corpus": 1780
}

If all chapters are done — `next_cursor` is null.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
CATALOG = ROOT / 'Source' / 'articles_catalog.json'
TRANSLATED = ROOT / 'Translated'


def main():
    with CATALOG.open(encoding='utf-8') as f:
        cat = json.load(f)

    chapters = {}
    for a in cat:
        key = (a['book_index'], a['chapter_order'])
        if key not in chapters:
            chapters[key] = {
                'chapter': a['chapter'],
                'chapter_ru': a['chapter_ru'],
                'book': a['book'],
                'book_index': a['book_index'],
                'chapter_order': a['chapter_order'],
                'total_articles': 0,
            }
        chapters[key]['total_articles'] += 1

    done_chapters = 0
    done_articles = 0
    total_articles_corpus = 0
    next_cursor = None

    for key in sorted(chapters.keys()):
        info = chapters[key]
        total_articles_corpus += info['total_articles']
        prog_file = TRANSLATED / info['book'] / info['chapter_ru'] / 'progress.json'
        completed = []
        if prog_file.exists():
            with prog_file.open(encoding='utf-8') as f:
                completed = json.load(f).get('completed', [])
        completed_count = len(completed)
        done_articles += completed_count
        if completed_count >= info['total_articles']:
            done_chapters += 1
        elif next_cursor is None:
            next_cursor = {**info, 'completed_count': completed_count}

    if next_cursor is None:
        out = {
            'next_cursor': None, 'book': None, 'chapter_ru': None,
            'book_index': None, 'chapter_order': None,
            'total_articles': None, 'completed_count': None,
        }
    else:
        out = {
            'next_cursor': next_cursor['chapter'],
            'book': next_cursor['book'],
            'chapter_ru': next_cursor['chapter_ru'],
            'book_index': next_cursor['book_index'],
            'chapter_order': next_cursor['chapter_order'],
            'total_articles': next_cursor['total_articles'],
            'completed_count': next_cursor['completed_count'],
        }
    out['done_chapters'] = done_chapters
    out['total_chapters'] = len(chapters)
    out['done_articles'] = done_articles
    out['total_articles_corpus'] = total_articles_corpus
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
