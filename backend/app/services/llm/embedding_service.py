"""Embedding 服务 —— OpenAI 兼容 /embeddings 封装（记忆层语义召回）.

设计要点：
- 未配置密钥或调用异常时优雅降级：``is_configured()`` 返回 False、
  ``embed_query``/``embed_texts`` 返回 None/[]，绝不向调用方抛出，
  以保证阅读链不因 embedding 硬失败。
- 返回向量统一做 L2 归一化，使 cosine 相似度等价于点积。
- 进程内小 LRU 缓存，减少同一 query 的重复网络调用。
"""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from typing import Any, Optional, Sequence

import httpx

from ...settings import get_settings

logger = logging.getLogger(__name__)

_CACHE_MAXSIZE = 256


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 0.0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


class EmbeddingService:
    """封装 OpenAI 兼容的 embeddings 接口（默认 dashscope text-embedding-v4）。"""

    def __init__(self) -> None:
        self._cache: "OrderedDict[tuple[str, int, str], list[float]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    # ── 配置读取（每次实时读取，便于配置热更新与测试注入）──
    def _cfg(self) -> dict[str, Any]:
        s = get_settings()
        return {
            "enabled": bool(getattr(s, "embed_enabled", True)),
            "api_key": (getattr(s, "embed_api_key", "") or "").strip(),
            "base_url": (getattr(s, "embed_base_url", "") or "").strip().rstrip("/"),
            "model": (getattr(s, "embed_model_name", "") or "").strip(),
            "dimensions": int(getattr(s, "embed_dimensions", 0) or 0),
            "batch_size": max(1, int(getattr(s, "embed_batch_size", 10) or 10)),
            "timeout": float(getattr(s, "embed_timeout_sec", 8.0) or 8.0),
            "trust_env": bool(getattr(s, "papergraph_httpx_trust_env", True)),
        }

    def is_configured(self) -> bool:
        c = self._cfg()
        return bool(c["enabled"] and c["api_key"] and c["base_url"] and c["model"])

    # ── 缓存 ──
    def _cache_get(self, key: tuple[str, int, str]) -> Optional[list[float]]:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def _cache_put(self, key: tuple[str, int, str], vec: list[float]) -> None:
        with self._cache_lock:
            self._cache[key] = vec
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_MAXSIZE:
                self._cache.popitem(last=False)

    # ── 核心调用 ──
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """批量生成向量。全部成功时返回与输入等长的归一化向量列表，否则返回 []。"""
        cleaned = [str(t or "").strip() for t in (texts or [])]
        cleaned = [t for t in cleaned if t]
        if not cleaned or not self.is_configured():
            return []

        c = self._cfg()
        url = f"{c['base_url']}/embeddings"
        headers = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}
        batch_size = c["batch_size"]

        out: list[list[float]] = []
        try:
            with httpx.Client(trust_env=c["trust_env"], timeout=c["timeout"]) as client:
                for i in range(0, len(cleaned), batch_size):
                    batch = cleaned[i : i + batch_size]
                    payload: dict[str, Any] = {
                        "model": c["model"],
                        "input": batch,
                        "encoding_format": "float",
                    }
                    if c["dimensions"] > 0:
                        payload["dimensions"] = c["dimensions"]
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        logger.debug(
                            "embedding_http_failed status=%s body=%s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        return []
                    data = resp.json()
                    items = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(items, list) or len(items) != len(batch):
                        logger.debug("embedding_bad_response_shape items=%s", type(items))
                        return []
                    # 按 index 排序，兼容乱序返回
                    items = sorted(
                        items,
                        key=lambda d: int(d.get("index", 0)) if isinstance(d, dict) else 0,
                    )
                    for it in items:
                        vec = it.get("embedding") if isinstance(it, dict) else None
                        if not isinstance(vec, list) or not vec:
                            logger.debug("embedding_missing_vector")
                            return []
                        out.append(_l2_normalize(vec))
        except Exception as exc:  # noqa: BLE001 - 优雅降级，绝不上抛
            logger.debug("embedding_request_failed", exc_info=exc)
            return []

        # 一致性校验：同批向量维度需相同
        dims = {len(v) for v in out}
        if len(dims) != 1:
            logger.debug("embedding_ragged_dimensions dims=%s", dims)
            return []
        return out

    def embed_query(self, text: str) -> Optional[list[float]]:
        """为单条查询生成归一化向量；未配置或失败时返回 None。带缓存。"""
        q = str(text or "").strip()
        if not q or not self.is_configured():
            return None
        c = self._cfg()
        key = (c["model"], c["dimensions"], q)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        vecs = self.embed_texts([q])
        if not vecs:
            return None
        vec = vecs[0]
        self._cache_put(key, vec)
        return vec


_embedding_service: Optional[EmbeddingService] = None
_service_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        with _service_lock:
            if _embedding_service is None:
                _embedding_service = EmbeddingService()
    return _embedding_service
