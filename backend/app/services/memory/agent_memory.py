"""Agent 间共享记忆的 SQLite 适配器。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ...settings import get_settings


_TOKEN_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)


class AgentMemory:
    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(get_settings().data_dir, "papers.db")
        self.db_path = str(Path(db_path))
        self._store = None

    @property
    def store(self):
        if self._store is None:
            from .memory_store import MemoryStore

            self._store = MemoryStore(self.db_path)
        return self._store

    def add(
        self,
        *,
        agent_name: str,
        content: str,
        memory_type: str = "working",
        importance: float = 0.5,
        shared: bool = False,
        **_: Any,
    ) -> int | None:
        return self.store.add(
            scope=f"agent:{str(agent_name or 'unknown').strip() or 'unknown'}",
            paper_id=None,
            kind=str(memory_type or "working"),
            content=content,
            importance=importance,
            shared=shared,
        )

    def recent(
        self,
        *,
        agent_name: str = "shared",
        memory_types: list[str] | None = None,
        limit: int = 20,
        shared: bool = False,
        **_: Any,
    ) -> list[str]:
        import sqlite3

        self.store.ensure_tables()
        kinds = [str(x).strip() for x in (memory_types or []) if str(x).strip()]
        where = ["1=1"]
        params: list[object] = []
        if shared or agent_name == "shared":
            where.append("shared = 1")
        else:
            where.append("(scope = ? OR shared = 1)")
            params.append(f"agent:{agent_name}")
        if kinds:
            where.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        params.append(max(1, min(int(limit or 20), 500)))
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT content FROM paper_memory WHERE "
                + " AND ".join(where)
                + " ORDER BY importance DESC, created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
        finally:
            conn.close()

    def build_context_block(self, *, agent_name: str, query: str = "", limit: int = 12) -> str:
        lines = self.recent(
            agent_name=agent_name,
            memory_types=["preference", "working", "episodic", "short"],
            limit=max(1, int(limit or 12)),
            shared=False,
        )
        if query:
            q_tokens = set(_TOKEN_RE.sub(" ", query.lower()).split())
            lines.sort(
                key=lambda x: len(q_tokens & set(_TOKEN_RE.sub(" ", x.lower()).split())),
                reverse=True,
            )
        return "【Agent 共享记忆】\n" + "\n".join(f"- {x}" for x in lines) if lines else ""

    def keywords_from_shared(self, *, limit_lines: int = 50, tokens_cap: int = 120) -> set[str]:
        texts = self.recent(agent_name="shared", memory_types=["working", "episodic"], limit=limit_lines, shared=True)
        freq: dict[str, int] = {}
        for text in texts:
            for token in _TOKEN_RE.sub(" ", text.lower()).split():
                if len(token) < 3:
                    continue
                freq[token] = freq.get(token, 0) + 1
        return {k for k, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:max(1, int(tokens_cap or 120))]}


_instances: dict[str, AgentMemory] = {}


def get_agent_memory(db_path: str | None = None) -> AgentMemory:
    resolved = str(db_path or os.path.join(get_settings().data_dir, "papers.db"))
    if resolved not in _instances:
        _instances[resolved] = AgentMemory(resolved)
    return _instances[resolved]
