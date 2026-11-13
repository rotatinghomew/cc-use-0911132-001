"""采购篮关联分析引擎。

口径要点（对应业务说明）：
- 一次订单算一个篮子；同一 SKU 多件只贡献一次共现（集合化处理）。
- 修订（退货/撤销）按 available_at 做时点门控：as_of 之后才可知的事实一律不采用；
  窗口内只采用窗口终点之前可知的修订，避免后期更正倒灌早期结果。
- 全部退完/撤销完的行不再支持搭配，但篮子仍保留在分母中（属于一次未购买证据）。
- 可售状态为左闭右开分段，重叠时段 start 较晚者优先；无状态段表示未知，不是缺货。
- 两件商品在该篮下单时刻没有同时可售机会时，该篮不计入该商品对的分母，
  即“没有同时可售机会”不被当作一次未购买。
- 规则只在较早的提案窗口筛选，后续验证窗口不参与筛选，只做独立复算。
- 输出的是关联证据（support/confidence/lift），不是促销因果效果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Mapping

from b2b_reorder_network.network.order_store import fingerprint, stable_json

ENGINE_VERSION = "basket-insights-1.0.0"

# 稳定性与候选取舍阈值（写入输出 methodology，可解释、可复算）。
MAX_BUYER_SHARE = 0.5            # 单个买家最多占支持篮份额，防止买家集中
WILSON_Z = 1.959963984540054     # 95% 置信区间


def parse_instant(value: str) -> datetime:
    """解析带时区 ISO 8601；无时区按 UTC 处理，保证可比较。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


def iso_week_key(instant: datetime) -> str:
    iso = instant.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def day_key(instant: datetime) -> str:
    return instant.date().isoformat()


