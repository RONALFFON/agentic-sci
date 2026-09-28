"""为存量论文记忆回填 embedding 向量。

运行：
    python scripts/backfill_memory_embeddings.py
    python scripts/backfill_memory_embeddings.py --batch 32 --max-rows 500

用途：记忆层语义召回接入后，旧记忆的 embedding 列为空。本脚本分批调用
EmbeddingService 为「缺失向量或模型不匹配」的记忆补写向量。未配置
EMBED_API_KEY 时会直接跳过（不会报错），可安全重复执行。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.llm.embedding_service import get_embedding_service
from app.services.memory.memory_store import MemoryStore
from app.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="回填论文记忆的 embedding 向量")
    parser.add_argument(
        "--db-path",
        default=None,
        help="papers.db 路径；默认取 settings.data_dir/papers.db",
    )
    parser.add_argument("--batch", type=int, default=50, help="每批处理的记忆条数（1-200）")
    parser.add_argument("--max-rows", type=int, default=None, help="最多处理的条数；默认不限制")
    args = parser.parse_args()

    db_path = args.db_path or os.path.join(get_settings().data_dir, "papers.db")
    if not os.path.isfile(db_path):
        print(f"[skip] 数据库不存在：{db_path}")
        return 0

    svc = get_embedding_service()
    if not svc.is_configured():
        print("[skip] 未配置 embedding（EMBED_API_KEY 为空或 EMBED_ENABLED=false），无需回填。")
        return 0

    print(f"[info] 数据库：{db_path}")
    print(f"[info] 模型：{get_settings().embed_model_name} 维度：{get_settings().embed_dimensions}")

    store = MemoryStore(db_path)
    processed = store.backfill_embeddings(batch=args.batch, max_rows=args.max_rows)
    print(f"[done] 已回填 {processed} 条记忆向量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
