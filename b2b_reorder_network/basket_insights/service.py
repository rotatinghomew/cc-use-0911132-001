"""采购篮分析应用服务：编排引擎、幂等存储与版本谱系。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from b2b_reorder_network.basket_insights.engine import (
    ENGINE_VERSION,
    analyze,
    input_fingerprint,
)
from b2b_reorder_network.basket_insights.store import AnalysisStore
from b2b_reorder_network.network.order_store import DomainError


def build_analysis_id(fingerprint: str) -> str:
    return "basket-" + fingerprint[:16]


def run_analysis(input_path: str | Path, database: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise DomainError("分析输入必须是 JSON 对象")
    for required in ("as_of", "orders", "availability", "analysis", "source_versions"):
        if required not in payload:
            raise DomainError(f"分析输入缺少字段：{required}")

    store = AnalysisStore(database)
    fingerprint = input_fingerprint(payload)
    analysis_id = build_analysis_id(fingerprint)

    existing = store.find_by_fingerprint(fingerprint)
    if existing is not None:
        # 同一批明细重放：复用既有分析记录，只标记重放，不产生第二份分析。
        existing = dict(existing)
        existing["replayed"] = True
        return existing

    result = analyze(payload)

    # 新版本（例如退货修订变化）另存；与同库上一版分析建立谱系，便于对照。
    previous = store.latest_analysis()
    supersedes = previous["analysis_id"] if previous else None

    baseline_case_id = payload.get("baseline_case_id")
    baseline_run_id = store.find_baseline_run(baseline_case_id)

    envelope = {
        "analysis_id": analysis_id,
        "input_fingerprint": fingerprint,
        "source_versions": dict(payload["source_versions"]),
        "engine_version": ENGINE_VERSION,
        "baseline_case_id": baseline_case_id,
        "baseline_run_id": baseline_run_id,
        "lineage": {
            "supersedes_analysis_id": supersedes,
            "previous_input_fingerprint": (
                previous["input_fingerprint"] if previous else None
            ),
        },
        "rules": result["rules"],
        "validation": {
            "window": result["windows"]["validation"],
            "rules": [
                {
                    "pair": rule["pair"],
                    "rule_id": rule["rule_id"],
                    "checks": rule["validation"],
                }
                for rule in result["rules"]
            ],
            "summary": _validation_summary(result["rules"]),
        },
        "exclusions": result["exclusions"],
        "windows": result["windows"],
        "basket_counts": result["basket_counts"],
        "methodology": result["methodology"],
        "replayed": False,
    }

    store.save(
        analysis_id=analysis_id,
        input_fingerprint=fingerprint,
        source_versions=envelope["source_versions"],
        engine_version=ENGINE_VERSION,
        baseline_case_id=baseline_case_id,
        baseline_run_id=baseline_run_id,
        supersedes_analysis_id=supersedes,
        result=envelope,
    )
    return envelope


def _validation_summary(rules: list[Mapping[str, Any]]) -> dict[str, int]:
    summary = {"validated": 0, "not_validated": 0, "insufficient_evidence": 0}
    for rule in rules:
        for check in rule.get("validation", []):
            status = check.get("status")
            if status in summary:
                summary[status] += 1
    return summary
