#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_data.py — 《投资的纪律》Skill 统一市场数据适配器（纯标准库，零第三方依赖）

把不同数据源封装成可替换的适配器，输出统一信息格式（见 references/data-schema.md），
每条数据都带：来源、原始链接、数据时间、获取时间、时效状态、核实状态。

命令
----
  sources        列出已注册数据源（--probe 做一次连通性探测）
  quote          取最新可用行情（可多标的、可指定 provider）
  verify-quote   多源交叉验证同一标的行情（价格一致性 + 各源实测延迟）
  news           搜索新闻/事件（--tier official|media|all）
  filings        取上市公司正式公告/备案（cn=巨潮资讯网，us=SEC EDGAR）
  macro          取宏观经济数据（cpi/ppi/pmi/gdp/us_cpi/treasury）
  policy         按政策关键词抓取政策类信息（官方源优先）
  history        取历史行情（日/周/月，前复权/后复权/不复权）
  calendar       交易日历与交易时段（--verify 用最近K线核对当日是否交易日）
  research       最小闭环：行情 + 公告 + 新闻 打包成一个研究数据集

通用参数
--------
  --json         输出 JSON（默认）
  --md           输出 Markdown 摘要
  --no-cache     跳过缓存直接请求
  --offline      只用缓存，不发网络请求（缓存缺失则报错）
  --timeout N    单请求超时秒（默认 8）
  --retries N    失败重试次数（默认 1，总尝试 2 次）

环境变量（只写变量名，不写密钥）
--------------------------------
  INVESTMENT_DISCIPLINE_CACHE      缓存目录（默认 ~/.cache/investment-discipline）
  INVESTMENT_DISCIPLINE_TIMEOUT    默认超时秒
  INVESTMENT_DISCIPLINE_UA         自定义 User-Agent
  EDGAR_USER_AGENT                 访问 SEC EDGAR 的 UA（须含联系方式，SEC 要求）

时效状态（freshness_status）
---------------------------
  realtime      交易时段内、实测延迟 <= 60 秒
  delayed       交易时段内、实测延迟 > 60 秒（附实测延迟秒数）
  latest        非交易时段（午休/盘后/收盘）取得的当日最新数据
  after_hours   盘前/盘后数据
  historical    历史数据（非最近交易日）
  unknown       无法确认时效

原则
----
  数据取不到就报错并降级，绝不用猜测值、随机值或模型记忆填补实时数据缺口。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent

# ============================================================ 配置 / 缓存

