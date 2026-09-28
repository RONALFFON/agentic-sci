"""记忆层 embedding 语义召回的离线测试（全部注入 fake embedder，不触网）。"""

from __future__ import annotations

import sqlite3
import time

from app.services.memory.memory_store import MemoryStore, _vec_to_blob
from app.settings import get_settings


class FakeEmbedder:
    """按「概念关键词」映射到固定维度的确定性向量，用于离线验证混合打分。"""

    def __init__(self, *, configured: bool = True, dim: int = 3) -> None:
        self._configured = configured
        self._dim = dim

    def is_configured(self) -> bool:
        return self._configured

    def _vec(self, text: str) -> list[float]:
        t = str(text or "").lower()
        v = [0.0] * self._dim
        if any(k in t for k in ("创新", "新", "novel", "innovation")):
            v[0] = 1.0
        if any(k in t for k in ("数据", "实验", "dataset", "benchmark")):
            v[1] = 1.0
        if any(k in t for k in ("作者", "author")):
            v[2] = 1.0
        norm = sum(x * x for x in v) ** 0.5
        return v if norm == 0.0 else [x / norm for x in v]

    def embed_query(self, text: str):
        if not self._configured or not str(text or "").strip():
            return None
        return self._vec(text)

    def embed_texts(self, texts):
        if not self._configured:
            return []
        cleaned = [str(t or "").strip() for t in (texts or [])]
        cleaned = [t for t in cleaned if t]
        if not cleaned:
            return []
        return [self._vec(t) for t in cleaned]


def _mem_lines(block: str) -> list[str]:
    return [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]


def _insert_raw(
    store: MemoryStore,
    *,
    content: str,
    embedding: bytes | None = None,
    embed_model: str | None = None,
    embed_dim: int | None = None,
    paper_id: int = 1,
    importance: float = 0.5,
) -> None:
    conn = store._connect()
    try:
        conn.execute(
            "INSERT INTO paper_memory "
            "(scope, paper_id, kind, content, importance, shared, created_at, "
            " embedding, embed_model, embed_dim) "
            "VALUES ('paper', ?, 'short', ?, ?, 0, ?, ?, ?, ?)",
            (paper_id, content, importance, int(time.time()), embedding, embed_model, embed_dim),
        )
        conn.commit()
    finally:
        conn.close()


def test_semantic_recall_beats_keyword_without_lexical_overlap(tmp_path):
    """词面 0 重叠时，语义相近的记忆应被召回并排在前面。"""
    store = MemoryStore(str(tmp_path / "m.db"), embedder=FakeEmbedder(configured=True))
    store.add(scope="paper", paper_id=1, kind="short", content="方法的创新点在于图结构增强", importance=0.5)
    store.add(scope="paper", paper_id=1, kind="short", content="实验数据集与评测指标说明", importance=0.5)

    block = store.get_context_for_query(paper_id=1, query="它新在哪里", limit=2)
    lines = _mem_lines(block)
    assert lines, "应至少召回一条记忆"
    assert lines[0].startswith("方法的创新点")


def test_degrades_to_keyword_when_embedding_disabled(tmp_path):
    """未配置 embedding 时，行为退回纯关键词匹配（防回归）。"""
    store = MemoryStore(str(tmp_path / "m.db"), embedder=FakeEmbedder(configured=False))
    store.add(scope="paper", paper_id=1, kind="short", content="graph neural network method", importance=0.5)
    store.add(scope="paper", paper_id=1, kind="short", content="dataset benchmark evaluation", importance=0.5)

    block = store.get_context_for_query(paper_id=1, query="benchmark evaluation", limit=2)
    lines = _mem_lines(block)
    assert lines[0].startswith("dataset benchmark")


def test_empty_query_skips_embedding(tmp_path):
    """build_context_block（空 query）不触发语义项，仍按重要度/时近返回。"""
    store = MemoryStore(str(tmp_path / "m.db"), embedder=FakeEmbedder(configured=True))
    store.add(scope="paper", paper_id=1, kind="short", content="方法的创新点在于图结构增强", importance=0.9)
    store.add(scope="paper", paper_id=1, kind="short", content="实验数据集与评测指标说明", importance=0.2)

    block = store.build_context_block(paper_id=1, limit=2)
    lines = _mem_lines(block)
    assert lines[0].startswith("方法的创新点")  # importance 高者在前


