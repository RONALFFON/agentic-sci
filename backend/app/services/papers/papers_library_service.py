"""文献库管理服务 —— 论文存储、分类、标签、阅读记录与 PDF 下载管理."""

import logging
import os

from fastapi import BackgroundTasks, HTTPException, Request

from ...settings import get_settings
from ...models.schemas import (
    DeletePaperResponse,
    LibraryCategoriesResponse,
    LibraryCategoryFolder,
    Paper,
    PapersResponse,
    SavePapersRequest,
    SavePapersResponse,
    UpdatePaperRequest,
    UpdatePaperResponse,
)

logger = logging.getLogger(__name__)

def _merge_tag_lists(base: list[str], extra: list[str], max_n: int = 24) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in (base or []) + (extra or []):
        k = (t or "").strip()
        if not k:
            continue
        low = k.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(k)
        if len(out) >= max_n:
            break
    return out

def list_library_categories(*, db) -> LibraryCategoriesResponse:
    try:
        from app.core.paper_paths import LIBRARY_PDF_ROOT_DIR

        items = db.list_library_category_folders()
        return LibraryCategoriesResponse(
            success=True,
            store_root=LIBRARY_PDF_ROOT_DIR,
            folders=[LibraryCategoryFolder(**x) for x in items],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def get_library(
    *,
    db,
    litpaper_to_api_paper_fn,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    read_status=None,
    tags: str | None = None,
    category: str | None = None,
) -> PapersResponse:
    try:

        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        cat = (category or "").strip() or None
        filters = dict(query=q, tags=tag_list, year_from=year_from, year_to=year_to,
                       read_status=read_status.value if read_status else None, category=cat)
        papers_data = db.search_library(**filters, limit=limit, offset=offset)

        ids_missing_pdf = [
            int(p.id)
            for p in papers_data
            if p.id is not None and not (getattr(p, "local_pdf_path", None) or "").strip()
        ]
        if ids_missing_pdf:
            repaired = db.repair_library_local_pdf_paths_batch(ids_missing_pdf)
            for p in papers_data:
                if p.id is not None and int(p.id) in repaired:
                    p.local_pdf_path = repaired[int(p.id)]

        total = db.count_library(**filters)
        papers = [litpaper_to_api_paper_fn(p) for p in papers_data]
        return PapersResponse(success=True, total=total, papers=papers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def save_papers(
    *,
    db,
    request: SavePapersRequest,
    background_tasks: BackgroundTasks,
    api_to_lit_fn,
    litpaper_to_api_paper_fn,
) -> SavePapersResponse:
    from functools import partial
    from anyio import to_thread
    return await to_thread.run_sync(partial(_persist_papers, db=db, request=request, api_to_lit_fn=api_to_lit_fn))


def _persist_papers(*, db, request, api_to_lit_fn):
    try:
        from app.core.paper_paths import normalize_library_category_display
        lit_list = [api_to_lit_fn(p) for p in request.papers]
        for api_p, lit_p in zip(request.papers, lit_list):
            if (api_p.source_url or "").strip():
                lit_p.source_url = (api_p.source_url or "").strip()
            if (api_p.pdf_url or "").strip():
                lit_p.pdf_url = (api_p.pdf_url or "").strip()
            if (api_p.doi or "").strip():
                lit_p.doi = (api_p.doi or "").strip()
            if (api_p.arxiv_id or "").strip():
                lit_p.arxiv_id = (api_p.arxiv_id or "").strip()
            # Fill missing arXiv ID from DOI/source URL.
            if not (getattr(lit_p, "arxiv_id", None) or "").strip():
                import re
                for field_val in ((api_p.doi or "").strip(), (api_p.source_url or "").strip()):
                    m = re.search(r"arxiv/([\d.]+)", field_val, re.I)
                    if m:
                        lit_p.arxiv_id = m.group(1)
                        break
            # Infer venue type from journal name when absent.
            if not (getattr(lit_p, "venue_type", None) or "").strip():
                j = (getattr(lit_p, "journal", None) or "").strip()
                if j:
                    import re
                    if re.search(r"(?i)\b(journal|transactions|letters|magazine|review|annals|acta|bulletin)\b", j):
                        lit_p.venue_type = "journal"
                    else:
                        lit_p.venue_type = "conference"

            # Fill missing DOI from source URL.
            if not (getattr(lit_p, "doi", None) or "").strip():
                su = (api_p.source_url or "").strip()
                import re
                m = re.search(r"doi\.org/(10\.\S+)", su, re.I)
                if m:
                    lit_p.doi = m.group(1)

            if (api_p.category or "").strip():
                lit_p.category = normalize_library_category_display(api_p.category)

        for paper in lit_list:
            paper.category = normalize_library_category_display(paper.category)
        # Missing abstracts remain missing. Search snippets are not authoritative abstracts.
        ids, added, updated = db.add_papers(lit_list, processing_options={
            "llm_classify": request.llm_classify, "download_pdfs": request.download_pdfs,
        })
        queued = len({pid for pid in ids if pid > 0})
        return SavePapersResponse(
            success=all(pid > 0 for pid in ids), added=added, updated=updated, ids=ids,
            processing_queued=queued,
            message="论文已入库，后台处理进度可在文献库查看。" if queued else "没有成功保存的论文。",
        )
    except Exception as e:
        logger.exception("POST /api/papers/save 失败")
        raise HTTPException(status_code=500, detail=str(e))

def get_paper_by_id(*, db, paper_id: int, litpaper_to_api_paper_fn) -> Paper:
    p = db.get_paper_by_id(paper_id)
    if not p:
        raise HTTPException(status_code=404, detail="文献不存在")

    if p.id is not None and not (getattr(p, "local_pdf_path", None) or "").strip():
        repaired = db.repair_library_local_pdf_paths_batch([int(p.id)])
        if repaired:
            p2 = db.get_paper_by_id(paper_id)
            if p2:
                p = p2
    return litpaper_to_api_paper_fn(p)

def update_paper_by_id(*, db, paper_id: int, body: UpdatePaperRequest) -> UpdatePaperResponse:
    try:
        from app.core.paper_paths import normalize_library_category_display

        fields = {}
        if body.notes is not None:
            fields["notes"] = body.notes
        if body.tags is not None:
            fields["tags"] = body.tags
        if body.category is not None:
            fields["category"] = normalize_library_category_display(body.category)
        if body.rating is not None:
            fields["rating"] = body.rating
        if body.read_status is not None:
            fields["read_status"] = body.read_status.value
        if body.importance is not None:
            fields["importance"] = body.importance
        ok = db.update_paper(paper_id, **fields)
        if not ok:
            raise HTTPException(status_code=404, detail="未更新或文献不存在")
        return UpdatePaperResponse(success=True, updated_fields=list(fields.keys()))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def delete_paper_by_id(*, db, paper_id: int) -> DeletePaperResponse:
    ok = db.delete_paper(paper_id)
    if not ok:
        raise HTTPException(status_code=404, detail="文献不存在")
    return DeletePaperResponse(success=True, message="已删除")

def build_library_pdf_response_service(*, paper_id: int, request: Request, db_path: str, logger_obj):
    from ..pdf.pdf_service import build_library_pdf_response

    return build_library_pdf_response(
        paper_id=int(paper_id),
        request=request,
        db_path=db_path,
        logger=logger_obj,
    )