DEFAULT_TIMEOUT = float(os.environ.get("INVESTMENT_DISCIPLINE_TIMEOUT", "8"))
DEFAULT_RETRIES = 1
USER_AGENT = os.environ.get(
    "INVESTMENT_DISCIPLINE_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
SEC_UA = os.environ.get(
    "EDGAR_USER_AGENT",
    "investment-discipline-skill/1.0 (research tool; set EDGAR_USER_AGENT env with contact)")

# 每类数据的缓存有效期（秒）。行情短、公告/宏观长；缓存必须携带原始数据时间。
CACHE_TTL = {
    "quote": int(os.environ.get("INVESTMENT_DISCIPLINE_TTL_QUOTE", "10")),
    "verify_quote": 10,
    "news": int(os.environ.get("INVESTMENT_DISCIPLINE_TTL_NEWS", "180")),
    "filings": 1800,
    "macro": 86400,
    "history": 86400,
    "calendar": 86400,
    "meta": 86400,
    "research": 60,
}


class DataSourceError(RuntimeError):
    """数据源不可用（网络、权限、限流、解析失败）。message 内含来源与原因。"""


def cache_dir() -> Path:
    p = os.environ.get("INVESTMENT_DISCIPLINE_CACHE")
    base = Path(p).expanduser() if p else Path.home() / ".cache" / "investment-discipline"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(os.environ.get("TMPDIR", "/tmp")) / "investment-discipline-cache"
        base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_file(kind: str, key: str) -> Path:
    h = hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()[:24]
    return cache_dir() / f"{kind}-{h}.json"


def cache_read(kind: str, key: str, ttl: int, offline: bool = False) -> dict | None:
    f = _cache_file(kind, key)
    if not f.exists():
        if offline:
            raise DataSourceError(f"[cache] 离线模式且无缓存：{kind}/{key}")
        return None
    try:
        wrap = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    age = time.time() - wrap.get("cached_at", 0)
    if age > ttl:
        if offline:
            return wrap  # 离线：过期也用，并在输出里标注 stale
        return None
    wrap["cache_hit"] = True
    wrap["cache_age_seconds"] = int(age)
    return wrap


def cache_write(kind: str, key: str, payload: Any, data_time: str | None) -> None:
    wrap = {
        "cached_at": time.time(),
        "retrieved_at": _iso(dt.datetime.now(dt.timezone.utc)),
        "data_time": data_time,
        "payload": payload,
    }
    try:
        _cache_file(kind, key).write_text(
            json.dumps(wrap, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _iso(d: dt.datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ============================================================ 网络层

_CTX = ssl.create_default_context()


def http_get(url: str, headers: dict | None = None, timeout: float | None = None,
             retries: int = DEFAULT_RETRIES, encoding: str = "utf-8") -> str:
    """GET 一个 URL，返回解码后的文本。失败抛 DataSourceError（含来源与原因）。"""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    hdrs.update(headers or {})
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                for enc in (encoding, "utf-8", "gbk", "latin-1"):
                    try:
                        return raw.decode(enc)
                    except (UnicodeDecodeError, LookupError):
                        continue
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            if e.code in (403, 404, 429) or 400 <= e.code < 500:
                break  # 4xx 不重试
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.4 * (attempt + 1))
    raise DataSourceError(f"[{urllib.parse.urlparse(url).netloc}] {last_err} ({url[:120]})")


def http_post(url: str, data: dict, headers: dict | None = None,
              timeout: float | None = None, retries: int = DEFAULT_RETRIES) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    hdrs.update(headers or {})
    body = urllib.parse.urlencode(data).encode("utf-8")
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                raw = resp.read()
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("gbk", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.4 * (attempt + 1))
    raise DataSourceError(f"[{urllib.parse.urlparse(url).netloc}] {last_err} ({url[:120]})")


# ============================================================ 数据源注册表
# status: ok（实测连通）/ untested（未实测）/ disabled（已停用）
SOURCES: list[dict] = [
    dict(id="sina_hq", name="新浪财经行情接口", official="https://finance.sina.com.cn",
         markets=["cn", "hk", "us", "fx", "commodity"], types=["quote"],
         latency="盘中推送（第三方源，未公开授权延迟口径）", auth="无（需 Referer 头）",
         quota="未公开，单次可批量", timeout_s=8, fallback="tencent_hq",
         status="ok", verified_at="2026-10-13",
         notes="A股/港股/美股/外汇/期货行情，返回自带日期时间字段，可实测延迟"),
    dict(id="tencent_hq", name="腾讯行情接口", official="https://stockapp.finance.qq.com",
         markets=["cn", "hk", "us"], types=["quote"],
         latency="盘中推送（第三方源，未公开授权延迟口径）", auth="无",
         quota="未公开，单次可批量", timeout_s=8, fallback="eastmoney_quote",
         status="ok", verified_at="2026-10-13",
         notes="用于行情交叉验证；返回 14 位时间戳，可实测延迟"),
    dict(id="eastmoney_quote", name="东方财富行情接口", official="https://www.eastmoney.com",
         markets=["cn", "hk", "us"], types=["quote", "index", "industry", "breadth", "history"],
         latency="push2 为盘中；push2delay 为延迟接口", auth="无",
         quota="未公开", timeout_s=8, fallback="sina_hq",
         status="ok", verified_at="2026-10-13",
         notes="指数/行业板块/涨跌家数/资金流/历史K线，字段口径见 f 系列编号"),
    dict(id="cninfo", name="巨潮资讯网（法定披露平台）", official="http://www.cninfo.com.cn",
         markets=["cn"], types=["filings", "announcement"],
         latency="交易所披露后上传", auth="无", quota="未公开，建议 ≤1 req/s",
         timeout_s=10, fallback="eastmoney_notice",
         status="ok", verified_at="2026-10-13",
         notes="沪深北上市公司法定公告披露平台，PDF 直链为官方静态域名"),
    dict(id="eastmoney_notice", name="东方财富公告接口", official="https://data.eastmoney.com/notices/",
         markets=["cn"], types=["filings", "announcement"],
         latency="聚合披露平台，可能晚于交易所", auth="无", quota="未公开",
         timeout_s=8, fallback="cninfo", status="ok", verified_at="2026-10-13",
         notes="作为巨潮的备用公告源；引用时应标注为聚合源"),
    dict(id="eastmoney_search", name="东方财富资讯搜索", official="https://so.eastmoney.com",
         markets=["cn", "hk", "us"], types=["news", "policy"],
         latency="聚合媒体资讯", auth="无", quota="未公开，建议 ≤1 req/s",
         timeout_s=10, fallback="agent_web_search",
         status="ok", verified_at="2026-10-13",
         notes="可按时间排序、按关键词检索；含媒体名与原文链接，用于发现线索与交叉验证"),
    dict(id="eastmoney_datacenter", name="东方财富数据中心（宏观）", official="https://data.eastmoney.com/cjsj/",
         markets=["cn"], types=["macro"],
         latency="官方统计发布后聚合", auth="无", quota="未公开", timeout_s=10,
         fallback="stats_gov", status="ok", verified_at="2026-10-13",
         notes="CPI/PPI/PMI/GDP 等；字段含报告期与发布口径，修订状态需回原始统计公告核对"),
    dict(id="bls", name="美国劳工统计局 BLS 公开 API", official="https://www.bls.gov/",
         markets=["us"], types=["macro"],
         latency="官方发布", auth="无（v2 限流：10 series/请求）", quota="每日 25 requests（无 key）",
         timeout_s=12, fallback="eastmoney_search", status="ok", verified_at="2026-10-13",
         notes="CUUR* = 未季调 CPI 指数；修订状态以 BLS 公告为准"),
    dict(id="us_treasury", name="美国财政部 每日收益率曲线", official="https://home.treasury.gov/",
         markets=["us"], types=["macro", "rate"],
         latency="每交易日发布", auth="无", quota="未公开", timeout_s=12,
         fallback="eastmoney_search", status="ok", verified_at="2026-10-13",
         notes="官方 CSV，含 2Y/10Y/30Y 等期限，单位为百分比"),
    dict(id="sec_edgar", name="SEC EDGAR（美国上市公司申报）", official="https://www.sec.gov/edgar",
         markets=["us"], types=["filings"],
         latency="申报即公开", auth="无（要求自定义 UA 含联系方式）",
         quota="≤10 req/s", timeout_s=12, fallback="issuer_ir_page",
         status="ok", verified_at="2026-10-13",
         notes="10-K/10-Q/8-K/Form4 等；XBRL companyfacts 可取结构化财务数据"),
    dict(id="sse_szse", name="沪深交易所官网", official="https://www.sse.com.cn",
         markets=["cn"], types=["filings", "index", "calendar"],
         latency="官方", auth="无", quota="未公开，禁止高频抓取",
         timeout_s=10, fallback="cninfo", status="untested", verified_at="",
         notes="交易日历与官方公告的最终依据；当前适配器未直连接口，公告走巨潮（同源法定披露）"),
    dict(id="agent_web_search", name="Agent 自带联网搜索/网页读取工具", official="（运行环境提供）",
         markets=["global"], types=["news", "policy", "background"],
         latency="取决于引擎", auth="取决于运行环境", quota="取决于运行环境",
         timeout_s=20, fallback="eastmoney_search", status="untested", verified_at="",
         notes="由 Skill 运行时按需调用；本脚本不依赖它，但可用它做第二来源交叉验证"),
]

SOURCE_BY_ID = {s["id"]: s for s in SOURCES}


# ============================================================ 标的代码规范化

MARKET_TZ = {
    "cn": "Asia/Shanghai", "hk": "Asia/Hong_Kong", "us": "America/New_York",
    "fx": "UTC", "commodity": "UTC",
}
CURRENCY = {"cn": "CNY", "hk": "HKD", "us": "USD"}


def normalize_symbol(raw: str) -> dict:
    """
    返回 {symbol, market, kind, sina, tencent, em_secid, currency}
    规则：裸 6 位数字 = A股个股；指数必须带 sh/sz 前缀（避免 000001 歧义）。
    """
    s = raw.strip().upper()
    out: dict[str, Any] = {"input": raw.strip(), "market": "", "kind": "",
                           "symbol": "", "sina": "", "tencent": "", "em_secid": "",
                           "currency": "", "warning": ""}
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)
    if m:
        p, code = m.group(1).lower(), m.group(2)
        market = "cn"
        kind = "index" if (p in ("sh", "sz") and code.startswith(("000", "399"))) else "stock"
        out.update(market=market, kind=kind, symbol=code,
                   sina=f"{p}{code}", tencent=f"{p}{code}",
                   em_secid=f"{1 if p=='sh' else 0}.{code}" if p != "bj" else f"0.{code}",
                   currency="CNY")
        return out
    if re.fullmatch(r"\d{6}", s):
        p = "sh" if s[0] in "69" else ("sz" if s[0] in "03" else "bj")
        out.update(market="cn", kind="stock", symbol=s,
                   sina=f"{p}{s}", tencent=f"{p}{s}",
                   em_secid=("1." if p == "sh" else "0.") + s, currency="CNY")
        if s.startswith("000"):
            out["warning"] = ("6位数字 '000xxx' 可能是深市个股也可能是上证指数；"
                              "如需指数请写 sz000xxx / sh000001")
        return out
    if re.fullmatch(r"\d{5}", s):
        out.update(market="hk", kind="stock", symbol=s,
                   sina=f"hk{s}", tencent=f"hk{s}", em_secid=f"116.{s}", currency="HKD")
        return out
    if re.fullmatch(r"HK\d{5}", s):
        code = s[2:]
        out.update(market="hk", kind="stock", symbol=code,
                   sina=f"hk{code}", tencent=f"hk{code}", em_secid=f"116.{code}", currency="HKD")
        return out
    # 主要指数别名（港股 / 美股），A股指数必须写 sh000001 / sz399001
    IDX = {
        "HSI": ("hk", "恒生指数", "hkHSI", "", "100.HSI", "HKD"),
        "HSCEI": ("hk", "恒生国企指数", "hkHSCEI", "", "100.HSCEI", "HKD"),
        "HSTECH": ("hk", "恒生科技指数", "hkHSTECH", "", "100.HSTECH", "HKD"),
        "SPX": ("us", "标普500", "", "", "100.SPX", "USD"),
        "IXIC": ("us", "纳斯达克综合", "", "", "100.NDX", "USD"),
        "NDX": ("us", "纳斯达克100", "", "", "100.NDX", "USD"),
        "DJIA": ("us", "道琼斯工业", "", "", "100.DJIA", "USD"),
        "DJI": ("us", "道琼斯工业", "", "", "100.DJIA", "USD"),
        "N225": ("us", "日经225", "", "", "100.N225", "USD"),
    }
    if s in IDX:
        mk, name, sina, tencent, secid, cur = IDX[s]
        out.update(market=mk, kind="index", symbol=s, name_zh=name,
                   sina=sina, tencent=tencent, em_secid=secid, currency=cur)
        return out
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
        out.update(market="us", kind="stock", symbol=s,
                   sina=f"gb_{s.lower()}", tencent=f"us{s}",
                   em_secid=f"105.{s}", currency="USD")
        return out
    raise DataSourceError(f"无法识别的标的代码：{raw!r}（支持 6位A股 / sh|sz+6位指数 / 5位港股 / 美股代码）")


# ============================================================ 交易时段与时效

SESSIONS: dict[str, dict] = {
    # market -> {"tz":, "windows": [(start,end,标签)], "pre":, "post":}
    "cn": {"tz": "Asia/Shanghai",
           "windows": [("09:30", "11:30", "上午连续竞价"), ("13:00", "15:00", "下午连续竞价")],
           "pre": ("09:15", "09:25"), "post": None, "lunch": ("11:30", "13:00")},
    "hk": {"tz": "Asia/Hong_Kong",
           "windows": [("09:30", "12:00", "上午交易"), ("13:00", "16:00", "下午交易")],
           "pre": ("09:00", "09:30"), "post": ("16:00", "16:08"), "lunch": ("12:00", "13:00")},
    "us": {"tz": "America/New_York",
           "windows": [("09:30", "16:00", "常规交易时段")],
           "pre": ("04:00", "09:30"), "post": ("16:00", "20:00"), "lunch": None},
}


def tz_of(market: str):
    name = MARKET_TZ.get(market, "UTC")
    if ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return dt.timezone.utc


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def session_state(market: str, at: dt.datetime | None = None) -> dict:
    """
    返回 {state, market, local_time, date, is_weekend, tz}
    state ∈ pre_market / trading / lunch_break / after_hours / closed / unknown
    注意：不含节假日表，非交易日判断需 calendar --verify。
    """
    sess = SESSIONS.get(market)
    if not sess:
        return {"state": "unknown", "market": market, "note": "该市场未配置交易时段表"}
    tz = tz_of(market)
    at = (at or _now()).astimezone(tz)
    label = at.strftime("%A")
    weekend = at.weekday() >= 5
    t = at.time()

    def between(win):
        if not win:
            return False
        return _hm(win[0]) <= t < _hm(win[1])

    if any(between(w) for w in sess["windows"]):
        state = "trading"
    elif sess.get("lunch") and between(sess["lunch"]):
        state = "lunch_break"
    elif sess.get("pre") and between(sess["pre"]):
        state = "pre_market"
    elif sess.get("post") and between(sess["post"]):
        state = "after_hours"
    else:
        state = "closed"
    if weekend and state not in ("pre_market", "after_hours"):
        state = "closed"
    return {"state": state, "market": market, "tz": sess["tz"],
            "local_date": at.strftime("%Y-%m-%d"), "local_time": at.strftime("%H:%M:%S"),
            "weekday": label, "is_weekend": weekend,
            "holiday_checked": False,
            "note": "节假日未内置，需 calendar --verify 用交易所数据核对"}


def _expected_last_session_date(market: str, at: dt.datetime) -> dt.date:
    """
    估计『最近一个已开盘的交易日』（不含节假日表）：
    - 当日已进入开盘时段（trading/lunch/after_hours/盘后）→ 今天
    - 否则 → 上一个工作日（周一至周五；节假日未覆盖，需 calendar --verify 核对）
    """
    tz = tz_of(market)
    loc = at.astimezone(tz)
    st = session_state(market, at)["state"]
    if st in ("trading", "lunch_break", "after_hours"):
        return loc.date()
    # 若当日时段尚未开始（例如美股纽约时间凌晨），最近交易日为上一工作日
    sess = SESSIONS.get(market, {})
    starts = [_hm(w[0]) for w in sess.get("windows", [])]
    earliest = min(starts) if starts else dt.time(9, 30)
    d = loc.date()
    if loc.time() < earliest and d.weekday() < 5:
        d -= dt.timedelta(days=1)
    for _ in range(8):
        if d.weekday() < 5:
            return d
        d -= dt.timedelta(days=1)
    return loc.date()


def freshness(as_of: str | None, market: str, at: dt.datetime | None = None,
              trading_day_verified: bool = False) -> dict:
    """
    根据数据时间 + 交易时段判定时效状态。
    返回 {freshness_status, delay_seconds, session_state, trading_day_verified, note}
    """
    if not as_of:
        return {"freshness_status": "unknown", "delay_seconds": None,
                "session_state": "unknown", "trading_day_verified": trading_day_verified,
                "note": "来源未提供数据时间"}
    try:
        t = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return {"freshness_status": "unknown", "delay_seconds": None,
                "session_state": "unknown", "trading_day_verified": trading_day_verified,
                "note": f"数据时间无法解析：{as_of}"}
    if t.tzinfo is None:
        t = t.replace(tzinfo=tz_of(market))
    now = (at or _now())
    sess = session_state(market, now)
    tz = tz_of(market)
    t_loc = t.astimezone(tz)
    delay = int((now.astimezone(tz) - t_loc).total_seconds())
    st = sess["state"]
    expected_last = _expected_last_session_date(market, now)

    if st == "trading":
        status = "realtime" if 0 <= delay <= 60 else "delayed"
        note = f"交易时段内，实测延迟 {delay} 秒"
    elif st in ("lunch_break", "after_hours", "pre_market"):
        if t_loc.date() == expected_last:
            status = "after_hours" if st in ("pre_market", "after_hours") else "latest"
            note = (f"{st} 时段，来源数据时间 "
                    f"{t_loc.strftime('%H:%M:%S')}（当日最新可用）")
        else:
            status = "historical"
            note = "当前非交易时段，且数据非最近交易日"
    else:  # closed
        if t_loc.date() == expected_last:
            status = "latest"
            note = "非交易时段，取到最近交易日最新数据（收盘口径）"
        else:
            status = "historical"
            note = (f"非交易时段，数据日期 {t_loc.date()} 早于"
                    f"最近交易日 {expected_last}")
    if status in ("latest", "historical") and not trading_day_verified:
        note += "；节假日未核验（可用 calendar --verify）"
    return {"freshness_status": status, "delay_seconds": delay,
            "session_state": st, "session_date": sess.get("local_date"),
            "as_of_date": t_loc.strftime("%Y-%m-%d"),
            "trading_day_verified": trading_day_verified, "note": note}


# ============================================================ 行情适配器

def _f(x: Any) -> float | None:
    try:
        v = float(str(x).replace(",", ""))
        return v
    except (TypeError, ValueError):
        return None


def sina_quote(symbols: list[str]) -> dict:
    """新浪财经行情。返回 {provider, items[], errors[]}"""
    ns = [normalize_symbol(s) for s in symbols]
    usable = [n for n in ns if n["sina"]]
    errors = [{"symbol": n["input"], "source": "sina_hq",
               "error": "该数据源不支持此标的类型（如美股指数请用 eastmoney）"}
              for n in ns if not n["sina"]]
    if not usable:
        return {"provider": "sina_hq", "items": [], "errors": errors}
    codes = ",".join(n["sina"] for n in usable)
    url = "https://hq.sinajs.cn/list=" + codes
    txt = http_get(url, headers={"Referer": "https://finance.sina.com.cn"}, encoding="gbk")
    items: dict[str, dict] = {}
    for line in txt.strip().splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        code, payload = m.group(1), m.group(2)
        if not payload:
            continue
        f = payload.split(",")
        n = next((x for x in usable if x["sina"] == code), None)
        if not n:
            continue
        try:
            items[n["input"]] = _parse_sina(n, f)
        except Exception as e:  # noqa: BLE001
            items[n["input"]] = {"error": f"sina 解析失败：{e}"}
    out_items = []
    for n in usable:
        it = items.get(n["input"])
        if not it:
            errors.append({"symbol": n["input"], "source": "sina_hq",
                           "error": "无返回数据（可能代码不存在或被限流）"})
            continue
        if "error" in it:
            errors.append({"symbol": n["input"], "source": "sina_hq", "error": it["error"]})
            continue
        it.update(symbol=n["symbol"], market=n["market"], currency=n["currency"],
                  source="sina_hq", source_name="新浪财经行情接口")
        out_items.append(it)
    return {"provider": "sina_hq", "items": out_items, "errors": errors}


def _parse_sina(n: dict, f: list[str]) -> dict:
    mk, kind = n["market"], n["kind"]
    if mk == "cn":
        date, tm = f[30], f[31]
        as_of = f"{date}T{tm}+08:00"
        price, prev = _f(f[3]), _f(f[2])
        return {"name": f[0], "price": price, "prev_close": prev, "open": _f(f[1]),
                "high": _f(f[4]), "low": _f(f[5]),
                "change": round(price - prev, 4) if price and prev else None,
                "change_pct": round((price - prev) / prev * 100, 3)
                if price and prev else None,
                "volume_shares": _f(f[8]), "amount": _f(f[9]), "amount_unit": "CNY",
                "as_of": as_of, "price_type": "latest_trade"}
    if mk == "hk":
        date, tm = f[17], f[18]
        as_of = f"{date.replace('/', '-')}T{tm}:00+08:00"
        price, prev = _f(f[6]), _f(f[3])
        return {"name": f[1], "price": price, "prev_close": prev, "open": _f(f[2]),
                "high": _f(f[4]), "low": _f(f[5]),
                "change": _f(f[7]), "change_pct": _f(f[8]),
                "volume_shares": _f(f[12]), "amount": _f(f[11]), "amount_unit": "HKD",
                "as_of": as_of, "price_type": "latest_trade"}
    if mk == "us":
        as_of_raw = f[3]  # 例 2026-10-13 08:14:44（实测为北京时间，见 tz_note）
        as_of = None
        if len(as_of_raw) >= 19:
            naive = dt.datetime.strptime(as_of_raw[:19], "%Y-%m-%d %H:%M:%S")
            as_of = naive.replace(tzinfo=tz_of("cn")).isoformat(timespec="seconds")
        price, prev = _f(f[1]), (_f(f[35]) if len(f) > 35 else None)
        return {"name": f[0], "price": price, "prev_close": prev,
                "open": _f(f[6]) if len(f) > 6 else None,
                "high": _f(f[7]), "low": _f(f[8]),
                "change": _f(f[4]), "change_pct": _f(f[2]),
                "volume_shares": _f(f[11]), "amount": _f(f[30]) if len(f) > 30 else None,
                "amount_unit": "USD",
                "as_of": as_of, "price_type": "latest_trade",
                "tz_note": "新浪美股接口的时间字段实测为北京时间（UTC+8），已按此解析；"
                           "该行情为延迟行情，具体延迟未公开"}
    raise DataSourceError(f"sina 未支持的市场：{mk}")


def tencent_quote(symbols: list[str]) -> dict:
    ns = [normalize_symbol(s) for s in symbols]
    usable = [n for n in ns if n["tencent"]]
    errors = [{"symbol": n["input"], "source": "tencent_hq",
               "error": "该数据源不支持此标的类型"} for n in ns if not n["tencent"]]
    if not usable:
        return {"provider": "tencent_hq", "items": [], "errors": errors}
    codes = ",".join(n["tencent"] for n in usable)
    txt = http_get(f"https://qt.gtimg.cn/q={codes}", encoding="gbk")
    out_items, found = [], {}
    for line in txt.strip().splitlines():
        m = re.match(r'v_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        code, payload = m.group(1), m.group(2)
        if not payload or payload.startswith("pv_none"):
            continue
        f = payload.split("~")
        n = next((x for x in usable if x["tencent"] == code), None)
        if not n:
            continue
        try:
            found[n["input"]] = _parse_tencent(n, f)
        except Exception as e:  # noqa: BLE001
            errors.append({"symbol": n["input"], "source": "tencent_hq",
                           "error": f"解析失败：{e}"})
    for n in usable:
        it = found.get(n["input"])
        if not it:
            errors.append({"symbol": n["input"], "source": "tencent_hq", "error": "无返回数据"})
            continue
        it.update(symbol=n["symbol"], market=n["market"], currency=n["currency"],
                  source="tencent_hq", source_name="腾讯行情接口")
        out_items.append(it)
    return {"provider": "tencent_hq", "items": out_items, "errors": errors}


def _parse_tencent(n: dict, f: list[str]) -> dict:
    ts_idx = None
    for i, v in enumerate(f):
        if re.fullmatch(r"\d{14}|\d{4}[/-]\d{2}[/-]\d{2} \d{2}:\d{2}:\d{2}", v):
            ts_idx = i
            break
    if ts_idx is None:
        raise ValueError("未找到时间戳字段")
    raw_ts = f[ts_idx]
    if len(raw_ts) == 14:
        naive = dt.datetime.strptime(raw_ts, "%Y%m%d%H%M%S")
    else:
        naive = dt.datetime.strptime(raw_ts.replace("/", "-"), "%Y-%m-%d %H:%M:%S")
    as_of = naive.replace(tzinfo=tz_of(n["market"])).isoformat(timespec="seconds")
    price, prev = _f(f[3]), _f(f[4])
    item = {"name": f[1], "price": price, "prev_close": prev, "open": _f(f[5]),
            "high": _f(f[ts_idx + 3]), "low": _f(f[ts_idx + 4]),
            "change": _f(f[ts_idx + 1]), "change_pct": _f(f[ts_idx + 2]),
            "as_of": as_of, "price_type": "latest_trade"}
    if n["market"] == "cn" and len(f) > 35 and "/" in f[35]:
        parts = f[35].split("/")
        if len(parts) == 3:
            item["volume_shares"] = _f(parts[1]) * 100 if _f(parts[1]) else None
            item["amount"] = _f(parts[2])
            item["amount_unit"] = "CNY"
    elif n["market"] == "hk" and len(f) > 37:
        item["volume_shares"] = _f(f[6])
        item["amount"] = _f(f[37])
        item["amount_unit"] = "HKD"
    elif n["market"] == "us":
        item["volume_shares"] = _f(f[6])
        item["amount_unit"] = "USD"
    return item


def eastmoney_quote(symbols: list[str]) -> dict:
    ns = [normalize_symbol(s) for s in symbols]
    # 美股个股按 纳斯达克(105)/纽交所(106)/美交所(107) 依次尝试，取有返回者
    candidates: dict[str, list[str]] = {}
    all_secids: list[str] = []
    for n in ns:
        base = n["em_secid"]
        cands = [base]
        if n["market"] == "us" and n["kind"] == "stock":
            cands = [f"105.{n['symbol']}", f"106.{n['symbol']}", f"107.{n['symbol']}"]
        candidates[n["input"]] = cands
        all_secids += cands
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids="
           + urllib.parse.quote(",".join(dict.fromkeys(all_secids)))
           + "&fields=f2,f3,f4,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f124,f86")
    txt = http_get(url)
    try:
        data = json.loads(txt).get("data") or {}
    except json.JSONDecodeError:
        raise DataSourceError("[push2.eastmoney.com] 返回非 JSON") from None
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    by_secid = {}
    for d in diff:
        mk = d.get("f13")
        by_secid[f"{mk}.{d.get('f12')}"] = d
    out_items, errors = [], []
    for n in ns:
        d = next((by_secid.get(c) for c in candidates[n["input"]] if by_secid.get(c)), None)
        if not d:
            errors.append({"symbol": n["input"], "source": "eastmoney_quote",
                           "error": "无返回数据"})
            continue
        ts = d.get("f124") or d.get("f86")
        as_of = None
        if ts:
            as_of = dt.datetime.fromtimestamp(int(ts), tz_of(n["market"])).isoformat(timespec="seconds")
        out_items.append({
            "symbol": n["symbol"], "market": n["market"], "name": d.get("f14"),
            "currency": n["currency"], "price": _f(d.get("f2")),
            "change": _f(d.get("f4")), "change_pct": _f(d.get("f3")),
            "open": _f(d.get("f17")), "high": _f(d.get("f15")), "low": _f(d.get("f16")),
            "prev_close": _f(d.get("f18")), "volume_shares": None,
            "turnover_rate": _f(d.get("f8")),
            "as_of": as_of, "price_type": "latest_trade",
            "source": "eastmoney_quote", "source_name": "东方财富行情接口",
        })
    return {"provider": "eastmoney_quote", "items": out_items, "errors": errors}


PROVIDERS: dict[str, Callable] = {
    "sina": sina_quote, "tencent": tencent_quote, "eastmoney": eastmoney_quote,
}
PROVIDER_ORDER = ["sina", "tencent", "eastmoney"]


def get_quote(symbols: list[str], provider: str = "auto",
              no_cache: bool = False, offline: bool = False,
              timeout: float | None = None) -> dict:
    key = f"{provider}:{','.join(sorted(symbols))}"
    ttl = CACHE_TTL["quote"]
    if not no_cache:
        hit = cache_read("quote", key, ttl, offline=offline)
        if hit:
            return _finalize_quote(hit["payload"], hit)
    errors: list[dict] = []
    order = PROVIDER_ORDER if provider == "auto" else [provider]
    items: list[dict] = []
    used = None
    for p in order:
        if offline:
            break
        try:
            res = PROVIDERS[p](symbols)
            errors.extend(res["errors"])
            if res["items"]:
                items = res["items"]
                used = p
                break
        except DataSourceError as e:
            errors.append({"source": p, "error": str(e)})
    if not items:
        payload = {"status": "unavailable", "items": [], "errors": errors,
                   "note": "全部数据源不可用；不返回任何猜测值"}
        if not no_cache:
            cache_write("quote", key, payload, None)
        return payload
    payload = {"status": "ok", "provider": used, "items": items, "errors": errors}
    if not no_cache:
        cache_write("quote", key, payload, items[0].get("as_of"))
    return _finalize_quote(payload, None)


def _finalize_quote(payload: dict, wrap: dict | None) -> dict:
    out = dict(payload)
    out["retrieved_at"] = (wrap or {}).get("retrieved_at") or _iso(_now())
    if wrap:
        out["from_cache"] = True
        out["cache_age_seconds"] = wrap.get("cache_age_seconds")
    for it in out.get("items", []):
        fr = freshness(it.get("as_of"), it.get("market", "cn"))
        it.update({k: v for k, v in fr.items() if k not in it or it.get(k) is None})
        it["retrieved_at"] = out["retrieved_at"]
        it["data_time"] = it.get("as_of")
    return out


def verify_quote(symbol: str, no_cache: bool = False, offline: bool = False) -> dict:
    """同一标的从 ≥2 个独立数据源取行情，比较价格一致性并报告实测延迟。"""
    key = symbol
    ttl = CACHE_TTL["verify_quote"]
    if not no_cache:
        hit = cache_read("verify_quote", key, ttl, offline=offline)
        if hit:
            return hit["payload"]
    n = normalize_symbol(symbol)
    sources, errors = [], []
    for p in PROVIDER_ORDER:
        if offline:
            break
        try:
            res = PROVIDERS[p]([symbol])
            if res["items"]:
                it = res["items"][0]
                sources.append({"provider": p, "price": it.get("price"),
                                "as_of": it.get("as_of"), "name": it.get("name"),
                                "freshness": freshness(it.get("as_of"), n["market"])})
            errors.extend(res["errors"])
        except DataSourceError as e:
            errors.append({"provider": p, "error": str(e)})
    prices = [s["price"] for s in sources if s.get("price") is not None]
    agree = None
    max_diff_pct = None
    if len(prices) >= 2:
        lo, hi = min(prices), max(prices)
        max_diff_pct = round((hi - lo) / lo * 100, 4) if lo else None
        agree = max_diff_pct is not None and max_diff_pct <= 0.10
    payload = {
        "status": "ok" if len(sources) >= 2 else ("degraded" if sources else "unavailable"),
        "symbol": n["symbol"], "market": n["market"],
        "compared_sources": len(sources),
        "price_agreement": ("一致（差异≤0.10%）" if agree else
                            (f"不一致（最大差异 {max_diff_pct}%）" if agree is False
                             else "样本不足，无法交叉验证")),
        "max_diff_pct": max_diff_pct,
        "sources": sources, "errors": errors,
        "retrieved_at": _iso(_now()),
        "note": "价格一致只说明传输口径相同，不代表估值或基本面已核验",
    }
    if not no_cache:
        cache_write("verify_quote", key, payload, sources[0]["as_of"] if sources else None)
    return payload


# ============================================================ 新闻 / 政策

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _normalize_pub_time(pub: str | None) -> str | None:
    """把各来源五花八门的发布时间统一为 ISO 8601（含时区）。

    例：'2026-10-13 13:23:34' → '2026-10-13T13:23:34+08:00'；
        '2026-10-13' → '2026-10-13T00:00:00+08:00'；
        已规范的 ISO 串原样返回；无法识别时返回原值（交由上层标 unknown）。
    """
    if not pub:
        return None
    s = pub.strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1)
    if "T" not in s and len(s) == 10:          # 只有日期
        return s + "T00:00:00+08:00"
    if "T" in s and not re.search(r"([+-]\d{2}:?\d{2}|Z)$", s):
        return s + "+08:00"
    return s


def search_news(query: str, limit: int = 10, since: str | None = None,
                tier: str = "all", no_cache: bool = False,
                offline: bool = False) -> dict:
    key = json.dumps([query, limit, since, tier], ensure_ascii=False)
    ttl = CACHE_TTL["news"]
    if not no_cache:
        hit = cache_read("news", key, ttl, offline=offline)
        if hit:
            p = dict(hit["payload"])
            p["from_cache"] = True
            p["cache_age_seconds"] = hit.get("cache_age_seconds")
            return p
    if offline:
        raise DataSourceError("[cache] 离线模式且无新闻缓存")
    kw = query
    if tier == "official":
        kw = query + " 政策 公告 通知"  # 关键词收窄，配合来源过滤
    param = {
        "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": max(limit * 3, 30)}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    txt = http_get(url)
    m = re.match(r"^cb\((.*)\)\s*$", txt.strip(), re.S)
    if not m:
        raise DataSourceError("[search-api-web.eastmoney.com] 返回格式变化，无法解析")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        raise DataSourceError("[search-api-web.eastmoney.com] JSON 解析失败") from None
    raw_items = (data.get("result") or {}).get("cmsArticleWebOld") or []
    items, seen = [], set()
    for r in raw_items:
        title = _strip_html(r.get("title"))
        pub = r.get("date")
        if since and pub and pub[:10] < since:
            continue
        dedup = _dedup_key(r.get("url"), title, pub)
        if dedup in seen:
            continue
        seen.add(dedup)
        media = r.get("mediaName", "")
        pub_iso = _normalize_pub_time(pub)
        item = {
            "title": title,
            "content_summary": _strip_html(r.get("content"))[:300],
            "source_name": f"东方财富资讯搜索 · {media}" if media else "东方财富资讯搜索",
            "source_url": r.get("url"),
            "published_at": pub_iso,
            "retrieved_at": _iso(_now()),
            "market": "cn",
            "asset_type": "news",
            "symbols": [],
            "event_type": "policy" if tier == "official" else "news",
            "fact_status": "unverified",
            "freshness_status": freshness(pub_iso, "cn")["freshness_status"],
            "impact_hypothesis": "",
            "confidence": {"level": "low",
                           "basis": "聚合媒体线索，未与官方原始文件交叉核验"},
            "dedup_key": dedup,
            "tier": "media",
        }
        items.append(item)
        if len(items) >= limit:
            break
    payload = {"status": "ok", "query": query, "count": len(items), "items": items,
               "source": "eastmoney_search", "retrieved_at": _iso(_now()),
               "note": "媒体线索默认 fact_status=unverified；引用前须回官方原始来源核验"}
    cache_write("news", key, payload, items[0]["published_at"] if items else None)
    return payload


def _dedup_key(url: str | None, title: str, published: str | None) -> str:
    """去重标识：优先原文链接；否则 标题规范化 + 发布日期。"""
    if url:
        u = url.split("#")[0].rstrip("/")
        return "url:" + u
    norm = re.sub(r"[\s\W_]+", "", title or "")
    day = (published or "")[:10]
    return f"title:{norm}:{day}"


def get_policy_updates(query: str, limit: int = 10, **kw) -> dict:
    res = search_news(query + " 政策", limit=limit, **kw)
    for it in res.get("items", []):
        it["event_type"] = "policy"
        it["confidence"] = {"level": "low",
                            "basis": "需回原始政策文件（发布机关官网）核验后才能作为事实"}
    res["policy_note"] = ("政策类信息必须以发布机关官网原文为准；"
                          "本接口返回的是聚合媒体转述，仅作线索")
    return res


# ============================================================ 公告 / 申报

def _org_id(code: str) -> str | None:
    txt = http_post("http://www.cninfo.com.cn/new/information/topSearch/query",
                    {"keyWord": code, "maxNum": "10"},
                    headers={"Referer": "http://www.cninfo.com.cn/"})
    try:
        arr = json.loads(txt)
    except json.JSONDecodeError:
        return None
    for row in arr:
        if str(row.get("code")) == code:
            return row.get("orgId")
    return None


def get_filings(symbol: str, market: str | None = None, limit: int = 10,
                no_cache: bool = False, offline: bool = False) -> dict:
    n = normalize_symbol(symbol)
    mk = market or n["market"]
    key = f"{mk}:{n['symbol']}:{limit}"
    ttl = CACHE_TTL["filings"]
    if not no_cache:
        hit = cache_read("filings", key, ttl, offline=offline)
        if hit:
            p = dict(hit["payload"])
            p["from_cache"] = True
            p["cache_age_seconds"] = hit.get("cache_age_seconds")
            return p
    if offline:
        raise DataSourceError("[cache] 离线模式且无公告缓存")
    if mk == "cn":
        payload = _cninfo_filings(n, limit)
    elif mk == "us":
        payload = _edgar_filings(n, limit)
    else:
        raise DataSourceError(f"暂不支持 {mk} 市场的公告源（需在数据源注册表中扩展适配器）")
    payload["retrieved_at"] = _iso(_now())
    cache_write("filings", key, payload,
                payload["items"][0]["published_at"] if payload["items"] else None)
    return payload


def _cninfo_filings(n: dict, limit: int) -> dict:
    code = n["symbol"]
    org = _org_id(code)
    column = "sse" if code.startswith(("6", "9")) else "szse"
    data = {"stock": f"{code},{org}" if org else code, "tabName": "fulltext",
            "pageSize": str(limit), "pageNum": "1", "column": column,
            "category": "", "seDate": "", "isHLtitle": "true"}
    txt = http_post("http://www.cninfo.com.cn/new/hisAnnouncement/query", data,
                    headers={"Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"})
    try:
        js = json.loads(txt)
    except json.JSONDecodeError:
        raise DataSourceError("[cninfo] 返回非 JSON") from None
    rows = js.get("announcements") or []
    items = []
    for r in rows[:limit]:
        pub_ms = r.get("announcementTime")
        pub = (dt.datetime.fromtimestamp(pub_ms / 1000, tz_of("cn")).isoformat(timespec="seconds")
               if isinstance(pub_ms, (int, float)) else None)
        url = ("https://static.cninfo.com.cn/" + r["adjunctUrl"]) if r.get("adjunctUrl") else None
        items.append({
            "title": _strip_html(r.get("announcementTitle", "")),
            "content_summary": "",
            "source_name": "巨潮资讯网（法定披露平台）",
            "source_url": url,
            "published_at": pub,
            "retrieved_at": _iso(_now()),
            "market": "cn",
            "asset_type": "filing",
            "symbols": [n["symbol"]],
            "event_type": "announcement",
            "announcement_id": str(r.get("announcementId") or ""),
            "file_type": r.get("adjunctType"),
            "fact_status": "verified",
            "freshness_status": "unknown",
            "confidence": {"level": "high", "basis": "法定披露平台官方原文"},
            "dedup_key": "ann:" + str(r.get("announcementId") or ""),
        })
    for i, it in enumerate(items):
        it["freshness_status"] = "latest" if i == 0 else "historical"
    return {"status": "ok" if items else "empty", "market": "cn", "symbol": n["symbol"],
            "source": "cninfo", "count": len(items), "items": items,
            "note": "PDF 直链为官方静态域名；巨潮公告时间精确到披露日（不精确到时分）；"
                    "引用时请标注公告标题与披露日期"}


def _edgar_filings(n: dict, limit: int) -> dict:
    tickers = _edgar_ticker_map()
    cik = tickers.get(n["symbol"].upper())
    if not cik:
        raise DataSourceError(f"[sec_edgar] 未找到代码 {n['symbol']} 对应的 CIK")
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    txt = http_get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    js = json.loads(txt)
    recent = js.get("filings", {}).get("recent", {})
    items = []
    for i in range(min(limit, len(recent.get("form", [])))):
        acc = recent["accessionNumber"][i].replace("-", "")
        doc = recent["primaryDocument"][i]
        link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
        filed = recent["filingDate"][i]
        items.append({
            "title": f"{recent['form'][i]} — {js.get('name', n['symbol'])}",
            "content_summary": recent.get("primaryDocDescription", [""])[i]
            if recent.get("primaryDocDescription") else "",
            "source_name": "SEC EDGAR",
            "source_url": link,
            "published_at": filed + "T00:00:00-04:00",
            "retrieved_at": _iso(_now()),
            "market": "us", "asset_type": "filing", "symbols": [n["symbol"]],
            "event_type": "filing",
            "form_type": recent["form"][i],
            "announcement_id": recent["accessionNumber"][i],
            "fact_status": "verified",
            "freshness_status": "unknown",
            "confidence": {"level": "high", "basis": "SEC 官方申报文件"},
            "dedup_key": "edgar:" + recent["accessionNumber"][i],
        })
    for i, it in enumerate(items):
        it["freshness_status"] = "latest" if i == 0 else "historical"
    return {"status": "ok" if items else "empty", "market": "us", "symbol": n["symbol"],
            "source": "sec_edgar", "count": len(items), "items": items,
            "note": "CIK=" + str(cik) + "；申报文件以 EDGAR 原文为准"}


def _edgar_ticker_map() -> dict[str, int]:
    txt = cache_read("meta", "edgar_ticker_map", CACHE_TTL["meta"])
    if txt:
        return txt["payload"]
    raw = http_get("https://www.sec.gov/files/company_tickers.json",
                   headers={"User-Agent": SEC_UA})
    js = json.loads(raw)
    m = {v["ticker"].upper(): int(v["cik_str"]) for v in js.values()}
    cache_write("meta", "edgar_ticker_map", m, None)
    return m


# ============================================================ 宏观

MACRO_SPECS = {
    "cpi": dict(source="eastmoney_datacenter", report="RPT_ECONOMY_CPI", market="cn",
                name="中国 CPI 同比/环比", unit="%",
                fields={"period": "TIME", "value": "NATIONAL_SAME", "seq": "NATIONAL_SEQUENTIAL",
                        "cum": "NATIONAL_ACCUMULATE"},
                published_field="REPORT_DATE",
                season_adj="未标注（国家统计局口径以公告为准）"),
    "ppi": dict(source="eastmoney_datacenter", report="RPT_ECONOMY_PPI", market="cn",
                name="中国 PPI 同比", unit="%",
                fields={"period": "TIME", "value": "BASE"}, published_field="REPORT_DATE",
                season_adj="未标注（国家统计局口径以公告为准）"),
    "pmi": dict(source="eastmoney_datacenter", report="RPT_ECONOMY_PMI", market="cn",
                name="中国制造业/非制造业 PMI", unit="点",
                fields={"period": "TIME", "value": "MAKE_INDEX", "value2": "NMAKE_INDEX"},
                published_field="REPORT_DATE", season_adj="季节调整标注需回统计局公告核对"),
    "gdp": dict(source="eastmoney_datacenter", report="RPT_ECONOMY_GDP", market="cn",
                name="中国 GDP", unit="%",
                fields={"period": "TIME", "value": "DOMESTICL_PRODUCT_SAME"},
                published_field="REPORT_DATE", season_adj="累计口径，需注意季度可比"),
}


def get_macro(indicator: str, limit: int = 6, no_cache: bool = False,
              offline: bool = False) -> dict:
    ind = indicator.lower()
    key = f"{ind}:{limit}"
    ttl = CACHE_TTL["macro"]
    if not no_cache:
        hit = cache_read("macro", key, ttl, offline=offline)
        if hit:
            p = dict(hit["payload"])
            p["from_cache"] = True
            p["cache_age_seconds"] = hit.get("cache_age_seconds")
            return p
    if offline:
        raise DataSourceError("[cache] 离线模式且无宏观数据缓存")
    if ind in MACRO_SPECS:
        payload = _em_macro(MACRO_SPECS[ind], ind, limit)
    elif ind == "us_cpi":
        payload = _bls_cpi(limit)
    elif ind == "treasury":
        payload = _treasury_yields(limit)
    else:
        raise DataSourceError(
            f"未知宏观指标 {indicator!r}；可选：{', '.join(list(MACRO_SPECS) + ['us_cpi', 'treasury'])}")
    payload["retrieved_at"] = _iso(_now())
    cache_write("macro", key, payload,
                payload["items"][0].get("published_at") if payload["items"] else None)
    return payload


def _em_macro(spec: dict, ind: str, limit: int) -> dict:
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName="
           + spec["report"] + "&columns=ALL&pageSize=" + str(limit)
           + "&sortColumns=REPORT_DATE&sortTypes=-1")
    txt = http_get(url)
    try:
        js = json.loads(txt)
    except json.JSONDecodeError:
        raise DataSourceError("[datacenter-web.eastmoney.com] 返回非 JSON") from None
    rows = ((js.get("result") or {}).get("data")) or []
    if not rows:
        raise DataSourceError(f"[datacenter] {ind} 无数据（报表名可能已变更）")
    items = []
    for r in rows:
        val = r.get(spec["fields"]["value"])
        item = {"indicator": ind, "indicator_name": spec["name"],
                "period": r.get(spec["fields"].get("period", "")),
                "value": val, "unit": spec["unit"],
                "published_at": (r.get(spec["published_field"]) or "")[:10] + "T00:00:00+08:00"
                if r.get(spec["published_field"]) else None,
                "source_name": "东方财富数据中心（聚合自国家统计局等官方发布）",
                "source_url": "https://data.eastmoney.com/cjsj/",
                "market": spec["market"], "asset_type": "macro", "event_type": "macro",
                "seasonal_adjustment": spec["season_adj"],
                "revision_status": "未标注（需回官方发布核对是否修订）",
                "retrieved_at": _iso(_now()),
                "fact_status": "unverified",
                "freshness_status": "latest" if rows else "unknown",
                "confidence": {"level": "medium",
                               "basis": "聚合数据源，口径与修订状态需回官方公告核对"}}
        for k, f2 in spec["fields"].items():
            if k in ("period", "value"):
                continue
            item[k] = r.get(f2)
        items.append(item)
    return {"status": "ok", "indicator": ind, "source": spec["source"],
            "count": len(items), "items": items,
            "note": "同一指标的不同口径（同比/环比/累计）不得混用比较"}


