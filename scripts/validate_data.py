#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_data.py — 统一信息格式校验器（《投资的纪律》Skill 配套）。

职责：
  1. schema 校验：market_data.py 各子命令 JSON 输出的必填字段与类型；
  2. 时效检查：freshness_status 枚举合法、自相矛盾（如自称 realtime 却延迟 1 小时）、
     过期数据（行情超过 7 天）、未来时间戳、"实时"话术与实际时效状态不符；
  3. 去重检查：同一 (类型, 去重键) 只保留一条，报告被折叠的重复数；
  4. 数值合理性：价格为正、涨跌幅在 ±11%（A股涨跌停外为异常，仅提示）等。

设计原则（与 SKILL.md 一致）：
  - 纯标准库，零依赖；
  - 数据缺失 → 输出"数据不足/待核实"，绝不猜测填充；
  - 退出码：0 = 全部通过；1 = 存在校验失败；2 = 输入错误。

用法：
  python3 validate_data.py --input result.json
  python3 validate_data.py --stdin < result.json
  python3 validate_data.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent

FRESHNESS_STATES = ("realtime", "delayed", "latest", "after_hours", "historical", "unknown")
REALTIME_MAX_DELAY_SEC = 5          # 自称 realtime 的最大可接受延迟
QUOTE_STALE_AFTER = timedelta(days=7)   # 行情超过 7 天视为过期
FUTURE_TOLERANCE = timedelta(minutes=5)  # 允许的时钟误差

COMMON_REQUIRED = ("retrieved_at", "freshness_status", "source_name")
QUOTE_REQUIRED = ("symbol", "price", "as_of", "freshness_status", "source")
MACRO_REQUIRED = ("indicator", "period", "published_at", "freshness_status")
FILING_REQUIRED = ("title", "published_at", "freshness_status")

PASS, FAIL, WARN, SKIP = "通过", "未通过", "警告", "跳过"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row(check: str, target: str, result: str, reason: str, action: str) -> dict:
    return {"check": check, "target": target, "result": result,
            "reason": reason, "action": action}


def detect_kind(payload: dict) -> str:
    """从顶层结构推断输出类型：quote / macro / filing / news / unknown。"""
    if "indicator" in payload and "items" in payload:
        return "macro"
    if "market" in payload and "symbol" in payload and "items" in payload:
        kind = "filing"
        items = payload.get("items") or []
        if items and "price" in items[0]:
            kind = "quote"
        return kind
    items = payload.get("items") or []
    if items and "price" in items[0]:
        return "quote"
    if items and "title" in items[0]:
        return "filing"
    return "unknown"


def required_for(kind: str) -> tuple[str, ...]:
    base = COMMON_REQUIRED
    if kind == "quote":
        return base + QUOTE_REQUIRED
    if kind == "macro":
        return base + MACRO_REQUIRED
    if kind == "filing":
        return base + FILING_REQUIRED
    return base


def check_top(payload: dict, kind: str, retrieved_at: datetime | None) -> list[dict]:
    out: list[dict] = []
    if payload.get("status") != "ok":
        out.append(_row("顶层 status", "(payload)", FAIL,
                        f"status={payload.get('status')!r}，非 ok 的输出不得进入分析环节",
                        "按 error-handling.md 处理：换源重试或记为待核实"))
    if not isinstance(payload.get("items"), list):
        out.append(_row("items 为列表", "(payload)", FAIL,
                        "items 缺失或不是列表", "检查采集命令与 JSON 输出"))
        return out
    if not payload["items"]:
        out.append(_row("items 非空", "(payload)", WARN,
                        "items 为空：可能确实无数据（如无新公告），也可能是抓取失败被吞掉",
                        "用其他源交叉验证后，在报告中写明『未取得数据』而非『无事件』"))
    else:
        out.append(_row("items 非空", "(payload)", PASS,
                        f"共 {len(payload['items'])} 条", "—"))
    if payload.get("errors"):
        out.append(_row("errors 为空", "(payload)", WARN,
                        f"存在 {len(payload['errors'])} 条来源错误记录",
                        "错误来源的数据不得当作全量；报告中列明缺口"))
    if retrieved_at is None:
        out.append(_row("retrieved_at 可解析", "(payload)", FAIL,
                        f"retrieved_at={payload.get('retrieved_at')!r} 无法解析",
                        "重新采集；不得用未知采集时间的数据下结论"))
    else:
        out.append(_row("retrieved_at 可解析", "(payload)", PASS,
                        retrieved_at.isoformat(), "—"))
    _ = kind
    return out


