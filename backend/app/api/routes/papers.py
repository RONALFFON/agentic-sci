"""论文库管理 API 路由 —— 论文保存、删除、搜索、分类与阅读状态管理."""

import csv
import io
import json
import logging


import anyio
from fastapi import APIRouter, Query, BackgroundTasks, Request, Depends, HTTPException
from fastapi.responses import Response
from ...utils.common import safe_http_500
from ...models.schemas import (
    DeletePaperResponse,
    LibraryCategoriesResponse,
    Paper,
    PapersResponse,
    ReadStatus,
    SavePapersRequest,
    SavePapersResponse,
    LibraryGraphResponse,
    UpdatePaperRequest,
    UpdatePaperResponse,
    DailyPapersRequest,
    DailyPapersResponse,
    DailyRecommendFeedbackRequest,
    DailyRecommendFeedbackResponse,
    ReadingCalendarItem,
    ReadingLogRequest,
    ReadingCalendarResponse,
    GraphRelationUpsertRequest,
    GraphRelationMutationResponse,
)

from ...services.papers.papers_converters import api_paper_to_litpaper, litpaper_to_api_paper

from ...services.papers.papers_helpers import (
    daily_paper_identity_sig,
)
from ...services.graph.graph_service import build_library_graph
from ...services.graph.kg_relations import delete_relation, ensure_tables, upsert_relations
from ...services.papers.papers_library_service import (
    build_library_pdf_response_service,
    delete_paper_by_id,
    get_library as get_library_service,
    get_paper_by_id,
    list_library_categories as list_library_categories_service,
    save_papers as save_papers_service,
    update_paper_by_id,
)
from ...services.daily.daily_auto_refresh import get_daily_compute_lock
from ...services.daily.daily_service import (
    compute_daily_papers as compute_daily_service,
    read_daily_cached_or_204 as get_daily_cached_or_204_service,
    record_user_daily_feedback as record_daily_feedback_service,
)
from ...settings import get_settings
from ..dependencies import get_database, get_db_path, get_searcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["文献管理"])

def _parse_relation_types(value: str | None) -> set[str] | None:
    if value is None:
        return None
    parsed = {part.strip() for part in str(value).split(",") if part.strip()}
    return parsed or None

class DailyServices:
    def __init__(self, db_path=Depends(get_db_path), searcher=Depends(get_searcher)):
        self.db_path = db_path
        self.searcher = searcher

# ── 知识图谱 ──
@router.get("/graph/library", response_model=LibraryGraphResponse)
def library_graph(
    limit: int = Query(default=200, ge=1, le=1000),
    category: str | None = Query(default=None),
    include_authors: bool = Query(default=False),
    include_keywords: bool = Query(default=False),
    relation_edge_limit: int = Query(default=400, ge=0, le=5000),
    focus_paper_id: int | None = Query(default=None, ge=1),
    relation_types: str | None = Query(default=None, description="逗号分隔的关系类型"),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    db=Depends(get_database),
):
    try:
        return build_library_graph(
            db=db,
            limit=int(limit),
            category=category,
            include_authors=bool(include_authors),
            include_keywords=bool(include_keywords),
            relation_edge_limit=int(relation_edge_limit),
            focus_paper_id=focus_paper_id,
            relation_types=_parse_relation_types(relation_types),
            min_score=float(min_score),
        )
    except Exception as e:
        logger.exception("GET /api/papers/graph/library 失败")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/graph/library/export")
