"""只读导出本地真实论文评测草案；相关性标签必须经人工填写，不自动冒充金标。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def export_cases(db_path, limit=20):
    with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT id,title,year,doi,arxiv_id,category FROM papers ORDER BY id LIMIT ?', (limit,)).fetchall()
    return [{
        'id': f'library-{row["id"]}', 'kind': 'retrieval',
        'message': f'寻找与《{row["title"]}》研究主题相关的论文，包括早期方法和后续工作',
        'anchor_paper': dict(row), 'judgments': {},
        'review': '先确定候选池，以 DOI/arXiv ID 标识论文并人工标注 0..3 相关性；空标签不计入召回与排序指标。',
    } for row in rows]


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('数量必须在 1..100 内')
    cases = export_cases(args.db, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'已导出 {len(cases)} 个真实论文评测草案，需人工标注后评测。')


if __name__ == '__main__':
    main()
