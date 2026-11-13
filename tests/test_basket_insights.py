from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from b2b_reorder_network.basket_insights.engine import (
    AvailabilityIndex,
    analyze,
    input_fingerprint,
    parse_instant,
    wilson_interval,
)
from b2b_reorder_network.basket_insights.service import build_analysis_id, run_analysis
from b2b_reorder_network.basket_insights.store import AnalysisStore

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def rule_by_pair(result: dict, pair: list[str]) -> dict | None:
    for rule in result["rules"]:
        if rule["pair"] == pair:
            return rule
    return None


def exclusion_reasons(result: dict) -> dict:
    return {tuple(item["pair"]): item["reason"] for item in result["exclusions"]["candidates"]}


class FixtureAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.result = analyze(load_fixture())

    def test_output_contract_fields(self):
        # 引擎原始结果：规则内嵌验证；服务层信封再汇总顶层 validation。
        for field in ("rules", "exclusions", "methodology", "windows", "basket_counts"):
            self.assertIn(field, self.result)
        for rule in self.result["rules"]:
            self.assertIn("validation", rule)
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(load_fixture()), encoding="utf-8")
            envelope = run_analysis(input_path, str(Path(tmp) / "a.sqlite3"))
        for field in ("analysis_id", "rules", "validation", "exclusions", "source_versions"):
            self.assertIn(field, envelope)

    def test_inflated_cover_sheet_pair_is_excluded_and_explained(self):
        reasons = exclusion_reasons(self.result)
        self.assertEqual(reasons[("cover", "sheet")], "insufficient_distinct_buyers")
        cover_sheet = next(
            item for item in self.result["exclusions"]["candidates"] if item["pair"] == ["cover", "sheet"]
        )
        snapshot = cover_sheet["proposal"]
        # 单一采购商：22 个重复返单只贡献 1 个买家
        self.assertEqual(snapshot["distinct_buyers"], 1)
        self.assertEqual(snapshot["buyer_ids"], ["buyer-wholesale"])
        # 40 个篮子处于 cover 缺货期，没有同时可售机会
        self.assertEqual(snapshot["stockout_opportunity_baskets"], 40)
        self.assertEqual(len(snapshot["stockout_order_ids"]), 40)
        # 缺货篮不被当作未购买：分母只剩 22 个真正同时可售的篮子
        self.assertEqual(snapshot["joint_opportunity_baskets"], 22)
        self.assertIn("虚高", cover_sheet["detail"])

    def test_normal_pairs_have_independent_validation(self):
        for pair in (["pillow", "sheet"], ["sheet", "towel"]):
            rule = rule_by_pair(self.result, pair)
            self.assertIsNotNone(rule, pair)
            self.assertTrue(rule["cross_category"])
            statuses = {check["status"] for check in rule["validation"]}
            self.assertEqual(statuses, {"validated"}, pair)

    def test_measure_fields_present_for_every_direction(self):
        for rule in self.result["rules"]:
            for direction in rule["proposal"]["directions"]:
                for field in (
                    "support",
                    "confidence",
                    "confidence_interval_95",
                    "baseline_probability",
                    "lift",
                    "lift_interval_95",
                ):
                    self.assertIn(field, direction)
                self.assertTrue(0.0 <= direction["confidence"] <= 1.0)

    def test_lift_is_association_not_causal_claim(self):
        for rule in self.result["rules"]:
            self.assertEqual(rule["evidence_type"], "association_only")
            self.assertIn("不能解释为促销因果", rule["causal_note"])
        self.assertIn("causal_boundary", self.result["methodology"])

    def test_late_return_is_not_backfilled(self):
        global_reasons = {(item.get("revision_id"), item["reason"]) for item in self.result["exclusions"]["global"]}
        self.assertIn(("return-2", "late_fact_beyond_as_of"), global_reasons)
        # return-2 指向的 repeat-0 在提案窗口仍被计为 cover+sheet 支持篮
        cover_sheet = next(
            item for item in self.result["exclusions"]["candidates"] if item["pair"] == ["cover", "sheet"]
        )
        self.assertIn("repeat-0", cover_sheet["proposal"]["support_order_ids"])

    def test_fully_returned_line_does_not_support_pairing_but_basket_stays_in_denominator(self):
        # order-8 的 pillow 行全退
        line_exclusions = [
            item for item in self.result["exclusions"]["global"] if item.get("order_id") == "order-8"
        ]
        self.assertTrue(line_exclusions)
        self.assertEqual(line_exclusions[0]["reason"], "fully_returned_or_cancelled")
        rule = rule_by_pair(self.result, ["pillow", "sheet"])
        # order-8 不在 pillow 支持篮中，但仍出现在“有机会未共购”的排除订单里（保留在分母）
        self.assertNotIn("order-8", rule["evidence_orders"]["support_order_ids"])
        self.assertIn("order-8", rule["evidence_orders"]["excluded_order_ids"])

    def test_multi_unit_sku_counts_once_per_basket(self):
        # 所有订单每个 SKU 只出现一行；共现按集合计算，数量 2/10 都只算一次
        rule = rule_by_pair(self.result, ["pillow", "sheet"])
        self.assertEqual(rule["proposal"]["support_baskets"], 32)
        towel_rule = rule_by_pair(self.result, ["sheet", "towel"])
        self.assertEqual(towel_rule["proposal"]["support_baskets"], 17)

    def test_proposal_validation_split_and_window_counts(self):
        counts = self.result["basket_counts"]
        self.assertEqual(counts["proposal_total"], 62)   # 50 常规 + 12 批发
        self.assertEqual(counts["validation_total"], 35)  # 25 常规 + 10 批发
        self.assertEqual(
            self.result["windows"]["proposal"]["end"], "2026-09-20T00:00:00+00:00"
        )

    def test_buyer_diversity_guardrail(self):
        for rule in self.result["rules"]:
            self.assertGreaterEqual(rule["proposal"]["distinct_buyers"], 3)
            self.assertLessEqual(rule["proposal"]["top_buyer_share"], 0.5)
            self.assertGreaterEqual(len(rule["proposal"]["distinct_iso_weeks"]), 2)

    def test_pillow_towel_never_co_purchased_is_zero_support_not_fake_zero_lift(self):
        reasons = exclusion_reasons(self.result)
        self.assertEqual(reasons[("pillow", "towel")], "no_observed_co_purchase")
        entry = next(
            item
            for item in self.result["exclusions"]["candidates"]
            if item["pair"] == ["pillow", "towel"]
        )
        # 有充分同时可售机会但零共购：如实记为零支持，不产出 lift=0 的规则
        self.assertGreater(entry["proposal"]["joint_opportunity_baskets"], 0)
        self.assertEqual(entry["proposal"]["support_baskets"], 0)


