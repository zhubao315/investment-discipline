#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_data_sources.py — 数据源联网冒烟测试（需要网络）。

默认执行；设 NO_NETWORK=1 或 --offline 时全部跳过（用于离线环境）。
运行：python3 tests/test_data_sources.py
      NO_NETWORK=1 python3 tests/test_data_sources.py

断言的是"结构与时效标注"，不是"数值大小"，避免测试绑定某天的行情。
"""
from __future__ import annotations

import io
import json
import os
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

# 让测试自带隔离缓存目录：不依赖、也不污染用户缓存（CI 上尤其重要）
os.environ["INVESTMENT_DISCIPLINE_CACHE"] = tempfile.mkdtemp(prefix="inv-disc-cache-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import market_data as md  # noqa: E402

OFFLINE = os.environ.get("NO_NETWORK") == "1" or os.environ.get("OFFLINE") == "1"
net = unittest.skipIf(OFFLINE, "离线模式（NO_NETWORK=1），跳过联网测试")


@net
class TestSourcesProbe(unittest.TestCase):
    def test_probe_returns_ok_sources(self):
        res = md.list_sources(probe=True)
        sources = res.get("sources") or []
        self.assertTrue(sources, "未返回任何数据源")
        ok = [s for s in sources if s.get("probe") == "ok"]
        self.assertGreaterEqual(len(ok), 3,
                                f"可连通数据源过少：{[s.get('name') for s in sources]}")


@net
class TestQuoteFlow(unittest.TestCase):
    def test_quote_has_freshness_and_not_claimed_realtime(self):
        res = md.get_quote(["600519"])
        self.assertEqual(res.get("status"), "ok")
        items = res.get("items") or []
        self.assertTrue(items, "行情为空")
        it = items[0]
        for f in ("price", "as_of", "freshness_status", "delay_seconds", "source"):
            self.assertIn(f, it, f"行情缺字段 {f}")
        self.assertIn(it["freshness_status"],
                      ("realtime", "delayed", "latest", "after_hours", "historical", "unknown"))
        self.assertGreater(it["price"], 0)

    def test_verify_quote_multi_source(self):
        res = md.verify_quote("600519")
        self.assertIn(res.get("status"), ("ok", "degraded"))
        sources = res.get("sources") or []
        self.assertGreaterEqual(len(sources), 1)
        for q in sources:
            self.assertIn("freshness", q)
            self.assertIn("freshness_status", q["freshness"])


@net
class TestMacroAndFilings(unittest.TestCase):
    def test_macro_cpi_structure(self):
        res = md.get_macro("cpi", limit=3)
        self.assertEqual(res.get("status"), "ok")
        self.assertTrue(res.get("items"))
        it = res["items"][0]
        for f in ("indicator", "period", "value", "published_at", "freshness_status"):
            self.assertIn(f, it, f"宏观缺字段 {f}")

    def test_filings_structure(self):
        res = md.get_filings("600519", limit=3)
        self.assertEqual(res.get("status"), "ok")
        self.assertTrue(res.get("items"), "公告为空")
        it = res["items"][0]
        for f in ("title", "published_at", "source_name", "freshness_status"):
            self.assertIn(f, it, f"公告缺字段 {f}")


class TestOfflineDegradation(unittest.TestCase):
    """即使离线且无缓存，命令行也应优雅返回结构化错误（退出码 3），而不是抛未捕获异常。"""

    def test_offline_without_cache_returns_structured_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = md.main(["quote", "600519", "--offline"])
        self.assertEqual(rc, 3, "离线且无缓存应返回退出码 3")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload.get("status"), "unavailable")
        self.assertIn("error", payload)
        self.assertTrue(payload.get("note"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
