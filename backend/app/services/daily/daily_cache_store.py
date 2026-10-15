"""每日缓存存储 —— arXiv 原始数据本地缓存以避免重复拉取."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from ...utils.common import exec_sql
from ..llm.agent_runtime import check_active
from ..task_state import identity
from ...settings import get_settings

def _versioned_key(key: str) -> str:
    cfg = get_settings()
    return str(key) + ":" + identity("daily-v2", os.getenv("LLM_MODEL_ID") or os.getenv("OPENAI_MODEL") or cfg.openai_model, os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or cfg.openai_base_url)

def get_revision(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS daily_cache_revision (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)")
        row = conn.execute("SELECT revision FROM daily_cache_revision WHERE id=1").fetchone()
        return int(row[0]) if row else 0

def invalidate_cache(db_path: str) -> None:
    ensure_tables(db_path)
    get_revision(db_path)
    with sqlite3.connect(db_path) as conn:
        check_active()
        conn.execute("INSERT INTO daily_cache_revision VALUES(1,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1")
        conn.execute("DELETE FROM daily_papers_cache")


def ensure_tables(db_path: str) -> None:
    exec_sql(db_path,
        """CREATE TABLE IF NOT EXISTS daily_papers_cache (
          date_key TEXT NOT NULL,
          cache_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          hit_count INTEGER DEFAULT 0,
          PRIMARY KEY (date_key, cache_key)
        )""",
    )

def get_cache(db_path: str, *, date_key: str, cache_key: str) -> dict[str, Any | None]:
    ensure_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT payload_json FROM daily_papers_cache WHERE date_key=? AND cache_key=?",
            (str(date_key), _versioned_key(cache_key)),
        )
        row = cur.fetchone()
        if not row:
            return None
        raw = row[0] or ""
        try:
            data = json.loads(raw)
        except Exception:
            data = None

        try:
            cur.execute(
                "UPDATE daily_papers_cache SET hit_count=hit_count+1, updated_at=? WHERE date_key=? AND cache_key=?",
                (int(time.time()), str(date_key), _versioned_key(cache_key)),
            )
            conn.commit()
        except Exception:
            pass
        return data if isinstance(data, dict) else None
    finally:
        conn.close()

def set_cache(db_path: str, *, date_key: str, cache_key: str, payload: dict[str, Any], expected_revision: int | None = None) -> None:
    ensure_tables(db_path)
    get_revision(db_path)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        check_active()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM daily_cache_revision WHERE id=1").fetchone()
        if expected_revision is not None and (int(row[0]) if row else 0) != expected_revision:
            return
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO daily_papers_cache(date_key, cache_key, payload_json, created_at, updated_at, hit_count)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(date_key, cache_key) DO UPDATE SET
              payload_json=excluded.payload_json,
              updated_at=excluded.updated_at
            """,
            (str(date_key), _versioned_key(cache_key), json.dumps(payload, ensure_ascii=False), now, now, 0),
        )
        check_active()
        conn.commit()
    finally:
        conn.close()
