"""共享 JSON 解码及任务输出约束，不依赖供应商的结构化输出扩展。"""
from __future__ import annotations

import json
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


def parse_json(text: str):
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
    return json.loads(value)


class TaskOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class DailySelection(TaskOutput):
    personalized: list[StrictInt]
    general: list[StrictInt]


class NegativePreference(TaskOutput):
    topics_to_downrank: list[str] = Field(default_factory=list, max_length=8)
    subdomains_to_downrank: list[str] = Field(default_factory=list, max_length=5)
    venues_to_downrank: list[str] = Field(default_factory=list, max_length=3)
    sources_to_downrank: list[Literal["arxiv", "openalex", "dblp"]] = Field(default_factory=list, max_length=2)
    styles_to_downrank: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


def valid_indices(values, size: int, cap: int, *, excluded=()) -> list[int]:
    seen = set(excluded)
    result = []
    for index in values if isinstance(values, list) else []:
        if type(index) is int and 0 <= index < size and index not in seen:
            seen.add(index)
            result.append(index)
            if len(result) >= cap:
                break
    return result if cap > 0 else []


def finite_score(value, lower=0.0, upper=1.0) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean_score")
    score = float(value)
    if not math.isfinite(score) or not lower <= score <= upper:
        raise ValueError("score_out_of_range")
    return score


class IntentFields(TaskOutput):
    query: str | None = None
    keywords: list[str] | None = None
    authors: list[str] | None = None
    venues: list[str] | None = None
    target_titles: list[str] | None = None
    target_authors: list[str] | None = None
    arxiv_categories: list[str] | None = None
    arxiv_id_list: list[str] | None = None
    year_from: StrictInt | None = Field(default=None, ge=1900, le=2100)
    year_to: StrictInt | None = Field(default=None, ge=1900, le=2100)
    max_results: StrictInt | None = Field(default=None, ge=1, le=100)
    sort: Literal["date", "relevance"] | None = None
    use_tavily: bool | None = None
    is_short_acronym: bool | None = None
    wants_classic: bool | None = None
    wants_recent: bool | None = None
    main_conference_proceedings_only: bool | None = None
    confidence_level: Literal["high", "medium", "low"] | None = None
    search_strategy: Literal["keyword_matching", "semantic_search", "hybrid", "targeted_lookup"] | None = None
    sources: list[Literal["arxiv", "dblp", "openalex", "tavily"]] | None = None
    use_llm_rank: bool | None = None
    use_two_stage_rerank: bool | None = None
    rerank_recall_max: StrictInt | None = None
    rerank_coarse_top_n: StrictInt | None = None


def validate_intent_payload(payload: dict) -> None:
    IntentFields.model_validate(payload)
    for section in ("search", "flags", "ranking"):
        if section in payload:
            if not isinstance(payload[section], dict):
                raise ValueError(f"intent_invalid_section:{section}")
            IntentFields.model_validate(payload[section])


class ThemeKeywords(TaskOutput):
    personalized: list[str] = Field(max_length=8)
    general: list[str] = Field(max_length=8)

class ReferenceQueries(TaskOutput):
    queries: list[str] = Field(max_length=24)

class ReaderRanking(TaskOutput):
    keep_n: StrictInt = Field(ge=0, le=20)
    order: list[StrictInt]
