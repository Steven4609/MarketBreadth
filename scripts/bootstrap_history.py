"""一次性生成报告的初始历史数据（2024-11-22 至 2026-11-20）

只在从零搭建（或彻底重建）时运行一次；之后每天由 update_daily.py 追加。
口径与每日更新完全相同：
  - 价格：Yahoo 原始收盘价（auto_adjust=False 的 Close），保留两位小数
  - 参与度：收盘价严格高于自身 SMA20 / SMA50（含当日）的占比；
            每只票用它自己的 K 线序列，满 20 / 50 根才计入分母；只统计 SPY 交易日
  - 成分股：固定使用 scripts/sp500_members_2026-11-20.txt（2026-11-20 的 Wikipedia 名单，503 只），
            保证任何时候重跑都得到同一份历史
  - CNN：GitHub whit3rabbit/fear-greed-data（由 CNN 官方端点每日重建），保留两位小数
  - AAII：AAII 官网 sentiment.xls，只保留 2024-10-31 起的数据
用法：python scripts/bootstrap_history.py
"""
from __future__ import annotations

import csv
import io
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_daily as U  # noqa: E402  复用下载、评级与输出函数

START, END = "2024-11-22", "2026-11-20"
PRICE_FROM = "2024-11-01"     # 价格多拉三周余量
BREADTH_FROM = "2024-09-01"   # 让 2024-11-22 当天就有满 50 根 K 线
AAII_FROM = "2024-10-31"
MEMBERS = Path(__file__).resolve().parent / "sp500_members_2026-11-20.txt"
END_EXCL = "2026-11-21"


def jsround(v) -> float | int:
    x = math.floor(float(v) * 100 + 0.5) / 100
    return int(x) if x == int(x) else x  # 整数价写成 597 而不是 597.0，与最初文件一致


def prices() -> dict:
    df = U.download(["SPY", "QQQ", "^VIX"], start=PRICE_FROM, end=END_EXCL)
    out = {}
    for t in ["SPY", "QQQ", "^VIX"]:
        s = U.closes(df, t)
        # 与最初浏览器计算一致：四舍五入到分（Math.round 规则）
        out[t] = [[d, jsround(v)] for d, v in s.items() if START <= d <= END]
        U.log(f"{t}: {len(out[t])} 行")
    return out


def breadth(axis: list[str]) -> list[list]:
    syms = MEMBERS.read_text().split()
    series: dict[str, pd.Series] = {}
    todo = list(syms)
    for rnd in range(3):
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            df = U.download(batch, start=BREADTH_FROM, end=END_EXCL)
            for s in batch:
                c = U.closes(df, s) if not df.empty else pd.Series(dtype=float)
                if not c.empty:
                    series[s] = c
            time.sleep(2)
        todo = [s for s in syms if s not in series]
        U.log(f"参与度：第 {rnd + 1} 轮后取到 {len(series)}/{len(syms)} 只")
        if not todo:
            break
    if todo:
        U.log(f"参与度：未取到 {todo}")
    axis_set = set(axis)
    cnt = {d: [0, 0, 0, 0] for d in axis}
    for s, c in series.items():
        vals, dates = c.values, list(c.index)
        s20 = s50 = 0.0
        for i, (d, v) in enumerate(zip(dates, vals)):
            s20 += v; s50 += v
            if i >= 20: s20 -= vals[i - 20]
            if i >= 50: s50 -= vals[i - 50]
            if d < START or d not in axis_set:
                continue
            if i >= 19:
                cnt[d][1] += 1
                if v > s20 / 20: cnt[d][0] += 1
            if i >= 49:
                cnt[d][3] += 1
                if v > s50 / 50: cnt[d][2] += 1
    return [[d, *cnt[d]] for d in axis]


def cnn() -> list[dict]:
    r = requests.get("https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/main/fear-greed.csv",
                     headers=U.UA, timeout=60)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text))
    d.columns = ["date", "score", "rating"]
    d = d[(d.date >= START) & (d.date <= END)].drop_duplicates("date")
    d["score"] = d.score.round(2)
    return [{"date": a, "score": float(b), "rating": U.fg_rating(float(b))} for a, b in zip(d.date, d.score)]


def aaii() -> list[tuple]:
    return [r for r in U.aaii_source() if AAII_FROM <= r[0] <= END]


def main():
    U.DATA.mkdir(exist_ok=True)
    px = prices()
    axis = [d for d, _ in px["SPY"]]
    json.dump(px, U.F_PRICES.open("w"))

    rows = breadth(axis)
    with U.F_BR_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "pct20", "pct50", "count20", "n20", "count50", "n50"])
        for d, a20, n20, a50, n50 in rows:
            w.writerow([d, round(a20 / n20 * 100, 2), round(a50 / n50 * 100, 2), a20, n20, a50, n50])
    json.dump([{"date": d, "p20": round(a20 / n20 * 100, 2), "p50": round(a50 / n50 * 100, 2)}
               for d, a20, n20, a50, n50 in rows], U.F_BR_JSON.open("w"))
    U.log(f"参与度：{len(rows)} 天，首日 {rows[0]}，末日 {rows[-1]}")

    fg = cnn()
    json.dump(fg, U.F_FG_JSON.open("w"))
    with U.F_FG_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "score", "rating"])
        for r in fg:
            w.writerow([r["date"], r["score"], r["rating"]])
    U.log(f"CNN：{len(fg)} 天，首日 {fg[0]}，末日 {fg[-1]}")

    aa = aaii()
    with U.F_AAII_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "bullish", "neutral", "bearish", "spread"])
        for d, b, n, s in aa:
            w.writerow([d, b, n, s, round(b - s, 1)])
    json.dump([{"date": d, "bullish": b, "neutral": n, "bearish": s} for d, b, n, s in aa], U.F_AAII_JSON.open("w"))
    U.log(f"AAII：{len(aa)} 周，首周 {aa[0]}，末周 {aa[-1]}")

    U.build_outputs([])
    U.log("完成")


if __name__ == "__main__":
    main()
