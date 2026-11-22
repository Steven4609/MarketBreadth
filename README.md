# MarketBreadth · 市场宽度和情绪报告

标普 500 市场宽度（参与度）、SPY / QQQ / .VIX 行情、CNN 恐慌贪婪指数与 AAII 散户情绪的报告。
**每个交易日美东 19:30 自动取数更新**，不需要任何人操作。

- 报告网页：`https://<你的用户名>.github.io/MarketBreadth/`
- 数据文件：本仓库的 [`data/`](data/) 文件夹（CSV 可直接用 Excel 打开）

## 文件说明

| 文件 | 内容 |
|---|---|
| `index.html` | 报告网页 |
| `data/breadth_history_2y.csv` / `.json` | 每日 20 日 / 50 日参与度（`count` = 均线上方只数，`n` = 分母） |
| `data/cnn_fear_greed_2y.csv` / `.json` | 每日 CNN 恐慌贪婪分数与档位 |
| `data/overlay_prices.json` | SPY / QQQ / ^VIX 每日原始收盘价 |
| `data/details/breadth_YYYY-MM-DD.csv` | 当天每只成分股的收盘价、SMA20、SMA50、是否在均线上方 |
| `data/latest.json` | 最近一次更新的日期、数值、**缺失说明** |
| `data/dashboard_data.json` | 网页读取的合并数据（由脚本自动生成，不用手动改） |
| `scripts/update_daily.py` | 每日取数与计算脚本 |
| `.github/workflows/daily-update.yml` | 定时任务设置 |

## 口径

- **参与度** = 标普 500 成分股中，收盘价**严格高于**自身 20 日 / 50 日简单均线（含当日）的占比。
  只计历史足够长、且当天有收盘价的票。成分股用当天的 Wikipedia 名单，历史读数有幸存者偏差。
- **价格** = Yahoo Finance 原始收盘价（只复权拆股，不复权分红），与 Bloomberg / TradingView 同口径。
- **CNN** = CNN 官方端点；取不到时用 GitHub `whit3rabbit/fear-greed-data`。
- 取不到的数据**不插值、不补数**，写进 `data/latest.json` 的 `missing`。
  某天没取到的数据，之后的运行会在源头有数据时自动补上（最多回补 40 个交易日）。

## 日常怎么用

- **看更新有没有成功**：点仓库顶部的 **Actions** 标签，绿色对勾 = 成功，红叉 = 失败（点进去能看到原因）。
- **手动立即更新一次**：Actions → 左侧 **Daily update** → 右侧 **Run workflow**（勾选 force 可忽略 19:15 的时间限制）。
- **把数据下载到电脑**：点进任意 CSV → 右上角下载按钮；或仓库首页绿色 **Code** 按钮 → **Download ZIP**。
- 周末和美股休市日不会有新数据，这是正常的。
