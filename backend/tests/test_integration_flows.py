from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.agents.support.search_models import SearchIntent
from app.api.dependencies import get_database, get_db_path, get_searcher
from app.api.main import app
from app.core.author import Author as LitAuthor
from app.core.paper import Paper as LitPaper
from app.core.storage import PaperDatabase
from app.services.reader.paper_reader_service import PaperReaderService
from app.services.retrieval.search_cache import SearchResultCache


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def db(tmp_path):
    return PaperDatabase(str(tmp_path / "papers.db"))


@pytest.fixture
def clean_overrides():
    old = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)


@dataclass
class FakeSearchAgent:
    llm: object | None = None

    def understand_intent(self, message: str, profile: str = "accuracy") -> SearchIntent:
        return SearchIntent(
            query="graph retrieval",
            raw_user_message=message,
            keywords=["graph", "retrieval"],
            sources=["arxiv", "dblp", "openalex"],
            max_results=2,
            use_llm_rank=False,
            sort="relevance",
        )

    def explain_results(self, intent: SearchIntent, papers: list[object], mode: str = "accuracy") -> str:
        return f"测试检索完成：命中 {len(papers)} 篇。"


@dataclass
class FakeSearchBackend:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def search_async(self, query: str, sources: list[str], max_results: int, **kwargs):
        self.calls.append({"query": query, "sources": list(sources), "kwargs": kwargs})
        return [
            LitPaper(
                title="Graph Retrieval for Research",
                abstract="A graph retrieval paper for integration testing.",
                authors=[LitAuthor(name="Test Author")],
                arxiv_id="2401.00001",
                source="arxiv",
                year=2024,
                keywords=["graph", "retrieval"],
            ),
            # Same arXiv identity: the pipeline should remove this duplicate.
            LitPaper(
                title="Graph Retrieval for Research (OpenAlex copy)",
                abstract="A duplicate metadata record.",
                arxiv_id="2401.00001",
                source="openalex",
                year=2024,
            ),
            LitPaper(
                title="Neural Retrieval Benchmarks",
                abstract="A second candidate for the retrieval flow.",
                arxiv_id="2402.00002",
                source="dblp",
                year=2023,
                keywords=["retrieval"],
            ),
        ]


class FakeReaderAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def paper_reader_reply(self, context_block, history_lines, user_message, reader_snap=None):
        self.calls.append(
            {
                "context": str(context_block),
                "history": str(history_lines),
                "message": str(user_message),
            }
        )
        if "导读" in str(user_message):
            return "这是一段测试导读：论文研究图检索方法，并在检索任务上进行评估。", [], []
        return "论文回答：核心方法是图结构增强的检索。", [], []


class FakeDailyBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def search_arxiv_async(self, query: str, max_results: int, **kwargs):
        batch = []
        start = self.calls * 30
        self.calls += 1
        for i in range(30):
            idx = start + i + 1
            batch.append(
                LitPaper(
                    title=f"Daily Graph Paper {idx}",
                    abstract="A recent paper for daily recommendation integration testing.",
                    arxiv_id=f"2501.{idx:05d}",
                    source="arxiv",
                    year=2025,
                    keywords=["graph", "retrieval"],
                )
            )
        return batch


async def _read_sse_events(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]


@pytest.mark.asyncio
async def test_search_agent_stream_runs_plan_recall_and_sse(
    client, clean_overrides, monkeypatch, tmp_path
):
    from app.api.routes import search as search_route
    from app.settings import get_settings

    search_backend = FakeSearchBackend()
    monkeypatch.setattr(search_route, "get_search_agent", lambda: FakeSearchAgent())
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path / "search-cache"))
    app.dependency_overrides[get_searcher] = lambda: search_backend

    response = await client.post(
        "/api/papers/search-agent/stream",
        json={"message": "找图检索论文", "mode": "accuracy"},
    )

    assert response.status_code == 200
    events = await _read_sse_events(response)
    assert any(event.get("type") == "status" for event in events)
    stage_events = [event for event in events if event.get("type") == "stage"]
    assert stage_events
    assert {event.get("stage") for event in stage_events} >= {
        "agent_init",
        "intent_parsing",
        "cache_lookup",
        "search_pipeline",
        "request",
    }
    request_ids = {event.get("request_id") for event in stage_events if event.get("request_id")}
    assert len(request_ids) == 1
    final = next(event for event in events if event.get("type") == "final_result")
    result = final["result"]
    assert result["success"] is True
    assert result["total"] == 2
    assert len(result["papers"]) == 2
    assert search_backend.calls
    assert set(search_backend.calls[0]["sources"]) == {"arxiv", "dblp", "openalex"}

    cached_response = await client.post(
        "/api/papers/search-agent/stream",
        json={"message": "找图检索论文", "mode": "accuracy"},
    )
    cached_events = await _read_sse_events(cached_response)
    cached_final = next(event for event in cached_events if event.get("type") == "final_result")
    assert cached_final["result"]["metadata"]["cache_hit"] is True
    assert search_backend.calls == search_backend.calls[:1]


