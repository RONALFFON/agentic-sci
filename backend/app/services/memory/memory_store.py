"""论文级记忆存储。

这里采用 SQLite 而不是向量数据库，目标是先保证阅读链和推荐链有稳定、
可测试的记忆接口。后续可以在本模块上增加 embedding 检索，而不改变调用方。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path


_TOKEN_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "论文", "用户", "助手", "问题", "回答",
})


def _tokens(text: str) -> set[str]:
    raw = _TOKEN_RE.sub(" ", str(text or "").lower())
    return {x for x in raw.split() if len(x) >= 2 and x not in _STOPWORDS}


class MemoryStore:
    """保存论文范围内的 working/short/preference 等记忆片段。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self.ensure_tables()

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
            return int(cur.lastrowid)
        finally:
            conn.close()

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
        query_tokens = _tokens(query)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT content, importance, created_at
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

        scored: list[tuple[float, str]] = []
        now = int(time.time())
        for row in rows:
            content = str(row["content"] or "").strip()
            if not content:
                continue
            overlap = len(query_tokens & _tokens(content)) if query_tokens else 0
            age_days = max(0.0, (now - int(row["created_at"] or now)) / 86400.0)
            recency = max(0.0, 1.0 - age_days / 30.0)
            score = overlap * 2.0 + float(row["importance"] or 0.5) + recency * 0.1
            scored.append((score, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        selected = [content for _, content in scored[:limit2]]
        return "【论文记忆】\n" + "\n".join(f"- {x}" for x in selected) if selected else ""

    def build_context_block(self, *, paper_id: int, limit: int = 10) -> str:
        return self.get_context_for_query(paper_id=int(paper_id), query="", limit=limit)

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
