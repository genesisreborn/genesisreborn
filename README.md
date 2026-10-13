# 📈 StockPulse 热门股雷达

从全网公开资讯（财经新闻 RSS、Reddit、StockTwits）抓取讨论，结合 Yahoo Finance 行情，
实时推测**热门上涨股**，在网页 Dashboard 展示，并通过 **Telegram** 每日推送简报和盘中异动提醒。
配置 Claude API Key 后，由 Claude 生成中文市场看法；未配置时使用内置规则引擎。

> ⚠️ 本项目仅用于信息聚合与研究，输出内容不构成任何投资建议。

## 工作原理

```
 资讯源 ──► 识别股票代码 ──► 情绪分析 ──► SQLite
 (RSS/Reddit/StockTwits)                   │
                                           ▼
 Yahoo 行情 ─► 价格/量能特征 ──► 热度评分(0-100) ──► Claude / 规则引擎看法
                                           │
                     ┌─────────────────────┼─────────────────────┐
                     ▼                     ▼                     ▼
               Web Dashboard        Telegram 每日简报      Telegram 异动提醒
```

每 `REFRESH_MINUTES`（默认 15 分钟）运行一轮：

1. **采集**：Yahoo Finance / CNBC / MarketWatch / Nasdaq / Seeking Alpha / Investing.com / Benzinga /
   Google News 的 RSS，关注列表个股的 Yahoo 新闻源，Reddit（wallstreetbets、stocks 等）热帖，StockTwits 热门榜。
2. **识别股票**：`$TSLA` 这类 cashtag、`(NASDAQ: NVDA)`、大写代码、公司名（"Nvidia" → NVDA）。
   `AI`、`IT`、`ON` 这类容易误判的词只有写成 `$AI` 才计入。
3. **情绪**：VADER + 金融词典（beat/surge/upgrade 为正，miss/plunge/probe 为负），分值 -1~1。
4. **评分**（满分 100，见 `stockpulse/scoring.py`）：

   | 分项 | 权重 | 含义 |
   |---|---|---|
   | 讨论热度 | 25 | 24h 提及数 + 社交互动量（候选股之间的百分位排名） |
   | 热度加速 | 20 | 24h 提及数 ÷ 过去 7 天日均提及数 |
   | 情绪 | 20 | 平均情绪；提及少的股票会向 0 收缩 |
   | 量能 | 15 | 当日成交量 ÷ 20 日均量 |
   | 价格动能 | 15 | 1 日 / 5 日涨跌幅，是否站上 20 日均线 |
   | 来源广度 | 5 | 有多少个不同来源在讨论 |

   信号分档：强势看涨 / 看涨 / 关注 / 中性 / 热度高·偏空 / 追高风险。
5. **看法**：前 15 名连同最新标题一起交给 Claude（`claude-opus-5-5`，结构化 JSON 输出），
   得到市场概览、热点主题和每只股票的观点、信心、催化剂、风险。

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 按需填写

# 先用离线样例数据看看效果（不联网，数据写入 data/demo.db）
python -m stockpulse --demo serve
# 打开 http://localhost:8000

# 正式运行：Dashboard + 定时刷新 + Telegram 机器人
python -m stockpulse serve
```

其他命令：

```bash
python -m stockpulse once      # 跑一轮分析，在终端打印热门榜
python -m stockpulse report    # 立即向 Telegram 订阅者推送最新简报
python -m pytest               # 运行测试
```

Docker：

```bash
cp .env.example .env && docker compose up -d --build
```

## 配置 Telegram

1. 在 Telegram 里找 **@BotFather**，发送 `/newbot` 创建机器人，把 token 填到 `.env` 的 `TELEGRAM_BOT_TOKEN`。
2. 启动服务后，在 Telegram 里给你的机器人发送 `/start` 即完成订阅（也可以把 chat id 写进 `TELEGRAM_CHAT_IDS`）。
3. 机器人命令：
   - `/top` 当前热门榜
   - `/stock NVDA` 个股详情（评分、看法、最新资讯）
   - `/stop` 取消订阅

推送规则：
- **每日简报**：`DAILY_REPORT_TIMES`（按 `TIMEZONE`，默认北京时间 08:30 和 21:00，分别在美股收盘后和开盘前）。
- **异动提醒**：某只股票进入前 `ALERT_TOP_N` 名且热度 ≥ `ALERT_MIN_SCORE` 时立即推送，同一只股票每天只提醒一次。

## 配置 Claude（可选）

在 `.env` 填写 `ANTHROPIC_API_KEY`。默认模型 `claude-opus-5-5`，可通过 `CLAUDE_MODEL` 修改。
请求启用了服务端 refusal fallback（`fallbacks: "default"`）；调用失败或被拒绝时自动回退到规则引擎，不影响其他功能。
每轮刷新调用一次，15 分钟一轮的话每天约 96 次；想省成本可以调大 `REFRESH_MINUTES`。

## Dashboard / API

| 路径 | 说明 |
|---|---|
| `/` | Dashboard：市场看法、热门榜（含走势迷你图）、关注列表、最新资讯；点击股票查看评分构成和热度走势。每 60 秒自动更新 |
| `GET /api/snapshot` | 最新一轮分析结果 |
| `GET /api/stock/{ticker}` | 个股详情、热度历史、近 3 天资讯 |
| `GET /api/news` | 最新资讯 |
| `GET /api/status` | 运行状态与下次任务时间 |
| `POST /api/refresh` | 立即触发一轮分析 |
| `POST /api/telegram/test` | 立即推送一次简报 |

## 项目结构

```
stockpulse/
  config.py         环境变量配置
  sources/          资讯源：rss.py、social.py（Reddit/StockTwits）、demo.py（离线样例）
  tickers.py        股票代码识别
  sentiment.py      情绪分析
  market.py         行情特征（yfinance）
  scoring.py        热度评分与信号
  insights.py       Claude / 规则引擎生成看法
  pipeline.py       一轮完整的分析流程
  telegram_bot.py   推送与机器人命令
  scheduler.py      定时任务
  web/              FastAPI + Dashboard
tests/              单元测试与端到端测试
```

## 扩展

- **加资讯源**：在 `sources/rss.py` 的 `GENERAL_FEEDS` 里加 RSS 地址，或参照 `social.py` 写一个返回 `RawItem` 列表的函数。
- **加股票**：`tickers.py` 的 `UNIVERSE`（代码 → 公司名别名），或在 `.env` 的 `WATCHLIST` 里加。
- **调权重**：`scoring.py` 的 `WEIGHTS`。
- **港股/A 股**：yfinance 支持 `0700.HK`、`600519.SS` 这类代码，但需要另外接入中文资讯源和中文代码识别。

## 局限

- 热度 ≠ 涨幅。社交媒体热度很容易被炒作带动，评分只代表“正在被关注且舆论偏正面”，不预测价格。
- 免费数据源有频率限制，偶尔会失败；单个源失败不影响整轮分析。
- Yahoo 行情有延迟，不适合日内交易。
