"""论文级记忆存储。

这里采用 SQLite 而不是向量数据库，目标是先保证阅读链和推荐链有稳定、
可测试的记忆接口。已在本模块接入可选的 embedding 语义召回（见
``services.llm.embedding_service``）：未配置密钥或调用失败时自动退回关键词
匹配，调用方接口保持不变。
"""

from __future__ import annotations

import array
import logging
import re
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Optional

from ...settings import get_settings

logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "论文", "用户", "助手", "问题", "回答",
})


def _tokens(text: str) -> set[str]:
    raw = _TOKEN_RE.sub(" ", str(text or "").lower())
    return {x for x in raw.split() if len(x) >= 2 and x not in _STOPWORDS}


def _vec_to_blob(vec: Iterable[float]) -> bytes:
    """float 序列 → float32 小端字节，用于 SQLite BLOB 存储。"""
    return array.array("f", (float(x) for x in vec)).tobytes()


def _blob_to_vec(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    try:
        a = array.array("f")
        a.frombytes(bytes(blob))
        return list(a)
    except Exception:
        return None


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    """点积；维度不一致时返回 0.0（向量已归一化，故等价 cosine）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(float(x) * float(y) for x, y in zip(a, b))


class MemoryStore:
    """保存论文范围内的 working/short/preference 等记忆片段。"""

    def __init__(self, db_path: str, embedder: object | None = None) -> None:
        self.db_path = str(db_path)
        self._embedder = embedder
        self.ensure_tables()

    def _get_embedder(self):
        """惰性获取 embedding 服务；测试可通过构造参数注入 fake。"""
        if self._embedder is None:
            from ..llm.embedding_service import get_embedding_service

            self._embedder = get_embedding_service()
        return self._embedder

    def _embed_model_name(self) -> str:
        return str(getattr(get_settings(), "embed_model_name", "") or "")

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_tables(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_memory (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  scope TEXT NOT NULL,
                  paper_id INTEGER,
                  kind TEXT NOT NULL,
                  content TEXT NOT NULL,
                  importance REAL NOT NULL DEFAULT 0.5,
                  shared INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_memory_scope "
                "ON paper_memory(scope, paper_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_memory_shared "
                "ON paper_memory(shared, created_at)"
            )
            # 迁移：为 embedding 语义召回增列（幂等，兼容已存在的库）
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(paper_memory)").fetchall()}
            if "embedding" not in cols:
                conn.execute("ALTER TABLE paper_memory ADD COLUMN embedding BLOB")
            if "embed_model" not in cols:
                conn.execute("ALTER TABLE paper_memory ADD COLUMN embed_model TEXT")
            if "embed_dim" not in cols:
                conn.execute("ALTER TABLE paper_memory ADD COLUMN embed_dim INTEGER")
            conn.commit()
        finally:
            conn.close()

    def add(
        self,
        *,
        scope: str,
        paper_id: int | None,
        kind: str,
        content: str,
        importance: float = 0.5,
        shared: bool = False,
    ) -> int | None:
        text = str(content or "").strip()
        if not text:
            return None
        scope2 = str(scope or "global").strip() or "global"
        kind2 = str(kind or "working").strip() or "working"
        now = int(time.time())
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO paper_memory
                  (scope, paper_id, kind, content, importance, shared, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope2,
                    int(paper_id) if paper_id is not None else None,
                    kind2,
                    text[:4000],
                    max(0.0, min(1.0, float(importance or 0.5))),
                    1 if shared else 0,
                    now,
                ),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        finally:
            conn.close()

        self._embed_and_store(row_id, text[:4000])
        return row_id

    def list_recent_contents(
        self,
        *,
        scope: str,
        paper_id: int | None,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[str]:
        limit2 = max(1, min(int(limit or 20), 500))
        kind_list = [str(x).strip() for x in (kinds or []) if str(x).strip()]
        where = ["scope = ?"]
        params: list[object] = [str(scope or "global")]
        if paper_id is None:
            where.append("paper_id IS NULL")
        else:
            where.append("paper_id = ?")
            params.append(int(paper_id))
        if kind_list:
            where.append("kind IN (" + ",".join("?" for _ in kind_list) + ")")
            params.extend(kind_list)
        params.append(limit2)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT content FROM paper_memory WHERE "
                + " AND ".join(where)
                + " ORDER BY importance DESC, created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [str(row["content"] or "").strip() for row in rows if str(row["content"] or "").strip()]
        finally:
            conn.close()

    def get_context_for_query(self, *, paper_id: int, query: str, limit: int = 6) -> str:
        limit2 = max(1, min(int(limit or 6), 30))
        q = str(query or "").strip()
        query_tokens = _tokens(q)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT content, importance, created_at, embedding, embed_model
                FROM paper_memory
                WHERE (scope = 'paper' AND paper_id = ?)
                   OR shared = 1
                ORDER BY importance DESC, created_at DESC, id DESC
                LIMIT 120
                """,
                (int(paper_id),),
            ).fetchall()
        finally:
            conn.close()

        # 语义查询向量：仅在有查询词且 embedding 可用时计算；失败则退回纯关键词
        qvec: Optional[list[float]] = None
        cur_model = ""
        if q:
            try:
                embedder = self._get_embedder()
                if embedder is not None and embedder.is_configured():
                    qvec = embedder.embed_query(q)
                    cur_model = self._embed_model_name()
            except Exception:
                logger.debug("memory_embed_query_failed", exc_info=True)
                qvec = None

        s = get_settings()
        kw_w = float(getattr(s, "papergraph_memory_kw_weight", 2.0))
        sem_w = float(getattr(s, "papergraph_memory_sem_weight", 3.0))

        scored: list[tuple[float, str]] = []
        now = int(time.time())
        for row in rows:
            content = str(row["content"] or "").strip()
            if not content:
                continue
            overlap = len(query_tokens & _tokens(content)) if query_tokens else 0
            age_days = max(0.0, (now - int(row["created_at"] or now)) / 86400.0)
            recency = max(0.0, 1.0 - age_days / 30.0)
            sem = 0.0
            if qvec is not None:
                row_vec = _blob_to_vec(row["embedding"])
                # 仅采用同模型写入的向量；维度不一致时 _dot 返回 0
                if row_vec is not None and str(row["embed_model"] or "") == cur_model:
                    sem = max(0.0, _dot(qvec, row_vec))
            score = overlap * kw_w + sem * sem_w + float(row["importance"] or 0.5) + recency * 0.1
            scored.append((score, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [content for _, content in scored[:limit2]]
        return "【论文记忆】\n" + "\n".join(f"- {x}" for x in selected) if selected else ""

    def build_context_block(self, *, paper_id: int, limit: int = 10) -> str:
        return self.get_context_for_query(paper_id=int(paper_id), query="", limit=limit)

    def _embed_and_store(self, row_id: int, text: str) -> None:
        """best-effort 为一行记忆生成并写入向量；失败静默（读时按关键词处理）。"""
        txt = str(text or "").strip()
        if not txt:
            return
        try:
            embedder = self._get_embedder()
            if embedder is None or not embedder.is_configured():
                return
            vecs = embedder.embed_texts([txt])
            if not vecs:
                return
            vec = vecs[0]
            model = self._embed_model_name()
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE paper_memory SET embedding = ?, embed_model = ?, embed_dim = ? WHERE id = ?",
                    (_vec_to_blob(vec), model, len(vec), int(row_id)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.debug("memory_embed_write_failed row_id=%s", row_id, exc_info=True)

    def backfill_embeddings(self, *, batch: int = 50, max_rows: Optional[int] = None) -> int:
        """为缺失或模型不匹配的旧记忆补写向量。返回成功写入的行数。

        整批 embedding 失败时中止（这些行下次仍可重试），避免死循环。
        """
        try:
            embedder = self._get_embedder()
        except Exception:
            return 0
        if embedder is None or not embedder.is_configured():
            return 0
        model = self._embed_model_name()
        batch = max(1, min(int(batch or 50), 200))
        processed = 0
        while True:
            if max_rows is not None and processed >= int(max_rows):
                break
            this_batch = batch
            if max_rows is not None:
                this_batch = max(1, min(batch, int(max_rows) - processed))
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, content FROM paper_memory "
                    "WHERE embedding IS NULL OR embed_model IS NULL OR embed_model != ? "
                    "ORDER BY id LIMIT ?",
                    (model, this_batch),
                ).fetchall()
            finally:
                conn.close()
            if not rows:
                break
            pairs = [(int(r["id"]), str(r["content"] or "").strip()) for r in rows]
            pairs = [(rid, txt) for rid, txt in pairs if txt]
            if not pairs:
                break
            vecs = embedder.embed_texts([txt for _, txt in pairs])
            if len(vecs) != len(pairs):
                logger.debug("memory_backfill_batch_failed rows=%s", len(pairs))
                break
            conn = self._connect()
            try:
                for (rid, _txt), vec in zip(pairs, vecs):
                    conn.execute(
                        "UPDATE paper_memory SET embedding = ?, embed_model = ?, embed_dim = ? WHERE id = ?",
                        (_vec_to_blob(vec), model, len(vec), rid),
                    )
                    processed += 1
                conn.commit()
            finally:
                conn.close()
            if len(rows) < this_batch:
                break
        return processed

    def extract_memory_via_llm(self, paper_id: int, user_message: str, assistant_reply: str) -> None:
        """保存一条稳定的问答摘要。

        这里不强制调用 LLM，避免记忆写入成为阅读请求的硬依赖；后续可替换为
        LLM 抽取器，保持方法签名不变。
        """
        user = str(user_message or "").strip()
        reply = str(assistant_reply or "").strip()
        if not user and not reply:
            return
        content = f"问答要点：{user[:240]} → {reply[:420]}"
        self.add(
            scope="paper",
            paper_id=int(paper_id),
            kind="short",
            content=content,
            importance=0.55,
        )

    def compress_working(self, *, scope: str, paper_id: int | None, min_entries: int = 6) -> None:
        """限制单个范围内 working 记忆的数量，保留最近和重要内容。"""
        keep = max(12, int(min_entries or 6) * 4)
        conn = self._connect()
        try:
            where = ["scope = ?", "kind = 'working'"]
            params: list[object] = [str(scope or "global")]
            if paper_id is None:
                where.append("paper_id IS NULL")
            else:
                where.append("paper_id = ?")
                params.append(int(paper_id))
            ids = conn.execute(
                "SELECT id FROM paper_memory WHERE "
                + " AND ".join(where)
                + " ORDER BY importance DESC, created_at DESC, id DESC LIMIT -1 OFFSET ?",
                [*params, keep],
            ).fetchall()
            if ids:
                conn.executemany("DELETE FROM paper_memory WHERE id = ?", [(int(row[0]),) for row in ids])
                conn.commit()
        finally:
            conn.close()
