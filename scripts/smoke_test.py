#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test.py — 《投资的纪律》Skill 端到端闭环冒烟测试。

三条流程（对应 SKILL.md 模式 A/B/C）：
  A 市场日报     ：采集 watchlist 行情 → 统一格式校验 → 时效标注 → 日报
  B 个股研究     ：采集行情+公告+新闻 → 校验 → 事实/待核实分离
  C 买入纪律检查 ：组合输入 → 规则引擎 → 纪律判定表 + 冲突提示

用法：
  python3 scripts/smoke_test.py --flow all
  python3 scripts/smoke_test.py --flow A --offline      # 用 tests/fixtures 回放，不联网
  python3 scripts/smoke_test.py --flow C --input plan.json
  python3 scripts/smoke_test.py --flow all --json

退出码：0 全部流程通过；1 有流程失败；2 输入错误。
不构成投资建议；不荐股；不承诺收益。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))

import market_data as md  # noqa: E402
import validate_data as vd  # noqa: E402
import validate_rules as vr  # noqa: E402

FIXTURES = SKILL / "tests" / "fixtures"
DEFAULT_WATCHLIST = ["600519", "000001", "sh000001", "00700", "AAPL"]
DISCLAIMER = "本报告为投资纪律与数据核对工具输出，不构成投资建议，不推荐具体证券，不承诺收益。"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _replay_freshness(payload: dict) -> dict:
    """离线回放：把快照时间平移到当前，仅用于结构/流程验证，报告须注明『回放』。"""
    now = dt.datetime.now(md.tz_of("cn"))
    for it in payload.get("items", []):
        market = it.get("market", "cn")
        if it.get("as_of"):
            it["as_of"] = now.isoformat()
        if it.get("data_time"):
            it["data_time"] = now.isoformat()
        it["retrieved_at"] = now.isoformat()
        it["note"] = "（离线回放样本，非本次实时采集）"
        # 平移时间后重算时效，避免 realtime 与延迟秒数自相矛盾
        fr = md.freshness(now.isoformat(), market)
        it["freshness_status"] = fr["freshness_status"]
        it["delay_seconds"] = fr["delay_seconds"]
    payload["retrieved_at"] = now.isoformat()
    payload["_replay"] = True
    return payload


# ---------------------------------------------------------------------- 流程 A
def flow_market_report(watchlist: list[str], offline: bool = False,
                       no_cache: bool = False) -> dict:
    steps: list[dict] = []
    offline_note = None
    if offline:
        quote = _replay_freshness(_load_fixture("quote_sample.json"))
        offline_note = "离线回放：使用 tests/fixtures/quote_sample.json 快照"
    else:
        quote = md.get_quote(watchlist, no_cache=no_cache)
    steps.append({"step": "采集行情", "status": quote.get("status"),
                  "detail": f"{len(quote.get('items', []))} 条"})
    vres = vd.validate(quote)
    steps.append({"step": "统一格式校验", "status": "ok" if vres["passed"] else "fail",
                  "detail": f"未通过 {vres['summary'][vd.FAIL]} 项"})
    rows = []
    for it in quote.get("items", []):
        rows.append({
            "标的": f"{it.get('name', '')}（{it.get('symbol', '')}）",
            "价格": it.get("price"),
            "涨跌幅%": it.get("change_pct"),
            "时效": it.get("freshness_status"),
            "延迟秒": it.get("delay_seconds"),
            "来源": it.get("source_name"),
        })
    return {"flow": "A 市场日报", "ok": vres["passed"] and quote.get("status") == "ok",
            "offline_note": offline_note, "steps": steps, "rows": rows,
            "validation": vres["summary"],
            "footer": "行情为『最新可用』口径，时效逐条标注；本表仅为行情事实，不含买卖建议。"}


