"""MarketBreadth 每日更新脚本（GitHub Actions 每个交易日 19:30 ET 运行）

做什么：
  1. 价格：yfinance 拉 SPY / QQQ / ^VIX 原始收盘价（auto_adjust=False 的 Close），追加新交易日。
  2. CNN 恐慌贪婪：只用 CNN 官方端点，追加新交易日。
  3. 参与度：Wikipedia 当日标普 500 名单 + yfinance 原始收盘价，
     算收盘价严格高于自身 SMA20 / SMA50 的占比，追加新交易日，并保存逐只明细。
  4. 重建 data/dashboard_data.json（网页读这一份）和 data/latest.json。

原则：已定稿的历史锁定，与官方不一致只记录并通知（见下方“历史核对”），确认后才修改；
取不到就写进 missing，绝不插值补数。
时间轴 = SPY 的交易日；.VIX 在休市日的值不进入。
没赶上的日子（例如某天 Yahoo 限流）在之后的运行中会自动补算，只要源头还有数据。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

import tempfile
try:  # 给 yfinance 一个独立的缓存目录，避免缓存数据库被锁
    yf.set_tz_cache_location(tempfile.mkdtemp(prefix="yf-cache-"))
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DETAILS = DATA / "details"
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
MIN_STOCKS = 450          # 当天取到的成分股少于这个数，就不写当天参与度，留给下次补算
BACKFILL_DAYS = 40        # 最多回补最近多少个交易日的缺口

F_PRICES = DATA / "overlay_prices.json"
F_BR_CSV = DATA / "breadth_history_2y.csv"
F_BR_JSON = DATA / "breadth_history_2y.json"
F_FG_CSV = DATA / "cnn_fear_greed_2y.csv"
F_FG_JSON = DATA / "cnn_fear_greed_2y.json"
F_AAII_CSV = DATA / "aaii.csv"
F_AAII_JSON = DATA / "aaii.json"
F_STATE = DATA / "verify_state.json"   # 初值清单、待确认差异、已通知记录
F_DASH = DATA / "dashboard_data.json"
F_LATEST = DATA / "latest.json"


# ---------------------------------------------------------------- 历史核对
# 规则：已定稿的历史数据锁定不动。每次运行都把能取到的近期官方数据与本地历史对比，
# 不一致只记录进 verify_state.json 的 pending、并开 GitHub Issue 通知，不自动修改；
# 只有手动运行时勾选 accept，才把仍然成立的差异改成官方值。
# 例外是"初值"：写入时就知道尚未定稿的数据（CNN 最新两天、用收盘后实时报价补的收盘价、
# 用这些收盘价算的参与度），它们之后从同一官方来源自动定稿，日志里注明。
ACCEPT = "--accept" in sys.argv
REVISIONS: list[str] = []
STATE: dict = {}
CHECKED: set = set()       # 本次运行核对过的 (类别, 名称, 日期)
FOUND: dict = {}           # 本次发现的差异 key -> 记录


def load_state():
    global STATE
    STATE = json.loads(F_STATE.read_text()) if F_STATE.exists() else {}
    STATE.setdefault("provisional", {})
    for k in ("price", "cnn", "breadth"):
        STATE["provisional"].setdefault(k, [])
    STATE.setdefault("pending", [])
    STATE.setdefault("reported", [])


def prov(kind: str) -> list:
    return STATE["provisional"][kind]


def differ(kind: str, name: str, date: str, stored, official) -> bool:
    """记录一处与官方不一致。返回 True 表示应改成官方值（仅 accept 且此前已通知过）。"""
    key = f"{kind}|{name}|{date}|{official}"
    if ACCEPT and key in {x["key"] for x in STATE["pending"]}:
        REVISIONS.append(f"按确认采用官方值：{kind} {name} {date} {stored} → {official}")
        return True
    FOUND[key] = {"key": key, "kind": kind, "name": name, "date": date, "stored": stored, "official": official}
    return False


def finish_state():
    today = dt.datetime.now(NY).date().isoformat()
    old = {x["key"]: x for x in STATE["pending"]}
    keep = [x for x in STATE["pending"] if (x["kind"], x["name"], x["date"]) not in CHECKED and x["key"] not in FOUND]
    cur = [{**v, "first_seen": old.get(k, {}).get("first_seen", today)} for k, v in FOUND.items()]
    STATE["pending"] = sorted(keep + cur, key=lambda x: (x["date"], x["kind"], x["name"]))
    new = [x for x in STATE["pending"] if x["key"] not in STATE["reported"]]
    STATE["reported"] = sorted(set(STATE["reported"]) | {x["key"] for x in new})
    F_STATE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1))
    if new:
        path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "diff_issue.md"
        lines = ["每日核对发现以下历史数据与官方来源不一致。**没有自动修改**，仍保留原值。", "",
                 "| 类别 | 项目 | 日期 | 本地保存 | 官方现值 |", "|---|---|---|---|---|"]
        lines += [f"| {x['kind']} | {x['name']} | {x['date']} | {x['stored']} | {x['official']} |" for x in new]
        lines += ["", f"目前待确认差异共 {len(STATE['pending'])} 处。",
                  "", "处理方法：确认采用官方值 → Actions → Daily update → Run workflow，勾选 accept 运行；"
                  "保留原值 → 不用操作（同一差异不会重复通知）。"]
        path.write_text("\n".join(lines))
        log(f"核对：新发现 {len(new)} 处与官方不一致，已写入通知")


def log(*a):
    print(*a, flush=True)


def fg_rating(s: float) -> str:
    # CNN 官方分档：0–24 极度恐慌，25–44 恐慌，45–55 中性，56–75 贪婪，76–100 极度贪婪
    if s < 25: return "extreme fear"
    if s < 45: return "fear"
    if s <= 55: return "neutral"
    if s <= 75: return "greed"
    return "extreme greed"


def closes(df: pd.DataFrame, ticker: str) -> pd.Series:
    """从 yf.download(group_by='ticker') 的结果里取某只票的原始 Close，去空值，索引换成 YYYY-MM-DD。"""
    try:
        s = df[ticker]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
    except KeyError:
        return pd.Series(dtype=float)
    s = s.dropna()
    s.index = [pd.Timestamp(i).strftime("%Y-%m-%d") for i in s.index]
    return s.astype(float)


def download(tickers, **kw) -> pd.DataFrame:
    for attempt in range(3):
        try:
            # threads=False：并发下载会让 yfinance 的本地缓存数据库报 "database is locked" 而丢票
            return yf.download(tickers, interval="1d", auto_adjust=False, progress=False,
                               group_by="ticker", threads=False, **kw)
        except Exception as e:  # noqa: BLE001
            log(f"  yfinance 出错（第 {attempt + 1} 次）：{e}")
            time.sleep(5 * (attempt + 1))
    return pd.DataFrame()


# ---------------------------------------------------------------- 当日收盘价补丁
# Yahoo 的日 K 线在收盘后数小时内常常只有空的收盘价（close = null），
# 当天收盘价只出现在实时报价字段 regularMarketPrice 里。
# 规则：某只票缺 d 日收盘价，且其实时报价时间是纽约时间 d 日 16:00 之后，就用实时报价作为 d 日收盘价。
_YS = None


def live_close(ticker: str, d: str) -> float | None:
    global _YS
    if _YS is None:
        from curl_cffi import requests as creq
        _YS = creq.Session(impersonate="chrome")
    for attempt in range(3):
        try:
            r = _YS.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
                        params={"range": "5d", "interval": "1d"}, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1)); continue
            meta = r.json()["chart"]["result"][0]["meta"]
            t = dt.datetime.fromtimestamp(meta["regularMarketTime"], NY)
            px = meta.get("regularMarketPrice")
            if px and t.date().isoformat() == d and t.time() >= dt.time(16, 0):
                return float(px)
            return None
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return None


def patch_today(series: dict, tickers: list[str], d: str) -> list[str]:
    """给缺 d 日收盘价的票补上实时报价收盘价；返回补了哪些。"""
    done = []
    for t in tickers:
        s = series.get(t)
        if s is not None and d in s.index:
            continue
        v = live_close(t, d)
        if v is None:
            continue
        add = pd.Series([v], index=[d])
        series[t] = add if s is None or s.empty else pd.concat([s[s.index < d], add])
        done.append(t)
    return done


def ny_close_day() -> str | None:
    """纽约时间已过 16:00 的工作日返回当天日期，否则返回 None。"""
    now = dt.datetime.now(NY)
    return now.date().isoformat() if now.weekday() < 5 and now.time() >= dt.time(16, 0) else None


# ---------------------------------------------------------------- prices
def update_prices(missing: list) -> list[str]:
    prices = json.loads(F_PRICES.read_text())
    df = download(["SPY", "QQQ", "^VIX"], period="3mo")
    bars = {t: closes(df, t) for t in ["SPY", "QQQ", "^VIX"]}      # Yahoo 日 K 线（官方定稿值）
    ser = {t: s.copy() for t, s in bars.items()}
    today = ny_close_day()
    patched = []
    if today:
        patched = patch_today(ser, ["SPY", "QQQ", "^VIX"], today)
        if patched:
            log(f"价格：{today} 日 K 线收盘价为空，改用收盘后实时报价：{patched}")
    spy = ser["SPY"]
    if spy.empty:
        missing.append("价格：Yahoo 未返回 SPY 数据，本次不更新任何价格")
        return [d for d, _ in prices["SPY"]]
    changed = False
    # 1) 初值定稿：之前用实时报价补的收盘价，日 K 线出来后改成日 K 线值
    for d in list(prov("price")):
        if all(d in bars[t].index for t in bars):
            for t in bars:
                new = round(float(bars[t][d]), 2)
                for row in prices[t]:
                    if row[0] == d and abs(row[1] - new) > 0.001:
                        REVISIONS.append(f"价格初值定稿：{t} {d} {row[1]} → {new}")
                        row[1] = new; changed = True
            prov("price").remove(d)
    # 2) 已定稿历史与官方日 K 线对比：不一致只记录
    for t in bars:
        for row in prices[t]:
            d = row[0]
            if d in bars[t].index and d not in prov("price"):
                CHECKED.add(("价格", t, d))
                off = round(float(bars[t][d]), 2)
                if abs(off - row[1]) > 0.011 and differ("价格", t, d, row[1], off):
                    row[1] = off; changed = True
    # 3) 追加新交易日
    last_known = max(d for d, _ in prices["SPY"])
    new_dates = [d for d in spy.index if d > last_known]
    for t in ["SPY", "QQQ", "^VIX"]:
        s = ser[t]
        for d in new_dates:
            if d in s.index:
                prices[t].append([d, round(float(s[d]), 2)])
            else:
                missing.append(f"价格：{t} 缺 {d}")
        prices[t].sort()
    if today in new_dates and patched and today not in prov("price"):
        prov("price").append(today)
    if new_dates or changed:  # 没有变化就不重写文件；格式与初始文件一致
        F_PRICES.write_text(json.dumps(prices))
    log(f"价格：新增 {len(new_dates)} 天 {new_dates}")
    return [d for d, _ in prices["SPY"]]


# ---------------------------------------------------------------- CNN
def cnn_source() -> dict[str, float]:
    """只用 CNN 官方端点；取不到就算缺失，不用第三方数据。"""
    try:
        r = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                         headers={**UA, "Referer": "https://www.cnn.com/markets/fear-and-greed",
                                  "Origin": "https://www.cnn.com"}, timeout=30)
        r.raise_for_status()
        pts = r.json()["fear_and_greed_historical"]["data"]
        out = {}
        for p in pts:  # 同一日期有多个点时，只认当天 00:00 UTC 的那个日点
            t = dt.datetime.fromtimestamp(p["x"] / 1000, dt.timezone.utc)
            d = t.strftime("%Y-%m-%d")
            if t.hour == 0 and t.minute == 0 or d not in out:
                out[d] = float(p["y"])
        log(f"CNN：官方端点 {len(out)} 个日期")
        return out
    except Exception as e:  # noqa: BLE001
        log(f"CNN：官方端点失败（{e}）")
        return {}


def update_cnn(axis: list[str], missing: list):
    """CNN 最新两个日期是初值（之后会被 CNN 修订，2026-11-20 实测 9.67 → 18.59）。"""
    rows = json.loads(F_FG_JSON.read_text())
    have = {r["date"]: r for r in rows}
    src = cnn_source()
    if not src:
        if axis[-1] not in have:
            missing.append(f"CNN：官方端点暂无 {axis[-1]} 的数据")
        return
    order = sorted(src)
    latest2, final = set(order[-2:]), set(order[:-2])
    changed = False
    for d in list(prov("cnn")):                      # 初值定稿
        if d in final and d in have:
            new = round(src[d], 2)
            if abs(have[d]["score"] - new) > 0.006:
                REVISIONS.append(f"CNN 初值定稿：{d} {have[d]['score']} → {new}")
                have[d]["score"], have[d]["rating"] = new, fg_rating(new); changed = True
            prov("cnn").remove(d)
    for d, r in have.items():                        # 已定稿历史对比
        if d in final and d not in prov("cnn"):
            CHECKED.add(("CNN", "恐慌贪婪指数", d))
            new = round(src[d], 2)
            if abs(src[d] - r["score"]) > 0.006 and differ("CNN", "恐慌贪婪指数", d, r["score"], new):
                r["score"], r["rating"] = new, fg_rating(new); changed = True
    added = []
    for d in axis[-BACKFILL_DAYS:]:                  # 追加新交易日
        if d not in have and d in src:
            s = round(src[d], 2)
            rows.append({"date": d, "score": s, "rating": fg_rating(s)})
            added.append(d)
            if d in latest2:
                prov("cnn").append(d)
    if changed or added:
        rows.sort(key=lambda r: r["date"])
        F_FG_JSON.write_text(json.dumps(rows))
        with F_FG_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "score", "rating"])
            for r in rows:
                w.writerow([r["date"], r["score"], r["rating"]])
    log(f"CNN：新增 {added}；初值待定稿 {prov('cnn')}")
    if axis[-1] not in have and axis[-1] not in added:
        missing.append(f"CNN：官方端点暂无 {axis[-1]} 的数据")


# ---------------------------------------------------------------- AAII
AAII_URL = "https://www.aaii.com/files/surveys/sentiment.xls"
AAII_START = "2024-10-01"   # 公开仓库只保留报告需要的区间，不转存 AAII 的完整历史


def aaii_source() -> list[tuple[str, float, float, float]]:
    import xlrd
    r = requests.get(AAII_URL, headers={**UA, "Referer": "https://www.aaii.com/sentimentsurvey/sent_results"}, timeout=60)
    r.raise_for_status()
    wb = xlrd.open_workbook(file_contents=r.content)
    sh = wb.sheet_by_index(0)
    out = []
    for i in range(sh.nrows):
        v = sh.row_values(i)
        if isinstance(v[0], float) and v[0] > 30000 and all(isinstance(x, float) for x in v[1:4]):
            d = xlrd.xldate_as_datetime(v[0], wb.datemode).date().isoformat()
            out.append((d, round(v[1] * 100, 1), round(v[2] * 100, 1), round(v[3] * 100, 1)))
    return sorted(out)


def update_aaii(missing: list):
    rows = json.loads(F_AAII_JSON.read_text())
    last = rows[-1]["date"] if rows else AAII_START
    try:
        src = aaii_source()
    except Exception as e:  # noqa: BLE001
        missing.append(f"AAII：下载失败（{e}）")
        return
    off = {r[0]: r for r in src}
    changed = False
    for r in rows:                                   # 已有历史对比
        o = off.get(r["date"])
        if o is None:
            continue
        CHECKED.add(("AAII", "看多/中性/看空", r["date"]))
        mine = (r["bullish"], r["neutral"], r["bearish"])
        if any(abs(a - b) > 0.05 for a, b in zip(mine, o[1:])):
            if differ("AAII", "看多/中性/看空", r["date"], "/".join(map(str, mine)), "/".join(map(str, o[1:]))):
                r["bullish"], r["neutral"], r["bearish"] = o[1:]; changed = True
    new = [r for r in src if r[0] > last and r[0] >= AAII_START]
    for d, b, n, s in new:
        rows.append({"date": d, "bullish": b, "neutral": n, "bearish": s})
    if new or changed:
        F_AAII_JSON.write_text(json.dumps(rows))
        with F_AAII_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "bullish", "neutral", "bearish", "spread"])
            for r in rows:
                w.writerow([r["date"], r["bullish"], r["neutral"], r["bearish"], round(r["bullish"] - r["bearish"], 1)])
    log(f"AAII：新增 {[r[0] for r in new]}")


# ---------------------------------------------------------------- breadth
def sp500() -> list[str]:
    r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=30)
    r.raise_for_status()
    t = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
    return [str(s).strip().replace(".", "-") for s in t["Symbol"]]


def update_breadth(axis: list[str], missing: list):
    rows = json.loads(F_BR_JSON.read_text())
    have = {r["date"] for r in rows}
    targets = [d for d in axis[-BACKFILL_DAYS:] if d not in have]
    recheck = [d for d in prov("breadth") if d in have]   # 用实时报价算的初值，K 线定稿后重算
    if not targets and not recheck:
        return
    try:
        syms = sp500()
    except Exception as e:  # noqa: BLE001
        missing.append(f"参与度：Wikipedia 名单获取失败（{e}）")
        return
    span = sorted(targets + recheck)
    start = (dt.date.fromisoformat(span[0]) - dt.timedelta(days=130)).isoformat()
    end = (dt.date.fromisoformat(span[-1]) + dt.timedelta(days=1)).isoformat()
    series: dict[str, pd.Series] = {}
    todo = list(syms)
    for rnd in range(2):  # 第二轮只重试第一轮没拿到的票
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            df = download(batch, start=start, end=end)
            for s in batch:
                c = closes(df, s) if not df.empty else pd.Series(dtype=float)
                if not c.empty:
                    series[s] = c
            time.sleep(2)
        todo = [s for s in syms if s not in series]
        log(f"参与度：第 {rnd + 1} 轮后取到 {len(series)}/{len(syms)} 只")
        if not todo:
            break
    today = ny_close_day()
    fixed = []
    if today and today in targets:
        fixed = patch_today(series, syms, today)
        if fixed:
            log(f"参与度：{len(fixed)} 只的 {today} 日 K 线收盘价为空，改用收盘后实时报价")
    if len(series) < MIN_STOCKS:
        missing.append(f"参与度：只取到 {len(series)}/{len(syms)} 只成分股，低于 {MIN_STOCKS}，本次不写入，下次自动补算")
        return

    added = []
    csv_rows = None
    for d in span:
        a20 = n20 = a50 = n50 = 0
        detail = []
        for s in syms:
            c = series.get(s)
            if c is None or d not in c.index:
                detail.append([s, "", "", "", "", 0, "", ""])
                continue
            c = c[c.index <= d]
            px, n = float(c.iloc[-1]), len(c)
            m20 = float(c.iloc[-20:].mean()) if n >= 20 else None
            m50 = float(c.iloc[-50:].mean()) if n >= 50 else None
            up20 = up50 = ""
            if m20 is not None:
                n20 += 1; up20 = int(px > m20); a20 += up20
            if m50 is not None:
                n50 += 1; up50 = int(px > m50); a50 += up50
            detail.append([s, d, round(px, 4), "" if m20 is None else round(m20, 4),
                           "" if m50 is None else round(m50, 4), n, up20, up50])
        if n20 < MIN_STOCKS or n50 < MIN_STOCKS:
            if d in recheck:
                log(f"参与度：{d} 的日 K 线还没定稿，初值保留，下次再核")
            else:
                missing.append(f"参与度：{d} 当天有收盘价的成分股太少（{n20}），未写入")
            continue
        p20, p50 = round(a20 / n20 * 100, 2), round(a50 / n50 * 100, 2)
        if d in recheck:   # 初值定稿：K 线出来后重算，结果不同就替换这一行
            if csv_rows is None:
                with F_BR_CSV.open() as f:
                    csv_rows = list(csv.reader(f))
            for cr in csv_rows[1:]:
                if cr[0] == d and [int(cr[3]), int(cr[4]), int(cr[5]), int(cr[6])] != [a20, n20, a50, n50]:
                    REVISIONS.append(f"参与度初值定稿：{d} {cr[3]}/{cr[4]} · {cr[5]}/{cr[6]} → {a20}/{n20} · {a50}/{n50}")
                    cr[1:] = [p20, p50, a20, n20, a50, n50]
                    for r in rows:
                        if r["date"] == d:
                            r["p20"], r["p50"] = p20, p50
            prov("breadth").remove(d)
        else:
            rows.append({"date": d, "p20": p20, "p50": p50})
            with F_BR_CSV.open("a", newline="") as f:
                csv.writer(f).writerow([d, p20, p50, a20, n20, a50, n50])
            if d == today and fixed:
                prov("breadth").append(d)
        DETAILS.mkdir(parents=True, exist_ok=True)
        with (DETAILS / f"breadth_{d}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "date", "close", "sma20", "sma50", "bars", "above20", "above50"])
            w.writerows(detail)
        nodata = [x[0] for x in detail if x[1] == ""]
        if nodata:
            missing.append(f"参与度 {d}：{len(nodata)} 只无当日收盘价未计入（{', '.join(nodata[:20])}"
                           f"{' 等' if len(nodata) > 20 else ''}）")
        if d not in recheck:
            added.append(d)
        log(f"参与度 {d}：20日 {a20}/{n20}={p20}%  50日 {a50}/{n50}={p50}%")
    if csv_rows is not None:
        with F_BR_CSV.open("w", newline="") as f:
            csv.writer(f).writerows(csv_rows)
    rows.sort(key=lambda r: r["date"])
    F_BR_JSON.write_text(json.dumps(rows))


# ---------------------------------------------------------------- outputs
def build_outputs(missing: list):
    prices = json.loads(F_PRICES.read_text())
    axis = [d for d, _ in prices["SPY"]]
    px = {k: dict(map(tuple, v)) for k, v in prices.items()}
    br = {}
    with F_BR_CSV.open() as f:
        for r in csv.DictReader(f):
            br[r["date"]] = [int(r["count20"]), int(r["n20"]), int(r["count50"]), int(r["n50"])]
    fg = {r["date"]: r["score"] for r in json.loads(F_FG_JSON.read_text())}
    now = dt.datetime.now(NY).strftime("%Y-%m-%d %H:%M ET")
    D = {"d": axis, "spy": [px["SPY"][d] for d in axis], "qqq": [px["QQQ"].get(d) for d in axis],
         "vix": [px["^VIX"].get(d) for d in axis], "b": [br.get(d) for d in axis],
         "cnn": [fg.get(d) for d in axis], "updated": now,
         "aaii": [[r["date"], r["bullish"], r["neutral"], r["bearish"]] for r in json.loads(F_AAII_JSON.read_text())]}
    F_DASH.write_text(json.dumps(D, separators=(",", ":")))
    last = axis[-1]
    b = br.get(last)
    F_LATEST.write_text(json.dumps({
        "date": last, "updated": now,
        "prices": {k: px[k].get(last) for k in ["SPY", "QQQ", "^VIX"]},
        "cnn": fg.get(last),
        "breadth": None if not b else {"p20": round(b[0] / b[1] * 100, 2), "p50": round(b[2] / b[3] * 100, 2),
                                       "count20": b[0], "n20": b[1], "count50": b[2], "n50": b[3]},
        "missing": missing, "revisions": REVISIONS,
        "pending": [f"{x['kind']} {x['name']} {x['date']}：本地 {x['stored']} / 官方 {x['official']}" for x in STATE.get("pending", [])],
        "provisional": STATE.get("provisional", {})}, ensure_ascii=False, indent=1))


def check_sources():
    """只测试各数据源能否取到数，不写任何文件。"""
    ok = True
    df = download(["SPY", "QQQ", "^VIX"], period="5d")
    for t in ["SPY", "QQQ", "^VIX"]:
        s = closes(df, t)
        log(f"[检查] Yahoo {t}: " + (f"最后 {s.index[-1]} = {s.iloc[-1]:.2f}" if not s.empty else "失败"))
        ok &= not s.empty
    src = cnn_source()
    log(f"[检查] CNN: {len(src)} 点" + (f"，最后 {max(src)} = {src[max(src)]:.2f}" if src else ""))
    ok &= bool(src)
    try:
        syms = sp500()
        log(f"[检查] Wikipedia 名单: {len(syms)} 只")
        df = download(syms[:100], period="3mo")
        got = sum(not closes(df, s).empty for s in syms[:100])
        log(f"[检查] Yahoo 成分股（前 100 只）: 取到 {got}/100")
        ok &= got >= 90
    except Exception as e:  # noqa: BLE001
        log(f"[检查] Wikipedia / 成分股失败：{e}")
        ok = False
    try:
        a = aaii_source()
        log(f"[检查] AAII: {len(a)} 周，最后 {a[-1][0]} 看多 {a[-1][1]}% 看空 {a[-1][3]}%")
    except Exception as e:  # noqa: BLE001
        log(f"[检查] AAII 失败：{e}")
        ok = False
    log("[检查] 结果：" + ("全部正常" if ok else "有数据源失败"))
    if not ok:
        sys.exit(1)


def main():
    if "--check" in sys.argv:
        check_sources()
        return
    force = "--force" in sys.argv
    now = dt.datetime.now(NY)
    log(f"运行时间（纽约）：{now:%Y-%m-%d %H:%M}")
    # 收盘后一段时间内数据仍在变化；早于 19:15 的运行不写当天数据
    if not force and now.weekday() < 5 and now.time() < dt.time(19, 15):
        prices = json.loads(F_PRICES.read_text())
        if prices["SPY"][-1][0] < now.date().isoformat():
            log("纽约时间未到 19:15，本次跳过（另一个定时会在 19:30 运行）")
            return
    missing: list[str] = []
    load_state()
    srcs = [F_PRICES, F_FG_JSON, F_BR_JSON, F_AAII_JSON, F_STATE]
    before = [p.read_bytes() if p.exists() else b"" for p in srcs]
    axis = update_prices(missing)
    update_cnn(axis, missing)
    update_breadth(axis, missing)
    update_aaii(missing)
    finish_state()
    if REVISIONS:
        log("修订：" + "；".join(REVISIONS))
    if STATE["pending"]:
        log(f"待确认差异 {len(STATE['pending'])} 处（未修改）")
    if [p.read_bytes() if p.exists() else b"" for p in srcs] == before and F_DASH.exists() and "--rebuild" not in sys.argv:
        log("没有任何新数据（周末、休市日，或今天已更新过），不改动文件")
        if missing:
            log("缺失：" + "；".join(missing))
        return
    build_outputs(missing)
    log("缺失：" + ("；".join(missing) if missing else "无"))


if __name__ == "__main__":
    main()