class UncertaintyTests(unittest.TestCase):
    def test_wilson_interval_brackets_probability(self):
        interval = wilson_interval(8, 20)
        self.assertIsNotNone(interval)
        self.assertLessEqual(interval[0], 8 / 20 <= interval[1])

    def test_wilson_interval_undefined_for_zero_trials(self):
        self.assertIsNone(wilson_interval(0, 0))

    def test_fixture_lift_intervals_bracket_one(self):
        result = analyze(load_fixture())
        for rule in result["rules"]:
            for direction in rule["proposal"]["directions"]:
                low, high = direction["lift_interval_95"]
                self.assertLessEqual(low, 1.0 <= high)


class AvailabilityTests(unittest.TestCase):
    def test_later_segment_overrides_and_half_open(self):
        index = AvailabilityIndex.build(
            [
                {"sku": "cover", "start": "2026-08-01T00:00:00+00:00", "end": "2026-10-30T00:00:00+00:00", "available": False},
                {"sku": "cover", "start": "2026-09-10T00:00:00+00:00", "end": "2026-10-30T00:00:00+00:00", "available": True},
            ]
        )
        self.assertFalse(index.state_at("cover", parse_instant("2026-09-09T23:59:59+00:00")))
        self.assertTrue(index.state_at("cover", parse_instant("2026-09-10T00:00:00+00:00")))
        # 左闭右开：终点不再覆盖 -> 未知
        self.assertIsNone(index.state_at("cover", parse_instant("2026-10-30T00:00:00+00:00")))

    def test_unknown_state_is_not_stockout(self):
        index = AvailabilityIndex.build([])
        self.assertIsNone(index.state_at("anything", parse_instant("2026-09-01T00:00:00+00:00")))