# ---------------------------------------------------------------------- 流程 B
def flow_stock_research(symbol: str, offline: bool = False,
                        no_cache: bool = False) -> dict:
    steps: list[dict] = []
    offline_note = None
    if offline:
        filings = _load_fixture("filings_sample.json")
        offline_note = "离线回放：使用 tests/fixtures/filings_sample.json 快照"
        quote = {"status": "unavailable", "sources": [], "errors": [],
                 "price_agreement": "离线未采集（回放模式）"}
        news_items: list[dict] = []
    else:
        ds = md.research_dataset(symbol, no_cache=no_cache)
        quote = ds["quote"]
        filings = ds["filings"]
        news_items = (ds.get("news") or {}).get("items", [])
        steps.append({"step": "采集数据集", "status": ds.get("status"),
                      "detail": f"缺口：{ds.get('data_gaps') or '无'}"})
    vf = vd.validate(filings)
    steps.append({"step": "公告格式校验", "status": "ok" if vf["passed"] else "fail",
                  "detail": f"未通过 {vf['summary'][vd.FAIL]} 项"})
    facts = [{"类型": "公告", "标题": it.get("title", "")[:60],
              "时间": it.get("published_at"), "来源": it.get("source_name"),
              "时效": it.get("freshness_status")} for it in filings.get("items", [])]
    facts += [{"类型": "新闻", "标题": it.get("title", "")[:60],
               "时间": it.get("published_at"), "来源": it.get("source_name"),
               "时效": it.get("freshness_status")} for it in news_items]
    pending = []
    for lbl, cond in [("最新可用行情（多源交叉验证）", quote.get("status") != "ok"),
                      ("最新正式公告/申报", not filings.get("items")),
                      ("同期新闻线索", not news_items)]:
        if cond:
            pending.append(lbl)
    return {"flow": "B 个股研究", "ok": vf["passed"], "offline_note": offline_note,
            "steps": steps, "rows": facts, "pending": pending,
            "quote_agreement": quote.get("price_agreement"),
            "footer": "事实与待核实分离；缺失项一律记『待核实』，不猜测补齐。"}


# ---------------------------------------------------------------------- 流程 C
def flow_buy_discipline(input_path: Path | None = None) -> dict:
    data = json.loads(input_path.read_text(encoding="utf-8")) if input_path \
        else _load_fixture("rules_input_sample.json")
    index = vr.load_index()
    problems = vr.verify_thresholds(index)
    steps = [{"step": "阈值一致性（对 rules-index）",
              "status": "ok" if not problems else "fail", "detail": str(problems)}]
    res = vr.run(data)
    steps.append({"step": "规则引擎", "status": "ok",
                  "detail": f"检查 {len(res['checks'])} 项"})
    return {"flow": "C 买入纪律检查", "ok": not problems,
            "steps": steps, "rows": res["checks"], "summary": res["summary"],
            "blocking": res["blocking_failures"], "data_gaps": res["data_gaps"],
            "conflicts": res["open_conflicts"],
            "footer": res["disclaimer"]}


# ---------------------------------------------------------------------- 渲染
def render_markdown(result: dict) -> str:
    lines = [f"# 冒烟流程 · {result['flow']}", ""]
    if result.get("offline_note"):
        lines += [f"> {result['offline_note']}", ""]
    lines.append("## 步骤")
    for s in result["steps"]:
        lines.append(f"- [{s['status']}] {s['step']}：{s['detail']}")
    lines.append("")
    rows = result.get("rows") or []
    if rows:
        keys = list(rows[0].keys())
        lines += ["## 结果表", "", "| " + " | ".join(keys) + " |",
                  "|" + "---|" * len(keys)]
        for r in rows:
            lines.append("| " + " | ".join(
                str(r.get(k, "")).replace("|", "／") for k in keys) + " |")
        lines.append("")
    if result.get("pending"):
        lines += ["## 缺失数据（待核实）", ""]
        lines += [f"- {p}" for p in result["pending"]]
        lines.append("")
    if "summary" in result:
        s = result["summary"]
        lines.append(f"## 汇总 · 通过 {s['通过']} / 未通过 {s['未通过']} / "
                     f"关注 {s['需要关注']} / 数据不足 {s['数据不足，无法判断']} / "
                     f"冲突 {s['规则冲突，等待确认']}")
        if result.get("conflicts"):
            lines.append("")
            lines.append(f"> ⚠ 存在 {result['conflicts']} 项规则冲突，已输出『等待作者确认』，"
                         f"引擎不做单方裁决。")
        lines.append("")
    lines += [f"> {result['footer']}", f"> {DISCLAIMER}"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Skill 端到端闭环冒烟测试")
    ap.add_argument("--flow", default="all", choices=["A", "B", "C", "all"])
    ap.add_argument("--symbol", default="600519", help="流程 B 标的（联网时用）")
    ap.add_argument("--input", help="流程 C 输入 JSON（默认用内置样本）")
    ap.add_argument("--offline", action="store_true", help="用 fixtures 回放，不联网")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    results = []
    if a.flow in ("A", "all"):
        results.append(flow_market_report(DEFAULT_WATCHLIST, a.offline, a.no_cache))
    if a.flow in ("B", "all"):
        results.append(flow_stock_research(a.symbol, a.offline, a.no_cache))
    if a.flow in ("C", "all"):
        results.append(flow_buy_discipline(Path(a.input) if a.input else None))

    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(render_markdown(r))
            print()
    failed = [r["flow"] for r in results if not r["ok"]]
    if failed:
        print(f"⛔ 未通过流程：{failed}", file=sys.stderr)
        return 1
    print(f"✅ 冒烟测试通过（{len(results)} 个流程）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