def export_library_graph(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    category: str | None = Query(default=None),
    include_authors: bool = Query(default=True),
    include_keywords: bool = Query(default=True),
    relation_edge_limit: int = Query(default=400, ge=0, le=5000),
    focus_paper_id: int | None = Query(default=None, ge=1),
    relation_types: str | None = Query(default=None, description="逗号分隔的关系类型"),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    db=Depends(get_database),
):
    graph = build_library_graph(
        db=db,
        limit=int(limit),
        category=category,
        include_authors=bool(include_authors),
        include_keywords=bool(include_keywords),
        relation_edge_limit=int(relation_edge_limit),
        focus_paper_id=focus_paper_id,
        relation_types=_parse_relation_types(relation_types),
        min_score=float(min_score),
    )
    if format == "json":
        content = json.dumps(graph.model_dump(mode="json"), ensure_ascii=False, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=papergraph.json"},
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "record_type", "id", "source", "target", "type", "label", "node_type",
        "paper_id", "year", "category", "weight", "evidence",
    ])
    for node in graph.nodes:
        writer.writerow([
            "node", node.id, "", "", "", node.label, node.type,
            node.paper_id or "", node.year or "", node.category or "", node.weight, "",
        ])
    for edge in graph.edges:
        writer.writerow([
            "edge", "", edge.source, edge.target, edge.type, "", "",
            "", "", "", edge.weight, edge.evidence or "",
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=papergraph.csv"},
    )

@router.put("/graph/relations", response_model=GraphRelationMutationResponse)
def upsert_graph_relation(
    body: GraphRelationUpsertRequest,
    db=Depends(get_database),
    db_path=Depends(get_db_path),
):
    source_id = int(body.source_paper_id)
    target_id = int(body.target_paper_id)
    if source_id == target_id:
        raise HTTPException(status_code=422, detail="source_paper_id 与 target_paper_id 不能相同")
    if not db.get_paper_by_id(source_id) or not db.get_paper_by_id(target_id):
        raise HTTPException(status_code=404, detail="关系两端的论文不存在")

    relation = body.relation.strip()
    ensure_tables(db_path)
    updated_fields = ["relation", "score", "evidence"]
    previous = (body.previous_relation or "").strip()
    if previous and previous != relation:
        delete_relation(db_path, source_id, target_id, previous)
        updated_fields.append("previous_relation")
    written = upsert_relations(
        db_path,
        source_id,
        [{"target_paper_id": target_id, "relation": relation, "score": body.score, "evidence": body.evidence or ""}],
    )
    if written != 1:
        raise HTTPException(status_code=400, detail="关系保存失败")
    return GraphRelationMutationResponse(
        success=True,
        message="知识图谱关系已保存",
        source_paper_id=source_id,
        target_paper_id=target_id,
        relation=relation,
        updated_fields=updated_fields,
    )

@router.delete("/graph/relations", response_model=GraphRelationMutationResponse)
def remove_graph_relation(
    source_paper_id: int = Query(..., ge=1),
    target_paper_id: int = Query(..., ge=1),
    relation: str = Query(..., min_length=2, max_length=32),
    db_path=Depends(get_db_path),
):
    relation_name = relation.strip()
    if not delete_relation(db_path, int(source_paper_id), int(target_paper_id), relation_name):
        raise HTTPException(status_code=404, detail="知识图谱关系不存在")
    return GraphRelationMutationResponse(
        success=True,
        message="知识图谱关系已删除",
        source_paper_id=int(source_paper_id),
        target_paper_id=int(target_paper_id),
        relation=relation_name,
        updated_fields=["deleted"],
    )

# ── 文献库管理 ──
@router.get("/library/categories", response_model=LibraryCategoriesResponse)
def list_library_categories(db=Depends(get_database)):
    return list_library_categories_service(db=db)

@router.get("/library", response_model=PapersResponse)
def get_library(
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    read_status: ReadStatus | None = None,
    tags: str | None = Query(default=None, description="逗号分隔标签"),
    category: str | None = Query(default=None, description="领域筛选"),
    db=Depends(get_database),
):
    return get_library_service(
        db=db,
        litpaper_to_api_paper_fn=litpaper_to_api_paper,
        limit=limit,
        offset=offset,
        q=q,
        year_from=year_from,
        year_to=year_to,
        read_status=read_status,
        tags=tags,
        category=category,
    )

# ── 论文保存 ──
@router.post("/save", response_model=SavePapersResponse)
async def save_papers(
    request: SavePapersRequest,
    background_tasks: BackgroundTasks,
    db=Depends(get_database),
):
    try:
        return await save_papers_service(
            db=db,
            request=request,
            background_tasks=background_tasks,
            api_to_lit_fn=api_paper_to_litpaper,
            litpaper_to_api_paper_fn=litpaper_to_api_paper,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise safe_http_500("save_papers", e)

# ── 每日推荐 ──
@router.get("/daily")
async def daily_papers_get(db_path=Depends(get_db_path)):
    logger.info("HTTP GET /api/papers/daily")
    return await get_daily_cached_or_204_service(db_path=db_path)

@router.post("/daily", response_model=DailyPapersResponse)
async def daily_papers(
    body: DailyPapersRequest,
    services: DailyServices = Depends(),
    settings=Depends(get_settings),
):
    logger.info(
        "HTTP POST /api/papers/daily force_refresh=%s",
        getattr(body, "force_refresh", False),
    )
    lock = get_daily_compute_lock()
    async with lock:
        try:
            with anyio.fail_after(180.0):
                resp = await compute_daily_service(
                body=body, db_path=services.db_path, searcher=services.searcher,
                daily_paper_identity_sig_fn=daily_paper_identity_sig,
                daily_arxiv_cs_categories=settings.get_daily_arxiv_cs_categories(),
                papergraph_to_api_fn=litpaper_to_api_paper, logger=logger,
            )
        except TimeoutError:
            err_msg = "每日论文计算超时（>180s），请稍后重试或缩小范围"
            raise HTTPException(status_code=504, detail=err_msg)
        except HTTPException:
            raise
        except Exception as e:
            raise safe_http_500("daily_papers", e)
        else:
            return resp

# ── 阅读日志 ──
@router.post("/reading/log")
def log_reading_session(body: ReadingLogRequest, db_path=Depends(get_db_path)):
    from ...services.reading_log.log import append_session
    append_session(db_path, paper_id=int(body.paper_id), duration_sec=int(body.duration_sec),
                   client_ts=int(body.client_ts) if body.client_ts is not None else None)
    return {"success": True}

@router.get("/reading/calendar", response_model=ReadingCalendarResponse)
def reading_calendar(days: int = Query(default=180, ge=7, le=366), db_path=Depends(get_db_path)):
    from ...services.reading_log.log import list_daily_aggregate
    items = list_daily_aggregate(db_path, days=int(days))
    return ReadingCalendarResponse(success=True, days=int(days),
                                   items=[ReadingCalendarItem(**x) for x in items])

@router.get("/{paper_id}/library-pdf")
async def get_paper_library_pdf(
    paper_id: int,
    request: Request,
    db_path=Depends(get_db_path),
):
    return build_library_pdf_response_service(paper_id=paper_id, request=request, db_path=db_path, logger_obj=logger)

@router.get("/{paper_id}", response_model=Paper)
def get_paper(paper_id: int, db=Depends(get_database)):
    return get_paper_by_id(db=db, paper_id=paper_id, litpaper_to_api_paper_fn=litpaper_to_api_paper)

@router.put("/{paper_id}", response_model=UpdatePaperResponse)
def update_paper(paper_id: int, body: UpdatePaperRequest, db=Depends(get_database)):
    return update_paper_by_id(db=db, paper_id=paper_id, body=body)

@router.delete("/{paper_id}", response_model=DeletePaperResponse)
def delete_paper(paper_id: int, db=Depends(get_database)):
    return delete_paper_by_id(db=db, paper_id=paper_id)

@router.post("/daily/feedback", response_model=DailyRecommendFeedbackResponse)
async def record_daily_recommend_feedback(
    body: DailyRecommendFeedbackRequest,
    db_path=Depends(get_db_path),
):
    try:
        return await record_daily_feedback_service(body=body, db_path=db_path)
    except HTTPException:
        raise
    except Exception as e:
        raise safe_http_500("record_daily_recommend_feedback", e)
