# investment-discipline — Agent Skill 使用说明

> 《投资的纪律：从情绪交易到纪律复利》· 朱宝 著
> 个人投资研究与纪律执行助手。**可脱离网站独立运行，整个目录即完整分发包。**

## 一、这是什么

把全书的 11 章方法、买入 25 项 / 卖出 12 项清单、仓位与风控硬约束、90 天计划，封装成智能体可**发现、读取、按需加载、可追溯引用**的 Skill：

- `SKILL.md` — 统一入口：身份、触发条件、硬约束、任务路由、输出格式、禁止行为
- `references/` — 18 份按需加载的参考文档（含 `rules-index.md/.json` **规则唯一事实来源**，以及采集/时效/报告/异常处理文档）
- `templates/` — 5 份可复用输出模板（研究 / 买入前检查 / 卖出复核 / 组合检查 / 复盘）
- `scripts/market_data.py` — 统一市场数据适配器（行情 / 公告 / 新闻 / 宏观 / 日历 / 数据集）
- `scripts/validate_data.py` — 采集结果格式与时效校验
- `scripts/validate_rules.py` — 按 `rules-index.json` 的纪律规则引擎（含冲突报告）
- `scripts/validate_skill.py` — 结构、链接、规则一致性、可移植性校验

**两类参考文档**：`investment-philosophy.md`…`practical-workflow.md` 等由 `build_skill_references.py` 从书稿素材 `_content/*.md` **派生**（正文同源，引文回源断言，不得手改）；`market-analysis.md`、`data-sources.md`、`data-schema.md`、`report-template.md`、`error-handling.md` 为**工具与流程文档**（不含书稿阈值数字，手写维护）。

## 二、安装

**前置要求：仅需 Python 3.8+ 标准库，无第三方依赖。** 规则检查与校验**无需联网**；模式 H（实时采集）可选联网，离线时走本地缓存或 `tests/fixtures/` 夹具。

### 方式 A：编程智能体（Claude Code / Codex 等支持 Agent Skills 的环境）

```bash
# 项目级：把整个目录放进项目的 skills/ 下
cp -r skills/investment-discipline /你的项目/skills/

# 用户级（跨项目可用）
cp -r skills/investment-discipline ~/.claude/skills/        # Claude Code
cp -r skills/investment-discipline ~/.codex/skills/         # Codex（若支持）
```

重启会话后，智能体应能自动发现 `SKILL.md`；也可显式指示：
> 请加载 investment-discipline Skill。

### 方式 B：任何能读 Markdown 的通用 Agent

1. 把 `investment-discipline/` 整个目录复制到你的工作区任意位置；
2. 让 Agent 先读 `SKILL.md`；
3. 按第 4 节的任务路由，按需读取 `references/` 下的对应文件。

**无需网站、无需服务器、无需内网地址**——所有规则随包分发。

### 方式 C：网站侧入口（供外部智能体发现）

线上入口（部署后）：`/agent/index.html`（人读）与 `/agent/index.md`、`/agent/quickstart.md`（原始 Markdown）。
站点根目录另提供 `llms.txt` 做内容发现。

## 三、目录结构

```
investment-discipline/
├── SKILL.md                     # 入口（YAML frontmatter，name: investment-discipline）
├── README.md                    # 本文件
├── references/
│   ├── investment-philosophy.md # ch01 投资哲学
│   ├── asset-allocation.md      # ch02 资产配置
│   ├── industry-analysis.md     # ch03 行业分析
│   ├── financial-analysis.md    # ch04 财报分析
│   ├── valuation.md             # ch05 估值与安全边际
│   ├── buy-checklist.md         # ch06 买入 25 项（五维度）
│   ├── sell-checklist.md        # ch06 卖出 12 项（四类）
│   ├── risk-management.md       # ch07 风控：三层风险、四止损
│   ├── investor-psychology.md   # ch08 心理与偏差
│   ├── review-workflow.md       # ch09 复盘工作流
│   ├── ai-research.md           # ch10 AI 投研
│   ├── practical-workflow.md    # ch11 + 90 天执行表
│   ├── rules-index.md           # ★ 规则唯一事实来源（人读）
│   ├── rules-index.json         # ★ 同上（机读，字段一一对应；含 conflicts 冲突登记）
│   ├── market-analysis.md       # 模式 H：采集→校验→分析→规则→输出闭环
│   ├── data-sources.md          # 数据源注册表（层级/时效/降级/密钥约定）
│   ├── data-schema.md           # 统一信息格式、时效状态机、去重与缓存
│   ├── report-template.md       # 各模式统一输出模板与机读字段约定
│   └── error-handling.md        # 失败分级、降级留痕、Prompt 注入防护
├── templates/
│   ├── investment-research.md   # 模式 A
│   ├── pre-purchase-review.md   # 模式 B
│   ├── sell-review.md           # 模式 C
│   ├── portfolio-review.md      # 模式 D
│   └── monthly-review.md        # 模式 E（交易/月度复盘）
├── tests/
│   ├── test_rule_engine.py      # 规则引擎单测（阈值/边界/冲突）
│   ├── test_freshness.py        # 时效状态机与代码归一化单测
│   ├── test_data_sources.py     # 联网冒烟（NO_NETWORK=1 跳过）
│   └── fixtures/                # 离线回放样本
└── scripts/
    ├── market_data.py           # 采集适配器
    ├── validate_data.py         # 数据格式/时效校验
    ├── validate_rules.py        # 纪律规则引擎
    ├── validate_skill.py        # 全量校验（纯标准库）
    └── smoke_test.py            # 端到端三流程冒烟
```

