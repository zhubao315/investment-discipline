#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_rule_engine.py — 纪律规则引擎单测（离线）。

覆盖：阈值与 rules-index 唯一事实来源一致性、边界值（5pp / 30% / 灯色 /
止损 / 卖出计数）、缺失数据必须输出"数据不足"、冲突必须输出"等待确认"、
输出七列结构。运行：python3 tests/test_rule_engine.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import validate_rules as vr  # noqa: E402

BASE = {"as_of": "2026-10-13T16:00:00+08:00"}


def _results(rows, rule_id):
    return [r for r in rows if r["rule_id"] == rule_id]


class TestThresholdSync(unittest.TestCase):
    """rules-index.json 是唯一事实来源，引擎阈值必须与之一致。"""

    def test_index_loads(self):
        idx = vr.load_index()
        self.assertTrue(idx)

    def test_no_threshold_mismatch(self):
        problems = vr.verify_thresholds(vr.load_index())
        self.assertEqual(problems, [], f"阈值不一致：{problems}")

    def test_selftest_passes(self):
        self.assertEqual(vr.selftest(), 0)


class TestPositionRules(unittest.TestCase):
    def test_no_positions_is_insufficient_not_guess(self):
        res = vr.run(dict(BASE, total_assets=1000000, cash=200000))
        rows = _results(res["checks"], "R-POS-01")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["result"], vr.INSUFFICIENT)

    def test_weight_20_exact_pass(self):
        d = dict(BASE, total_assets=100000,
                 positions=[{"symbol": "X", "market_value": 20000}])
        self.assertEqual(vr.check_position_limits(d)[0]["result"], vr.PASS)

    def test_weight_20_over_fail(self):
        d = dict(BASE, total_assets=100000,
                 positions=[{"symbol": "X", "market_value": 20001}])
        self.assertEqual(vr.check_position_limits(d)[0]["result"], vr.FAIL)

    def test_core_annotated_missing_is_insufficient(self):
        d = dict(BASE, total_assets=1000000,
                 positions=[{"symbol": f"S{i}", "market_value": 100000}
                            for i in range(10)])
        got = _results(vr.check_position_limits(d), "R-POS-04")
        self.assertEqual(got[0]["result"], vr.INSUFFICIENT)


class TestValuationConflict(unittest.TestCase):
    """安全边际：>30% 才放行；20%-30% 区间附 C-02 冲突注记。"""

    def _mos(self, mos, market_state="normal"):
        return vr.check_buy(dict(BASE, market_state=market_state,
                                 buy_candidate={"symbol": "X", "red_lights": 0,
                                                "yellow_lights": 0,
                                                "margin_of_safety_pct": mos}))[0]

    def test_30_exact_fails(self):
        self.assertEqual(self._mos(30.0)["result"], vr.FAIL)

    def test_30_plus_passes(self):
        self.assertEqual(self._mos(30.01)["result"], vr.PASS)

    def test_25_zone_mentions_c02(self):
        r = self._mos(25.0)
        self.assertEqual(r["result"], vr.FAIL)
        self.assertIn("C-02", r["reason"])

    def test_bull_needs_40(self):
        self.assertEqual(self._mos(35.0, market_state="bull")["result"], vr.FAIL)
        self.assertEqual(self._mos(40.01, market_state="bull")["result"], vr.PASS)


class TestRebalanceConflict(unittest.TestCase):
    """C-01：偏离恰好 5.0pp → 等待确认；>5pp → 执行。"""

    def _dev(self, actual):
        d = dict(BASE, asset_classes=[{"class": "股票", "target_pct": 60, "actual_pct": actual}])
        return _results(vr.check_rebalance(d), "R-RISK-03")[0]

    def test_exactly_5pp_conflict(self):
        self.assertEqual(self._dev(65.0)["result"], vr.CONFLICT)

    def test_over_5pp_fails(self):
        self.assertEqual(self._dev(65.01)["result"], vr.FAIL)

    def test_under_5pp_passes(self):
        self.assertEqual(self._dev(64.99)["result"], vr.PASS)


class TestSellReview(unittest.TestCase):
    def _sell(self, trig):
        return _results(vr.check_sell(dict(BASE, sell_review={"symbol": "X",
                                                              "triggers": trig})),
                        "R-SELL-01")[0]

    def test_two_triggers_fail(self):
        self.assertEqual(self._sell({"S1": "triggered", "S2": "triggered"})["result"],
                         vr.FAIL)

    def test_one_trigger_attention(self):
        self.assertEqual(self._sell({"S1": "triggered", "S2": "not_triggered"})["result"],
                         vr.ATTENTION)

    def test_pending_must_not_count_as_not_triggered(self):
        self.assertEqual(self._sell({"S1": "triggered", "S2": "待核实"})["result"],
                         vr.INSUFFICIENT)

    def test_all_clear_passes(self):
        self.assertEqual(self._sell({"S1": "not_triggered"})["result"], vr.PASS)


class TestOutputContract(unittest.TestCase):
    ROW_FIELDS = ("rule_id", "check", "inputs", "data_time", "condition",
                  "result", "reason", "action")

    def test_every_row_has_seven_columns(self):
        res = vr.run(dict(BASE, total_assets=1000000, cash=200000))
        for r in res["checks"]:
            for f in self.ROW_FIELDS:
                self.assertIn(f, r, f"行缺字段 {f}")

    def test_summary_counts_add_up(self):
        res = vr.run(dict(BASE))
        self.assertEqual(sum(res["summary"].values()), len(res["checks"]))

    def test_markdown_table_wellformed(self):
        md = vr.to_markdown(vr.run(dict(BASE)))
        header = [ln for ln in md.splitlines() if ln.startswith("| 规则编号")]
        self.assertEqual(len(header), 1)
        self.assertIn("不构成投资建议", md)


class TestConflicts(unittest.TestCase):
    def test_three_conflicts_registered(self):
        self.assertEqual([c["id"] for c in vr.CONFLICTS], ["C-01", "C-02", "C-03"])

    def test_conflict_report_lists_sources(self):
        rep = vr.conflict_report()
        for token in ("C-01", "C-02", "C-03", "等待作者裁决", "ch02.md", "ch06.md"):
            self.assertIn(token, rep)

    def test_rules_index_records_conflicts(self):
        idx = json.loads((ROOT / "references" / "rules-index.json").read_text("utf-8"))
        blob = json.dumps(idx, ensure_ascii=False)
        for cid in ("C-01", "C-02", "C-03"):
            self.assertIn(cid, blob, f"{cid} 未登记进 rules-index.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
