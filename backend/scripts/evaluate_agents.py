"""固定案例评测。默认离线汇总；只有 --live 才调用配置的真实模型。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mean(values):
    return statistics.mean(values) if values else None


def evaluate(cases, predictions, k=10):
    rows = {row["id"]: row for row in predictions}
    intents, constraints, recalls, ndcgs, evidence, latencies, calls, tokens = ([] for _ in range(8))
    usage_missing = 0
    for case in cases:
        row = rows.get(case["id"])
        if row is None:
            continue
        if "intent" in row and case.get("expected"):
            checks = []
            for field, expected in case["expected"].items():
                actual = row["intent"].get(field)
                valid = (actual == [] if not expected else isinstance(actual, list) and all(value in actual for value in expected)) if isinstance(expected, list) else actual == expected
                checks.append(valid)
            intents.append(float(all(checks)))
            constraints.extend(map(float, checks))
        judgments = case.get("judgments")
        if judgments and "ranked_ids" in row:
            ranked = list(dict.fromkeys(map(str, row["ranked_ids"])))[:k]
            relevant = {str(key) for key, grade in judgments.items() if grade > 0}
            if relevant:
                recalls.append(len(set(ranked) & relevant) / len(relevant))
            gains = [float(judgments.get(key, 0)) for key in ranked]
            ideal = sorted(map(float, judgments.values()), reverse=True)[:k]
            dcg = lambda grades: sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(grades))
            if dcg(ideal):
                ndcgs.append(dcg(gains) / dcg(ideal))
        # supported 必须由人工或独立评审提供，不能用模型自己的声明充当金标。
        evidence.extend(float(item["supported"]) for item in row.get("evidence_reviews", []) if type(item.get("supported")) is bool)
        if isinstance(row.get("latency_ms"), (float, int)):
            latencies.append(row["latency_ms"])
        metrics = row.get("metrics", {})
        if "calls" in metrics:
            calls.append(metrics["calls"])
            usage_missing += metrics.get("usage_missing", 0)
            if not metrics.get("usage_missing", 0):
                tokens.append(metrics.get("total_tokens", 0))
    return {
        "case_count": len(cases), "observed_count": len(set(rows) & {case["id"] for case in cases}),
        "intent_accuracy": mean(intents), "constraint_satisfaction": mean(constraints),
        f"recall@{k}": mean(recalls), f"ndcg@{k}": mean(ndcgs), "evidence_support_rate": mean(evidence),
        "p95_latency_ms": sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        "mean_model_calls": mean(calls), "mean_total_tokens_when_available": mean(tokens),
        "calls_without_usage": usage_missing,
        "sample_counts": {"intent": len(intents), "constraints": len(constraints), "retrieval": len(recalls), "evidence": len(evidence)},
    }


def live_intents(cases, max_calls):
    from app.agents.search_agent import SearchAgent
    from app.services.llm.agent_runtime import execution_scope
    from app.services.llm.llm_service import get_llm
    from app.services.search_intent.conversation import format_search_message

    llm = get_llm()
    original_invoke = llm.invoke
    count = 0
    def limited_invoke(*args, **kwargs):
        nonlocal count
        if count >= max_calls:
            raise RuntimeError("eval_call_budget_exhausted")
        count += 1
        return original_invoke(*args, **kwargs)
    # 仅覆盖当前评测进程中的实例，finally 恢复；不修改 SDK 全局类。
    llm.invoke = limited_invoke
    predictions = []
    try:
        agent = SearchAgent()
        for case in cases:
            if case.get("kind") != "intent" or count >= max_calls:
                continue
            started = time.monotonic()
            with execution_scope(60, independent=True) as budget:
                row = {"id": case["id"]}
                try:
                    # 直调解析阶段，避免缓存把真实模型评测变成命中率评测。
                    intent = agent.intent_parser._parse_with_retry(format_search_message(case["message"], case.get("history", [])), "accuracy")
                    row["intent"] = dataclasses.asdict(intent)
                except Exception as exc:
                    row["intent"], row["error"] = {}, type(exc).__name__
                row["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                row["metrics"] = budget.stats.snapshot()
                predictions.append(row)
    finally:
        llm.invoke = original_invoke
    return predictions


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/agent_cases.json")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-calls", type=int, default=10)
    parser.add_argument("--k", type=int, default=10)
    args = parser.parse_args()
    if args.live and args.predictions:
        parser.error("--live 与 --predictions 不能同时使用")
    if not 1 <= args.max_calls <= 100 or not 1 <= args.k <= 100:
        parser.error("调用预算和 K 必须在 1..100 内")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    predictions = live_intents(cases, args.max_calls) if args.live else json.loads(args.predictions.read_text(encoding="utf-8")) if args.predictions else []
    report = {"mode": "live-intent" if args.live else "offline", "metrics": evaluate(cases, predictions, args.k), "predictions": predictions}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"已写入评测报告：{args.output}")


if __name__ == "__main__":
    main()