def test_search_result_cache_normalizes_keys_and_supports_clear(tmp_path, monkeypatch):
    first = SimpleNamespace(
        query="Graph Retrieval",
        raw_user_message=" find graph retrieval ",
        keywords=["Retrieval", "Graph"],
        authors=[],
        venues=["arXiv"],
        target_titles=[],
        arxiv_id_list=[],
        year_from=2020,
        year_to=2024,
        sources=["openalex", "arxiv"],
        sort="relevance",
        ranking_profile="accuracy",
        use_llm_rank=False,
        recall_max_candidates=24,
        max_results=10,
        recipe="general",
        use_tavily=False,
    )
    second = SimpleNamespace(**{**first.__dict__, "query": " graph   retrieval ", "keywords": ["graph", "retrieval"]})
    cache = SearchResultCache(db_path=tmp_path / "cache.db", ttl_sec=60)

    assert cache.key_for_plan(first, mode="accuracy") == cache.key_for_plan(second, mode="accuracy")
    assert cache.key_for_plan(first, mode="accuracy") != cache.key_for_plan(first, mode="novelty")

    key = cache.key_for_plan(first, mode="accuracy")
    assert cache.set(key, {"success": True, "papers": [{"title": "cached"}]}) is True
    assert cache.get(key)["papers"][0]["title"] == "cached"
    cache.clear()
    assert cache.get(key) is None

    from app.services.retrieval import search_cache as search_cache_module

    clock = [1000]
    monkeypatch.setattr(search_cache_module.time, "time", lambda: clock[0])
    expiring = SearchResultCache(db_path=tmp_path / "expiring.db", ttl_sec=60)
    assert expiring.set("expires", {"ok": True}) is True
    assert expiring.get("expires") == {"ok": True}
    clock[0] += 61
    assert expiring.get("expires") is None