def _bls_cpi(limit: int) -> dict:
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0?startyear=2024&endyear=2026"
    txt = http_get(url, headers={"User-Agent": SEC_UA})
    js = json.loads(txt)
    if js.get("status") != "REQUEST_SUCCEEDED":
        raise DataSourceError(f"[bls] {js.get('message')}")
    data = js["Results"]["series"][0]["data"]
    items = []
    for i, row in enumerate(data[:max(limit, 12)]):
        y, p = int(row["year"]), row["period"]  # M01..M13
        item = {"indicator": "us_cpi", "indicator_name": "美国 CPI（未季调指数）",
                "period": f"{y}-{p[1:]}", "value": _f(row["value"]), "unit": "指数 1982-84=100",
                "published_at": None, "source_name": "美国劳工统计局 BLS",
                "source_url": "https://www.bls.gov/cpi/",
                "market": "us", "asset_type": "macro", "event_type": "macro",
                "seasonal_adjustment": "未季调（series CUUR = 城市未季调）",
                "revision_status": "以 BLS 发布为准，本接口不返回修订标记",
                "retrieved_at": _iso(_now()), "fact_status": "unverified",
                "freshness_status": "latest",
                "confidence": {"level": "high", "basis": "官方统计机构公开 API"}}
        # 同比为派生值，必须标注 derived
        nxt = next((r for r in data[i + 1:] if r["year"] == str(y - 1) and r["period"] == p), None)
        if nxt:
            yoy = (_f(row["value"]) / _f(nxt["value"]) - 1) * 100
            item["derived_yoy_pct"] = round(yoy, 2)
            item["derived_note"] = "同比为本脚本按指数值计算的派生值，非官方直接发布"
        items.append(item)
        if len(items) >= limit:
            break
    return {"status": "ok", "indicator": "us_cpi", "source": "bls",
            "count": len(items), "items": items,
            "note": "BLS v2 无 key 限每日 25 次请求；发布日期以 BLS 日历为准"}


