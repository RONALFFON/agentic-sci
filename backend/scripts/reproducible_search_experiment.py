"""离线、可复现的搜索缓存样例。

运行：
    python scripts/reproducible_search_experiment.py

脚本不访问外部 API，使用固定的 fake agent/searcher 连续运行两次同一计划，
输出第一次实时检索、第二次缓存命中的结果，适合验证缓存键和 SSE 元数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.support.search_models import SearchIntent
from app.api.routes.search import SearchAgentMessage, _run_search_agent_core
from app.core.author import Author
from app.core.paper import Paper
from app.settings import get_settings


class ReproducibleSearchAgent:
    def understand_intent(self, message: str, profile: str = "accuracy") -> SearchIntent:
        return SearchIntent(
            query="graph retrieval",
            raw_user_message=message,
            keywords=["graph", "retrieval"],
            sources=["arxiv", "dblp", "openalex"],
            max_results=3,
            use_llm_rank=False,
            sort="relevance",
        )

    def explain_results(self, intent: SearchIntent, papers: list[object], mode: str = "accuracy") -> str:
        return f"离线实验完成：{len(papers)} 篇结果。"


class ReproducibleSearchBackend:
    async def search_async(self, query: str, sources: list[str], max_results: int, **kwargs):
        return [
            Paper(
                title="Deterministic Graph Retrieval Benchmark",
                abstract="Fixed offline candidate used to verify the search pipeline.",
                authors=[Author(name="PaperGraph Test")],
                arxiv_id="2501.00001",
                source="arxiv",
                year=2025,
                keywords=["graph", "retrieval"],
            ),
            Paper(
                title="Deterministic Graph Retrieval Benchmark",
                abstract="Duplicate source record intentionally removed by the pipeline.",
                arxiv_id="2501.00001",
                source="openalex",
                year=2025,
            ),
        ]


async def run(query: str, mode: str, cache_dir: Path) -> dict[str, object]:
    settings = get_settings()
    previous_data_dir = settings.data_dir
    settings.data_dir = str(cache_dir)
    stages: list[dict[str, object]] = []
    try:
        request = SearchAgentMessage(message=query, mode=mode)
        agent = ReproducibleSearchAgent()
        backend = ReproducibleSearchBackend()

        def on_stage(stage: str, status: str, message: str, details: dict[str, object]) -> None:
            stages.append({"stage": stage, "status": status, "message": message, **details})

        first = await _run_search_agent_core(
            agent=agent,
            request=request,
            merged_query=query,
            searcher=backend,
            stage_callback=on_stage,
        )
        second = await _run_search_agent_core(
            agent=agent,
            request=request,
            merged_query=query,
            searcher=backend,
            stage_callback=on_stage,
        )
        return {
            "query": query,
            "mode": mode,
            "cache_dir": str(cache_dir),
            "first_run": {
                "success": first.success,
                "total": first.total,
                "cache_hit": (first.metadata or {}).get("cache_hit"),
                "cache_key": (first.metadata or {}).get("cache_key"),
            },
            "second_run": {
                "success": second.success,
                "total": second.total,
                "cache_hit": (second.metadata or {}).get("cache_hit"),
                "cache_key": (second.metadata or {}).get("cache_key"),
            },
            "stages": stages,
        }
    finally:
        settings.data_dir = previous_data_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperGraph offline search/cache experiment")
    parser.add_argument("--query", default="找图检索论文")
    parser.add_argument("--mode", choices=("accuracy", "novelty"), default="accuracy")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "data" / "experiments" / "search-cache",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args.query, args.mode, args.cache_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
