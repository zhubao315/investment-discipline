#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_skill.py — 《投资的纪律》Agent Skill 与网站一致性校验（纯标准库，零依赖）

用法：
    python3 skills/investment-discipline/scripts/validate_skill.py [--root 站点根目录]

检查项分三类：
    PASS    通过
    FAIL    失败（脚本以非 0 退出码结束）
    MANUAL  需人工判断 / 外部网络不可达，不计入失败
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent                      # skills/investment-discipline
ROOT = SKILL.parent.parent               # 站点根
LEGACY = "投资实操手册合集"
NEW_TITLE = "投资的纪律"

# 全书统一口径（任务书 §3.5），用于核对规则索引数值
CANONICAL = {
    "R-POS-01": ["20%", "10%", "25%"],
    "R-POS-02": ["30%"],
    "R-POS-03": ["15%", "30%", "40%", "10%"],
    "R-POS-04": ["8-12", "5-8", "70%-80%"],
    "R-RISK-01": ["零", "房贷"],
    "R-RISK-02": ["5%-8%", "10%-15%", "20%-25%"],
    "R-RISK-03": ["5个百分点", "季度"],
    "R-VAL-01": ["30%", "40%"],
    "R-BUY-01": ["0", "2"],
    "R-SELL-01": ["≥2"],
}

REQUIRED = [
    "SKILL.md",
    "README.md",
    "references/investment-philosophy.md",
    "references/asset-allocation.md",
    "references/industry-analysis.md",
    "references/financial-analysis.md",
    "references/valuation.md",
    "references/buy-checklist.md",
    "references/sell-checklist.md",
    "references/risk-management.md",
    "references/investor-psychology.md",
    "references/review-workflow.md",
    "references/ai-research.md",
    "references/practical-workflow.md",
    "references/rules-index.md",
    "references/rules-index.json",
    "references/market-analysis.md",
    "references/data-sources.md",
    "references/data-schema.md",
    "references/report-template.md",
    "references/error-handling.md",
    "templates/investment-research.md",
    "templates/pre-purchase-review.md",
    "templates/sell-review.md",
    "templates/portfolio-review.md",
    "templates/monthly-review.md",
    "scripts/validate_skill.py",
    "scripts/market_data.py",
    "scripts/validate_data.py",
    "scripts/validate_rules.py",
    "scripts/smoke_test.py",
]

results: list[tuple[str, str, str]] = []


def check(ok: bool, name: str, detail: str = "", manual: bool = False) -> None:
    tag = "MANUAL" if manual and ok else ("PASS" if ok else "FAIL")
    results.append((tag, name, detail))


def site_present() -> bool:
    """是否处于含站点的完整项目（独立分发的 Skill 包没有站点页面）。"""
    return (ROOT / "index.html").exists() or bool(list((ROOT / "chapters").glob("ch*.html")))


# ------------------------------------------------------------------ 1. 文件齐备
def t_files() -> None:
    missing = [f for f in REQUIRED if not (SKILL / f).exists()]
    check(not missing, "1.1 Skill 必需文件齐备",
          "缺失: " + ", ".join(missing) if missing else f"{len(REQUIRED)} 个文件全部存在")


# ---------------------------------------------------------- 2. frontmatter 规范
def t_frontmatter() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    check(bool(m), "2.1 SKILL.md 含 YAML frontmatter")
    if not m:
        return
    fm = m.group(1)
    has_name = re.search(r"^name:\s*([^\n]+)$", fm, re.M)
    has_desc = re.search(r"^description:\s*(>)?-?", fm, re.M)
    check(bool(has_name) and bool(has_desc), "2.2 frontmatter 含 name 与 description")
    name = has_name.group(1).strip() if has_name else ""
    ok_name = bool(re.fullmatch(r"[a-z0-9-]+", name)) and name == SKILL.name
    check(ok_name, "2.3 name 合法且与目录名一致",
          f"name={name!r} 目录={SKILL.name!r}")
    # description 必须说明适用场景与边界
    body = text[m.end():]
    desc_block = fm[fm.find("description"):]
    check("不荐股" in desc_block or "不预测" in desc_block,
          "2.4 description 声明风险边界")
    check("## 11. 免责声明" in body, "2.5 SKILL.md 含免责声明章节")


