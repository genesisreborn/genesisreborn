# 📈 StockPulse 热门股雷达

从全网公开资讯（财经新闻 RSS、Reddit、StockTwits）抓取讨论，结合 Yahoo Finance 行情，
实时推测**热门上涨股**，在网页 Dashboard 展示，并通过 **Telegram** 推送每日 AI 报告和盘中异动提醒。

**Claude 只在生成报告时调用一次。** 新闻抓取、行情和热度评分每 15 分钟自动刷新，这部分完全免费，不调用 Claude。
到了每天的报告时间（或你手动调 API），系统才把当时的热门股数据交给 Claude 分析一次，生成完整报告并推送。

> ⚠️ 本项目仅用于信息聚合与研究，输出内容不构成任何投资建议。

## 工作原理

```
每 15 分钟（免费）：
 资讯源 ──► 识别股票代码 ──► 情绪分析 ──► 行情特征 ──► 热度评分 ──► 规则引擎摘要
                                                                     │
                                                    Web Dashboard 实时榜 + Telegram 异动提醒

每天报告时间 / POST /api/report（调用 Claude 一次）：
 最新数据 ──► Claude 分析 ──► 报告（存数据库 + Markdown 文件）──► Telegram 推送 + Dashboard 展示
```

每轮刷新的步骤：

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
5. **实时摘要**：规则引擎根据上面的数据写一句话看法（不花钱）。

生成报告时，前 15 名连同最新标题一起交给 Claude（`claude-opus-5-5`，结构化 JSON 输出），得到：
标题、市场概览、热点主题、重点关注股票、每只股票的观点/信心/催化剂/风险、后市关注、风险提示。

## 一键安装（推荐）

```bash
git clone <你的仓库地址> stockpulse && cd stockpulse
bash setup.sh
```

脚本会：创建虚拟环境并安装依赖 → 引导你填 Telegram token、Claude API Key、时区、报告时间、关注列表 →
生成 `REPORT_API_TOKEN` → 跑一遍自检 → 验证 Telegram token → （Linux 服务器上）安装为开机自启的 systemd 服务。
装好后在 Telegram 里给你的机器人发 `/start` 就能收到报告。

## 手动安装

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
python -m stockpulse once              # 跑一轮免费分析（不调用 Claude），打印热门榜
python -m stockpulse report            # 抓最新数据 + 调用 Claude 一次 → 生成报告并推送 Telegram
python -m stockpulse report --no-send  # 只生成并打印报告，不推送
python -m pytest               # 运行测试
```

不想常驻服务也可以：用 crontab 定时执行 `python -m stockpulse report`，每次运行就是“抓数据 → Claude 分析一次 → 推送报告”。

Docker：

```bash
cp .env.example .env && docker compose up -d --build
```

## 配置 Telegram

1. 在 Telegram 里找 **@BotFather**，发送 `/newbot` 创建机器人，把 token 填到 `.env` 的 `TELEGRAM_BOT_TOKEN`。
2. 启动服务后，在 Telegram 里给你的机器人发送 `/start` 即完成订阅（也可以把 chat id 写进 `TELEGRAM_CHAT_IDS`）。
3. 机器人命令：
   - `/report` 最新一份 AI 报告（读取已生成的报告，不会触发新的 Claude 调用）
   - `/top` 实时热门榜
   - `/stock NVDA` 个股详情（评分、看法、最新资讯）
   - `/stop` 取消订阅

推送规则：
- **每日 AI 报告**：`DAILY_REPORT_TIMES`（按 `TIMEZONE`，默认北京时间 08:30 和 21:00，分别在美股收盘后和开盘前），每个时间点调用 Claude 一次。
- **异动提醒**：某只股票进入前 `ALERT_TOP_N` 名且热度 ≥ `ALERT_MIN_SCORE` 时立即推送，同一只股票每天只提醒一次。

## 配置 Claude（可选）

在 `.env` 填写 `ANTHROPIC_API_KEY`。默认模型 `claude-opus-5-5`，可通过 `CLAUDE_MODEL` 修改。

- **调用次数**：只在生成报告时调用。默认每天 2 个报告时间点，就是每天 2 次；手动生成另算。
- **防止重复调用**：手动生成（API 或 Dashboard 按钮）有 `REPORT_COOLDOWN_MINUTES`（默认 30 分钟）冷却，
  冷却期内再请求会返回 429 和上一份报告的编号。定时报告不受冷却限制。
- **接口保护**：设置 `REPORT_API_TOKEN` 后，`POST /api/report` 必须带 `Authorization: Bearer <token>`。服务放在公网时一定要设置。
- 请求启用了服务端 refusal fallback（`fallbacks: "default"`）；调用失败或被拒绝时自动改用规则引擎出报告。

## Dashboard / API

| 路径 | 说明 |
|---|---|
| `/` | Dashboard：AI 报告（可切换历史报告、下载 Markdown、手动生成）、实时热门榜、关注列表、最新资讯；点击股票查看评分构成和热度走势。每 60 秒自动更新 |
| `POST /api/report` | 抓最新数据并调用 Claude 一次，返回报告。参数：`notify=true` 同时推送 Telegram；`force=true` 跳过冷却 |
| `GET /api/report/latest` | 最新一份报告 |
| `GET /api/report/{id}` | 指定报告；加 `?format=md` 返回 Markdown |
| `GET /api/reports` | 历史报告列表 |
| `GET /api/snapshot` | 最新一轮分析结果 |
| `GET /api/stock/{ticker}` | 个股详情、热度历史、近 3 天资讯 |
| `GET /api/news` | 最新资讯 |
| `GET /api/status` | 运行状态与下次任务时间 |
| `POST /api/refresh` | 立即触发一轮分析 |
| `POST /api/telegram/test` | 把最新一份报告推送到 Telegram（不调用 Claude） |

调用示例：

```bash
curl -X POST "http://localhost:8000/api/report?notify=true" -H "Authorization: Bearer $REPORT_API_TOKEN"
```

报告同时保存为 Markdown 文件，在 `data/reports/` 目录。

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
  report.py         报告生成（调用 Claude 一次）、冷却、Markdown 输出
  telegram_bot.py   推送与机器人命令
  scheduler.py      定时任务
  web/              FastAPI + Dashboard
tests/              单元测试与端到端测试
setup.sh            一键安装脚本
deploy/             systemd 服务模板
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