@pytest.mark.asyncio
async def test_knowledge_graph_relation_filter_edit_and_export(client, db, clean_overrides):
    first_id, _ = db.add_paper(
        LitPaper(
            title="Graph Relation Source",
            abstract="Source paper for graph relation integration testing.",
            arxiv_id="2404.00001",
            source="arxiv",
            year=2024,
        )
    )
    second_id, _ = db.add_paper(
        LitPaper(
            title="Graph Relation Target",
            abstract="Target paper for graph relation integration testing.",
            arxiv_id="2404.00002",
            source="arxiv",
            year=2024,
        )
    )
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_db_path] = lambda: db.db_path

    update = await client.put(
        "/api/papers/graph/relations",
        json={
            "source_paper_id": first_id,
            "target_paper_id": second_id,
            "relation": "supports",
            "score": 0.82,
            "evidence": "integration test evidence",
        },
    )
    assert update.status_code == 200
    assert update.json()["relation"] == "supports"

    filtered = await client.get(
        "/api/papers/graph/library",
        params={
            "limit": 10,
            "include_authors": False,
            "include_keywords": False,
            "relation_types": "paper_supports",
            "min_score": 0.8,
        },
    )
    assert filtered.status_code == 200
    assert [edge["type"] for edge in filtered.json()["edges"]] == ["paper_supports"]

    exported_json = await client.get(
        "/api/papers/graph/library/export",
        params={"format": "json", "include_authors": False, "include_keywords": False},
    )
    assert exported_json.status_code == 200
    assert exported_json.headers["content-disposition"].endswith("papergraph.json")
    assert any(edge["type"] == "paper_supports" for edge in exported_json.json()["edges"])

    exported_csv = await client.get(
        "/api/papers/graph/library/export",
        params={"format": "csv", "include_authors": False, "include_keywords": False},
    )
    assert exported_csv.status_code == 200
    assert "record_type,id,source,target,type" in exported_csv.text
    assert "paper_supports" in exported_csv.text

    deleted = await client.delete(
        "/api/papers/graph/relations",
        params={"source_paper_id": first_id, "target_paper_id": second_id, "relation": "supports"},
    )
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_save_then_reader_and_history_flow(client, db, clean_overrides):
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_db_path] = lambda: db.db_path

    save_response = await client.post(
        "/api/papers/save",
        json={
            "papers": [
                {
                    "title": "Graph Retrieval for Research",
                    "abstract": "A paper about graph-enhanced retrieval.",
                    "arxiv_id": "2401.12345",
                    "source": "arxiv",
                    "year": 2024,
                    "authors": [{"name": "Test Author"}],
                }
            ],
            "download_pdfs": False,
            "llm_classify": False,
        },
    )
    assert save_response.status_code == 200
    saved = save_response.json()
    assert saved["success"] is True
    assert saved["added"] == 1
    paper_id = saved["ids"][0]

    library_response = await client.get("/api/papers/library", params={"limit": 10})
    assert library_response.status_code == 200
    assert library_response.json()["total"] == 1

    reader_agent = FakeReaderAgent()
    reader_service = PaperReaderService(db=db, agent=reader_agent)
    from app.api.routes import paper_reader as reader_route

    app.dependency_overrides[reader_route.get_paper_reader_service] = lambda: reader_service

    opening_response = await client.post(
        "/api/ai/paper-reader/opening", json={"paper_id": paper_id}
    )
    assert opening_response.status_code == 200
    assert "测试导读" in opening_response.json()["opening"]

    chat_response = await client.post(
        "/api/ai/paper-reader/chat",
        json={
            "paper_id": paper_id,
            "messages": [{"role": "assistant", "content": "已有导读"}],
            "user_message": "核心方法是什么？",
        },
    )
    assert chat_response.status_code == 200
    assert "图结构增强" in chat_response.json()["reply"]

    history_response = await client.get(
        "/api/ai/paper-reader/history", params={"paper_id": paper_id}
    )
    assert history_response.status_code == 200
    turns = history_response.json()["turns"]
    assert [turn["role"] for turn in turns][-2:] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_daily_recommendation_cache_and_feedback_flow(
    client, db, clean_overrides, monkeypatch
):
    from app.services.daily import daily_service
    from app.services.daily.daily_support import invalidate_user_profile_cache

    db.add_paper(
        LitPaper(
            title="Existing Graph Retrieval Paper",
            abstract="User library interest in graph retrieval.",
            arxiv_id="2403.00003",
            source="arxiv",
            year=2024,
            keywords=["graph", "retrieval"],
        )
    )
    daily_backend = FakeDailyBackend()
    invalidate_user_profile_cache()
    monkeypatch.setattr(daily_service, "get_search_agent", lambda: FakeSearchAgent())
    monkeypatch.setattr(daily_service, "is_llm_configured", lambda: False)
    app.dependency_overrides[get_db_path] = lambda: db.db_path
    app.dependency_overrides[get_searcher] = lambda: daily_backend

    response = await client.post(
        "/api/papers/daily",
        json={
            "days_back": 1,
            "personalized_k": 2,
            "library_limit": 50,
            "use_llm_theme_keywords": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["personalized"]) == 2
    calls_after_compute = daily_backend.calls

    cached = await client.get("/api/papers/daily")
    assert cached.status_code == 200
    assert len(cached.json()["personalized"]) == 2
    assert daily_backend.calls == calls_after_compute

    identity = payload["personalized"][0].get("arxiv_id")
    feedback = await client.post(
        "/api/papers/daily/feedback",
        json={
            "identity_key": f"arxiv:{identity}",
            "title": payload["personalized"][0]["title"],
            "action": "skip",
            "source_list": "personalized",
            "keywords": ["graph", "retrieval"],
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["success"] is True