## 四、按需加载策略（勿全量加载）

| 任务 | 只读这些 |
|---|---|
| 判断是否需要本 Skill | `SKILL.md` |
| 研究公司 | `investment-philosophy.md` + `industry-analysis.md` + `financial-analysis.md` + `valuation.md` |
| 买入前检查 | `buy-checklist.md` + `valuation.md` + `financial-analysis.md` |
| 卖出复核 | `sell-checklist.md` + `risk-management.md` |
| 组合/仓位检查 | `asset-allocation.md` + `risk-management.md` |
| 交易复盘 | `review-workflow.md` + `investor-psychology.md` |
| 90 天计划 | `practical-workflow.md` |
| AI 投研 | `ai-research.md` |
| 市场日报 / 采集数据 | `market-analysis.md` + `data-sources.md` + `data-schema.md` + `report-template.md` + `error-handling.md` |
| 任何数值口径核对 | `rules-index.json` |

## 五、校验

```bash
python3 skills/investment-discipline/scripts/validate_skill.py
# 退出码 0 = 全部通过；报告会列出每一项 PASS / FAIL / MANUAL
```

覆盖：文件齐备、frontmatter 规范、相对链接、rule_id 唯一性、
**数值与全书统一口径一致**、**引文回源**、旧书名残留、全站断链、
私网地址泄漏、整目录移植测试、14 条验收用例，以及实时采集脚本齐备、
数据源/时效/缺口纪律、规则冲突登记、运行时缓存隔离。

> 在**独立分发包**（本目录单独复制，无网站与书稿素材）中运行本脚本时，与站点/书稿相关的检查
> （品牌一致性、全站断链、章节导航、引文回源、`llms.txt`）会标记为「跳过」，其余全部照常校验。

其它自检命令：

```bash
python3 scripts/market_data.py sources --probe               # 数据源连通性
python3 scripts/market_data.py quote 600519 --json \
  | python3 scripts/validate_data.py --stdin                 # 采集→校验
python3 scripts/validate_rules.py --selftest                 # 规则引擎边界自测
python3 scripts/validate_rules.py --conflicts                # 规则冲突报告
python3 scripts/smoke_test.py --flow all --offline           # 端到端三流程（离线夹具）
python3 -m unittest discover -s tests -p 'test_*.py'         # 单元测试
```

## 六、多平台兼容说明

| 环境 | 接入方式 | 兼容性说明 |
|---|---|---|
| Claude Code | 复制到 `~/.claude/skills/` 或项目 `.claude/skills/` | 按官方 Agent Skills 约定发现 `SKILL.md` |
| Codex / OpenCode | 项目 `skills/` 目录，或在 AGENTS.md 中显式引用 | 各环境发现规则不一，必要时显式引用路径 |
| 通用 Markdown Agent | 直接读 `SKILL.md` | 无需插件；frontmatter 为标准 YAML 子集 |
| 无 Python 环境 | Skill 本身仍可用（纯 Markdown） | 仅 `validate_skill.py` 需要 Python |

**不承诺所有平台发现规则相同**；若你的平台需要特定路径或打包，整目录复制即可满足。

## 七、边界（不可突破）

- 不预测市场、不荐股、不承诺收益或胜率；
- 不执行交易、不连接证券账户、未授权不保存持仓；
- 不建议融资、配资、借钱炒股；
- 缺失数据 = 待核实，**永不等于通过**；
- 用户要求绕过纪律时：指出偏离项与后果，不偷偷改规则。

本 Skill 为投资方法与教育工具，不构成证券投资建议，不推荐具体证券，不承诺收益。投资有风险。
