> **用途**：统一采集结果的数据结构、时间口径、时效状态、去重与缓存规则。
> **使用规则**：任何采集结果进入分析前，先按本schema 校验（`scripts/validate_data.py`）；字段缺失的条目降级为「待核实」。

# 数据 Schema（统一信息格式）

## 1. 标准信息条目

所有来源（行情、公告、新闻、宏观）统一为以下结构（JSON）：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `title` | 是 | string | 信息标题（保留来源原文用词，不改写数字） |
| `content_summary` | 是 | string | 事实摘要（2-4 句，只写可验证内容，不加推断） |
| `source_name` | 是 | string | 来源名称（例：巨潮资讯网） |
| `source_url` | 是 | string | 原始链接（禁止编造；无链接写 `null` 并说明） |
| `source_tier` | 是 | enum | `L1官方原始` / `L2专业数据` / `L3可信媒体` / `L4非正式` |
| `published_at` | 是 | ISO8601 | 原始发布时间（带时区；无则 `null` + 备注） |
| `retrieved_at` | 是 | ISO8601 | 本次获取时间 |
| `effective_at` | 否 | ISO8601 | 事件生效时间（如政策施行日） |
| `market` | 是 | enum | `cn` / `hk` / `us` / `global` |
| `asset_type` | 是 | enum | `equity` / `index` / `industry_index` / `fx` / `commodity` / `bond` / `macro` / `policy` |
| `symbols` | 是 | string[] | 关联代码（可空数组） |
| `event_type` | 是 | enum | `announcement` / `report` / `earnings` / `policy` / `quote` / `news` / `macro` / `risk` |
| `fact_status` | 是 | enum | `verified`（官方原始）/ `cross_checked`（≥2 独立源一致）/ `single_source`（仅一处）/ `unverified`（未核验）/ `disputed`（存在争议）/ `corrected`（已被更正） |
| `freshness_status` | 是 | enum | `realtime` / `delayed` / `latest` / `after_hours` / `historical` / `unknown` |
| `freshness_note` | 是 | string | 时效依据（如「实测延迟 42 秒」「午休时段，数据时间 11:30」） |
| `data_time` | 是 | ISO8601 | 数据对应的时间点（行情=成交时间，公告=披露时间，宏观=统计期） |
| `impact_hypothesis` | 否 | string | 可能的投资影响假设（**必须放在假设栏，不得写进事实**） |
| `confidence` | 是 | object | `{level: high/medium/low, basis: string}` |
| `dedup_key` | 是 | string | 去重标识（见 §4） |

## 2. 行情数据附加字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `price` | 是 | 最新价 |
| `prev_close` / `open` / `high` / `low` | 否 | 昨收/今开/最高/最低 |
| `change` / `change_pct` | 否 | 涨跌额 / 涨跌幅（%） |
| `volume_shares` / `amount` / `amount_unit` | 否 | 成交量（股）/ 成交额 + 币种 |
| `currency` | 是 | CNY / HKD / USD |
| `exchange_timezone` | 是 | Asia/Shanghai / Asia/Hong_Kong / America/New_York |
| `session_state` | 是 | `pre_market` / `trading` / `lunch_break` / `after_hours` / `closed` |
| `trading_day_verified` | 是 | 是否用交易所数据核对过当日为交易日 |
| `adjust` | 否 | 复权口径：`不复权` / `前复权` / `后复权`（历史数据必填） |
| `price_type` | 是 | `latest_trade` / `close` / `pre_market` / `post_market` |

**禁止**：把不同币种、不同时区、不同复权口径、不同价格类型的价格直接比较或拼接。

## 3. 宏观数据附加字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `indicator` / `indicator_name` | 是 | 指标代码与名称 |
| `period` | 是 | 统计周期（如「2026年08月份」） |
| `value` / `unit` | 是 | 数值与单位（%、点、指数值） |
| `seasonal_adjustment` | 是 | 季调口径（未知写「未标注」） |
| `revision_status` | 是 | 是否修订（未知写「未标注」） |
| `derived_*` | 否 | 本脚本计算的派生值，必须带 `derived_note` 说明算法 |

**禁止**：同比/环比/累计混用比较；把指数值与百分比混为一谈。

## 4. 去重与增量

`dedup_key` 生成优先级：

1. 官方公告：`ann:<announcement_id>`（巨潮/EDGAR 的唯一编号）
2. 有原文链接：`url:<去掉锚点与末尾斜杠的链接>`
3. 其他：`title:<去除空白与标点的标题>:<发布日期>`

规则：

- 相同 `dedup_key` 只保留最新一条；同一事件的**后续进展**（新编号/新链接）是新条目，必须保留并标注 `is_update: true`。
- 「旧闻重发」判定：标题几乎相同、内容一致、但 `published_at` 明显晚于事件本身 → 在 `freshness_note` 标注「旧闻重发，事件时间 X」。

## 5. 缓存策略（`market_data.py` 已实现）

| 数据类型 | 默认 TTL | 理由 |
|---|---|---|
| 行情 | 10 秒 | 盘中变化快，缓存只为去抖 |
| 行情交叉验证 | 10 秒 | 同上 |
| 新闻/政策 | 180 秒 | 兼顾发现速度与重复请求 |
| 公告/申报 | 1800 秒 | 披露低频，但重大公告不希望漏 |
| 宏观数据 | 86400 秒 | 日级更新 |
| 历史 K 线 | 86400 秒 | 日级更新 |
| 交易日历 | 86400 秒 | 变更极少 |

- 缓存文件携带 `cached_at / retrieved_at / data_time`；命中缓存时输出**原始数据时间**，并标注 `from_cache` 与 `cache_age_seconds`。
- **例外**：重大风险事件、正式更正公告、退市/停牌/ST 类公告，采集时用 `--no-cache` 强制重新请求。
- 过期数据不得伪装成最新数据：超过 TTL 的条目在报告中标注其 `data_time` 与当前时间差。

## 6. 校验脚本

```bash
python3 skills/investment-discipline/scripts/validate_data.py --input items.json
python3 skills/investment-discipline/scripts/validate_data.py --stdin
```

校验项：必填字段齐备、时间可解析且带时区、枚举值合法、`retrieved_at >= published_at`、`dedup_key` 可生成、行情字段自洽（high ≥ low，price 在区间内或标注盘前盘后）、宏观口径字段不缺失、来源层级与 `fact_status` 匹配（非 L1 不得为 `verified`）。

退出码：0 = 通过；1 = 有 error 级问题（禁止进入分析）；2 = 仅 warning（可进入分析但需标注）。