def test_stale_model_vector_is_ignored(tmp_path):
    """embed_model 与当前配置不一致的旧向量按 sem=0 处理。"""
    store = MemoryStore(str(tmp_path / "m.db"), embedder=FakeEmbedder(configured=True))
    store.add(scope="paper", paper_id=1, kind="short", content="创新点甲", importance=0.5)
    _insert_raw(
        store,
        content="创新点乙",
        embedding=_vec_to_blob([1.0, 0.0, 0.0]),
        embed_model="stale-model",
        embed_dim=3,
    )

    block = store.get_context_for_query(paper_id=1, query="新在哪", limit=2)
    lines = _mem_lines(block)
    assert lines[0].startswith("创新点甲")


def test_dimension_mismatch_vector_scores_zero(tmp_path):
    """同模型但维度不一致的向量，点积返回 0，不影响排序。"""
    store = MemoryStore(str(tmp_path / "m.db"), embedder=FakeEmbedder(configured=True))
    store.add(scope="paper", paper_id=1, kind="short", content="创新点甲", importance=0.5)
    _insert_raw(
        store,
        content="创新点乙",
        embedding=_vec_to_blob([1.0, 0.0, 0.0, 0.0, 0.0]),
        embed_model=get_settings().embed_model_name,
        embed_dim=5,
    )

    block = store.get_context_for_query(paper_id=1, query="新在哪", limit=2)
    lines = _mem_lines(block)
    assert lines[0].startswith("创新点甲")


def test_schema_migration_is_idempotent(tmp_path):
    """对旧结构（无向量列）的库执行迁移，且重复执行不报错、旧数据保留。"""
    dbp = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(dbp)
    conn.execute(
        "CREATE TABLE paper_memory ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, paper_id INTEGER,"
        " kind TEXT NOT NULL, content TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5,"
        " shared INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO paper_memory (scope, paper_id, kind, content, importance, shared, created_at) "
        "VALUES ('paper', 1, 'short', '遗留记忆', 0.5, 0, ?)",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()

    store = MemoryStore(dbp, embedder=FakeEmbedder(configured=False))
    cols = {r["name"] for r in store._connect().execute("PRAGMA table_info(paper_memory)").fetchall()}
    assert {"embedding", "embed_model", "embed_dim"} <= cols

    store.ensure_tables()  # 幂等，不应抛异常
    store2 = MemoryStore(dbp, embedder=FakeEmbedder(configured=False))
    block = store2.get_context_for_query(paper_id=1, query="遗留", limit=5)
    assert "遗留记忆" in block


def test_backfill_embeddings_fills_legacy_rows(tmp_path):
    """backfill 为无向量的旧记忆补写向量，随后语义召回生效。"""
    dbp = str(tmp_path / "bf.db")
    store = MemoryStore(dbp, embedder=FakeEmbedder(configured=False))
    _insert_raw(store, content="方法的创新点在于图结构增强", embedding=None, embed_model=None)
    _insert_raw(store, content="实验数据集与评测指标说明", embedding=None, embed_model=None)

    store._embedder = FakeEmbedder(configured=True)
    n = store.backfill_embeddings(batch=10)
    assert n == 2

    block = store.get_context_for_query(paper_id=1, query="它新在哪里", limit=2)
    assert _mem_lines(block)[0].startswith("方法的创新点")


def test_backfill_noop_when_not_configured(tmp_path):
    """未配置 embedding 时 backfill 直接返回 0，不触网。"""
    store = MemoryStore(str(tmp_path / "bf2.db"), embedder=FakeEmbedder(configured=False))
    _insert_raw(store, content="方法的创新点", embedding=None, embed_model=None)
    assert store.backfill_embeddings() == 0


def test_embedding_service_degrades_without_key(monkeypatch):
    """真实 EmbeddingService 在无密钥时 is_configured=False 且不发起请求。"""
    from app.services.llm import embedding_service as es

    monkeypatch.setattr(get_settings(), "embed_api_key", "", raising=False)
    svc = es.EmbeddingService()
    assert svc.is_configured() is False
    assert svc.embed_query("hello") is None
    assert svc.embed_texts(["hello"]) == []


def test_l2_normalize():
    from app.services.llm.embedding_service import _l2_normalize

    v = _l2_normalize([3.0, 4.0])
    assert abs(v[0] - 0.6) < 1e-6 and abs(v[1] - 0.8) < 1e-6
    assert _l2_normalize([0.0, 0.0]) == [0.0, 0.0]