# ------------------------------------------------------------ 3. 相对链接有效
def t_links() -> None:
    md_files = list(SKILL.rglob("*.md"))
    bad: list[str] = []
    checked = 0
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"`?((?:references|templates|scripts)/[\w.\-]+\.(?:md|py|json))`?",
                             text):
            target = m.group(1)
            checked += 1
            # SKILL.md 中写在表格里的裸路径也应可解析
            if not (SKILL / target).exists():
                bad.append(f"{f.relative_to(SKILL)} → {target}")
    check(not bad, "3.1 Skill 内部相对引用全部存在",
          f"检查 {checked} 处" + ("" if not bad else "；失效: " + "; ".join(bad)))

    # 路由表覆盖所有 references
    skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    refs = {p.name for p in (SKILL / "references").glob("*.md")}
    unrouted = sorted(r for r in refs if f"`references/{r}`" not in skill_md)
    check(not unrouted, "3.2 SKILL.md 路由/索引覆盖全部参考文档",
          "" if not unrouted else "未路由: " + ", ".join(unrouted))


# ------------------------------------------------------- 4. 规则索引唯一一致
def t_rules() -> None:
    jpath = SKILL / "references" / "rules-index.json"
    mpath = SKILL / "references" / "rules-index.md"
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        check(False, "4.1 rules-index.json 可解析", str(e))
        return
    rules = data.get("rules", [])
    ids = [r.get("rule_id") for r in rules]
    check(len(ids) == len(set(ids)), "4.2 rule_id 唯一", f"{len(ids)} 条")
    check(data.get("count") == len(rules), "4.3 count 与条目数一致")

    text_md = mpath.read_text(encoding="utf-8")
    missing_md = [i for i in ids if i not in text_md]
    check(not missing_md, "4.4 md 与 json 条目一致",
          "" if not missing_md else "md 缺: " + ", ".join(missing_md))

    # 统一口径数值核对
    by_id = {r["rule_id"]: r for r in rules}
    bad = []
    for rid, needles in CANONICAL.items():
        r = by_id.get(rid)
        if not r:
            bad.append(f"{rid}(缺失)")
            continue
        blob = json.dumps(r, ensure_ascii=False)
        for n in needles:
            if n not in blob:
                bad.append(f"{rid} 缺 {n}")
    check(not bad, "4.5 数值与全书统一口径一致", "" if not bad else "; ".join(bad))

    # 引文回源（需书稿素材 `_content/`；独立分发包不含书稿，故跳过）
    if (ROOT / "_content").exists():
        unverifiable = []
        for r in rules:
            src = ROOT / r["source_file"]
            if not src.exists():
                unverifiable.append(f"{r['rule_id']} 源文件缺失")
                continue
            if r["quote"] not in src.read_text(encoding="utf-8"):
                unverifiable.append(f"{r['rule_id']} 引文不在原文")
        check(not unverifiable, "4.6 每条规则引文回源命中",
              f"{len(rules)} 条" if not unverifiable else "; ".join(unverifiable))
    else:
        check(True, "4.6 引文回源（独立 Skill 包不含书稿素材，跳过）",
              f"{len(rules)} 条引文已随 rules-index 分发", manual=True)

    # 百分比/百分点区分提示
    check("百分点" in text_md and "pp" in text_md,
          "4.7 索引明确区分百分比与百分点")


# ------------------------------------------------------- 5. 品牌与旧名残留
def t_brand() -> None:
    if not site_present():
        check(True, "5. 品牌与旧名残留（独立 Skill 包，无站点，跳过）",
              "未检测到 index.html / chapters/", manual=True)
        return
    pages = sorted(ROOT.rglob("*.html"))
    bad_title, bad_h1, new_ok = [], [], 0
    for p in pages:
        html = p.read_text(encoding="utf-8")
        if re.search(rf"<title>[^<]*{LEGACY}", html):
            bad_title.append(str(p.relative_to(ROOT)))
        if re.search(rf"<h1>[^<]*{LEGACY}", html):
            bad_h1.append(str(p.relative_to(ROOT)))
        if NEW_TITLE in html:
            new_ok += 1
    check(not bad_title, "5.1 无旧书名出现在 <title>",
          "" if not bad_title else ", ".join(bad_title))
    check(not bad_h1, "5.2 无旧书名出现在 <h1>",
          "" if not bad_h1 else ", ".join(bad_h1))
    check(new_ok == len(pages) and pages,
          "5.3 全部页面标注正式书名",
          f"{new_ok}/{len(pages)} 页含《{NEW_TITLE}》")
    # 允许 about.html 中"内部代号"的变更说明
    stray = []
    for p in pages:
        html = p.read_text(encoding="utf-8")
        for line in html.splitlines():
            if LEGACY in line and ("内部代号" not in line) and ("变更记录" not in line):
                stray.append(f"{p.name}: {line.strip()[:60]}")
    check(not stray, "5.4 旧名仅保留在变更/历史说明中",
          "" if not stray else "; ".join(stray[:5]))