def _treasury_yields(limit: int) -> dict:
    year = _now().astimezone(ZoneInfo("America/New_York")).year if ZoneInfo else _now().year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
           f"&field_tdr_date_value={year}&page&_format=csv")
    txt = http_get(url)
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    if not lines:
        raise DataSourceError("[us_treasury] CSV 为空")
    head = [h.strip().strip('"') for h in lines[0].split(",")]
    items = []
    for line in lines[1:limit + 1]:
        vals = [v.strip().strip('"') for v in line.split(",")]
        if len(vals) != len(head):
            continue
        row = dict(zip(head, vals))
        d = row.pop("Date")
        mm, dd, yy = d.split("/")
        items.append({"indicator": "treasury", "indicator_name": "美国国债收益率曲线",
                      "period": f"{yy}-{mm}-{dd}",
                      "values_pct": {k: _f(v) for k, v in row.items()},
                      "unit": "%", "published_at": f"{yy}-{mm}-{dd}T00:00:00-04:00",
                      "source_name": "美国财政部（U.S. Treasury）",
                      "source_url": "https://home.treasury.gov/data-center/interest-rates",
                      "market": "us", "asset_type": "macro", "event_type": "rate",
                      "seasonal_adjustment": "不适用（每日名义收益率）",
                      "revision_status": "当日公布，历史日不修订",
                      "retrieved_at": _iso(_now()), "fact_status": "unverified",
                      "freshness_status": "latest",
                      "confidence": {"level": "high", "basis": "官方财政部每日发布"}})
    return {"status": "ok", "indicator": "treasury", "source": "us_treasury",
            "count": len(items), "items": items,
            "note": "单位为百分比年化收益率；不同期限不可与股息率直接比较，需说明口径"}