class SyntheticBasketTests(unittest.TestCase):
    """用自构造小数据验证零机会、未知机会、窗口门控等边界口径。"""

    def base_payload(self) -> dict:
        return {
            "schema_version": 1,
            "as_of": "2026-12-01T00:00:00+00:00",
            "source_versions": {"operations": "t1"},
            "catalog": ["alpha", "beta", "gamma"],
            "availability": [
                {"sku": "alpha", "start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00", "available": True},
                {"sku": "beta", "start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00", "available": True},
            ],
            "orders": [],
            "revisions": [],
            "analysis": {
                "proposal_end": "2026-06-01T00:00:00+00:00",
                "validation_end": "2026-09-01T00:00:00+00:00",
                "min_buyers": 2,
                "max_rules": 8,
            },
        }

    def order(self, oid: str, buyer: str, day: str, skus: list[tuple[str, int]]) -> dict:
        return {
            "order_id": oid,
            "buyer_id": buyer,
            "placed_at": f"2026-{day}T00:00:00+00:00",
            "available_at": f"2026-{day}T00:00:00+00:00",
            "lines": [{"line_id": f"{oid}-{sku}", "sku": sku, "quantity": qty} for sku, qty in skus],
        }

    def test_zero_simultaneous_opportunity_is_explicit_exclusion(self):
        payload = self.base_payload()
        # gamma 没有任何可售状态段 -> alpha/gamma 从未同时可售
        payload["orders"] = [
            self.order("o1", "b1", "02-01", [("alpha", 1), ("beta", 1)]),
            self.order("o2", "b2", "02-02", [("alpha", 1), ("beta", 1)]),
            self.order("o3", "b3", "02-03", [("alpha", 1), ("beta", 1)]),
        ]
        result = analyze(payload)
        reasons = exclusion_reasons(result)
        self.assertEqual(reasons[("alpha", "gamma")], "zero_simultaneous_opportunity")
        self.assertNotIn(("alpha", "gamma"), {tuple(r["pair"]) for r in result["rules"]})

    def test_unknown_opportunity_neither_denominator_nor_non_purchase(self):
        payload = self.base_payload()
        payload["orders"] = [
            self.order("o1", "b1", "02-01", [("alpha", 1)]),
            self.order("o2", "b2", "02-02", [("alpha", 1)]),
            self.order("o3", "b3", "02-03", [("alpha", 1)]),
        ]
        result = analyze(payload)
        entry = next(
            item for item in result["exclusions"]["candidates"] if item["pair"] == ["alpha", "gamma"]
        )
        self.assertEqual(entry["proposal"]["joint_opportunity_baskets"], 0)
        self.assertEqual(entry["proposal"]["unknown_opportunity_baskets"], 3)

    def test_cancelled_line_removed_and_fully_cancelled_basket_kept(self):
        payload = self.base_payload()
        payload["orders"] = [
            self.order("o1", "b1", "02-01", [("alpha", 1), ("beta", 1)]),
            self.order("o2", "b2", "02-02", [("alpha", 1), ("beta", 1)]),
            self.order("o3", "b3", "02-03", [("alpha", 1), ("beta", 1)]),
        ]
        payload["revisions"] = [
            {
                "revision_id": "cancel-1",
                "order_id": "o3",
                "line_id": "o3-beta",
                "kind": "cancel",
                "quantity": 1,
                "effective_at": "2026-02-04T00:00:00+00:00",
                "available_at": "2026-02-05T00:00:00+00:00",
            }
        ]
        result = analyze(payload)
        rule = rule_by_pair(result, ["alpha", "beta"])
        self.assertEqual(rule["proposal"]["support_baskets"], 2)
        self.assertIn("o3", rule["evidence_orders"]["excluded_order_ids"])
        self.assertEqual(result["basket_counts"]["proposal_total"], 3)

    def test_single_buyer_repeat_orders_blocked_even_with_many_baskets(self):
        payload = self.base_payload()
        payload["orders"] = [
            self.order(f"o{i}", "only-buyer", f"02-{i+1:02d}", [("alpha", 1), ("beta", 1)])
            for i in range(6)
        ]
        result = analyze(payload)
        reasons = exclusion_reasons(result)
        self.assertEqual(reasons[("alpha", "beta")], "insufficient_distinct_buyers")

    def test_single_promo_week_blocked_even_with_multiple_buyers(self):
        payload = self.base_payload()
        payload["orders"] = [
            self.order("o1", "b1", "02-02", [("alpha", 1), ("beta", 1)]),
            self.order("o2", "b2", "02-03", [("alpha", 1), ("beta", 1)]),
            self.order("o3", "b3", "02-04", [("alpha", 1), ("beta", 1)]),
        ]
        result = analyze(payload)
        reasons = exclusion_reasons(result)
        self.assertEqual(reasons[("alpha", "beta")], "single_promotion_week")

    def test_genuine_affinity_produces_lift_above_one_with_intervals(self):
        payload = self.base_payload()
        payload["availability"].append(
            {"sku": "gamma", "start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00", "available": True}
        )
        # 8 个篮子跨 4 周、4 个买家：alpha+beta 总是一起买（4 篮），gamma 与它们无关。
        orders = []
        plan = [
            ("01-05", "b1", ["alpha", "beta"]),
            ("02-05", "b2", ["alpha", "beta"]),
            ("03-05", "b3", ["alpha", "beta"]),
            ("04-05", "b4", ["alpha", "beta"]),
            ("01-15", "b1", ["gamma"]),
            ("02-15", "b2", ["gamma"]),
            ("03-15", "b3", ["gamma"]),
            ("04-15", "b4", ["gamma"]),
        ]
        for index, (day, buyer, skus) in enumerate(plan):
            orders.append(self.order(f"o{index}", buyer, day, [(sku, 1) for sku in skus]))
        payload["orders"] = orders
        result = analyze(payload)
        rule = rule_by_pair(result, ["alpha", "beta"])
        self.assertIsNotNone(rule)
        direction = next(d for d in rule["proposal"]["directions"] if d["direction"] == "alpha->beta")
        # alpha 出现的 4 个联合机会篮都买了 beta，而 beta 基准概率只有 4/8=0.5 -> lift=2
        self.assertAlmostEqual(direction["confidence"], 1.0)
        self.assertAlmostEqual(direction["baseline_probability"], 0.5)
        self.assertAlmostEqual(direction["lift"], 2.0)
        low, high = direction["lift_interval_95"]
        self.assertGreater(low, 1.0)

    def test_candidate_capacity_limit(self):
        payload = self.base_payload()
        payload["analysis"]["min_buyers"] = 2
        payload["analysis"]["max_rules"] = 1
        # 构造跨多周、多买家的 alpha-beta 与 alpha-gamma（gamma 补可售段）
        payload["availability"].append(
            {"sku": "gamma", "start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00", "available": True}
        )
        payload["orders"] = [
            self.order("o1", "b1", "01-05", [("alpha", 1), ("beta", 1), ("gamma", 1)]),
            self.order("o2", "b2", "03-05", [("alpha", 1), ("beta", 1), ("gamma", 1)]),
            self.order("o3", "b3", "05-05", [("alpha", 1), ("beta", 1), ("gamma", 1)]),
        ]
        result = analyze(payload)
        self.assertEqual(len(result["rules"]), 1)
        self.assertIn("candidate_capacity_limit", exclusion_reasons(result).values())


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "analysis.sqlite3")
        self.input_path = Path(self.tmp.name) / "input.json"
        self.input_path.write_text(json.dumps(load_fixture()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_uses_same_analysis_and_no_second_record(self):
        first = run_analysis(self.input_path, self.db)
        replay = run_analysis(self.input_path, self.db)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["analysis_id"], replay["analysis_id"])
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM basket_analyses").fetchone()[0], 1)
        con.close()

    def test_new_return_version_stored_separately_with_lineage(self):
        first = run_analysis(self.input_path, self.db)
        changed = load_fixture()
        for revision in changed["revisions"]:
            if revision["revision_id"] == "return-2":
                revision["available_at"] = "2026-10-20T00:00:00+00:00"
        changed["source_versions"]["operations"] = "r2"
        v2_path = Path(self.tmp.name) / "input-v2.json"
        v2_path.write_text(json.dumps(changed), encoding="utf-8")
        second = run_analysis(v2_path, self.db)
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(second["lineage"]["supersedes_analysis_id"], first["analysis_id"])
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM basket_analyses").fetchone()[0], 2)
        con.close()

    def test_rule_evidence_lookup_round_trips_orders(self):
        result = run_analysis(self.input_path, self.db)
        store = AnalysisStore(self.db)
        rule = result["rules"][0]
        evidence = store.rule_evidence(result["analysis_id"], rule["rule_id"])
        self.assertEqual(evidence["support_order_ids"], rule["evidence_orders"]["support_order_ids"])
        self.assertEqual(evidence["excluded_order_ids"], rule["evidence_orders"]["excluded_order_ids"])
        self.assertIsNone(store.rule_evidence(result["analysis_id"], "does-not-exist"))

    def test_analysis_id_is_deterministic_from_input(self):
        payload = load_fixture()
        fingerprint = input_fingerprint(payload)
        self.assertEqual(build_analysis_id(fingerprint), build_analysis_id(fingerprint))
        altered = deepcopy(payload)
        altered["source_versions"]["operations"] = "different"
        self.assertNotEqual(fingerprint, input_fingerprint(altered))


if __name__ == "__main__":
    unittest.main()
