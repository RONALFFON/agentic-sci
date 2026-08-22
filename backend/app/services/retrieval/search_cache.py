"""搜索结果缓存。

缓存保存最终 API 结果，而不是外部数据源的原始响应。这样缓存命中时可以
直接复现用户看到的论文顺序、解释和检索元数据，同时不绕过 SearchAgent 的
意图解析与参数规范化。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ...settings import get_settings


SEARCH_CACHE_VERSION = "search-result-v1"


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _norm_list(values: Any, *, sort: bool = True) -> list[str]:
    out = [_norm_text(x) for x in (values or []) if _norm_text(x)]
    return sorted(set(out)) if sort else out


class SearchResultCache:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        ttl_sec: int | None = None,
        max_entries: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        if db_path is None:
            db_path = os.path.join(settings.data_dir, "papers.db")
        self.db_path = str(db_path)
        self.ttl_sec = max(
            0,
            int(
                ttl_sec
                if ttl_sec is not None
                else getattr(settings, "papergraph_search_cache_ttl_sec", 86400)
            ),
        )
        self.max_entries = max(
            50,
            int(
                max_entries
                if max_entries is not None
                else getattr(settings, "papergraph_search_cache_max_entries", 2000)
            ),
        )
        self.enabled = bool(
            enabled
            if enabled is not None
            else getattr(settings, "papergraph_search_cache_enabled", True)
        )

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
                CREATE TABLE IF NOT EXISTS search_result_cache (
                  cache_key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  metadata_json TEXT,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  hit_count INTEGER NOT NULL DEFAULT 0,
                  last_hit_at INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_cache_expiry "
                "ON search_result_cache(expires_at, created_at)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def key_for_plan(plan: Any, *, mode: str, use_tavily: bool = False) -> str:
        settings = get_settings()
        model = (
            os.getenv("LLM_MODEL_ID")
            or os.getenv("OPENAI_MODEL")
            or getattr(settings, "openai_model", "")
        )
        identity = {
            "cache_version": SEARCH_CACHE_VERSION,
            "model": _norm_text(model),
            "mode": _norm_text(mode or "accuracy"),
            "use_tavily_request": bool(use_tavily),
            "query": _norm_text(getattr(plan, "query", "")),
            "raw_user_message": _norm_text(getattr(plan, "raw_user_message", "")),
            "keywords": _norm_list(getattr(plan, "keywords", [])),
            "authors": _norm_list(getattr(plan, "authors", [])),
            "venues": _norm_list(getattr(plan, "venues", [])),
            "target_titles": _norm_list(getattr(plan, "target_titles", []), sort=False),
            "arxiv_id_list": _norm_list(getattr(plan, "arxiv_id_list", [])),
            "year_from": getattr(plan, "year_from", None),
            "year_to": getattr(plan, "year_to", None),
            "sources": _norm_list(getattr(plan, "sources", [])),
            "sort": _norm_text(getattr(plan, "sort", "relevance")),
            "ranking_profile": _norm_text(getattr(plan, "ranking_profile", "accuracy")),
            "use_llm_rank": bool(getattr(plan, "use_llm_rank", True)),
            "recall_max_candidates": int(getattr(plan, "recall_max_candidates", 24) or 24),
            "max_results": int(getattr(plan, "max_results", 10) or 10),
            "recipe": str(getattr(getattr(plan, "recipe", None), "value", getattr(plan, "recipe", "general"))),
            "plan_use_tavily": bool(getattr(plan, "use_tavily", False)),
        }
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> dict[str, Any] | None:
        if not self.enabled or self.ttl_sec <= 0:
            return None
        self.ensure_tables()
        now = int(time.time())
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM search_result_cache WHERE cache_key = ?",
                (str(cache_key),),
            ).fetchone()
            if not row:
                return None
            if int(row["expires_at"] or 0) <= now:
                conn.execute("DELETE FROM search_result_cache WHERE cache_key = ?", (str(cache_key),))
                conn.commit()
                return None
            try:
                payload = json.loads(row["payload_json"] or "")
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                return None
            conn.execute(
                "UPDATE search_result_cache SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE cache_key = ?",
                (now, str(cache_key)),
            )
            conn.commit()
            return payload
        finally:
            conn.close()

    def set(self, cache_key: str, payload: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> bool:
        if not self.enabled or self.ttl_sec <= 0 or not isinstance(payload, dict):
            return False
        self.ensure_tables()
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO search_result_cache
                  (cache_key, payload_json, metadata_json, created_at, expires_at, hit_count, last_hit_at)
                VALUES (?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(cache_key) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  metadata_json = excluded.metadata_json,
                  created_at = excluded.created_at,
                  expires_at = excluded.expires_at,
                  hit_count = 0,
                  last_hit_at = NULL
                """,
                (
                    str(cache_key),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    now,
                    now + self.ttl_sec,
                ),
            )
            old_keys = conn.execute(
                "SELECT cache_key FROM search_result_cache ORDER BY created_at DESC "
                "LIMIT -1 OFFSET ?",
                (self.max_entries,),
            ).fetchall()
            if old_keys:
                conn.executemany(
                    "DELETE FROM search_result_cache WHERE cache_key = ?",
                    [(str(row["cache_key"]),) for row in old_keys],
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def clear(self) -> None:
        self.ensure_tables()
        conn = self._connect()
        try:
            conn.execute("DELETE FROM search_result_cache")
            conn.commit()
        finally:
            conn.close()
