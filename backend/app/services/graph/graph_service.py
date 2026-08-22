"""知识图谱构建服务 —— 论文关系抽取、图谱数据管理与查询."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from ...api.repo import RelationRepository
from ...models.schemas import GraphEdge, GraphNode, LibraryGraphResponse
from ...services.papers.papers_helpers import graph_author_label, graph_author_node_id
from .kg_relations import ensure_tables

logger = logging.getLogger(__name__)

def build_library_graph(
    *,
    db: Any,
    limit: int,
    category: str | None,
    include_authors: bool,
    include_keywords: bool,
    relation_edge_limit: int,
    focus_paper_id: int | None,
    relation_types: set[str] | None = None,
    min_score: float = 0.0,
) -> LibraryGraphResponse:
    try:
        ensure_tables(db.db_path)
        repo = RelationRepository(db.db_path)
        focus_id = int(focus_paper_id) if focus_paper_id is not None else None

        if focus_id is not None:
            fp = db.get_paper_by_id(int(focus_id))
            papers = [fp] if fp else []
            if not papers:
                return LibraryGraphResponse(success=True, nodes=[], edges=[])
            paper_ids_in_view: set[int] = {int(focus_id)}
        else:
            papers = db.get_all_papers(limit=int(limit), order_by="created_at DESC")
            cat = (category or "").strip() or None
            if cat:
                papers = [p for p in papers if (getattr(p, "category", None) or "").strip() == cat]
            paper_ids_in_view = set()
            for p in papers:
                pid = int(getattr(p, "id") or 0)
                if pid > 0:
                    paper_ids_in_view.add(pid)

        nodes: dict[str, GraphNode] = {}
        edges: dict[tuple[str, str, str], GraphEdge] = {}

        def up_node(n: GraphNode):
            if n.id in nodes:
                nodes[n.id].weight = float(nodes[n.id].weight) + float(n.weight or 1.0)
                return
            nodes[n.id] = n

        def up_edge(e: GraphEdge):
            k = (e.source, e.target, e.type)
            if k in edges:
                edges[k].weight = float(edges[k].weight) + float(e.weight or 1.0)
                return
            edges[k] = e

        def edge_allowed(edge_type: str, weight: float = 0.0) -> bool:
            if float(weight or 0.0) < float(min_score or 0.0):
                return False
            return relation_types is None or edge_type in relation_types

        for p in papers:
            pid = int(getattr(p, "id") or 0)
            if pid <= 0:
                continue
            paper_node_id = f"paper:{pid}"
            up_node(GraphNode(
                id=paper_node_id, type="paper",
                label=str(getattr(p, "title", "") or f"Paper {pid}")[:140],
                paper_id=pid, year=getattr(p, "year", None),
                category=getattr(p, "category", None),
                journal=(getattr(p, "journal", None) or "").strip() or None,
                venue_type=(getattr(p, "venue_type", None) or "").strip() or None,
                weight=3.0,
            ))

            if include_authors:
                for idx, a in enumerate(getattr(p, "authors", []) or []):
                    name = (getattr(a, "name", None) or "").strip()
                    if not name:
                        continue
                    aid = graph_author_node_id(pid, idx, a)
                    alabel = graph_author_label(a, idx, aid)
                    up_node(GraphNode(id=aid, type="author", label=alabel, weight=1.0))
                    if edge_allowed("authored_by", 1.0):
                        up_edge(GraphEdge(source=aid, target=paper_node_id, type="authored_by", weight=1.0))

            kws: list[str] = []
            if include_keywords:
                for kw in (getattr(p, "keywords", None) or [])[:24]:
                    k = str(kw or "").strip()
                    if not k:
                        continue
                    kws.append(k)
                    kid = f"kw:{k.lower()}"
                    up_node(GraphNode(id=kid, type="keyword", label=k, weight=1.0))
                    if edge_allowed("has_keyword", 1.0):
                        up_edge(GraphEdge(source=kid, target=paper_node_id, type="has_keyword", weight=1.0))

            if include_keywords and len(kws) > 1:
                base = [f"kw:{k.lower()}" for k in kws[:12]]
                for i in range(len(base)):
                    for j in range(i + 1, len(base)):
                        s, t = base[i], base[j]
                        if s == t:
                            continue
                        if s > t:
                            s, t = t, s
                        if edge_allowed("co_keyword", 0.5):
                            up_edge(GraphEdge(source=s, target=t, type="co_keyword", weight=0.5))

        try:
            rel_rows: list[tuple[int, int, str, float, str]] = []
            if focus_id is not None:
                rel_rows = repo.fetch_relation_rows(
                    focus_id=int(focus_id), paper_ids=None, limit=int(relation_edge_limit),
                    relation_types=_db_relation_types(relation_types), min_score=float(min_score),
                )
                rel_paper_ids: set[int] = {int(focus_id)}
                for sid, tid, _, _, _ in rel_rows:
                    rel_paper_ids.add(int(sid))
                    rel_paper_ids.add(int(tid))
                meta = repo.papers_minimal_by_ids(rel_paper_ids)
                for pid, (title, year, cat) in meta.items():
                    nid = f"paper:{pid}"
                    if nid in nodes:
                        continue
                    up_node(GraphNode(
                        id=nid, type="paper",
                        label=(title or f"Paper {pid}")[:140],
                        paper_id=int(pid), year=year, category=cat, weight=2.0,
                    ))
            else:
                rel_rows = repo.fetch_relation_rows(
                    focus_id=None, paper_ids=paper_ids_in_view, limit=int(relation_edge_limit),
                    relation_types=_db_relation_types(relation_types), min_score=float(min_score),
                )

            for sid, tid, rel, score, evidence in rel_rows:
                s = f"paper:{int(sid)}"
                t = f"paper:{int(tid)}"
                if s not in nodes or t not in nodes:
                    continue
                raw_rel = str(rel or "related")
                paper_rel = f"paper_{raw_rel}"
                rev = f"rev_{raw_rel}" if raw_rel else "related_to"
                paper_rev_rel = f"paper_{rev}"
                score_value = float(score or 0.6)
                evidence_value = str(evidence or "").strip()[:240] or None
                if _paper_relation_direction_allowed(relation_types, raw_rel, reverse=False):
                    up_edge(GraphEdge(
                        source=s, target=t, type=paper_rel,
                        weight=score_value, evidence=evidence_value,
                    ))
                if _paper_relation_direction_allowed(relation_types, raw_rel, reverse=True):
                    up_edge(GraphEdge(
                        source=t, target=s, type=paper_rev_rel,
                        weight=score_value * 0.8, evidence=evidence_value,
                    ))
        except Exception:
            logger.warning("graph_service: paper-paper relation fetch failed", exc_info=True)

        if len(papers) > 1:
            paper_kw: dict[int, set[str]] = {}
            paper_au: dict[int, set[str]] = {}
            for p in papers:
                pid = int(getattr(p, "id") or 0)
                if pid <= 0:
                    continue
                if include_authors:
                    paper_au[pid] = {graph_author_label(a, i, "") for i, a in enumerate(getattr(p, "authors", []) or []) if (getattr(a, "name", None) or "").strip()}
                if include_keywords:
                    paper_kw[pid] = {str(k).strip().lower() for k in (getattr(p, "keywords", None) or [])[:16] if str(k).strip()}

            if include_authors:
                pids = list(paper_au.keys())
                for i in range(len(pids)):
                    for j in range(i + 1, len(pids)):
                        shared = paper_au[pids[i]] & paper_au[pids[j]]
                        if shared:
                            weight = min(2.0, len(shared) * 0.6)
                            if edge_allowed("shared_author", weight):
                                up_edge(GraphEdge(source=f"paper:{pids[i]}", target=f"paper:{pids[j]}",
                                                 type="shared_author", weight=weight))
            if include_keywords:
                pids = list(paper_kw.keys())
                for i in range(len(pids)):
                    for j in range(i + 1, len(pids)):
                        shared = paper_kw[pids[i]] & paper_kw[pids[j]]
                        if shared:
                            weight = min(2.0, len(shared) * 0.35)
                            if edge_allowed("shared_keyword", weight):
                                up_edge(GraphEdge(source=f"paper:{pids[i]}", target=f"paper:{pids[j]}",
                                                 type="shared_keyword", weight=weight))

        return LibraryGraphResponse(success=True, nodes=list(nodes.values()), edges=list(edges.values()))
    except Exception as e:
        logger.exception("graph_service.build_library_graph_failed")
        raise HTTPException(status_code=500, detail=str(e))

def _db_relation_types(relation_types: set[str] | None) -> set[str] | None:
    if relation_types is None:
        return None
    raw: set[str] = set()
    for value in relation_types:
        text = str(value or "").strip()
        if not text:
            continue
        if text.startswith("paper_rev_"):
            raw.add(text[len("paper_rev_"):])
        elif text.startswith("paper_"):
            raw.add(text[len("paper_"):])
        elif text not in {"authored_by", "has_keyword", "co_keyword", "shared_author", "shared_keyword"}:
            raw.add(text)
    return raw

def _paper_relation_direction_allowed(
    relation_types: set[str] | None,
    raw_relation: str,
    *,
    reverse: bool,
) -> bool:
    if relation_types is None:
        return True
    raw = str(raw_relation or "related")
    wanted = f"paper_rev_{raw}" if reverse else f"paper_{raw}"
    return raw in relation_types or wanted in relation_types
