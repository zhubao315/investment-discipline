#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_rules.py — 《投资的纪律》投资纪律规则引擎（纯标准库，零第三方依赖）

职责：把研究结论 / 用户持仓映射到书稿正式规则，逐条输出可复核的检查结果。
它**只判断约束是否触发**，不解释市场、不预测涨跌、不改规则。

阈值唯一来源：`references/rules-index.json`。引擎启动时把内置阈值与该文件逐条比对，
任何一条对不上 → 立即以退出码 2 拒绝执行（防止正文/索引被改动后引擎悄悄用旧值）。

用法
----
  python3 validate_rules.py --input portfolio.json        # 检查一份组合/交易输入
  python3 validate_rules.py --stdin                       # 从标准输入读 JSON
  python3 validate_rules.py --conflicts                   # 输出已知规则冲突报告
  python3 validate_rules.py --selftest                   # 边界值自测（不依赖外部输入）

输入 JSON（所有字段都可缺省；缺省即『数据不足』，绝不假设）
----------------------------------------------------------------
{
  "as_of": "2026-10-13T11:30:00+08:00",        # 数据时间（必填，用于报告标注）
  "provided_by": "user",                        # 数据提供方（user/agent）
  "total_assets": 1000000,                      # 总资产（元）
  "cash": 150000,                              # 现金
  "leverage": false,                            # 是否使用融资/配资/杠杆
  "valuation_regime": "normal",                 # normal | extreme_overvalued | extreme_undervalued
  "market_state": "normal",                     # normal | bull（R-VAL-01 牛市上浮至 >40%）
  "horizon_type": "normal",                     # 全局默认：short|mid|long（可被单仓覆盖）
  "positions": [
    {"symbol":"600519","name":"贵州茅台","industry":"白酒",
     "market_value":180000,                     # 市值（元）
     "cost_basis":170000,                       # 成本（元）
     "horizon":"mid",                           # short|mid|long（决定止损档）
     "stop_loss_pct":15,                        # 买入前设定的止损线（正数，%）
     "is_core":true,                            # 是否核心持仓
     "target_weight_pct":15,                    # 目标权重（%）
     "actual_weight_pct":null                   # 实际权重；缺省用 market_value/total_assets 计算
    }
  ],
  "asset_classes": [                            # 资产大类再平衡（R-RISK-03）
    {"class":"股票","target_pct":60,"actual_pct":65}
  ],
  "age": 35,                                    # R-POS-06 股债基准
  "risk_type": "balanced",                      # conservative|balanced|aggressive（R-POS-07）
  "stress_drawdown_pct": 18,                    # 压力测试回撤（%）
  "buy_candidate": {                            # 模式 B 买入前检查（R-VAL-01/R-BUY-01）
    "symbol":"600519","price":1279.23,
    "intrinsic_value_low":1900,                 # 保守/内在价值下限
    "margin_of_safety_pct": null,               # 可直接给；否则用上两行计算
    "red_lights": 0, "yellow_lights": 1,
    "verified_items": 23, "total_items": 25
  },
  "sell_review": {                              # 模式 C 卖出复核（R-SELL-01/02）
    "symbol":"600519",
    "triggers": {"S1":"not_triggered","S6":"triggered","S11":"待核实"}
  }
}

输出
----
Markdown 表格 + JSON（--json）。每行固定字段：
  规则编号 | 检查项 | 输入数据 | 数据时间 | 判断条件 | 检查结果 | 触发原因 | 建议动作

检查结果枚举（与 SKILL.md 一致）
  通过 / 未通过 / 需要关注 / 数据不足，无法判断 / 规则冲突，等待确认

边界语义（严格按书稿，禁止模糊化）
  R-POS-01  ≤20% 通过，>20% 未通过，>25% 必须减仓
  R-POS-02  ≤30% 通过，>30% 未通过（银行/保险/券商合并为大金融）
  R-POS-03  ≥15% 通过，<15% 未通过；极端高估档要求 30%-40%，极端低估档 ≥10%
  R-POS-04  持股 8-12 只；核心 5-8 只；核心权重 70%-80%
  R-RISK-02 亏损达到（≥）预设止损线即触发；止损线必须落在所属档位区间内
  R-RISK-03 |偏离| > 5pp 触发；== 5pp → 规则冲突（见 --conflicts）
  R-VAL-01  安全边际 >30%（严格大于）；==30.0% 不通过；牛市 >40%
  R-SELL-01 触发 ≥2 项执行；==1 项观察期；缺失数据不得计为未触发

退出码
  0 = 全部检查完成（含"数据不足"也算完成）
  2 = 规则索引与引擎阈值不一致 / 规则冲突未确认，拒绝输出检查结论
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
RULES_INDEX = SKILL / "references" / "rules-index.json"

# ---------------------------------------------------------------- 阈值表
# 每条阈值都必须能在 rules-index.json 的 rule_value 中找到 tokens（启动时校验）。
THRESHOLDS = {
    "R-POS-01": {"limit": 20.0, "initial": 10.0, "passive_trim": 25.0,
                 "tokens": ["20%", "10%", "25%"]},
    "R-POS-02": {"limit": 30.0, "tokens": ["30%"]},
    "R-POS-03": {"normal": 15.0, "overvalued_low": 30.0, "overvalued_high": 40.0,
                 "undervalued": 10.0, "tokens": ["15%", "30%", "40%", "10%"]},
    "R-POS-04": {"min": 8, "max": 12, "core_min": 5, "core_max": 8,
                 "core_share_low": 70.0, "core_share_high": 80.0,
                 "tokens": ["8-12", "5-8", "70%-80%"]},
    "R-POS-05": {"max_step": 10.0, "tokens": ["10 个百分点"]},
    "R-POS-06": {"band": 10.0, "tokens": ["100−年龄", "±10 个百分点"]},
    "R-POS-07": {"drawdown": {"conservative": 10.0, "balanced": 25.0, "aggressive": 40.0},
                 "tokens": ["<10%", "<25%", "<40%"]},
    "R-RISK-01": {"tokens": ["零", "房贷"]},
    "R-RISK-02": {"bands": {"short": (5.0, 8.0), "mid": (10.0, 15.0), "long": (20.0, 25.0)},
                  "tokens": ["5%-8%", "10%-15%", "20%-25%"]},
    "R-RISK-03": {"deviation": 5.0, "tokens": ["5个百分点", "季度"]},
    "R-RISK-04": {"tokens": ["波动率看仓位"]},
    "R-VAL-01": {"margin": 30.0, "bull_margin": 40.0, "watch_low": 10.0,
                 "tokens": ["30%", "40%"]},
    "R-VAL-02": {"low_pct": 25.0, "high_pct": 75.0, "tokens": ["25%", "75%"]},
    "R-BUY-01": {"max_red": 0, "max_yellow": 2, "sell_review_yellow": 3,
                 "tokens": ["0", "2"]},
    "R-BUY-02": {"total": 25, "tokens": ["25 项全过才放行"]},
    "R-SELL-01": {"execute_at": 2, "watch_at": 1, "tokens": ["≥2"]},
    "R-SELL-02": {"tokens": ["当日收盘前完成"]},
    "R-PLAN-01": {"tokens": ["第51-90天"]},
}

