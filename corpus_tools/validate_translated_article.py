"""
Validate (and auto-fix common issues) of a translated article file.

Usage:
    python Scripts/validate_translated_article.py <path/to/NNN.md>

Exit codes:
    0 — file is valid (possibly after auto-fix)
    1 — file invalid / cannot be auto-fixed

Checks:
    - File exists, size > 0.
    - First non-empty line starts with "# Статья NN. " (single hash).
    - Auto-fix: "## Статья" → "# Статья" (agents sometimes use level-2 heading).

Prints a JSON report (so orchestrator can parse).
"""

import json
import re
import sys
from pathlib import Path

ARTICLE_HEADING_RE = re.compile(r'^#+ Статья (\d+)\.')


def validate(path: Path) -> dict:
    report = {'path': str(path), 'fixes': [], 'valid': False, 'issues': []}
    if not path.exists():
        report['issues'].append('file_missing')
        return report
    size = path.stat().st_size
    report['size'] = size
    if size == 0:
        report['issues'].append('empty_file')
        return report

    text = path.read_text(encoding='utf-8')
    lines = text.split('\n')
    first = lines[0]

    # Auto-fix: ## Статья → # Статья
    m = ARTICLE_HEADING_RE.match(first)
    if m and first.startswith('## '):
        lines[0] = '# ' + first[3:]
        path.write_text('\n'.join(lines), encoding='utf-8')
        report['fixes'].append('demoted_heading_##_to_#')
        first = lines[0]

    # Strict check
    if not first.startswith('# Статья '):
        report['issues'].append(f'bad_heading: {first[:60]!r}')
        return report

    report['heading'] = first
    report['valid'] = True
    return report


def main():
    if len(sys.argv) != 2:
        print('Usage: validate_translated_article.py <path/NNN.md>', file=sys.stderr)
        sys.exit(2)
    path = Path(sys.argv[1])
    report = validate(path)
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(0 if report['valid'] else 1)


if __name__ == '__main__':
    main()