def check_item(item: dict, kind: str, idx: int, retrieved_at: datetime | None) -> list[dict]:
    out: list[dict] = []
    label = (item.get("symbol") or item.get("indicator") or
             (item.get("title") or "")[:24] or f"item#{idx}")
    target = f"{kind} · {label}（#{idx}）"

    missing = [f for f in required_for(kind) if item.get(f) in (None, "")]
    if missing:
        out.append(_row("必填字段", target, FAIL,
                        f"缺失：{', '.join(missing)}",
                        "数据记为待核实；缺字段的记录不得用于规则判断"))
    else:
        out.append(_row("必填字段", target, PASS, "齐全", "—"))

    fs = item.get("freshness_status")
    if fs not in FRESHNESS_STATES:
        out.append(_row("时效状态枚举", target, FAIL,
                        f"freshness_status={fs!r} 不在 {FRESHNESS_STATES} 内",
                        "修复采集器枚举；状态不明一律标 unknown"))
    else:
        out.append(_row("时效状态枚举", target, PASS, fs, "—"))

    # 自称 realtime 但延迟超限
    delay = item.get("delay_seconds")
    if fs == "realtime" and isinstance(delay, (int, float)) and delay > REALTIME_MAX_DELAY_SEC:
        out.append(_row("实时与延迟自洽", target, FAIL,
                        f"自称 realtime 但 delay_seconds={delay} > {REALTIME_MAX_DELAY_SEC}",
                        "降级标注为 delayed；不得把延迟数据包装成实时"))
    elif fs == "realtime":
        out.append(_row("实时与延迟自洽", target, PASS,
                        f"delay_seconds={delay if delay is not None else '未提供'}", "—"))

    # 话术检查：标题/备注出现"实时"但状态不是 realtime
    text = str(item.get("note", "")) + str(item.get("title", ""))
    if "实时" in text and fs != "realtime":
        out.append(_row("话术与状态一致", target, FAIL,
                        f"文本含『实时』但 freshness_status={fs!r}",
                        "删除实时话术，按实际时效状态标注"))

    # 未来时间戳
    data_ts = _parse_ts(item.get("as_of") or item.get("data_time") or item.get("published_at"))
    if data_ts and retrieved_at and data_ts - retrieved_at > FUTURE_TOLERANCE:
        out.append(_row("时间戳不超前", target, FAIL,
                        f"数据时间 {data_ts.isoformat()} 晚于采集时间 {retrieved_at.isoformat()}",
                        "检查时区换算；超前时间戳的数据不可信"))

    # 行情过期
    if kind == "quote" and data_ts and retrieved_at:
        if retrieved_at - data_ts > QUOTE_STALE_AFTER:
            out.append(_row("行情新鲜度", target, FAIL,
                            f"行情时间距采集时间已 {(retrieved_at - data_ts).days} 天（>7 天）",
                            "标注 historical/unknown；不得当作当前行情使用"))
        else:
            out.append(_row("行情新鲜度", target, PASS,
                            f"距采集时间 {(retrieved_at - data_ts).seconds // 60} 分钟内", "—"))

    # 数值合理性
    price = item.get("price")
    if kind == "quote" and isinstance(price, (int, float)):
        if price <= 0:
            out.append(_row("价格为正", target, FAIL, f"price={price}",
                            "价格非正数，数据损坏，弃用该条"))
        else:
            out.append(_row("价格为正", target, PASS, str(price), "—"))
        cp = item.get("change_pct")
        if isinstance(cp, (int, float)) and abs(cp) > 11.0:
            out.append(_row("涨跌幅量级", target, WARN,
                            f"change_pct={cp}% 超出常规涨跌停（±10%~11%）范围",
                            "核对是否为新股/特殊品种或数据错误"))
    return out


def dedup_key(kind: str, item: dict) -> tuple:
    if kind == "quote":
        return ("quote", item.get("symbol"), item.get("source"), item.get("as_of"))
    if kind == "macro":
        return ("macro", item.get("indicator"), item.get("period"), item.get("source_name"))
    if kind == "filing":
        return ("filing", item.get("announcement_id") or item.get("dedup_key")
                or item.get("title"))
    return ("other", json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))