# ============================================================ 历史行情

def get_history(symbol: str, start: str | None = None, end: str | None = None,
                interval: str = "101", fq: int = 1,
                no_cache: bool = False, offline: bool = False) -> dict:
    n = normalize_symbol(symbol)
    key = f"{n['market']}:{n['symbol']}:{start}:{end}:{interval}:{fq}"
    ttl = CACHE_TTL["history"]
    if not no_cache:
        hit = cache_read("history", key, ttl, offline=offline)
        if hit:
            p = dict(hit["payload"])
            p["from_cache"] = True
            p["cache_age_seconds"] = hit.get("cache_age_seconds")
            return p
    if offline:
        raise DataSourceError("[cache] 离线模式且无历史行情缓存")
    beg = (start or "19900101").replace("-", "")
    eend = (end or _now().strftime("%Y-%m-%d")).replace("-", "")
    fq_name = {0: "不复权", 1: "前复权", 2: "后复权"}.get(fq, "前复权")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid="
           + urllib.parse.quote(n["em_secid"])
           + "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           + f"&klt={interval}&fqt={fq}&beg={beg}&end={eend}")
    txt = http_get(url)
    try:
        js = json.loads(txt)
    except json.JSONDecodeError:
        raise DataSourceError("[push2his.eastmoney.com] 返回非 JSON") from None
    data = js.get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        raise DataSourceError(f"[push2his] {n['symbol']} 无历史数据（区间 {beg}-{eend}）")
    rows = []
    for k in klines:
        p = k.split(",")
        if len(p) < 8:
            continue
        rows.append({"date": p[0], "open": _f(p[1]), "close": _f(p[2]),
                     "high": _f(p[3]), "low": _f(p[4]),
                     "volume": _f(p[5]), "amount": _f(p[6]),
                     "amplitude_pct": _f(p[7])})
    payload = {"status": "ok", "symbol": n["symbol"], "market": n["market"],
               "name": data.get("name"), "source": "eastmoney_kline",
               "source_name": "东方财富历史行情", "interval": interval,
               "interval_name": {"101": "日线", "102": "周线", "114": "月线"}.get(interval, interval),
               "adjust": fq_name, "adjust_note": "复权口径必须与估值/收益计算保持一致",
               "count": len(rows), "items": rows,
               "retrieved_at": _iso(_now()),
               "note": "历史数据 freshness_status=historical；不得当作最新行情"}
    if not no_cache:
        cache_write("history", key, payload, rows[-1]["date"])
    return payload


