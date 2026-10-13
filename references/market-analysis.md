> **用途**：把「采集 → 校验 → 分析 → 规则检查 → 行动建议 → 复盘」做成可执行闭环，供市场日报、个股数据核对与纪律检查使用（SKILL.md 模式 H）。
> **使用规则**：先选源（`data-sources.md`）→ 用脚本采集 → 过格式与时效校验（`data-schema.md`、`validate_data.py`）→ 按来源层级整理事实 → 需要纪律判断时跑 `validate_rules.py` → 按 `report-template.md` 输出。
> **红线**：行情数据本身不产生买卖结论；没有授权实时源时不得自称"实时"。

# 实时市场信息的采集与分析（Market Analysis · 模式 H）

## 1. 何时用本模式

触发词：市场日报 / 今日行情 / 这只股票最近有什么公告或新闻 / 帮我核对这条行情 / 宏观数据怎么样 / 采集数据做研究。

与其它模式的关系：本模式负责**把数据取准、标清时效**，是模式 A–D 的输入层；纪律结论仍由 `validate_rules.py` 与 `rules-index.md` 给出。

## 2. 六步闭环

1. **界定范围**：标的（代码或名称）、市场（cn/hk/us）、时间窗、用途（日报 / 研究 / 纪律检查）。
2. **选源**：查 `data-sources.md` 层级与降级链；L1 优先用于事实，L2 用于行情，L3 只作线索。
3. **采集**：用 `market_data.py` 对应子命令；失败按 `error-handling.md` 降级并留痕。
4. **校验**：用 `validate_data.py` 过格式/时效/去重；未通过的条目不进入分析。
5. **分析**：事实与推断分列；跨币种/时区/复权口径不直接比较；媒体观点进"来源观点"。
6. **规则与输出**：涉及买/卖/仓位时跑`validate_rules.py`；按 `report-template.md` 输出，附缺口与免责声明。

## 3. 采集命令速查

设 `MD=skills/investment-discipline/scripts/market_data.py`：

| 目的 | 命令 |
|---|---|
| 数据源连通性自检 | `python3 $MD sources --probe` |
| 行情（逗号分隔多标的） | `python3 $MD quote 600519,sh000001,00700,AAPL` |
| 多源交叉验证 | `python3 $MD verify-quote 600519` |
| 公告/申报 | `python3 $MD filings --symbol 600519 --limit 10` |
| 新闻线索 | `python3 $MD news --query 贵州茅台 --limit 10` |
| 政策类信息 | `python3 $MD policy --query 货币 政策 --limit 10` |
| 宏观数据 | `python3 $MD macro --indicator cpi --limit 6` |
| 历史 K 线 | `python3 $MD history --symbol 600519 --interval 101` |
| 交易日历核验 | `python3 $MD calendar --market cn --verify` |
| 一键数据集（行情+公告+新闻） | `python3 $MD research --symbol 600519` |

常用开关（挂在子命令后）：`--no-cache`（强刷）、`--md`（Markdown 摘要）、`--offline`（只用缓存，不发请求）。

## 4. 校验

```
python3 skills/investment-discipline/scripts/validate_data.py --input items.json
python3 skills/investment-discipline/scripts/validate_data.py --stdin
```

校验项：必填字段、时效枚举、`realtime` 与延迟自洽、"实时"话术与状态一致、时间戳不超前、行情新鲜度（>7 天判过期）、价格为正、去重。退出码 `0/1/2` 见 `error-handling.md`。

## 5. 分析纪律

- **事实 / 来源观点 / 我的推断** 三栏严格分开；推断必须写假设。
- **不可比**：不同 `currency`、`exchange_timezone`、`adjust`（复权）、`price_type`（latest/close/pre/post）的价格不得直接比较。
- **宏观口径**：同比 / 环比 / 累计不混用；指数值与百分比不混为一谈；标注季调与是否修订。
- **时效**：每条信息带 `freshness_status`；无授权实时源时统一写「最新可用行情（数据时间 X，实测延迟 Y 秒）」。
- **媒体线索**默认 `fact_status=unverified`，回官方原始来源交叉核验后才可升格为事实。
- **旧闻重发**、更正公告要单独标注。

## 6. 与规则引擎衔接

- 需要判断买 / 卖 / 仓位 / 再平衡时，把整理好的结构化输入交给 `validate_rules.py`，输出固定八列结论。
- 引擎阈值以 `rules-index.json` 为唯一事实来源，启动即比对，不一致则拒绝执行。
- 遇规则冲突（C-01/C-02/C-03 等）输出「规则冲突，等待确认」，**不自行裁决**。

## 7. 输出

按 `report-template.md`。市场日报/个股研究必须包含：来源与层级表、逐条时效、事实与推断分列、缺失数据栏、免责声明。

## 8. 复盘

每次输出后记录：数据缺口、降级路径、被弃条目及原因、用户后续补充了什么。用于改进选源顺序与校验规则（把高频缺口固化为脚本校验项），但**不得因此放宽纪律阈值**。

## 9. 常见错误（必须避免）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 把延迟数据写成"实时" | 误导判断 | 如实标 `delayed/latest` |
| 用行情涨跌直接下买卖结论 | 绕过清单与纪律 | 走 `validate_rules.py` 与清单 |
| 缺数据时用估算/同类值填充 | 造虚假事实 | 记「待核实」，列缺口 |
| 直接比较不同币种/复权价 | 得出错误结论 | 分行列出各自口径 |
| 把媒体目标价当事实 | 传播未核验观点 | 进"来源观点"，回原始来源核验 |
| 静默换源不说明 | 时效/口径被掩盖 | 记录降级路径与新源时效 |

---

> 本文件为数据采集与流程说明，不构成投资建议，不推荐具体证券，不承诺收益。
