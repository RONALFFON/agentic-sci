"""智能搜索 API 路由 —— 自然语言论文搜索与 SSE 流式响应."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...agents.search_agent import SearchAgent, SearchIntent, get_search_agent
from ...api.dependencies import get_searcher
from ...api.search_route_support import (
    ToolCallInfo,
    last_pipeline_tool_error,
    normalize_tool_calls,
    track_tool_call,
    user_facing_error_message,
)
from ...models.schemas import Paper
from ...services.papers.papers_converters import litpapers_to_api_papers
from ...services.retrieval.search_plan import ResolvedSearchPlan
from ...services.retrieval.search_pipeline import run_search_pipeline_async
from ...services.retrieval.search_cache import SearchResultCache
from ..tool_events import ToolCallTracker, sse_pack

router = APIRouter(prefix="/papers", tags=["智能搜索"])
logger = logging.getLogger(__name__)

_SSE_QUEUE_SIZE = 128
_SEARCH_AGENT_WALL_SEC = max(
    120.0,
    min(900.0, float(os.getenv("PAPERGRAPH_SEARCH_AGENT_WALL_SEC") or 420.0)),
)
_SEARCH_AGENT_INIT_SEC = 25.0
_PREFIX_CONFLICT_MARKER = "为您找到"


class SearchAgentMessage(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="用户搜索需求")
    mode: str = Field(default="accuracy", description="accuracy=准确性优先, novelty=新颖性优先")
    use_tavily: bool = Field(default=False, description="是否使用 Tavily 预搜索")
    history: List[Dict[str, str]] = Field(default_factory=list, description="对话历史")


class SearchAgentResponse(BaseModel):
    success: bool
    response: str
    search_params: Optional[Dict[str, Any]] = None
    tool_calls: List[ToolCallInfo] = Field(default_factory=list)
    papers: List[Paper] = Field(default_factory=list)
    total: int = 0
    message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


def _search_params_from_intent(intent: SearchIntent, **extra: Any) -> Dict[str, Any]:
    yf, yt = intent.year_from, intent.year_to
    if isinstance(yf, int) and isinstance(yt, int) and yf > yt:
        yf, yt = yt, yf
    out: Dict[str, Any] = {
        "query": intent.query,
        "keywords": intent.keywords,
        "authors": getattr(intent, "authors", []) or [],
        "arxiv_id_list": getattr(intent, "arxiv_id_list", []) or [],
        "venues": intent.venues,
        "year_from": yf,
        "year_to": yt,
        "sort": intent.sort,
        "use_llm_rank": intent.use_llm_rank,
        "rerank_recall_max": intent.rerank_recall_max,
        "ranking_rationale": intent.ranking_rationale or None,
    }
    out.update(extra)
    return out


def _generate_suggestions(intent: SearchIntent, papers: List[Paper]) -> List[str]:
    if len(papers) < 5:
        return [f"扩大搜索：尝试「{intent.query}」而不限定会议"]
    return []


def _strip_conflicting_search_summary_prefix(text: str) -> str:
    t = (text or "").strip()
    if not t or _PREFIX_CONFLICT_MARKER not in t:
        return t
    return t[: t.find(_PREFIX_CONFLICT_MARKER)].rstrip()


def _explanation_with_suggestions(
    agent: SearchAgent,
    intent: SearchIntent,
    papers: List[Paper],
    profile_mode: str,
    *,
    prefix_plain: str = "",
) -> str:
    base = _strip_conflicting_search_summary_prefix((prefix_plain or "").strip())
    expl = agent.explain_results(intent, papers, profile_mode)
    explanation = base + "\n\n---\n\n" + expl if (papers and base) else (base or expl)
    if papers:
        sug = _generate_suggestions(intent, papers)
        if sug:
            explanation += "\n\n🔍 **您可以这样优化**：\n" + "".join(
                f"{i}. {s}\n" for i, s in enumerate(sug, 1)
            )
    return explanation


def _error_response(msg: str) -> SearchAgentResponse:
    return SearchAgentResponse(
        success=False,
        response=user_facing_error_message(msg),
        message=msg,
    )


async def _prepare_agent_and_query(request: SearchAgentMessage) -> tuple[SearchAgent, str]:
    try:
        with anyio.fail_after(_SEARCH_AGENT_INIT_SEC):
            agent = await anyio.to_thread.run_sync(get_search_agent)
    except TimeoutError as exc:
        logger.warning("search-agent init timeout after %.0fs", _SEARCH_AGENT_INIT_SEC, exc_info=exc)
        raise HTTPException(status_code=504, detail="search_agent_init_timeout") from exc
    return agent, (request.message or "").strip()


async def _run_search_agent_core(
    *,
    agent: SearchAgent,
    request: SearchAgentMessage,
    merged_query: str,
    searcher: Any,
    stage_callback: Optional[Callable[[str, str, str, Dict[str, Any]], None]] = None,
) -> SearchAgentResponse:
    tool_calls: List[ToolCallInfo] = []

    def emit_stage(stage: str, status: str, message: str, **details: Any) -> None:
        if stage_callback is not None:
            stage_callback(stage, status, message, details)

    emit_stage("intent_parsing", "running", "正在解析检索意图")
    intent = agent.understand_intent(merged_query, request.mode)
    with track_tool_call(tool_calls, "understand_intent", {"query": merged_query}) as tc:
        tc.result_summary = f"sort={intent.sort}, venues={intent.venues}, yf={intent.year_from}, kw={intent.keywords}"
    emit_stage(
        "intent_parsing",
        "completed",
        "检索意图解析完成",
        query=intent.query,
        keyword_count=len(intent.keywords or []),
        source_count=len(intent.sources or []),
    )

    plan = ResolvedSearchPlan.from_search_intent(intent)
    cache = SearchResultCache()
    cache_key = cache.key_for_plan(
        plan,
        mode=request.mode,
        use_tavily=bool(request.use_tavily),
    )
    with track_tool_call(
        tool_calls,
        "search_cache",
        {"key": cache_key[:12]},
    ) as tc:
        emit_stage("cache_lookup", "running", "正在检查搜索结果缓存", cache_key=cache_key[:12])
        try:
            cached_payload = cache.get(cache_key)
            tc.result_summary = "命中缓存" if cached_payload else (
                "未命中，继续执行检索" if cache.enabled and cache.ttl_sec > 0 else "缓存未启用"
            )
        except Exception as exc:
            cached_payload = None
            tc.status = "error"
            tc.result_summary = f"缓存读取失败，已跳过: {str(exc)[:100]}"
            logger.warning("search_result_cache_read_failed", exc_info=exc)
    emit_stage(
        "cache_lookup",
        "completed",
        "命中搜索结果缓存" if cached_payload else "未命中缓存，继续检索",
        cache_hit=bool(cached_payload),
        cache_key=cache_key[:12],
        cache_enabled=bool(cache.enabled and cache.ttl_sec > 0),
    )
    if cached_payload:
        try:
            cached_response = SearchAgentResponse.model_validate(cached_payload)
            cached_metadata = dict(cached_response.metadata or {})
            cached_metadata.update({"cache_hit": True, "cache_key": cache_key[:12]})
            emit_stage("search_pipeline", "skipped", "命中缓存，跳过召回与精排", cache_hit=True)
            return cached_response.model_copy(
                update={
                    "tool_calls": normalize_tool_calls(tool_calls),
                    "metadata": cached_metadata,
                }
            )
        except Exception:
            logger.warning("search_result_cache_payload_invalid", exc_info=True)
            cached_payload = None
            emit_stage("cache_lookup", "warning", "缓存内容无效，改为执行实时检索", cache_hit=False)

    emit_stage("search_pipeline", "running", "正在执行多源召回与精排")
    with track_tool_call(tool_calls, "search_pipeline", {"query": intent.query or merged_query}) as tc:
        tc.result_summary = "intent→SearchPlan→pipeline"
        mr = int(getattr(plan, "max_results", None) or intent.max_results or 10)
        pip = await run_search_pipeline_async(searcher=searcher, plan=plan, max_results=mr)
        tc.result_summary = (
            f"candidates={pip.total_candidates}, ranked={len(pip.ranked or [])}, "
            f"method={pip.ranking_method}"
        )
    emit_stage(
        "search_pipeline",
        "completed",
        "多源召回与精排完成",
        candidates=pip.total_candidates,
        ranked=len(pip.ranked or []),
        ranking_method=pip.ranking_method,
        metadata=pip.metadata or {},
    )

    papers = litpapers_to_api_papers(rp.paper for rp in (pip.ranked or []))
    prefix = f"为您找到 {len(papers)} 篇论文。" if papers else "未找到相关论文。"

    pipeline_err = last_pipeline_tool_error(tool_calls)
    if not papers and pipeline_err:
        body = (
            "主检索未成功返回论文（多源召回或精排阶段出错），与「数据库里确实没有匹配文献」不同。\n\n"
            f"**错误摘要**：{pipeline_err}\n\n"
            "建议稍后重试，或略微改写查询；若频繁出现请查看服务端日志。"
        )
        return SearchAgentResponse(
            success=False,
            response=body,
            search_params=_search_params_from_intent(intent, mode=request.mode),
            tool_calls=normalize_tool_calls(tool_calls),
            papers=[],
            total=0,
            message="search_pipeline_error",
            metadata={
                **(pip.metadata or {}),
                "cache_hit": False,
                "cache_key": cache_key[:12],
                "candidates": pip.total_candidates,
                "ranked": len(pip.ranked or []),
                "ranking_method": pip.ranking_method,
            },
        )

    body = _explanation_with_suggestions(agent, intent, papers, request.mode, prefix_plain=prefix)
    response = SearchAgentResponse(
        success=True,
        response=body,
        search_params=_search_params_from_intent(intent, mode=request.mode),
        tool_calls=normalize_tool_calls(tool_calls),
        papers=papers,
        total=len(papers),
        metadata={
            **(pip.metadata or {}),
            "cache_hit": False,
            "cache_key": cache_key[:12],
            "candidates": pip.total_candidates,
            "ranked": len(pip.ranked or []),
            "ranking_method": pip.ranking_method,
        },
    )
    try:
        cache.set(
            cache_key,
            response.model_dump(mode="json", exclude={"tool_calls"}),
            metadata=response.metadata,
        )
    except Exception as exc:
        logger.warning("search_result_cache_write_failed", exc_info=exc)
        emit_stage("cache_write", "warning", "搜索结果已返回，但缓存写入失败", cache_key=cache_key[:12])
    return response


async def _search_agent_impl(
    request: SearchAgentMessage,
    searcher: Any,
    stage_callback: Optional[Callable[[str, str, str, Dict[str, Any]], None]] = None,
):
    def emit_stage(stage: str, status: str, message: str, **details: Any) -> None:
        if stage_callback is not None:
            stage_callback(stage, status, message, details)

    try:
        emit_stage("agent_init", "running", "正在初始化 SearchAgent")
        agent, merged_query = await _prepare_agent_and_query(request)
        emit_stage("agent_init", "completed", "SearchAgent 初始化完成")
        resp = await asyncio.wait_for(
            _run_search_agent_core(
                agent=agent,
                request=request,
                merged_query=merged_query,
                searcher=searcher,
                stage_callback=stage_callback,
            ),
            timeout=_SEARCH_AGENT_WALL_SEC,
        )
        return resp, None
    except asyncio.TimeoutError as exc:
        logger.warning("search-agent timeout after %.0fs", _SEARCH_AGENT_WALL_SEC, exc_info=exc)
        emit_stage("request", "error", "搜索处理超时", error_code="search_agent_timeout")
        return _error_response("search_agent_timeout"), HTTPException(status_code=504, detail="search_agent_timeout")
    except HTTPException as e:
        emit_stage("request", "error", user_facing_error_message(str(e.detail or "search_agent_http_error")), error_code=str(e.detail or "search_agent_http_error"))
        return _error_response(str(e.detail or "search_agent_http_error")), e
    except Exception:
        logger.exception("search-agent unexpected failure")
        emit_stage("request", "error", "搜索服务内部错误", error_code="search_agent_internal_error")
        return _error_response("search_agent_internal_error"), None


@router.post("/search-agent/stream")
async def search_agent_chat_stream(
    request: SearchAgentMessage,
    searcher=Depends(get_searcher),
):
    request_id = uuid4().hex[:16]

    async def gen():
        # SSE 流式生成器：通过 anyio 内存通道实现事件驱动的流式推送
        send, recv = anyio.create_memory_object_stream(_SSE_QUEUE_SIZE)
        tracker = ToolCallTracker(sink=lambda ev: send.send_nowait(ev))
        def emit_stage(
            stage: str,
            status: str,
            message: str,
            details: Optional[Dict[str, Any]] = None,
            **extra: Any,
        ) -> None:
            payload = dict(details or {})
            payload.update(extra)
            tracker.emit(
                "stage",
                {
                    "request_id": request_id,
                    "stage": stage,
                    "status": status,
                    "message": message,
                    **payload,
                },
            )

        tracker.emit("status", {"request_id": request_id, "message": "search-agent 已接入，开始处理"})

        async def run_once() -> SearchAgentResponse:
            tracker.emit(
                "status",
                {"request_id": request_id, "message": f"初始化 SearchAgent（mode={request.mode})"},
            )
            t0 = time.time()
            tracker.emit("status", {"request_id": request_id, "message": "正在检索论文…"})
            resp, exc = await _search_agent_impl(request, searcher, stage_callback=emit_stage)
            if exc:
                code = (
                    str(exc.detail or "search_agent_http_error")
                    if isinstance(exc, HTTPException)
                    else "search_agent_internal_error"
                )
                msg = user_facing_error_message(code)
                tracker.emit("error", {"message": msg})
                if not isinstance(exc, HTTPException):
                    logger.exception("search-agent stream run loop failed")
                failed = _error_response(msg)
                return failed.model_copy(
                    update={"metadata": {"request_id": request_id, "elapsed_ms": int((time.time() - t0) * 1000)}}
                )
            elapsed_ms = int((time.time() - t0) * 1000)
            metadata = dict(resp.metadata or {})
            metadata.update({"request_id": request_id, "elapsed_ms": elapsed_ms})
            resp = resp.model_copy(update={"metadata": metadata})
            emit_stage(
                "request",
                "completed",
                "搜索请求处理完成",
                success=bool(resp.success),
                total=resp.total,
                cache_hit=metadata.get("cache_hit"),
                elapsed_ms=elapsed_ms,
            )
            tracker.emit(
                "final",
                {"request_id": request_id, "elapsed_ms": elapsed_ms, "success": bool(resp.success)},
            )
            return resp

        box: Dict[str, Any] = {"resp": None}
        cancelled_exc = anyio.get_cancelled_exc_class()
        try:
            async with anyio.create_task_group() as tg:

                async def _run() -> None:
                    try:
                        box["resp"] = await run_once()
                    finally:
                        try:
                            await send.aclose()
                        except Exception:
                            pass

                tg.start_soon(_run)

                async for ev in recv:
                    try:
                        yield sse_pack(ev)
                    except (cancelled_exc, asyncio.CancelledError):
                        return
                    except Exception:
                        return
        except (cancelled_exc, asyncio.CancelledError):
            return
        finally:
            try:
                await recv.aclose()
            except Exception:
                pass

        resp: Optional[SearchAgentResponse] = box.get("resp")
        if resp is None:
            resp = _error_response("search_agent_stream_incomplete")
            resp = resp.model_copy(update={"metadata": {"request_id": request_id}})
            tracker.emit(
                "error",
                {"request_id": request_id, "message": resp.message or "search_agent_stream_incomplete"},
            )
        yield sse_pack({"type": "final_result", "result": resp.model_dump(mode="json")})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