# ------------------------------------------------- 已知规则冲突（唯一来源：rules-index.json）
# 冲突登记在 references/rules-index.json 的 conflicts 字段（由 build_skill_references.py
# 生成）。引擎只读取、只报告，不修改、不裁决。
def _load_conflicts() -> list[dict]:
    try:
        idx = json.loads(RULES_INDEX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return idx.get("conflicts") or []


CONFLICTS = _load_conflicts()

PASS, FAIL, ATTENTION, INSUFFICIENT, CONFLICT = (
    "通过", "未通过", "需要关注", "数据不足，无法判断", "规则冲突，等待确认")


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def load_index() -> dict:
    try:
        return json.loads(RULES_INDEX.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[规则引擎] 无法读取规则索引 {RULES_INDEX}：{e}")


def verify_thresholds(index: dict) -> list[str]:
    """把引擎内置阈值与 rules-index.json 逐条比对；不一致即拒绝执行。"""
    by_id = {r["rule_id"]: r for r in index.get("rules", [])}
    problems = []
    for rid, spec in THRESHOLDS.items():
        r = by_id.get(rid)
        if not r:
            problems.append(f"{rid} 不在规则索引中")
            continue
        blob = json.dumps(r, ensure_ascii=False)
        for t in spec.get("tokens", []):
            if t not in blob:
                problems.append(f"{rid} 的阈值标记 {t!r} 未出现在规则索引 rule_value/quote 中")
    return problems


# ---------------------------------------------------------------- 检查器
def row(rule_id: str, name: str, inputs: str, data_time: str, condition: str,
        result: str, reason: str, action: str) -> dict:
    return {"rule_id": rule_id, "check": name, "inputs": inputs, "data_time": data_time,
            "condition": condition, "result": result, "reason": reason, "action": action}


def _missing(fields: list[str]) -> str:
    return "缺少字段：" + "、".join(fields)


def check_position_limits(d: dict) -> list[dict]:
    """R-POS-01 单只仓位 + R-POS-04 持股数量/核心占比。"""
    out: list[dict] = []
    as_of = d.get("as_of", "未提供")
    total = _num(d.get("total_assets"))
    positions = d.get("positions") or []
    t = THRESHOLDS["R-POS-01"]

    if not positions or total is None or total <= 0:
        out.append(row("R-POS-01", "单只个股仓位上限",
                       _missing(["positions", "total_assets"]), as_of,
                       f"单只 ≤{t['limit']:.0f}%（初始建仓 ≤{t['initial']:.0f}%，"
                       f"被动 >{t['passive_trim']:.0f}% 必须减仓）",
                       INSUFFICIENT,
                       "未提供真实持仓与总资产，不得假设任何仓位",
                       "补充 positions 与 total_assets 后重新检查"))
    else:
        for p in positions:
            sym = p.get("symbol") or p.get("name") or "(未命名)"
            mv = _num(p.get("market_value"))
            w = _num(p.get("actual_weight_pct"))
            if w is None and mv is not None:
                w = mv / total * 100
            if w is None:
                out.append(row("R-POS-01", f"单只仓位 · {sym}",
                               _missing(["market_value 或 actual_weight_pct"]), as_of,
                               f"≤{t['limit']:.0f}%", INSUFFICIENT,
                               "该持仓缺市值与权重，无法计算占比", "补充该持仓市值"))
                continue
            inputs = f"{sym} 市值 {mv if mv is not None else '未提供'}，占总资产 {w:.2f}%"
            if w > t["passive_trim"]:
                out.append(row("R-POS-01", f"单只仓位 · {sym}", inputs, as_of,
                               f"≤{t['limit']:.0f}%（被动 >{t['passive_trim']:.0f}% 必须减仓）",
                               FAIL, f"占比 {w:.2f}% 已超过 {t['passive_trim']:.0f}%，触发被动减仓条款",
                               "按书稿原文『上涨被动超 25% 必须减仓』执行，无需主观判断"))
            elif w > t["limit"]:
                out.append(row("R-POS-01", f"单只仓位 · {sym}", inputs, as_of,
                               f"≤{t['limit']:.0f}%", FAIL,
                               f"占比 {w:.2f}% 超过单只上限 {t['limit']:.0f}%",
                               "将该持仓降至 ≤20%；若是上涨被动超限，同步写明减仓动作"))
            else:
                note = ""
                if p.get("build_stage") == "initial" and w > t["initial"]:
                    out.append(row("R-POS-01", f"初始建仓 · {sym}", inputs, as_of,
                                   f"初始建仓 ≤{t['initial']:.0f}%", FAIL,
                                   f"初始建仓占比 {w:.2f}% 超过 {t['initial']:.0f}%",
                                   "首次建仓应 ≤10%，其余仓位分批"))
                    continue
                if abs(w - t["limit"]) < 1e-9:
                    note = "（恰好等于上限，未超限；任何新增买入都会越线）"
                out.append(row("R-POS-01", f"单只仓位 · {sym}", inputs, as_of,
                               f"≤{t['limit']:.0f}%", PASS,
                               f"占比 {w:.2f}% 在上限内{note}",
                               "维持；新增买入前重新计算占比"))

    # R-POS-04 持股数量与核心占比
    t4 = THRESHOLDS["R-POS-04"]
    if not positions:
        out.append(row("R-POS-04", "持股数量与核心占比", _missing(["positions"]), as_of,
                       f"{t4['min']}-{t4['max']} 只；核心 {t4['core_min']}-{t4['core_max']} 只、"
                       f"占 {t4['core_share_low']:.0f}%-{t4['core_share_high']:.0f}%",
                       INSUFFICIENT, "未提供持仓数量", "提供完整持仓清单"))
    else:
        n = len(positions)
        annotated = [p for p in positions if p.get("is_core") is not None]
        core = [p for p in positions if p.get("is_core")]
        mv = [_num(p.get("market_value")) for p in positions]
        core_mv = [_num(p.get("market_value")) for p in core]
        total_mv = sum(v for v in mv if v) or None
        core_share = (sum(v for v in core_mv if v) / total_mv * 100
                      if total_mv and all(v is not None for v in core_mv) else None)
        inputs = f"持股 {n} 只，核心 {len(core)} 只" + (
            f"，核心市值占比 {core_share:.1f}%" if core_share is not None else "，核心占比无法计算（缺市值）")
        if 0 < len(annotated) < n:
            inputs += f"（另有 {n - len(annotated)} 只未标注 is_core，未计入核心）"
        if not (t4["min"] <= n <= t4["max"]):
            out.append(row("R-POS-04", "持股数量", inputs, as_of,
                           f"{t4['min']}-{t4['max']} 只", FAIL,
                           f"当前 {n} 只，超出区间", "按漏斗补足或精简到 8-12 只"))
        elif not annotated:
            out.append(row("R-POS-04", "核心持仓数量", inputs, as_of,
                           f"核心 {t4['core_min']}-{t4['core_max']} 只", INSUFFICIENT,
                           "没有任何持仓标注 is_core——缺失标注不得计为核心 0 只",
                           "逐只标注 is_core（true/false）后重新检查"))
        elif not (t4["core_min"] <= len(core) <= t4["core_max"]):
            out.append(row("R-POS-04", "核心持仓数量", inputs, as_of,
                           f"核心 {t4['core_min']}-{t4['core_max']} 只", FAIL,
                           f"核心 {len(core)} 只，超出区间", "重新界定核心/卫星，或调整持仓结构"))
        elif core_share is None:
            out.append(row("R-POS-04", "核心持仓占比", inputs, as_of,
                           f"{t4['core_share_low']:.0f}%-{t4['core_share_high']:.0f}%",
                           INSUFFICIENT, "缺核心持仓市值，无法计算占比", "补充各持仓市值"))
        elif not (t4["core_share_low"] <= core_share <= t4["core_share_high"]):
            out.append(row("R-POS-04", "核心持仓占比", inputs, as_of,
                           f"{t4['core_share_low']:.0f}%-{t4['core_share_high']:.0f}%",
                           FAIL, f"核心占比 {core_share:.1f}% 不在目标区间",
                           "调整核心/卫星比例至 70%-80%"))
        else:
            out.append(row("R-POS-04", "持股数量与核心占比", inputs, as_of,
                           f"{t4['min']}-{t4['max']} 只 / 核心 {t4['core_min']}-{t4['core_max']} 只 / "
                           f"核心占比 {t4['core_share_low']:.0f}%-{t4['core_share_high']:.0f}%",
                           PASS, f"{n} 只、核心 {len(core)} 只、核心占比 {core_share:.1f}% 均在区间",
                           "维持结构，每只保留一句话买入理由"))
    return out


def check_industry(d: dict) -> list[dict]:
    """R-POS-02 行业集中度（银行/保险/券商合并为大金融）。"""
    t = THRESHOLDS["R-POS-02"]
    as_of = d.get("as_of", "未提供")
    total = _num(d.get("total_assets"))
    positions = d.get("positions") or []
    FIN = {"银行", "保险", "证券", "券商", "多元金融", "金融"}
    if not positions or total is None or total <= 0:
        return [row("R-POS-02", "单行业仓位上限", _missing(["positions", "total_assets"]), as_of,
                    f"≤{t['limit']:.0f}%（大金融合并计）", INSUFFICIENT,
                    "未提供持仓与总资产，不得假设行业占比", "提供持仓行业与市值")]
    agg: dict[str, float] = {}
    unknown = []
    for p in positions:
        ind = p.get("industry")
        mv = _num(p.get("market_value"))
        w = _num(p.get("actual_weight_pct"))
        if w is None and mv is not None:
            w = mv / total * 100
        if not ind or w is None:
            unknown.append(p.get("symbol") or "(未命名)")
            continue
        key = "大金融" if ind in FIN else ind
        agg[key] = agg.get(key, 0.0) + w
    out = []
    for ind, w in sorted(agg.items(), key=lambda kv: -kv[1]):
        inputs = f"{ind} 合计占比 {w:.2f}%"
        if w > t["limit"] + 1e-9:
            out.append(row("R-POS-02", f"行业集中度 · {ind}", inputs, as_of,
                           f"≤{t['limit']:.0f}%（大金融合并计）", FAIL,
                           f"{ind} 占比 {w:.2f}% 超过 {t['limit']:.0f}%",
                           "降低该行业敞口至 ≤30%；银行/保险/券商不得伪分散"))
        else:
            out.append(row("R-POS-02", f"行业集中度 · {ind}", inputs, as_of,
                           f"≤{t['limit']:.0f}%（大金融合并计）", PASS,
                           f"{ind} 占比 {w:.2f}% 在上限内",
                           "维持；新增买入前重算行业合计"))
    if unknown:
        out.append(row("R-POS-02", "行业分类完整性", "未标注行业或市值的标的：" + "、".join(unknown),
                       as_of, "每只持仓必须有行业归类", INSUFFICIENT,
                       "缺行业/市值 → 无法完整核算集中度",
                       "补全行业归类；金融类合并计入大金融"))
    return out


def check_cash_leverage(d: dict) -> list[dict]:
    """R-POS-03 现金比例 + R-RISK-01 零杠杆。"""
    as_of = d.get("as_of", "未提供")
    t = THRESHOLDS["R-POS-03"]
    out = []
    total = _num(d.get("total_assets"))
    cash = _num(d.get("cash"))
    regime = d.get("valuation_regime", "normal")
    if total is None or cash is None or total <= 0:
        out.append(row("R-POS-03", "现金比例下限", _missing(["cash", "total_assets"]), as_of,
                       f"常规 ≥{t['normal']:.0f}%（极端高估 {t['overvalued_low']:.0f}%-"
                       f"{t['overvalued_high']:.0f}%，极端低估可降至 {t['undervalued']:.0f}%）",
                       INSUFFICIENT, "未提供现金与总资产，不得假设现金比例", "提供 cash 与 total_assets"))
    else:
        pct = cash / total * 100
        inputs = f"现金 {cash:,.0f} / 总资产 {total:,.0f} = {pct:.2f}%"
        if regime == "extreme_overvalued":
            cond = f"极端高估档：{t['overvalued_low']:.0f}%-{t['overvalued_high']:.0f}%"
            if pct < t["overvalued_low"]:
                out.append(row("R-POS-03", "现金比例 · 极端高估档", inputs, as_of, cond, FAIL,
                               f"现金 {pct:.2f}% 低于极端高估档下沿 {t['overvalued_low']:.0f}%",
                               "把现金提到 30%-40% 区间（需先确认确处极端高估，见 R-VAL-02 分位）"))
            else:
                out.append(row("R-POS-03", "现金比例 · 极端高估档", inputs, as_of, cond, PASS,
                               f"现金 {pct:.2f}% 在 {t['overvalued_low']:.0f}%-{t['overvalued_high']:.0f}% 区间",
                               "维持；分位回落到常态区间后回到 ≥15% 口径"))
        elif regime == "extreme_undervalued":
            cond = f"极端低估档 ≥{t['undervalued']:.0f}%（可降至 10%，但不得更低）"
            if pct < t["undervalued"] - 1e-9:
                out.append(row("R-POS-03", "现金比例 · 极端低估档", inputs, as_of, cond, FAIL,
                               f"现金 {pct:.2f}% 低于极端低估档允许下限 {t['undervalued']:.0f}%",
                               "补回现金至 ≥10%（书稿允许极端低估降至 10%，不允许更低）"))
            else:
                out.append(row("R-POS-03", "现金比例 · 极端低估档", inputs, as_of, cond, PASS,
                               f"现金 {pct:.2f}% ≥ {t['undervalued']:.0f}%", "维持"))
        else:
            cond = f"常规 ≥{t['normal']:.0f}%"
            if pct < t["normal"] - 1e-9:
                out.append(row("R-POS-03", "现金比例", inputs, as_of, cond, FAIL,
                               f"现金 {pct:.2f}% 低于 {t['normal']:.0f}% 下限",
                               "书稿要求『破 15% 当周补回』；现金是看涨期权，永不满仓"))
            else:
                out.append(row("R-POS-03", "现金比例", inputs, as_of, cond, PASS,
                               f"现金 {pct:.2f}% ≥ {t['normal']:.0f}%", "维持"))

    lev = d.get("leverage")
    if lev is None:
        out.append(row("R-RISK-01", "零杠杆", _missing(["leverage"]), as_of,
                       "不使用融资融券及任何投资杠杆（房贷除外）", INSUFFICIENT,
                       "未提供杠杆信息，不得假设无杠杆", "明确回答是否使用融资/配资/期权卖开"))
    else:
        bad = bool(lev)
        note = str(d.get("leverage_detail", ""))
        out.append(row("R-RISK-01", "零杠杆", f"leverage={lev}" + (f"；{note}" if note else ""),
                       as_of, "不使用融资融券及任何投资杠杆（房贷除外）",
                       FAIL if bad else PASS,
                       "检测到融资/配资/杠杆敞口，书稿要求任何融资配资即清零" if bad
                       else "未检测到杠杆敞口",
                       "清零杠杆敞口（唯一可接受长期负债是房贷）" if bad else "维持"))
    return out


def check_stop_loss(d: dict) -> list[dict]:
    """R-RISK-02 三档硬止损：档位合法性 + 是否触发。"""
    as_of = d.get("as_of", "未提供")
    bands = THRESHOLDS["R-RISK-02"]["bands"]
    out = []
    positions = d.get("positions") or []
    if not positions:
        return [row("R-RISK-02", "三档硬止损", _missing(["positions"]), as_of,
                    "短线 5%-8% / 中线 10%-15% / 长线 20%-25%，买入前设定",
                    INSUFFICIENT, "未提供持仓与止损设定", "提供每只持仓的持有周期与预设止损线")]
    for p in positions:
        sym = p.get("symbol") or p.get("name") or "(未命名)"
        horizon = p.get("horizon") or d.get("horizon_type")
        stop = _num(p.get("stop_loss_pct"))
        cost = _num(p.get("cost_basis"))
        price = _num(p.get("current_price"))
        mv = _num(p.get("market_value"))
        if cost and mv and price is None:
            # 允许用市值/成本推算价格：需要数量字段，缺数量则不推算（不猜）
            price = None
        if horizon not in bands:
            out.append(row("R-RISK-02", f"止损档位 · {sym}",
                           f"horizon={horizon!r}", as_of,
                           "short=短线 / mid=中线 / long=长线", INSUFFICIENT,
                           "持有周期未标注，无法确定止损档", "标注该持仓投资期限（short/mid/long）"))
            continue
        lo, hi = bands[horizon]
        if stop is None:
            out.append(row("R-RISK-02", f"止损线设定 · {sym}", _missing(["stop_loss_pct"]), as_of,
                           f"{horizon} 档 {lo:.0f}%-{hi:.0f}%，买入前设定", FAIL,
                           "买入前未设定硬止损线（书稿要求买入前写死）",
                           "立即补设定止损线并写入交易记录；不向下摊薄止损线"))
            continue
        if not (lo - 1e-9 <= stop <= hi + 1e-9):
            out.append(row("R-RISK-02", f"止损档位合法性 · {sym}",
                           f"预设止损 {stop}%, 持有周期 {horizon}", as_of,
                           f"{horizon} 档应落在 {lo:.0f}%-{hi:.0f}%", FAIL,
                           f"预设 {stop}% 不在 {horizon} 档区间 [{lo}%, {hi}%]",
                           "按书稿档位重设：短线 5-8% / 中线 10-15% / 长线 20-25%"))
            continue
        if cost is None or cost <= 0 or price is None:
            out.append(row("R-RISK-02", f"止损触发检查 · {sym}",
                           _missing(["cost_basis", "current_price"]), as_of,
                           f"现价较成本下跌 ≥ {stop}% 即触发", INSUFFICIENT,
                           "缺成本或现价，无法判断是否触发（不估算）",
                           "提供 cost_basis 与 current_price（注明价格时间与时效）"))
            continue
        loss = (cost - price) / cost * 100
        inputs = f"成本 {cost}, 现价 {price}, 亏损 {loss:.2f}%, 预设止损 {stop}%"
        if loss >= stop - 1e-9:
            out.append(row("R-RISK-02", f"止损触发 · {sym}", inputs, as_of,
                           f"亏损 {loss:.2f}% ≥ 预设止损 {stop}%", FAIL,
                           "已触发硬止损（达到即触发，不含糊）",
                           "无条件执行；短线/中线触发当日执行，长线不迟于次日；卖出后冷静 48 小时"))
        else:
            out.append(row("R-RISK-02", f"止损触发 · {sym}", inputs, as_of,
                           f"亏损 {loss:.2f}% < 预设止损 {stop}%", PASS,
                           f"未触发（距止损线还有 {stop - loss:.2f} 个百分点）",
                           "维持；每周核对止损线与现金比例"))
    return out


def check_rebalance(d: dict) -> list[dict]:
    """R-RISK-03 再平衡触发（含已知冲突 C-01）。"""
    as_of = d.get("as_of", "未提供")
    dev = THRESHOLDS["R-RISK-03"]["deviation"]
    classes = d.get("asset_classes") or []
    out = []
    if not classes:
        return [row("R-RISK-03", "再平衡触发", _missing(["asset_classes"]), as_of,
                    f"季度检查；单类偏离 >{dev:.0f} 个百分点即执行（等于 5pp 见冲突 C-01）",
                    INSUFFICIENT, "未提供目标/实际配置，不得假设偏离度",
                    "提供各类资产的目标比例与实际比例")]
    for c in classes:
        cls = c.get("class", "(未命名)")
        tgt, act = _num(c.get("target_pct")), _num(c.get("actual_pct"))
        if tgt is None or act is None:
            out.append(row("R-RISK-03", f"再平衡 · {cls}",
                           _missing(["target_pct", "actual_pct"]), as_of,
                           f"偏离 >{dev:.0f}pp 触发", INSUFFICIENT, "缺目标或实际比例",
                           "补充该资产类的目标与实际占比"))
            continue
        delta = act - tgt
        inputs = f"{cls} 目标 {tgt}%, 实际 {act}%, 偏离 {delta:+.1f} 个百分点"
        if abs(delta) > dev + 1e-9:
            out.append(row("R-RISK-03", f"再平衡 · {cls}", inputs, as_of,
                           f"|偏离| > {dev:.0f} 个百分点即执行", FAIL,
                           f"偏离 {abs(delta):.1f}pp 已超过 {dev:.0f}pp",
                           "执行再平衡（本质是纪律化的卖高买低）；全年执行 0 次属危险信号"))
        elif abs(abs(delta) - dev) < 1e-9:
            out.append(row("R-RISK-03", f"再平衡 · {cls}", inputs, as_of,
                           f"|偏离| > {dev:.0f}pp 触发；== {dev:.0f}pp 存在两种原文表述",
                           CONFLICT,
                           f"偏离恰为 {dev:.1f}pp：appendix/ch06/ch07/ch08 写『≥5 即执行』，"
                           f"ch02 写『>5 执行』——见冲突 C-01",
                           "等待作者确认 C-01；确认前不自动执行、不自动忽略"))
        else:
            out.append(row("R-RISK-03", f"再平衡 · {cls}", inputs, as_of,
                           f"|偏离| > {dev:.0f} 个百分点即执行", PASS,
                           f"偏离 {abs(delta):.1f}pp 未达触发线（<5pp 不操作）",
                           "维持；每季度复核一次"))
    return out


def check_buy(d: dict) -> list[dict]:
    """R-VAL-01 安全边际 + R-BUY-01/02 清单灯色与完整性。"""
    as_of = d.get("as_of", "未提供")
    tv = THRESHOLDS["R-VAL-01"]
    b = d.get("buy_candidate") or {}
    out = []
    if not b:
        return [row("R-VAL-01", "买入安全边际", _missing(["buy_candidate"]), as_of,
                    f"安全边际 >{tv['margin']:.0f}%（牛市 >{tv['bull_margin']:.0f}%）",
                    INSUFFICIENT, "未提供买入候选与估值输入", "提供价格与保守估值（或已算好的安全边际）")]
    sym = b.get("symbol", "候选标的")
    bull = d.get("market_state") == "bull"
    need = tv["bull_margin"] if bull else tv["margin"]
    price = _num(b.get("price"))
    iv = _num(b.get("intrinsic_value_low"))
    mos = _num(b.get("margin_of_safety_pct"))
    if mos is None and price is not None and iv is not None and iv > 0:
        mos = (iv - price) / iv * 100
    cond = (f"安全边际 =（内在价值下限−现价）÷内在价值下限 > {need:.0f}%"
            + ("（牛市情境上浮）" if bull else ""))
    if mos is None:
        out.append(row("R-VAL-01", f"安全边际 · {sym}",
                       _missing(["margin_of_safety_pct 或 (price + intrinsic_value_low)"]),
                       as_of, cond, INSUFFICIENT,
                       "缺估值输入，无法计算安全边际（不编目标价）",
                       "按 valuation.md 至少两种方法交叉验证后提供保守估值"))
    else:
        inputs = (f"现价 {price if price is not None else '未提供'}，"
                  f"保守估值下限 {iv if iv is not None else '未提供'}，安全边际 {mos:.2f}%")
        if mos <= tv["watch_low"] - 1e-9:
            out.append(row("R-VAL-01", f"安全边际 · {sym}", inputs, as_of, cond, FAIL,
                           f"安全边际 {mos:.2f}% ≤ {tv['watch_low']:.0f}%，低于观察清单门槛",
                           "不买入；连观察清单都需重新估值后再议"))
        elif mos <= need + 1e-9:
            conflict_note = ""
            if mos >= 20.0:
                conflict_note = "（另注：ch06 清单 V4 的『≥20% 通过』与本条 >30% 硬底线冲突，见 C-02）"
            out.append(row("R-VAL-01", f"安全边际 · {sym}", inputs, as_of, cond, FAIL,
                           f"安全边际 {mos:.2f}% 未超过 {need:.0f}%（等于阈值也不放行）{conflict_note}",
                           "只进观察清单，不动手；牛市情境按 >40% 重新衡量"))
        else:
            out.append(row("R-VAL-01", f"安全边际 · {sym}", inputs, as_of, cond, PASS,
                           f"安全边际 {mos:.2f}% > {need:.0f}%", "可进入买入清单逐项核验"))

    tb = THRESHOLDS["R-BUY-01"]
    red, yellow = b.get("red_lights"), b.get("yellow_lights")
    if red is None or yellow is None:
        out.append(row("R-BUY-01", "买入清单灯色上限", _missing(["red_lights", "yellow_lights"]),
                       as_of, f"红灯必须 {tb['max_red']} 个；黄灯 ≤{tb['max_yellow']} 个",
                       INSUFFICIENT, "未提供 25 项逐项打分结果",
                       "按 buy-checklist.md 逐项打分（缺失数据记待核实，不得记通过）"))
    else:
        inputs = f"红灯 {red} 个，黄灯 {yellow} 个"
        if red > tb["max_red"]:
            out.append(row("R-BUY-01", "买入清单灯色上限", inputs, as_of,
                           f"红灯必须 {tb['max_red']} 个", FAIL, f"存在 {red} 个红灯",
                           "任一红灯即否决买入；复核该项是否真的不可接受"))
        elif yellow > tb["max_yellow"]:
            out.append(row("R-BUY-01", "买入清单灯色上限", inputs, as_of,
                           f"黄灯 ≤{tb['max_yellow']} 个", FAIL,
                           f"黄灯 {yellow} 个超过上限；≥{tb['sell_review_yellow']} 个需启动卖出评估",
                           "先消除黄灯隐忧；持有场景下黄灯≥3 个启动卖出评估"))
        else:
            out.append(row("R-BUY-01", "买入清单灯色上限", inputs, as_of,
                           f"红灯 {tb['max_red']} 个、黄灯 ≤{tb['max_yellow']} 个", PASS,
                           "灯色在允许区间", "继续核验清单完整性"))

    tb2 = THRESHOLDS["R-BUY-02"]
    ver, tot = b.get("verified_items"), b.get("total_items", tb2["total"])
    if ver is None:
        out.append(row("R-BUY-02", "买入清单完整性", _missing(["verified_items"]), as_of,
                       f"{tb2['total']} 项全部逐项过，1 项未过则不买", INSUFFICIENT,
                       "未提供逐项核验数量", "25 项逐项核验，缺失数据记待核实"))
    elif ver < tot:
        out.append(row("R-BUY-02", "买入清单完整性", f"已核验 {ver}/{tot} 项", as_of,
                       f"{tb2['total']} 项全过才放行", FAIL,
                       f"尚有 {tot - ver} 项未逐项核验",
                       "补齐全部 25 项；缺失数据只能记待核实，不得记通过"))
    else:
        out.append(row("R-BUY-02", "买入清单完整性", f"已核验 {ver}/{tot} 项", as_of,
                       f"{tb2['total']} 项全过才放行", PASS,
                       "25 项已逐项过", "在安全边际与仓位约束内分批买入（初始 ≤10%）"))
    return out


def check_sell(d: dict) -> list[dict]:
    """R-SELL-01 卖出触发计数 + R-SELL-02 硬止损执行时效。"""
    as_of = d.get("as_of", "未提供")
    t = THRESHOLDS["R-SELL-01"]
    s = d.get("sell_review") or {}
    out = []
    if not s:
        return [row("R-SELL-01", "卖出触发计数", _missing(["sell_review"]), as_of,
                    f"触发 ≥{t['execute_at']} 项执行；=={t['watch_at']} 项进观察期",
                    INSUFFICIENT, "未提供 S1-S12 逐项复核结果",
                    "按 sell-checklist.md 逐项复核（缺失数据不得计为未触发）")]
    trig = s.get("triggers") or {}
    if not trig:
        return [row("R-SELL-01", "卖出触发计数", _missing(["triggers"]), as_of,
                    f"触发 ≥{t['execute_at']} 项执行；=={t['watch_at']} 项进观察期",
                    INSUFFICIENT, "未提供任何 S 项判定", "逐项判定 S1-S12")]
    fired = [k for k, v in trig.items() if v == "triggered"]
    unknown = [k for k, v in trig.items() if v not in ("triggered", "not_triggered")]
    sym = s.get("symbol", "持仓")
    inputs = (f"{sym}：触发 {len(fired)} 项（{'、'.join(sorted(fired)) or '无'}）"
              + (f"；待核实 {len(unknown)} 项（{'、'.join(sorted(unknown))}）" if unknown else ""))
    n = len(fired)
    if n >= t["execute_at"]:
        out.append(row("R-SELL-01", f"卖出触发计数 · {sym}", inputs, as_of,
                       f"触发 ≥{t['execute_at']} 项即执行卖出", FAIL,
                       f"已触发 {n} 项，达到执行标准", "按清单处置执行卖出，不再商量"))
    elif n == t["watch_at"]:
        if unknown:
            out.append(row("R-SELL-01", f"卖出触发计数 · {sym}", inputs, as_of,
                           f"=={t['watch_at']} 项进观察期并写复查日", INSUFFICIENT,
                           f"已触发 {n} 项、另有 {len(unknown)} 项待核实——待核实项若核实为触发"
                           f"将达到执行线 {t['execute_at']} 项，不得先判为观察期",
                           "先核实待核项，再判定是进入观察期还是执行卖出"))
        else:
            out.append(row("R-SELL-01", f"卖出触发计数 · {sym}", inputs, as_of,
                           f"=={t['watch_at']} 项进观察期并写复查日", ATTENTION,
                           "仅 1 项触发：进入观察期", "写下复查日期，逐项跟进新公告与新数据"))
    else:
        out.append(row("R-SELL-01", f"卖出触发计数 · {sym}", inputs, as_of,
                       f"触发 ≥{t['execute_at']} 项执行", PASS if not unknown else INSUFFICIENT,
                       f"触发 {n} 项，未达执行线" if not unknown else
                       f"触发 {n} 项未达执行线，但有 {len(unknown)} 项待核实——缺失数据不得计为未触发",
                       "维持持有并继续逐项监控" if not unknown else
                       "先核实待核项，结论只能是『待核实』而非『不触发』"))
    if "S11" in fired:
        out.append(row("R-SELL-02", "硬止损执行时效", f"S11 已触发（{sym}）", as_of,
                       "若为 S11 硬止损，当日收盘前完成", FAIL,
                       "硬止损触发，执行时效为当日收盘前（长线档另见冲突 C-03）",
                       "当日收盘前完成卖出，不做『再等等』；卖出后冷静 48 小时"))
    return out


def check_realloc(d: dict) -> list[dict]:
    """R-POS-05 单次调整幅度（若提供 reconfiguration 输入）。"""
    as_of = d.get("as_of", "未提供")
    t = THRESHOLDS["R-POS-05"]
    rc = d.get("reconfiguration")
    if not rc:
        return []
    out = []
    for c in rc if isinstance(rc, list) else [rc]:
        cls = c.get("class", "(未命名)")
        step = _num(c.get("step_pct"))
        if step is None:
            out.append(row("R-POS-05", f"单次调整幅度 · {cls}", _missing(["step_pct"]), as_of,
                           f"单次 ≤{t['max_step']:.0f} 个百分点，分 3-6 个月完成", INSUFFICIENT,
                           "未提供本次调整幅度", "提供调整百分点"))
            continue
        if abs(step) > t["max_step"] + 1e-9:
            out.append(row("R-POS-05", f"单次调整幅度 · {cls}", f"本次调整 {step:+.1f}pp", as_of,
                           f"≤{t['max_step']:.0f} 个百分点，分 3-6 个月完成", FAIL,
                           f"单次调整 {abs(step):.1f}pp 超过 {t['max_step']:.0f}pp",
                           "拆成 3-6 个月分批，避免一次性调仓拉满交易成本与择时风险"))
        else:
            out.append(row("R-POS-05", f"单次调整幅度 · {cls}", f"本次调整 {step:+.1f}pp", as_of,
                           f"≤{t['max_step']:.0f} 个百分点", PASS, "幅度合规", "按计划分批执行"))
    return out


def check_lifecycle(d: dict) -> list[dict]:
    """R-POS-06 股债基准 + R-POS-07 压力测试回撤上限。"""
    as_of = d.get("as_of", "未提供")
    out = []
    t6 = THRESHOLDS["R-POS-06"]
    age = d.get("age")
    stock_actual = None
    for c in d.get("asset_classes") or []:
        if c.get("class") == "股票":
            stock_actual = _num(c.get("actual_pct"))
    if age is not None and stock_actual is not None:
        bench = 100 - float(age)
        delta = stock_actual - bench
        out.append(row("R-POS-06", "股债基准（参考指标）",
                       f"年龄 {age} → 基准 {bench:.0f}%；实际股票 {stock_actual}%", as_of,
                       f"股票基准 = 100−年龄，±{t6['band']:.0f} 个百分点（建议，非硬约束）",
                       ATTENTION if abs(delta) > t6["band"] else PASS,
                       f"偏离基准 {delta:+.1f}pp" + ("，超出 ±10pp 建议带" if abs(delta) > t6["band"] else ""),
                       "生命周期表与个人四要素优先于公式；公式只是起点"))

    t7 = THRESHOLDS["R-POS-07"]
    rt = d.get("risk_type")
    dd = _num(d.get("stress_drawdown_pct"))
    if rt in t7["drawdown"] and dd is not None:
        cap = t7["drawdown"][rt]
        out.append(row("R-POS-07", "压力测试判定线",
                       f"{rt} 型回撤上限 <{cap:.0f}%；压力测试回撤 {dd:.1f}%", as_of,
                       f"冲击 < 方案回撤上限 且 < 个人承受上限 才允许维持仓位",
                       FAIL if dd >= cap - 1e-9 else PASS,
                       f"冲击 {dd:.1f}% {'≥' if dd >= cap else '<'} 上限 {cap:.0f}%",
                       "超上限则先降仓再谈配置；跑压力测试前不得维持当前仓位" if dd >= cap else "维持"))
    return out


CHECKS: list[Callable[[dict], list[dict]]] = [
    check_position_limits, check_industry, check_cash_leverage, check_stop_loss,
    check_rebalance, check_buy, check_sell, check_realloc, check_lifecycle,
]


def run(d: dict) -> dict:
    rows: list[dict] = []
    for fn in CHECKS:
        rows.extend(fn(d))
    summary = {PASS: 0, FAIL: 0, ATTENTION: 0, INSUFFICIENT: 0, CONFLICT: 0}
    for r in rows:
        summary[r["result"]] = summary.get(r["result"], 0) + 1
    blocking = [r for r in rows if r["result"] == FAIL]
    data_gaps = [r for r in rows if r["result"] == INSUFFICIENT]
    conflicts = [r for r in rows if r["result"] == CONFLICT]
    return {
        "as_of": d.get("as_of", "未提供"),
        "generated_by": "investment-discipline validate_rules.py",
        "rules_source": "references/rules-index.json（唯一事实来源）",
        "summary": summary,
        "checks": rows,
        "blocking_failures": len(blocking),
        "data_gaps": len(data_gaps),
        "open_conflicts": len(conflicts),
        "disclaimer": ("本表为纪律约束检查结果，不构成投资建议；"
                       "规则阈值出自《投资的纪律》书稿，未做任何修改"),
    }


def to_markdown(res: dict) -> str:
    lines = ["# 投资纪律检查结果", "",
             f"- 数据时间：{res['as_of']}",
             f"- 规则来源：{res['rules_source']}",
             f"- 汇总：通过 {res['summary'].get(PASS, 0)} · 未通过 {res['summary'].get(FAIL, 0)} · "
             f"需要关注 {res['summary'].get(ATTENTION, 0)} · "
             f"数据不足 {res['summary'].get(INSUFFICIENT, 0)} · "
             f"规则冲突 {res['summary'].get(CONFLICT, 0)}", "",
             "| 规则编号 | 检查项 | 输入数据 | 数据时间 | 判断条件 | 检查结果 | 触发原因 | 建议动作 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in res["checks"]:
        cells = [r["rule_id"], r["check"], r["inputs"], r["data_time"], r["condition"],
                 r["result"], r["reason"], r["action"]]
        lines.append("| " + " | ".join(str(c).replace("|", "／") for c in cells) + " |")
    lines += ["", "> " + res["disclaimer"]]
    return "\n".join(lines)


def conflict_report() -> str:
    lines = ["# 规则冲突报告（等待作者裁决，引擎不做单方选择）", ""]
    for c in CONFLICTS:
        lines += [
            f"## {c['id']} · {c['title']}",
            "",
            f"- 相关规则：`{c['rule_id']}`",
            f"- 严重程度：{c['severity']}",
            f"- 冲突边界：{c['boundary']}",
            "",
            f"**表述 A**：{c['side_a']['text']}",
        ]
        lines += [f"  - 出处：{s}" for s in c["side_a"]["sources"]]
        lines += [f"", f"**表述 B**：{c['side_b']['text']}"]
        lines += [f"  - 出处：{s}" for s in c["side_b"]["sources"]]
        lines += ["", f"**引擎行为**：{c['engine_behavior']}",
                  f"**待确认**：{c['resolution_required']}",
                  f"**状态**：{c['status']}", ""]
    lines.append("> 冲突未裁决前，引擎对边界值输出『规则冲突，等待确认』，不静默选择任一表述。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测（边界值）
def selftest() -> int:
    fails = []

    def expect(name, got, want):
        if got != want:
            fails.append(f"{name}: 期望 {want}，实际 {got}")

    # R-POS-01 边界：20.0 通过 / 20.01 未通过 / 25.1 强制减仓
    base = {"as_of": "2026-10-13", "total_assets": 1000000, "cash": 200000,
            "leverage": False, "positions": []}
    for w, want in [(20.0, PASS), (20.01, FAIL), (25.1, FAIL), (19.99, PASS)]:
        d = dict(base, positions=[{"symbol": "X", "market_value": w * 10000}])
        got = check_position_limits(d)[0]["result"]
        expect(f"R-POS-01 weight={w}", got, want)
    # 初始建仓 10% 边界
    d = dict(base, positions=[{"symbol": "X", "market_value": 100000, "build_stage": "initial"}])
    expect("R-POS-01 initial=10", check_position_limits(d)[0]["result"], PASS)
    d["positions"][0]["market_value"] = 100001
    expect("R-POS-01 initial=10.0001", check_position_limits(d)[0]["result"], FAIL)

    # R-POS-02：30.0 通过 / 30.01 未通过；大金融合并
    d = dict(base, positions=[{"symbol": "A", "industry": "银行", "market_value": 150000},
                              {"symbol": "B", "industry": "保险", "market_value": 100000},
                              {"symbol": "C", "industry": "券商", "market_value": 60000}])
    got = [r for r in check_industry(d) if r["rule_id"] == "R-POS-02" and "大金融" in r["check"]]
    expect("R-POS-02 大金融=31% 合并后超限", got[0]["result"], FAIL)
    d["positions"][2]["market_value"] = 50000
    got = [r for r in check_industry(d) if "大金融" in r["check"]]
    expect("R-POS-02 大金融=30% 恰好", got[0]["result"], PASS)

    # R-POS-03：15.0 通过 / 14.99 未通过；极端低估 10% 边界
    d = dict(base, cash=150000)
    expect("R-POS-03 cash=15%", check_cash_leverage(d)[0]["result"], PASS)
    d = dict(base, cash=149900)
    expect("R-POS-03 cash=14.99%", check_cash_leverage(d)[0]["result"], FAIL)
    d = dict(base, cash=100000, valuation_regime="extreme_undervalued")
    expect("R-POS-03 extreme_low=10%", check_cash_leverage(d)[0]["result"], PASS)
    d["cash"] = 99900
    expect("R-POS-03 extreme_low=9.99%", check_cash_leverage(d)[0]["result"], FAIL)
    d = dict(base, cash=300000, valuation_regime="extreme_overvalued")
    expect("R-POS-03 extreme_high=30%", check_cash_leverage(d)[0]["result"], PASS)
    d["cash"] = 299900
    expect("R-POS-03 extreme_high=29.99%", check_cash_leverage(d)[0]["result"], FAIL)

    # R-POS-04：未标 is_core → 数据不足（不得计为核心 0 只）；8 核心/80% 恰好在区间
    ten = [{"symbol": f"S{i}", "market_value": 100000} for i in range(10)]
    d = dict(base, positions=ten)
    got = [r for r in check_position_limits(d) if r["rule_id"] == "R-POS-04"]
    expect("R-POS-04 未标 is_core", got[0]["result"], INSUFFICIENT)
    for i in range(8):
        ten[i]["is_core"] = True
    ten[8]["is_core"] = False
    ten[9]["is_core"] = False
    got = [r for r in check_position_limits(d) if r["rule_id"] == "R-POS-04"]
    expect("R-POS-04 核心8只/80%", got[0]["result"], PASS)

    # R-RISK-02：止损档边界与触发边界
    d = dict(base, positions=[{"symbol": "X", "horizon": "mid", "stop_loss_pct": 15,
                               "cost_basis": 100, "current_price": 85}])
    expect("R-RISK-02 loss=15% 恰好触发", check_stop_loss(d)[0]["result"], FAIL)
    d["positions"][0]["current_price"] = 85.01
    expect("R-RISK-02 loss=14.99% 未触发", check_stop_loss(d)[0]["result"], PASS)
    d["positions"][0]["stop_loss_pct"] = 9
    expect("R-RISK-02 mid档 9% 档位外", check_stop_loss(d)[0]["result"], FAIL)
    d["positions"][0].pop("stop_loss_pct")
    expect("R-RISK-02 未设止损", check_stop_loss(d)[0]["result"], FAIL)

    # R-RISK-03：>5 触发 / ==5 冲突 / <5 不触发
    d = dict(base, asset_classes=[{"class": "股票", "target_pct": 60, "actual_pct": 65}])
    expect("R-RISK-03 dev=5.0 冲突", check_rebalance(d)[0]["result"], CONFLICT)
    d["asset_classes"][0]["actual_pct"] = 65.01
    expect("R-RISK-03 dev=5.01 触发", check_rebalance(d)[0]["result"], FAIL)
    d["asset_classes"][0]["actual_pct"] = 64.99
    expect("R-RISK-03 dev=4.99 不触发", check_rebalance(d)[0]["result"], PASS)

    # R-VAL-01：>30% 才过；==30 不过
    d = dict(base, buy_candidate={"symbol": "X", "price": 70, "intrinsic_value_low": 100})
    expect("R-VAL-01 mos=30% 不放行", check_buy(d)[0]["result"], FAIL)
    d["buy_candidate"]["price"] = 69.9
    expect("R-VAL-01 mos=30.1% 放行", check_buy(d)[0]["result"], PASS)
    d = dict(base, market_state="bull", buy_candidate={"symbol": "X", "price": 59,
                                                       "intrinsic_value_low": 100})
    expect("R-VAL-01 牛市 mos=41% 放行", check_buy(d)[0]["result"], PASS)
    d["buy_candidate"]["price"] = 60
    expect("R-VAL-01 牛市 mos=40% 不放行", check_buy(d)[0]["result"], FAIL)

    # R-BUY-01：红灯 0 / 黄灯 2 边界
    d = dict(base, buy_candidate={"symbol": "X", "red_lights": 0, "yellow_lights": 2,
                                  "verified_items": 25, "total_items": 25})
    expect("R-BUY-01 黄灯=2 通过", check_buy(d)[1]["result"], PASS)
    d["buy_candidate"]["yellow_lights"] = 3
    expect("R-BUY-01 黄灯=3 未通过", check_buy(d)[1]["result"], FAIL)
    d["buy_candidate"].update(red_lights=1, yellow_lights=0)
    expect("R-BUY-01 红灯=1 未通过", check_buy(d)[1]["result"], FAIL)

    # R-SELL-01：0/1/2 项
    for n, want in [(0, PASS), (1, ATTENTION), (2, FAIL)]:
        trig = {f"S{i}": ("triggered" if i <= n else "not_triggered") for i in range(1, 4)}
        d = dict(base, sell_review={"symbol": "X", "triggers": trig})
        expect(f"R-SELL-01 触发={n}", check_sell(d)[0]["result"], want)
    # 待核实不得当作未触发
    d = dict(base, sell_review={"symbol": "X", "triggers": {"S1": "triggered", "S2": "待核实"}})
    expect("R-SELL-01 待核实项存在", check_sell(d)[0]["result"], INSUFFICIENT)
    # S11 触发 → R-SELL-02
    d = dict(base, sell_review={"symbol": "X",
                                "triggers": {"S1": "triggered", "S11": "triggered"}})
    expect("R-SELL-02 S11 触发", check_sell(d)[1]["result"], FAIL)

    # 数据缺失 → 数据不足，且不得出现通过
    res = run({"as_of": "2026-10-13"})
    expect("空输入 无通过项", res["summary"].get(PASS, 0), 0)
    expect("空输入 存在数据不足", res["summary"].get(INSUFFICIENT, 0) > 0, True)

    if fails:
        print("自测失败：")
        for f in fails:
            print("  ✗", f)
        return 1
    print("规则引擎自测通过（含 5pp / 30% / 20% / 止损 / 灯色 / 卖出计数 边界值）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="《投资的纪律》纪律规则引擎")
    ap.add_argument("--input", help="输入 JSON 文件路径")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读 JSON")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--conflicts", action="store_true", help="输出规则冲突报告")
    ap.add_argument("--selftest", action="store_true", help="运行边界值自测")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.conflicts:
        print(conflict_report())
        return 0

    index = load_index()
    problems = verify_thresholds(index)
    if problems:
        print("⛔ 规则索引与引擎阈值不一致，拒绝执行检查：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        print("请先修复 rules-index 与引擎阈值的对应关系，或由作者确认后同步更新。",
              file=sys.stderr)
        return 2

    if a.input:
        try:
            data = json.loads(Path(a.input).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"⛔ 输入文件读取失败：{e}", file=sys.stderr)
            return 2
    elif a.stdin:
        try:
            data = json.load(sys.stdin)
        except Exception as e:  # noqa: BLE001
            print(f"⛔ 标准输入不是合法 JSON：{e}", file=sys.stderr)
            return 2
    else:
        ap.error("需要 --input 或 --stdin（或用 --selftest / --conflicts）")

    res = run(data)
    if res.get("open_conflicts"):
        print("⚠️  存在边界规则冲突（C-01），对应项输出『规则冲突，等待确认』；详见 --conflicts",
              file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else to_markdown(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
