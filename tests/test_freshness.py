#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_freshness.py — 时效状态机与代码归一化的离线单测（不联网）。

覆盖：交易时段判定、freshness 状态推导（realtime/delayed/latest/after_hours/
historical/unknown）、周末与跨时区、代码归一化、去重键、HTML 剥离。
运行：python3 tests/test_freshness.py
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import market_data as md  # noqa: E402


def _cn(y, mo, d, h, mi, s=0, market="cn"):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=md.tz_of(market))


class TestSessionState(unittest.TestCase):
    def test_cn_trading(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 10, 0))["state"], "trading")

    def test_cn_lunch(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 12, 0))["state"], "lunch_break")

    def test_cn_pre_market(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 9, 20))["state"], "pre_market")

    def test_cn_closed_evening(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 16, 0))["state"], "closed")

    def test_cn_weekend_closed_even_at_noon(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 17, 10, 0))["state"], "closed")

    def test_boundary_1130_is_lunch_not_trading(self):
        # 11:30 起算午休（交易窗口上界不含）
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 11, 30))["state"], "lunch_break")

    def test_boundary_0930_is_trading(self):
        self.assertEqual(md.session_state("cn", _cn(2026, 10, 14, 9, 30))["state"], "trading")

    def test_us_afternoon_is_after_hours_cn_view(self):
        # 北京时间周二 21:00 = 纽约时间 09:00 EDT → pre_market
        at = dt.datetime(2026, 10, 13, 21, 0, tzinfo=md.tz_of("cn"))
        self.assertEqual(md.session_state("us", at)["state"], "pre_market")

    def test_us_trading_when_ny_midday(self):
        at = dt.datetime(2026, 10, 13, 22, 30, tzinfo=md.tz_of("cn"))  # NY 10:30 EDT
        self.assertEqual(md.session_state("us", at)["state"], "trading")

    def test_unknown_market(self):
        self.assertEqual(md.session_state("jp", _cn(2026, 10, 14, 10, 0))["state"], "unknown")


class TestFreshness(unittest.TestCase):
    def test_missing_as_of_is_unknown(self):
        self.assertEqual(md.freshness(None, "cn")["freshness_status"], "unknown")

    def test_unparsable_as_of_is_unknown(self):
        self.assertEqual(md.freshness("不是时间", "cn")["freshness_status"], "unknown")

    def test_trading_instant_is_realtime(self):
        now = _cn(2026, 10, 14, 10, 0)
        r = md.freshness(now.isoformat(), "cn", at=now)
        self.assertEqual(r["freshness_status"], "realtime")
        self.assertEqual(r["delay_seconds"], 0)

    def test_trading_one_hour_old_is_delayed(self):
        now = _cn(2026, 10, 14, 10, 0)
        r = md.freshness((now - dt.timedelta(hours=1)).isoformat(), "cn", at=now)
        self.assertEqual(r["freshness_status"], "delayed")
        self.assertEqual(r["delay_seconds"], 3600)

    def test_lunch_with_morning_close_is_latest(self):
        now = _cn(2026, 10, 14, 12, 0)
        r = md.freshness(_cn(2026, 10, 14, 11, 30).isoformat(), "cn", at=now)
        self.assertEqual(r["freshness_status"], "latest")
        self.assertEqual(r["session_state"], "lunch_break")

    def test_closed_with_last_session_data_is_latest(self):
        now = _cn(2026, 10, 14, 20, 0)  # 周三晚
        r = md.freshness(_cn(2026, 10, 14, 15, 0).isoformat(), "cn", at=now)
        self.assertEqual(r["freshness_status"], "latest")

    def test_old_data_is_historical(self):
        now = _cn(2026, 10, 14, 20, 0)
        r = md.freshness(_cn(2026, 10, 9, 15, 0).isoformat(), "cn", at=now)
        self.assertEqual(r["freshness_status"], "historical")

    def test_naive_as_of_treated_as_market_tz(self):
        now = _cn(2026, 10, 14, 10, 0)
        r = md.freshness("2026-10-14T09:59:30", "cn", at=now)
        self.assertEqual(r["freshness_status"], "realtime")

    def test_verify_flag_propagates(self):
        now = _cn(2026, 10, 14, 20, 0)
        r = md.freshness(_cn(2026, 10, 14, 15, 0).isoformat(), "cn", at=now,
                         trading_day_verified=True)
        self.assertTrue(r["trading_day_verified"])
        self.assertNotIn("节假日未核验", r["note"])


class TestNormalizeSymbol(unittest.TestCase):
    def test_bare_cn_stock(self):
        r = md.normalize_symbol("600519")
        self.assertEqual((r["market"], r["sina"], r["currency"]), ("cn", "sh600519", "CNY"))

    def test_bare_sz_stock(self):
        self.assertEqual(md.normalize_symbol("000001")["sina"], "sz000001")

    def test_sh_prefix_index(self):
        r = md.normalize_symbol("sh000001")
        self.assertEqual(r["kind"], "index")

    def test_hk_bare_and_prefixed(self):
        self.assertEqual(md.normalize_symbol("00700")["market"], "hk")
        self.assertEqual(md.normalize_symbol("hk00700")["symbol"], "00700")

    def test_us_ticker(self):
        r = md.normalize_symbol("aapl")
        self.assertEqual((r["market"], r["tencent"]), ("us", "usAAPL"))

    def test_index_alias(self):
        self.assertEqual(md.normalize_symbol("HSI")["kind"], "index")

    def test_invalid_raises(self):
        with self.assertRaises(md.DataSourceError):
            md.normalize_symbol("???")


class TestDedupAndHtml(unittest.TestCase):
    def test_dedup_key_stable(self):
        k1 = md._dedup_key("http://x/a", "标题", "2026-01-01T00:00:00+08:00")
        k2 = md._dedup_key("http://x/a", "别的标题", None)
        self.assertEqual(k1, k2)

    def test_strip_html(self):
        self.assertEqual(md._strip_html("<p>你好<br/>世界</p>"), "你好世界")


class TestPubTimeNormalize(unittest.TestCase):
    def test_space_separated(self):
        self.assertEqual(md._normalize_pub_time("2026-10-13 13:23:34"),
                         "2026-10-13T13:23:34+08:00")

    def test_date_only(self):
        self.assertEqual(md._normalize_pub_time("2026-10-13"), "2026-10-13T00:00:00+08:00")

    def test_already_iso_kept(self):
        self.assertEqual(md._normalize_pub_time("2026-10-13T13:23:34+08:00"),
                         "2026-10-13T13:23:34+08:00")

    def test_utc_z_kept(self):
        self.assertEqual(md._normalize_pub_time("2026-10-13T05:23:34Z"),
                         "2026-10-13T05:23:34Z")

    def test_none(self):
        self.assertIsNone(md._normalize_pub_time(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
