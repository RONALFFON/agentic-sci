"""固定案例评测。默认离线汇总；只有 --live 才调用配置的真实模型。"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
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
    if len({row['id'] for row in predictions}) != len(predictions):
        raise ValueError('预测中存在重复案例 ID')
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
            if not metrics.get("usage_missing", 0) and 'total_tokens' in metrics:
                tokens.append(metrics.get("total_tokens", 0))
    return {
        "case_count": len(cases), "observed_count": len(set(rows) & {case["id"] for case in cases}),
        "coverage": len(set(rows) & {case['id'] for case in cases}) / len(cases) if cases else 0,
        "missing_ids": [case['id'] for case in cases if case['id'] not in rows],
        "intent_accuracy": mean(intents), "constraint_satisfaction": mean(constraints),
        f"recall@{k}": mean(recalls), f"ndcg@{k}": mean(ndcgs), "evidence_support_rate": mean(evidence),
        "p95_latency_ms": sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        "mean_model_calls": mean(calls), "mean_total_tokens_when_available": mean(tokens),
        "calls_without_usage": usage_missing,
        "sample_counts": {"intent": len(intents), "constraints": len(constraints), "retrieval": len(recalls), "evidence": len(evidence)},
    }


def compare_reports(cases, predictions, baseline, k):
    """只比较两个版本共同完成的案例，避免覆盖率差异伪装成质量提升。"""
    common = {row['id'] for row in predictions} & {row['id'] for row in baseline['predictions']}
    paired_cases = [case for case in cases if case['id'] in common]
    old_rows = {row['id']: row for row in baseline['predictions']}
    new_rows = {row['id']: row for row in predictions}
    old_paired, new_paired = [], []
    for case in paired_cases:
        old, new = old_rows[case['id']], new_rows[case['id']]
        shared = set(old) & set(new)
        left, right = ({key: row[key] for key in shared} for row in (old, new))
        if 'metrics' in shared:
            keys = set(old['metrics']) & set(new['metrics'])
            left['metrics'] = {key: old['metrics'][key] for key in keys}
            right['metrics'] = {key: new['metrics'][key] for key in keys}
            if old['metrics'].get('usage_missing') or new['metrics'].get('usage_missing'):
                left['metrics'].pop('total_tokens', None)
                right['metrics'].pop('total_tokens', None)
        old_paired.append(left)
        new_paired.append(right)
    before = evaluate(paired_cases, old_paired, k)
    after = evaluate(paired_cases, new_paired, k)
    higher_better = {'intent_accuracy', 'constraint_satisfaction', f'recall@{k}', f'ndcg@{k}', 'evidence_support_rate'}
    lower_better = {'p95_latency_ms', 'mean_model_calls', 'mean_total_tokens_when_available'}
    changes = {}
    for key in sorted(higher_better | lower_better):
        if before.get(key) is None or after.get(key) is None:
            continue
        delta = after[key] - before[key]
        changes[key] = {'baseline': before[key], 'candidate': after[key], 'delta': delta,
                        'regressed': delta < 0 if key in higher_better else delta > 0}
    return {'paired_case_count': len(paired_cases), 'changes': changes}


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
    parser.add_argument('--baseline', type=Path, help='同一案例集的历史报告')
    parser.add_argument('--min-coverage', type=float, default=0, help='最低案例覆盖率，0..1')
    parser.add_argument('--fail-on-regression', action='store_true')
    args = parser.parse_args()
    if args.live and args.predictions:
        parser.error("--live 与 --predictions 不能同时使用")
    if not 1 <= args.max_calls <= 100 or not 1 <= args.k <= 100:
        parser.error("调用预算和 K 必须在 1..100 内")
    if not 0 <= args.min_coverage <= 1 or (args.fail_on_regression and not args.baseline):
        parser.error('覆盖率必须在 0..1 内，回归门禁必须指定基线')
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if len({case['id'] for case in cases}) != len(cases):
        parser.error('案例 ID 重复')
    fingerprint = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    predictions = live_intents(cases, args.max_calls) if args.live else json.loads(args.predictions.read_text(encoding="utf-8")) if args.predictions else []
    report = {"mode": "live-intent" if args.live else "offline", "metrics": evaluate(cases, predictions, args.k), "predictions": predictions}
    report.update(cases_sha256=fingerprint, k=args.k)
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        if baseline.get('cases_sha256') != fingerprint or baseline.get('k') != args.k:
            parser.error('基线必须来自同一案例集及相同 K 值，请重新生成基线报告')
        report['comparison'] = compare_reports(cases, predictions, baseline, args.k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"已写入评测报告：{args.output}")
    if report['metrics']['coverage'] < args.min_coverage:
        raise SystemExit('案例覆盖率低于要求；报告已保留')
    if args.fail_on_regression:
        comparison = report['comparison']
        if not comparison['changes'] or any(item['regressed'] for item in comparison['changes'].values()):
            raise SystemExit('存在指标回退或没有可比较指标；报告已保留')


if __name__ == "__main__":
    main()