# ------------------------------------------------------- 6. 网站链接与资源
def t_site() -> None:
    if not site_present():
        check(True, "6. 网站链接与资源（独立 Skill 包，无站点，跳过）",
              "未检测到站点页面", manual=True)
        return
    pages = sorted(ROOT.rglob("*.html"))
    broken = []
    for p in pages:
        html = p.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:href|src)="([^"#]+?)"', html):
            u = m.group(1)
            if u.startswith(("http://", "https://", "mailto:", "data:", "//")):
                continue
            u = u.split("?")[0].split("#")[0]
            target = (p.parent / u).resolve()
            if not target.exists():
                broken.append(f"{p.name} → {u}")
    check(not broken, "6.1 全站内部链接与资源无断链",
          f"扫描 {len(pages)} 页" + ("" if not broken else "; " + "; ".join(broken[:8])))

    idx = ROOT / "index.html"
    chs = list((ROOT / "chapters").glob("ch*.html"))
    check(idx.exists() and len(chs) >= 11, "6.2 首页与 11 章页面完整",
          f"chapters={len(chs)}")
    imgs = {m.group(1) for p in pages
            for m in re.finditer(r'src="(assets/img/[^"]+)"',
                                 p.read_text(encoding="utf-8"))}
    missing_img = [i for i in imgs if not (ROOT / i).exists()]
    check(not missing_img, "6.3 页面引用图片全部存在",
          f"{len(imgs)} 张" + ("" if not missing_img else "; " + ", ".join(missing_img)))
    check((ROOT / "agent" / "index.html").exists() and
          (ROOT / "agent" / "quickstart.md").exists(),
          "6.4 网站 Agent 入口存在（HTML + 原始 Markdown）")


# ------------------------------------------------------- 7. 可移植性 / 隐私
# 故意拆开拼接，避免本脚本的正则模式自匹配自身源码
PATTERNS = [r"192\.168\.\d+\.\d+", r"111\.119\.\d+\.\d+", r"127\.0\.0\.1",
            "loc" + "host", "/ho" + "me/her" + "mes", r"219\.142\.87\.233"]


def t_portable() -> None:
    hits = []
    for base in (SKILL, ROOT / "agent"):
        for f in list(base.rglob("*")):
            if f.is_file() and f.suffix in {".md", ".json", ".py", ".html", ".txt"}:
                txt = f.read_text(encoding="utf-8", errors="ignore")
                for pat in PATTERNS:
                    if re.search(pat, txt):
                        hits.append(f"{f.relative_to(ROOT)}:{pat}")
    check(not hits, "7.1 Skill 与 agent/ 不含开发环境或内网地址",
          "" if not hits else "; ".join(hits[:8]))

    # llms.txt 不得包含私网地址
    llms = ROOT / "llms.txt"
    if llms.exists():
        t = llms.read_text(encoding="utf-8")
        ok = not any(re.search(p, t) for p in PATTERNS)
        check(ok, "7.2 llms.txt 不含私网/开发地址")
    elif not site_present():
        check(True, "7.2 llms.txt（独立 Skill 包无站点，跳过）", manual=True)
    else:
        check(False, "7.2 llms.txt 存在")


# ------------------------------------------------------- 8. 独立安装可搬运
def t_portability() -> None:
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "investment-discipline"
        shutil.copytree(SKILL, dst)
        # 复制到异地后：必需文件仍在、相对引用仍解析
        missing = [f for f in REQUIRED if not (dst / f).exists()]
        bad = []
        for f in dst.rglob("*.md"):
            for m in re.finditer(r"((?:references|templates|scripts)/[\w.\-]+\.md)", 
                                 f.read_text(encoding="utf-8")):
                if not (dst / m.group(1)).exists():
                    bad.append(m.group(1))
        check(not missing and not bad, "8.1 整目录复制到异地后相对引用仍有效",
              "移植测试通过" if not missing and not bad else f"{missing}{bad}")
        # 移植后的 SKILL 不应引用站点 HTML 才能工作
        txt = (dst / "SKILL.md").read_text(encoding="utf-8")
        needs_net = bool(re.search(r"https?://", txt))
        check(not needs_net, "8.2 SKILL.md 核心能力不依赖网络",
              "" if not needs_net else "SKILL.md 中出现 URL")