# ============================================================ 交易日历

def get_calendar(market: str, verify: bool = False,
                 no_cache: bool = False, offline: bool = False) -> dict:
    key = f"{market}:{verify}"
    ttl = CACHE_TTL["calendar"]
    if not no_cache:
        hit = cache_read("calendar", key, ttl if verify else 600, offline=offline)
        if hit:
            p = dict(hit["payload"])
            p["from_cache"] = True
            p["cache_age_seconds"] = hit.get("cache_age_seconds")
            return p
    sess = SESSIONS.get(market)
    if not sess:
        raise DataSourceError(f"未配置 {market} 市场交易时段表")
    state = session_state(market)
    payload = {"status": "ok", "market": market, "tz": sess["tz"],
               "session_windows": [{"start": a, "end": b, "label": lab}
                                   for a, b, lab in sess["windows"]],
               "pre_market": list(sess["pre"]) if sess.get("pre") else None,
               "after_hours": list(sess["post"]) if sess.get("post") else None,
               "lunch_break": list(sess["lunch"]) if sess.get("lunch") else None,
               "current_state": state,
               "retrieved_at": _iso(_now()),
               "holiday_note": "内置表只含周末与固定时段，节假日须以交易所公告为准",
               "trading_day_verified": False}
    if verify and not offline:
        secid = {"cn": "1.000001", "hk": "100.HSI", "us": "100.SPX"}[market]
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid="
               + secid + "&fields1=f1&fields2=f51,f53&klt=101&fqt=0&beg=20260101&end=20500101")
        try:
            txt = http_get(url)
            ks = (json.loads(txt).get("data") or {}).get("klines") or []
            last_day = ks[-1].split(",")[0] if ks else None
            today = state["local_date"]
            payload["last_trading_day"] = last_day
            payload["is_trading_today"] = (last_day == today)
            payload["trading_day_verified"] = True
            payload["verify_source"] = "东方财富指数日K线（实际成交日，可作交易日证据）"
            if state["state"] == "closed" and not state.get("is_weekend") and last_day != today:
                payload["holiday_warning"] = (f"今日 {today} 为工作日但无当日成交，"
                                              "可能是节假日休市，请以交易所公告确认")
        except DataSourceError as e:
            payload["verify_error"] = str(e)
    if not no_cache:
        cache_write("calendar", key, payload, payload.get("last_trading_day"))
    return payload