def check_dedup(items: list[dict], kind: str) -> list[dict]:
    seen: dict[tuple, int] = {}
    for it in items:
        k = dedup_key(kind, it)
        seen[k] = seen.get(k, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    if dup:
        total = sum(v - 1 for v in dup.values())
        return [_row("去重", f"{kind} 全量", WARN,
                     f"{len(dup)} 个键重复，共折叠 {total} 条",
                     "保留最新 retrieved_at 一条；重复多为多接口聚合所致")]
    return [_row("去重", f"{kind} 全量", PASS, "无重复键", "—")]


def validate(payload: dict) -> dict:
    kind = detect_kind(payload)
    retrieved_at = _parse_ts(payload.get("retrieved_at"))
    rows = check_top(payload, kind, retrieved_at)
    items = payload.get("items") or []
    for i, it in enumerate(items):
        if isinstance(it, dict):
            rows.extend(check_item(it, kind, i, retrieved_at))
    if items and all(isinstance(it, dict) for it in items):
        rows.extend(check_dedup(items, kind))
    summary = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for r in rows:
        summary[r["result"]] = summary.get(r["result"], 0) + 1
    return {"schema_version": SCHEMA_VERSION, "kind": kind,
            "item_count": len(items), "summary": summary, "checks": rows,
            "passed": summary[FAIL] == 0}


def to_markdown(res: dict) -> str:
    lines = ["# 统一信息格式校验结果", "",
             f"- 数据类型：{res['kind']} · 条数：{res['item_count']}",
             f"- 汇总：通过 {res['summary'][PASS]} · 未通过 {res['summary'][FAIL]} · "
             f"警告 {res['summary'][WARN]}", "",
             "| 校验项 | 对象 | 结果 | 原因 | 处置 |", "|---|---|---|---|---|"]
    for r in res["checks"]:
        cells = [r["check"], r["target"], r["result"], r["reason"], r["action"]]
        lines.append("| " + " | ".join(str(c).replace("|", "／") for c in cells) + " |")
    lines.append("")
    lines.append("> 校验通过不代表数据为实时；时效以每条 freshness_status 为准。")
    return "\n".join(lines)


def selftest() -> int:
    fails: list[str] = []

    def expect(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    now = "2026-10-13T12:00:00+08:00"
    good = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "price": 1279.23, "as_of": "2026-10-13T11:30:00+08:00",
        "freshness_status": "latest", "delay_seconds": 600, "source": "sina_hq",
        "source_name": "新浪", "retrieved_at": now, "note": "午间休市，当日最新"}]}
    res = validate(good)
    expect("正常行情通过", res["passed"] and res["kind"] == "quote")

    bad_realtime = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "price": 1279.23, "as_of": now,
        "freshness_status": "realtime", "delay_seconds": 3600, "source": "x",
        "source_name": "x", "retrieved_at": now}]}
    res = validate(bad_realtime)
    expect("realtime+1h 延迟必须未通过", not res["passed"])

    stale = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "price": 1279.23, "as_of": "2026-10-01T11:30:00+08:00",
        "freshness_status": "latest", "source": "x", "source_name": "x",
        "retrieved_at": now}]}
    expect("12 天前行情判过期", not validate(stale)["passed"])

    future = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "price": 1279.23, "as_of": "2026-10-13T20:00:00+08:00",
        "freshness_status": "latest", "source": "x", "source_name": "x",
        "retrieved_at": now}]}
    expect("未来时间戳未通过", not validate(future)["passed"])

    hype = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "price": 1279.23, "as_of": now,
        "freshness_status": "latest", "source": "x", "source_name": "x",
        "retrieved_at": now, "note": "实时行情推送"}]}
    expect("latest 却话术实时未通过", not validate(hype)["passed"])

    missing = {"status": "ok", "retrieved_at": now, "items": [{
        "symbol": "600519", "freshness_status": "latest", "source": "x",
        "source_name": "x"}]}
    expect("缺价格与时间未通过", not validate(missing)["passed"])

    dup = {"status": "ok", "retrieved_at": now, "items": [
        {"symbol": "600519", "price": 1.0, "as_of": now, "freshness_status": "latest",
         "source": "x", "source_name": "x", "retrieved_at": now},
        {"symbol": "600519", "price": 1.0, "as_of": now, "freshness_status": "latest",
         "source": "x", "source_name": "x", "retrieved_at": now}]}
    res = validate(dup)
    expect("重复键给警告", any(r["check"] == "去重" and r["result"] == WARN
                             for r in res["checks"]))

    filing = {"status": "ok", "market": "cn", "symbol": "600519",
              "retrieved_at": now, "items": [{
                  "title": "半年度报告", "published_at": "2026-08-15T00:00:00+08:00",
                  "freshness_status": "historical", "source_name": "巨潮",
                  "retrieved_at": now}]}
    expect("公告 schema 识别", validate(filing)["kind"] == "filing"
           and validate(filing)["passed"])

    if fails:
        print("自测失败：", file=sys.stderr)
        for f in fails:
            print("  ✗", f, file=sys.stderr)
        return 1
    print(f"数据校验器自测通过（{SCHEMA_VERSION}，含 realtime 矛盾 / 过期 / 未来时间戳 / "
          f"话术 / 缺字段 / 去重 用例）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="统一信息格式校验器")
    ap.add_argument("--input", help="输入 JSON 文件路径")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读 JSON")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="运行自测")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.input:
        try:
            payload = json.loads(Path(a.input).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"⛔ 输入文件读取失败：{e}", file=sys.stderr)
            return 2
    elif a.stdin:
        try:
            payload = json.load(sys.stdin)
        except Exception as e:  # noqa: BLE001
            print(f"⛔ 标准输入不是合法 JSON：{e}", file=sys.stderr)
            return 2
    else:
        ap.error("需要 --input 或 --stdin（或用 --selftest）")

    if not isinstance(payload, dict):
        print("⛔ 顶层必须是 JSON 对象", file=sys.stderr)
        return 2

    res = validate(payload)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(res))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