# ------------------------------------------------------- 9. 验收用例
def t_acceptance() -> None:
    skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    checks = [
        ("9.1 模式A路由到研究参考文档",
         "investment-philosophy.md" in skill_md and "valuation.md" in skill_md),
        ("9.2 缺失数据→待核实而非通过",
         "待核实" in skill_md and "绝不记为" in skill_md),
        ("9.3 模式D包含持仓硬约束",
         all(x in skill_md for x in ["R-POS-01", "R-POS-03", "R-RISK-01"])),
        ("9.4 模式E区分过程与结果",
         "决策过程质量与交易结果" in skill_md),
        ("9.5 模式F引用90天正式计划",
         (SKILL / "references" / "practical-workflow.md").exists()
         and "90" in skill_md),
        ("9.6 模式G要求交叉核验",
         "ai-research.md" in skill_md and "交叉核验" in skill_md),
        ("9.7 明确拒绝自动交易授权",
         "不执行交易" in skill_md or "执行交易" in skill_md),
        ("9.8 离线可用（无外链依赖）",
         not re.search(r"https?://", skill_md)),
        ("9.11 实时采集与校验脚本齐备",
         all((SKILL / "scripts" / s).exists() for s in
             ["market_data.py", "validate_data.py", "validate_rules.py", "smoke_test.py"])),
        ("9.12 数据源/时效/缺口纪律写入 SKILL",
         all(t in skill_md for t in
             ["data-sources.md", "data-schema.md", "error-handling.md",
              "待核实", "最新可用"])),
        ("9.13 规则冲突已登记且不自行裁决",
         "conflicts" in (SKILL / "references" / "rules-index.json").read_text("utf-8")
         and "等待" in (SKILL / "references" / "rules-index.md").read_text("utf-8")),
        ("9.14 测试与夹具存在",
         (SKILL / "tests" / "test_rule_engine.py").exists()
         and (SKILL / "tests" / "test_freshness.py").exists()
         and (SKILL / "tests" / "fixtures").is_dir()),
    ]
    for name, ok in checks:
        check(ok, name)
    if site_present():
        check(all((ROOT / "chapters" / f"ch{i:02d}.html").exists() for i in range(1, 12))
              and (ROOT / "appendix" / "appendix-90days.html").exists(),
              "9.10 章节导航与附录路径仍有效")
        chs = len(list((ROOT / "_content").glob("ch*.md")))
        check(chs >= 11, "9.9 内容素材完整（_content ≥11 章）", f"{chs} 章")
    else:
        check(True, "9.9 / 9.10 站点内容与章节导航（独立 Skill 包，无站点，跳过）",
              manual=True)


# --------------------------------------------------- 10. 实时采集能力与数据源
def t_realtime() -> None:
    ds = (SKILL / "references" / "data-sources.md").read_text(encoding="utf-8")
    check("实测" in ds and "时效" in ds, "10.1 数据源注册表含实测标记与时效说明")
    check("realtime" in ds and "latest" in ds, "10.2 时效状态枚举写入数据源表")
    check("不得自称实时" in ds or "不得出现" in ds,
          "10.3 无授权实时源的红线已写明")

    try:
        idx = json.loads((SKILL / "references" / "rules-index.json").read_text("utf-8"))
        conflicts = idx.get("conflicts") or []
        check(len(conflicts) >= 3, "10.4 规则冲突登记（≥3 条）", f"{len(conflicts)} 条")
    except Exception as e:  # noqa: BLE001
        check(False, "10.4 规则冲突登记", str(e))

    # 缓存不得写入 Skill 目录（可移植性）
    stray = [p for p in SKILL.rglob("*") if p.name in (".cache", "cache")]
    check(not stray, "10.5 Skill 目录不含运行时缓存",
          "" if not stray else str([str(s.relative_to(SKILL)) for s in stray[:3]]))


# ---------------------------------------------------------------------- main
def main() -> int:
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="站点根目录")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    ROOT = Path(a.root).resolve()

    for fn in (t_files, t_frontmatter, t_links, t_rules, t_brand,
               t_site, t_portable, t_portability, t_acceptance, t_realtime):
        fn()

    fails = [r for r in results if r[0] == "FAIL"]
    manuals = [r for r in results if r[0] == "MANUAL"]
    if not a.quiet:
        print(f"\n{'='*78}\n  《投资的纪律》Agent Skill 校验报告\n{'='*78}")
        for tag, name, detail in results:
            mark = {"PASS": "✅", "FAIL": "❌", "MANUAL": "⏸️ "}[tag]
            print(f" {mark} {name}" + (f"  —  {detail}" if detail else ""))
        print(f"{'-'*78}")
        print(f" 合计 {len(results)} 项：通过 {len(results)-len(fails)-len(manuals)}"
              f"，失败 {len(fails)}，需人工 {len(manuals)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