# ============================================================ 研究数据集（最小闭环）

def research_dataset(symbol: str, news_limit: int = 8, filings_limit: int = 6,
                     no_cache: bool = False, offline: bool = False) -> dict:
    """行情 + 公告 + 新闻 一次打包，附数据缺口清单。"""
    n = normalize_symbol(symbol)
    errors: list[dict] = []
    quote = verify_quote(symbol, no_cache=no_cache, offline=offline)
    if quote.get("status") != "ok":
        errors.append({"step": "quote", "detail": quote.get("errors")})
    try:
        filings = get_filings(symbol, limit=filings_limit, no_cache=no_cache, offline=offline)
    except DataSourceError as e:
        filings = {"status": "unavailable", "items": [], "error": str(e)}
        errors.append({"step": "filings", "detail": str(e)})
    try:
        news = search_news(n["symbol"] if n["market"] != "cn" else
                           (quote["sources"][0].get("name") or n["symbol"]),
                           limit=news_limit, no_cache=no_cache, offline=offline)
    except DataSourceError as e:
        news = {"status": "unavailable", "items": [], "error": str(e)}
        errors.append({"step": "news", "detail": str(e)})
    gaps = []
    if quote.get("status") != "ok":
        gaps.append("最新可用行情（多源交叉验证）")
    if not filings.get("items"):
        gaps.append("最新正式公告/申报")
    if not news.get("items"):
        gaps.append("同期新闻线索")
    return {
        "status": "ok" if not errors else ("degraded" if quote.get("status") == "ok" else "unavailable"),
        "symbol": n["symbol"], "market": n["market"],
        "assembled_at": _iso(_now()),
        "quote": quote, "filings": filings, "news": news,
        "data_gaps": gaps, "errors": errors,
        "usage_note": ("先看 quote 的时效状态与交叉验证结果，再按 filings → news 顺序整理事实；"
                       "缺口必须写进报告的『缺失数据』，不得用推测补齐"),
    }


# ============================================================ 数据源清单

def list_sources(probe: bool = False) -> dict:
    out = []
    for s in SOURCES:
        row = dict(s)
        if probe:
            t0 = time.time()
            try:
                if s["id"] in ("sina_hq", "tencent_hq", "eastmoney_quote"):
                    get_quote(["600519"], provider={"sina_hq": "sina", "tencent_hq": "tencent",
                                                    "eastmoney_quote": "eastmoney"}[s["id"]],
                              no_cache=True)
                elif s["id"] == "cninfo":
                    _org_id("600519")
                elif s["id"] == "eastmoney_search":
                    search_news("央行", limit=1, no_cache=True)
                elif s["id"] == "eastmoney_notice":
                    http_get("https://np-anotice-stock.eastmoney.com/api/security/ann"
                             "?sr=-1&page_size=2&page_index=1&ann_type=A&client_source=web&stock_list=600519")
                elif s["id"] == "eastmoney_datacenter":
                    get_macro("cpi", limit=1, no_cache=True)
                elif s["id"] == "bls":
                    get_macro("us_cpi", limit=1, no_cache=True)
                elif s["id"] == "us_treasury":
                    get_macro("treasury", limit=1, no_cache=True)
                elif s["id"] == "sec_edgar":
                    http_get("https://data.sec.gov/submissions/CIK0000320193.json",
                             headers={"User-Agent": SEC_UA})
                else:
                    row["probe"] = "skipped（无轻量探测动作）"
                    out.append(row)
                    continue
                row["probe"] = "ok"
                row["probe_ms"] = int((time.time() - t0) * 1000)
                row["status"] = "ok"
                row["verified_at"] = _now().astimezone(tz_of("cn")).strftime("%Y-%m-%d")
            except Exception as e:  # noqa: BLE001
                row["probe"] = f"fail: {e}"
                row["status"] = "unreachable"
        out.append(row)
    return {"status": "ok", "count": len(out), "sources": out,
            "policy": "官方原始来源 > 专业数据接口 > 可信媒体 > 社区线索（仅作待验证线索）"}