def round6(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


@dataclass(slots=True)
class AvailabilityIndex:
    """按 SKU 保存可售状态段；查询时刻落在左闭右开区间内，重叠时 start 晚者优先。"""

    segments: dict[str, list[dict[str, Any]]]

    @classmethod
    def build(cls, rows: list[Mapping[str, Any]]) -> "AvailabilityIndex":
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            sku = str(row["sku"])
            grouped.setdefault(sku, []).append(
                {
                    "start": parse_instant(row["start"]),
                    "end": parse_instant(row["end"]),
                    "available": bool(row["available"]),
                }
            )
        for segments in grouped.values():
            segments.sort(key=lambda item: (item["start"], item["end"]))
        return cls(grouped)

    def state_at(self, sku: str, instant: datetime) -> bool | None:
        """返回 True/False（可售/缺货）；无任何覆盖段返回 None（未知，非缺货）。"""
        chosen: dict[str, Any] | None = None
        for segment in self.segments.get(sku, ()):  # 已按 start 排序，后者覆盖前者
            if segment["start"] <= instant < segment["end"]:
                chosen = segment
        if chosen is None:
            return None
        return chosen["available"]


@dataclass(slots=True)
class Basket:
    order_id: str
    buyer_id: str
    placed_at: datetime
    skus: set[str]
    window: str
    removed_lines: list[str] = field(default_factory=list)
    empty_after_revision: bool = False


@dataclass(slots=True)
class RevisionLedger:
    """按 (order_id, line_id) 汇总窗口可知的退/撤件数；时点门控在构建时完成。"""

    quantities: dict[tuple[str, str], int]


def _admitted_revisions(
    revisions: list[Mapping[str, Any]], as_of: datetime, window_end: datetime
) -> RevisionLedger:
    totals: dict[tuple[str, str], int] = {}
    for revision in revisions:
        available_at = parse_instant(revision["available_at"])
        # as_of 是分析可知信息截止：之后才到达的事实完全不采用。
        if available_at > as_of:
            continue
        # 窗口门控：窗口终点之后才可知的更正不能倒灌该窗口。
        if available_at >= window_end:
            continue
        key = (str(revision["order_id"]), str(revision["line_id"]))
        totals[key] = totals.get(key, 0) + int(revision.get("quantity", 0) or 0)
    return RevisionLedger(totals)


def build_baskets(
    payload: Mapping[str, Any],
    window: str,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
    availability: AvailabilityIndex,
) -> tuple[list[Basket], list[dict[str, Any]], list[dict[str, Any]]]:
    """构建一个时点窗口内的有效篮子。

    返回 (篮子, 被排除订单, 窗口门控的修订)；门控修订在 as_of 可知但晚于窗口终点，
    因此不能倒灌该窗口，需要单独披露以保证口径可审计。
    """
    admitted = _admitted_revisions(payload.get("revisions", []), as_of, window_end)
    gated: list[dict[str, Any]] = []
    for revision in payload.get("revisions", []):
        available_at = parse_instant(revision["available_at"])
        if available_at > as_of:
            continue  # 全局晚到事实在 analyze 中统一披露
        if available_at >= window_end:
            gated.append(
                {
                    "scope": "revision",
                    "revision_id": str(revision["revision_id"]),
                    "order_id": str(revision["order_id"]),
                    "line_id": str(revision["line_id"]),
                    "reason": "revision_not_knowable_at_window_end",
                    "detail": "修订在窗口终点之后才可知，不倒灌该窗口",
                    "window": window,
                }
            )
    revisions = admitted
    baskets: list[Basket] = []
    exclusions: list[dict[str, Any]] = []
    for order in payload.get("orders", []):
        placed_at = parse_instant(order["placed_at"])
        if not (window_start <= placed_at < window_end):
            continue
        available_at = parse_instant(order["available_at"])
        if available_at > as_of or available_at >= window_end:
            exclusions.append(
                {
                    "scope": "order",
                    "order_id": str(order["order_id"]),
                    "reason": "not_yet_knowable_in_window",
                    "detail": "订单事实在窗口终点之后才可知，不能倒灌该窗口",
                }
            )
            continue
        skus: set[str] = set()
        removed_lines: list[str] = []
        for line in order.get("lines", []):
            line_id = str(line["line_id"])
            original = int(line.get("quantity", 0) or 0)
            revised = revisions.quantities.get((str(order["order_id"]), line_id), 0)
            effective = max(original - revised, 0)
            if effective <= 0:
                removed_lines.append(line_id)
                continue
            skus.add(str(line["sku"]))  # 多件同款只计一次
        empty_after_revision = bool(removed_lines) and not skus
        baskets.append(
            Basket(
                order_id=str(order["order_id"]),
                buyer_id=str(order["buyer_id"]),
                placed_at=placed_at,
                skus=skus,
                window=window,
                removed_lines=removed_lines,
                empty_after_revision=empty_after_revision,
            )
        )
    return baskets, exclusions, gated


@dataclass(slots=True)
class PairMetrics:
    pair: tuple[str, str]
    joint_opportunity_baskets: int          # 两 SKU 同时可售的篮子数（分母）
    unknown_opportunity_baskets: int        # 至少一个 SKU 可售状态未知的篮子数
    stockout_opportunity_baskets: int       # 至少一个 SKU 缺货、无同时可售机会的篮子数
    support_baskets: int                    # 同时购买的篮子数（两 SKU 均有效在售）
    antecedent_baskets: dict[str, int]      # 每个 SKU 在联合机会篮中的有效购买数
    support_order_ids: list[str]
    excluded_order_ids: list[str]           # 有同时可售机会但未同时购买（含全退后不支持）
    unknown_order_ids: list[str]
    stockout_order_ids: list[str]
    buyers: set[str]
    weeks: set[str]
    days: set[str]
    buyer_counts: dict[str, int]


def evaluate_pair(
    pair: tuple[str, str], baskets: list[Basket], availability: AvailabilityIndex
) -> PairMetrics:
    sku_a, sku_b = pair
    joint = 0
    unknown = 0
    stockout = 0
    support = 0
    antecedent: dict[str, int] = {sku_a: 0, sku_b: 0}
    support_ids: list[str] = []
    excluded_ids: list[str] = []
    unknown_ids: list[str] = []
    stockout_ids: list[str] = []
    buyers: set[str] = set()
    weeks: set[str] = set()
    days: set[str] = set()
    buyer_counts: dict[str, int] = {}
    for basket in baskets:
        state_a = availability.state_at(sku_a, basket.placed_at)
        state_b = availability.state_at(sku_b, basket.placed_at)
        if state_a is None or state_b is None:
            # 无状态段=未知机会：不计入分母，也不算未购买，单独披露。
            unknown += 1
            unknown_ids.append(basket.order_id)
            continue
        if not state_a or not state_b:
            # 至少一件缺货 -> 没有同时可售机会，不算一次未购买。
            stockout += 1
            stockout_ids.append(basket.order_id)
            continue
        joint += 1
        bought_a = sku_a in basket.skus
        bought_b = sku_b in basket.skus
        if bought_a:
            antecedent[sku_a] += 1
        if bought_b:
            antecedent[sku_b] += 1
        if bought_a and bought_b:
            support += 1
            support_ids.append(basket.order_id)
            buyers.add(basket.buyer_id)
            weeks.add(iso_week_key(basket.placed_at))
            days.add(day_key(basket.placed_at))
            buyer_counts[basket.buyer_id] = buyer_counts.get(basket.buyer_id, 0) + 1
        else:
            excluded_ids.append(basket.order_id)
    return PairMetrics(
        pair=pair,
        joint_opportunity_baskets=joint,
        unknown_opportunity_baskets=unknown,
        stockout_opportunity_baskets=stockout,
        support_baskets=support,
        antecedent_baskets=antecedent,
        support_order_ids=support_ids,
        excluded_order_ids=excluded_ids,
        unknown_order_ids=unknown_ids,
        stockout_order_ids=stockout_ids,
        buyers=buyers,
        weeks=weeks,
        days=days,
        buyer_counts=buyer_counts,
    )


def wilson_interval(successes: int, trials: int) -> tuple[float, float] | None:
    if trials <= 0:
        return None
    phat = successes / trials
    denominator = 1 + WILSON_Z * WILSON_Z / trials
    center = (phat + WILSON_Z * WILSON_Z / (2 * trials)) / denominator
    margin = (
        WILSON_Z
        * ((phat * (1 - phat) + WILSON_Z * WILSON_Z / (4 * trials)) / trials) ** 0.5
        / denominator
    )
    return max(center - margin, 0.0), min(center + margin, 1.0)


def _ratio_interval(
    numer_hits: int,
    numer_trials: int,
    denom_hits: int,
    denom_trials: int,
    point_estimate: float,
) -> tuple[float, float] | None:
    """用分子/分母各自 Wilson 端点构造提升比的保守区间；零证据返回 None。"""
    numer = wilson_interval(numer_hits, numer_trials)
    denom = wilson_interval(denom_hits, denom_trials)
    if numer is None or denom is None or denom[0] <= 0.0:
        return None
    # 比值区间：下端=分子下界/分母上界，上端=分子上界/分母下界。
    return point_estimate * (numer[0] / numer[1]), point_estimate * (numer[1] / denom[0])


def directional_metrics(metrics: PairMetrics, antecedent: str, consequent: str) -> dict[str, Any]:
    """计算某一方向 A->B 的支持度、条件购买概率、相对基准变化（lift）与不确定性。"""
    n_joint = metrics.joint_opportunity_baskets
    n_support = metrics.support_baskets
    n_antecedent = metrics.antecedent_baskets[antecedent]
    n_consequent = metrics.antecedent_baskets[consequent]
    support = n_support / n_joint if n_joint else None
    confidence = n_support / n_antecedent if n_antecedent else None
    baseline = n_consequent / n_joint if n_joint else None
    lift = confidence / baseline if confidence is not None and baseline else None
    confidence_interval = (
        wilson_interval(n_support, n_antecedent) if n_antecedent else None
    )
    baseline_interval = wilson_interval(n_consequent, n_joint) if n_joint else None
    lift_interval = (
        _ratio_interval(n_support, n_antecedent, n_consequent, n_joint, lift)
        if lift is not None
        else None
    )
    return {
        "direction": f"{antecedent}->{consequent}",
        "antecedent": antecedent,
        "consequent": consequent,
        "support": round6(support),
        "support_baskets": n_support,
        "confidence": round6(confidence),
        "confidence_interval_95": (
            [round6(confidence_interval[0]), round6(confidence_interval[1])]
            if confidence_interval
            else None
        ),
        "baseline_probability": round6(baseline),
        "baseline_interval_95": (
            [round6(baseline_interval[0]), round6(baseline_interval[1])]
            if baseline_interval
            else None
        ),
        "lift": round6(lift),
        "lift_interval_95": (
            [round6(lift_interval[0]), round6(lift_interval[1])]
            if lift_interval
            else None
        ),
    }


def _intervals_overlap(left: list[float] | None, right: list[float] | None) -> bool:
    if not left or not right:
        return False
    return left[0] <= right[1] and right[0] <= left[1]


def _relative_change(proposal: float | None, validation: float | None) -> float | None:
    if proposal in (None, 0) or validation is None:
        return None
    return round6((validation - proposal) / abs(proposal))


def validate_rule(
    proposal_direction: Mapping[str, Any], validation_direction: Mapping[str, Any]
) -> dict[str, Any]:
    """在未参与筛选的验证窗口独立复算，并给出可解释的稳定性结论。"""
    confidence_change = _relative_change(
        proposal_direction.get("confidence"), validation_direction.get("confidence")
    )
    lift_change = _relative_change(
        proposal_direction.get("lift"), validation_direction.get("lift")
    )
    stable_confidence = _intervals_overlap(
        proposal_direction.get("confidence_interval_95"),
        validation_direction.get("confidence_interval_95"),
    )
    stable_lift = _intervals_overlap(
        proposal_direction.get("lift_interval_95"),
        validation_direction.get("lift_interval_95"),
    )
    validation_n = validation_direction.get("support_baskets", 0)
    if validation_n <= 0:
        status = "not_validated"
        justification = "验证窗口联合机会篮中没有复现购买，关联未在独立时段复现"
    elif stable_confidence and stable_lift:
        status = "validated"
        justification = "验证窗口条件购买概率与提升比的 95% 区间均与提案窗口重叠"
    else:
        status = "not_validated"
        justification = "验证窗口指标的不确定性区间与提案窗口不重叠，稳定性不足"
    return {
        "status": status,
        "justification": justification,
        "confidence_relative_change": confidence_change,
        "lift_relative_change": lift_change,
        "confidence_interval_overlap": stable_confidence,
        "lift_interval_overlap": stable_lift,
        "window_metrics": validation_direction,
    }


def _candidate_pair(sku_a: str, sku_b: str, catalog: list[str]) -> dict[str, Any]:
    return {
        "pair": [sku_a, sku_b],
        "cross_category": sku_a in catalog and sku_b in catalog,
    }


def analyze(payload: Mapping[str, Any]) -> dict[str, Any]:
    as_of = parse_instant(payload["as_of"])
    proposal_end = parse_instant(payload["analysis"]["proposal_end"])
    validation_end = parse_instant(payload["analysis"]["validation_end"])
    min_buyers = int(payload["analysis"].get("min_buyers", 3))
    max_rules = int(payload["analysis"].get("max_rules", 8))
    availability = AvailabilityIndex.build(payload.get("availability", []))

    catalog = [str(sku) for sku in payload.get("catalog", [])]
    observed = {
        str(line["sku"])
        for order in payload.get("orders", [])
        for line in order.get("lines", [])
    }
    skus = sorted(observed | set(catalog))

    # 最早订单时刻作为提案窗口起点，避免人为下界。
    order_instants = [parse_instant(order["placed_at"]) for order in payload.get("orders", [])]
    window_start = min(order_instants) if order_instants else parse_instant("1970-01-01T00:00:00+00:00")

    proposal_baskets, proposal_order_exclusions, proposal_gated = build_baskets(
        payload, "proposal", window_start, proposal_end, as_of, availability
    )
    validation_baskets, validation_order_exclusions, validation_gated = build_baskets(
        payload, "validation", proposal_end, validation_end, as_of, availability
    )

    global_exclusions: list[dict[str, Any]] = []
    global_exclusions.extend(proposal_order_exclusions)
    global_exclusions.extend(validation_order_exclusions)
    global_exclusions.extend(proposal_gated)
    global_exclusions.extend(validation_gated)

    # 晚到事实（as_of 之后才可知）全局披露，任何窗口都不采用。
    for revision in payload.get("revisions", []):
        if parse_instant(revision["available_at"]) > as_of:
            global_exclusions.append(
                {
                    "scope": "revision",
                    "revision_id": str(revision["revision_id"]),
                    "order_id": str(revision["order_id"]),
                    "line_id": str(revision["line_id"]),
                    "reason": "late_fact_beyond_as_of",
                    "detail": "修订在 as_of 之后才可知，本次分析不采用，也不倒灌早期窗口",
                }
            )
    # 窗口内可知、并导致整行失效的退货/撤销，逐篮披露。
    for basket in proposal_baskets + validation_baskets:
        for line_id in basket.removed_lines:
            global_exclusions.append(
                {
                    "scope": "line",
                    "order_id": basket.order_id,
                    "line_id": line_id,
                    "reason": "fully_returned_or_cancelled",
                    "detail": "该订单行有效数量为零，不再支持任何搭配；篮子保留在分母中",
                    "window": basket.window,
                    "basket_emptied": basket.empty_after_revision,
                }
            )

    pair_exclusions: list[dict[str, Any]] = []
    eligible: list[tuple[PairMetrics, PairMetrics]] = []
    all_pairs = list(combinations(skus, 2))

    for sku_a, sku_b in all_pairs:
        proposal_metrics = evaluate_pair((sku_a, sku_b), proposal_baskets, availability)
        validation_metrics = evaluate_pair((sku_a, sku_b), validation_baskets, availability)
        candidate = _candidate_pair(sku_a, sku_b, catalog)
        if proposal_metrics.joint_opportunity_baskets <= 0:
            pair_exclusions.append(
                {
                    **candidate,
                    "reason": "zero_simultaneous_opportunity",
                    "detail": "提案窗口两件商品从未同时可售，不存在有效分母；不能把无证据包装成零提升",
                    "proposal": _metrics_snapshot(proposal_metrics),
                }
            )
            continue
        stock_gap_note = None
        if proposal_metrics.stockout_opportunity_baskets > 0:
            stock_gap_note = (
                f"提案窗口另有 {proposal_metrics.stockout_opportunity_baskets} 个篮子因至少一件缺货"
                "而没有同时可售机会，这些篮子不计入分母也不算未购买"
            )
        if proposal_metrics.support_baskets <= 0:
            pair_exclusions.append(
                {
                    **candidate,
                    "reason": "no_observed_co_purchase",
                    "detail": "存在同时可售机会但没有同时购买证据，支持度为零，不产出规则",
                    "stock_gap_note": stock_gap_note,
                    "proposal": _metrics_snapshot(proposal_metrics),
                    "validation": _metrics_snapshot(validation_metrics),
                }
            )
            continue
        distinct_buyers = len(proposal_metrics.buyers)
        if distinct_buyers < min_buyers:
            pair_exclusions.append(
                {
                    **candidate,
                    "reason": "insufficient_distinct_buyers",
                    "detail": (
                        f"支持篮仅来自 {distinct_buyers} 个买家，低于 min_buyers={min_buyers}；"
                        "重复返单不能替代买家多样性。"
                        + (f"同时{stock_gap_note}。" if stock_gap_note else "")
                        + " 表面高共现主要由单一采购商的重复大单与有限可售窗口造成，属于虚高搭配"
                    ),
                    "stock_gap_note": stock_gap_note,
                    "proposal": _metrics_snapshot(proposal_metrics),
                    "validation": _metrics_snapshot(validation_metrics),
                }
            )
            continue
        top_buyer_share = max(proposal_metrics.buyer_counts.values()) / proposal_metrics.support_baskets
        if top_buyer_share > MAX_BUYER_SHARE:
            pair_exclusions.append(
                {
                    **candidate,
                    "reason": "buyer_concentration",
                    "detail": (
                        f"最大买家占支持篮份额 {round6(top_buyer_share)}，超过上限 {MAX_BUYER_SHARE}，"
                        "单个采购商不能决定名单"
                    ),
                    "proposal": _metrics_snapshot(proposal_metrics),
                    "validation": _metrics_snapshot(validation_metrics),
                }
            )
            continue
        if len(proposal_metrics.weeks) < 2:
            pair_exclusions.append(
                {
                    **candidate,
                    "reason": "single_promotion_week",
                    "detail": (
                        f"支持篮只落在 {sorted(proposal_metrics.weeks)} 一个 ISO 周，"
                        "短促销周不能独立决定搭配名单"
                    ),
                    "proposal": _metrics_snapshot(proposal_metrics),
                    "validation": _metrics_snapshot(validation_metrics),
                }
            )
            continue
        eligible.append((proposal_metrics, validation_metrics))

    # 候选规模取舍：按支持篮数、买家数排序后截断到 max_rules，截断本身写入 exclusions。
    eligible.sort(
        key=lambda item: (
            -item[0].support_baskets,
            -len(item[0].buyers),
            item[0].pair,
        )
    )
    if len(eligible) > max_rules:
        for dropped_proposal, _ in eligible[max_rules:]:
            pair_exclusions.append(
                {
                    **_candidate_pair(*dropped_proposal.pair, catalog=catalog),
                    "reason": "candidate_capacity_limit",
                    "detail": f"候选数超过 max_rules={max_rules}，按支持篮数与买家数排序后截断",
                    "proposal": _metrics_snapshot(dropped_proposal),
                }
            )
    chosen = eligible[:max_rules]

    rules: list[dict[str, Any]] = []
    for proposal_metrics, validation_metrics in chosen:
        sku_a, sku_b = proposal_metrics.pair
        rule_id = "rule-" + fingerprint([sku_a, sku_b])[:12]
        directions: list[dict[str, Any]] = []
        validation_block: list[dict[str, Any]] = []
        for antecedent, consequent in ((sku_a, sku_b), (sku_b, sku_a)):
            proposal_direction = directional_metrics(
                proposal_metrics, antecedent, consequent
            )
            validation_direction = directional_metrics(
                validation_metrics, antecedent, consequent
            )
            directions.append(proposal_direction)
            validation_block.append(
                validate_rule(proposal_direction, validation_direction)
            )
        top_buyer_share = (
            max(proposal_metrics.buyer_counts.values()) / proposal_metrics.support_baskets
        )
        rules.append(
            {
                "rule_id": rule_id,
                "pair": [sku_a, sku_b],
                "cross_category": sku_a in catalog and sku_b in catalog,
                "evidence_type": "association_only",
                "causal_note": "观测性关联证据：无促销处理/对照实验数据，lift 仅衡量相对基准的购买变化，不能解释为促销因果提升",
                "proposal": {
                    "window": {"start": window_start.isoformat(), "end": proposal_end.isoformat()},
                    "joint_opportunity_baskets": proposal_metrics.joint_opportunity_baskets,
                    "unknown_opportunity_baskets": proposal_metrics.unknown_opportunity_baskets,
                    "stockout_opportunity_baskets": proposal_metrics.stockout_opportunity_baskets,
                    "support_baskets": proposal_metrics.support_baskets,
                    "distinct_buyers": len(proposal_metrics.buyers),
                    "top_buyer_share": round6(top_buyer_share),
                    "distinct_iso_weeks": sorted(proposal_metrics.weeks),
                    "distinct_days": sorted(proposal_metrics.days),
                    "directions": directions,
                },
                "validation": validation_block,
                "evidence_orders": {
                    "support_order_ids": proposal_metrics.support_order_ids,
                    "excluded_order_ids": proposal_metrics.excluded_order_ids,
                    "unknown_opportunity_order_ids": proposal_metrics.unknown_order_ids,
                    "stockout_opportunity_order_ids": proposal_metrics.stockout_order_ids,
                    "validation_support_order_ids": validation_metrics.support_order_ids,
                    "validation_excluded_order_ids": validation_metrics.excluded_order_ids,
                },
            }
        )

    return {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "as_of": payload["as_of"],
        "windows": {
            "proposal": {"start": window_start.isoformat(), "end": payload["analysis"]["proposal_end"]},
            "validation": {
                "start": payload["analysis"]["proposal_end"],
                "end": payload["analysis"]["validation_end"],
            },
        },
        "basket_counts": {
            "proposal_total": len(proposal_baskets),
            "proposal_non_empty": sum(1 for basket in proposal_baskets if basket.skus),
            "proposal_emptied_by_revision": sum(
                1 for basket in proposal_baskets if basket.empty_after_revision
            ),
            "validation_total": len(validation_baskets),
            "validation_non_empty": sum(1 for basket in validation_baskets if basket.skus),
            "validation_emptied_by_revision": sum(
                1 for basket in validation_baskets if basket.empty_after_revision
            ),
        },
        "rules": rules,
        "exclusions": {"global": global_exclusions, "candidates": pair_exclusions},
        "methodology": {
            "association_measures": [
                "support=联合可售机会篮中同时购买的篮子占比",
                "confidence=给定 antecedent 有效购买时 consequent 的条件购买概率",
                "lift=confidence 除以 consequent 在联合机会篮中的基准购买概率",
            ],
            "uncertainty": "条件概率与基准概率使用 Wilson 95% 区间；lift 区间由两端点保守构造",
            "stability": "规则只在提案窗口筛选；验证窗口独立复算，区间重叠且有复现购买记为 validated",
            "opportunity_rule": "两 SKU 未同时可售的篮子不计入该商品对分母；可售状态未知的篮子单独披露，不视为未购买",
            "buyer_guardrails": {
                "min_buyers": min_buyers,
                "max_buyer_share": MAX_BUYER_SHARE,
                "min_distinct_iso_weeks": 2,
            },
            "candidate_capacity": {"max_rules": max_rules, "eligible_pairs": len(eligible)},
            "causal_boundary": "本分析只输出相关搭配；没有干预/对照组，不声明促销因果效果",
        },
    }


def _metrics_snapshot(metrics: PairMetrics) -> dict[str, Any]:
    return {
        "joint_opportunity_baskets": metrics.joint_opportunity_baskets,
        "unknown_opportunity_baskets": metrics.unknown_opportunity_baskets,
        "stockout_opportunity_baskets": metrics.stockout_opportunity_baskets,
        "stockout_order_ids": metrics.stockout_order_ids,
        "support_baskets": metrics.support_baskets,
        "distinct_buyers": len(metrics.buyers),
        "buyer_ids": sorted(metrics.buyers),
        "distinct_iso_weeks": sorted(metrics.weeks),
        "support_order_ids": metrics.support_order_ids,
    }


def input_fingerprint(payload: Mapping[str, Any]) -> str:
    return fingerprint(stable_json(payload))