# ============================================================ 输出

def emit(payload: dict, as_md: bool = False) -> None:
    if as_md:
        print(to_markdown(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def to_markdown(p: dict) -> str:
    lines = [f"# {p.get('_title') or p.get('status', 'result')}", ""]
    if p.get("retrieved_at"):
        lines.append(f"- 获取时间：{p['retrieved_at']}")
    if p.get("from_cache"):
        lines.append(f"- 来自缓存（{p.get('cache_age_seconds')} 秒前取得，数据时间以条目为准）")
    if p.get("note"):
        lines.append(f"- 说明：{p['note']}")
    lines.append("")
    if p.get("items") is not None and isinstance(p["items"], list):
        if p["items"] and isinstance(p["items"][0], dict) and "price" in p["items"][0]:
            lines += ["| 标的 | 名称 | 最新价 | 涨跌幅 | 数据时间 | 时效 | 来源 |",
                      "|---|---|---|---|---|---|---|"]
            for it in p["items"]:
                lines.append(f"| {it.get('symbol')} | {it.get('name')} | {it.get('price')} | "
                             f"{it.get('change_pct')}% | {it.get('as_of')} | "
                             f"{it.get('freshness_status')}（{it.get('note', '')}） | {it.get('source_name')} |")
        elif p["items"] and isinstance(p["items"][0], dict) and "title" in p["items"][0]:
            lines += ["| 标题 | 来源 | 发布时间 | 核实状态 | 链接 |", "|---|---|---|---|---|"]
            for it in p["items"]:
                lines.append(f"| {it.get('title')} | {it.get('source_name')} | "
                             f"{it.get('published_at')} | {it.get('fact_status')} | "
                             f"{it.get('source_url')} |")
        else:
            lines += ["| 期间 | 值 | 单位 | 发布时间 | 来源 |", "|---|---|---|---|---|"]
            for it in p["items"]:
                lines.append(f"| {it.get('period')} | {it.get('value')} | {it.get('unit')} | "
                             f"{it.get('published_at')} | {it.get('source_name')} |")
        lines.append("")
    if p.get("sources"):
        lines += ["## 多源交叉验证", ""]
        for s in p["sources"]:
            fr = s.get("freshness") or {}
            lines.append(f"- **{s['provider']}**：{s['price']} @ {s['as_of']} "
                         f"（{fr.get('freshness_status')}，延迟 {fr.get('delay_seconds')}s）")
        lines.append(f"- 一致性：{p.get('price_agreement')}")
        lines.append("")
    if p.get("errors"):
        lines += ["## 采集失败记录", ""]
        for e in p["errors"]:
            lines.append(f"- `{e.get('source') or e.get('provider') or e.get('step')}` "
                         f"{e.get('symbol', '')} {e.get('error', e.get('detail', ''))}")
        lines.append("")
    return "\n".join(lines)


# ============================================================ CLI

def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON（默认）")
    common.add_argument("--md", action="store_true", help="输出 Markdown 摘要")
    common.add_argument("--no-cache", action="store_true", help="跳过缓存，直接请求")
    common.add_argument("--offline", action="store_true", help="只用缓存，不发请求")
    common.add_argument("--timeout", type=float, default=None, help="单请求超时秒")
    common.add_argument("--retries", type=int, default=None, help="失败重试次数")

    ap = argparse.ArgumentParser(prog="market_data.py",
                                 description="《投资的纪律》Skill 统一市场数据适配器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sources", help="列出数据源", parents=[common])
    s.add_argument("--probe", action="store_true", help="对每个源做一次连通性探测")

    s = sub.add_parser("quote", help="最新可用行情", parents=[common])
    s.add_argument("symbols", help="逗号分隔，如 600519,sh000001,00700,AAPL")
    s.add_argument("--provider", default="auto",
                   choices=["auto", "sina", "tencent", "eastmoney"])

    s = sub.add_parser("verify-quote", help="多源交叉验证行情", parents=[common])
    s.add_argument("symbol")

    s = sub.add_parser("news", help="新闻/事件检索", parents=[common])
    s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--since", default=None, help="YYYY-MM-DD")
    s.add_argument("--tier", default="all", choices=["all", "official"])

    s = sub.add_parser("policy", help="政策类信息（官方源优先）", parents=[common])
    s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("filings", help="公告 / 申报", parents=[common])
    s.add_argument("--symbol", required=True)
    s.add_argument("--market", default=None, choices=["cn", "us"])
    s.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("macro", help="宏观经济数据", parents=[common])
    s.add_argument("--indicator", required=True,
                   help="cpi/ppi/pmi/gdp/us_cpi/treasury")
    s.add_argument("--limit", type=int, default=6)

    s = sub.add_parser("history", help="历史行情", parents=[common])
    s.add_argument("--symbol", required=True)
    s.add_argument("--start", default=None, help="YYYYMMDD")
    s.add_argument("--end", default=None, help="YYYYMMDD")
    s.add_argument("--interval", default="101", help="101=日 102=周 114=月")
    s.add_argument("--fq", type=int, default=1, choices=[0, 1, 2])

    s = sub.add_parser("calendar", help="交易日历与时段", parents=[common])
    s.add_argument("--market", required=True, choices=["cn", "hk", "us"])
    s.add_argument("--verify", action="store_true", help="用最近K线核对当日是否交易日")

    s = sub.add_parser("research", help="最小闭环数据集：行情+公告+新闻", parents=[common])
    s.add_argument("--symbol", required=True)
    s.add_argument("--news-limit", type=int, default=8)
    s.add_argument("--filings-limit", type=int, default=6)

    a = ap.parse_args(argv)
    if a.timeout:
        global DEFAULT_TIMEOUT
        DEFAULT_TIMEOUT = a.timeout
    if a.retries is not None:
        global DEFAULT_RETRIES
        DEFAULT_RETRIES = a.retries

    try:
        if a.cmd == "sources":
            p = list_sources(probe=a.probe)
            p["_title"] = "数据源注册表"
        elif a.cmd == "quote":
            p = get_quote(a.symbols.split(","), provider=a.provider,
                          no_cache=a.no_cache, offline=a.offline)
            p["_title"] = "最新可用行情"
        elif a.cmd == "verify-quote":
            p = verify_quote(a.symbol, no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"行情交叉验证 · {a.symbol}"
        elif a.cmd == "news":
            p = search_news(a.query, limit=a.limit, since=a.since, tier=a.tier,
                            no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"新闻检索 · {a.query}"
        elif a.cmd == "policy":
            p = get_policy_updates(a.query, limit=a.limit,
                                   no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"政策检索 · {a.query}"
        elif a.cmd == "filings":
            p = get_filings(a.symbol, market=a.market, limit=a.limit,
                            no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"公告/申报 · {a.symbol}"
        elif a.cmd == "macro":
            p = get_macro(a.indicator, limit=a.limit,
                          no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"宏观数据 · {a.indicator}"
        elif a.cmd == "history":
            p = get_history(a.symbol, start=a.start, end=a.end, interval=a.interval,
                            fq=a.fq, no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"历史行情 · {a.symbol}"
        elif a.cmd == "calendar":
            p = get_calendar(a.market, verify=a.verify,
                             no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"交易日历 · {a.market}"
        elif a.cmd == "research":
            p = research_dataset(a.symbol, news_limit=a.news_limit,
                                 filings_limit=a.filings_limit,
                                 no_cache=a.no_cache, offline=a.offline)
            p["_title"] = f"研究数据集 · {a.symbol}"
        else:  # pragma: no cover
            ap.error("未知命令")
            return 2
    except DataSourceError as e:
        payload = {"status": "unavailable", "error": str(e),
                   "retrieved_at": _iso(_now()),
                   "note": "数据源不可用：已记录失败原因，不返回任何替代或估算值"}
        emit(payload, a.md)
        return 3
    emit(p, a.md)
    return 0 if p.get("status") in ("ok", "degraded") else 3


if __name__ == "__main__":
    sys.exit(main())
