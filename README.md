# 📈 StockPulse 热门股雷达 + 图表策略平台

从全网公开资讯（财经新闻 RSS、Reddit、StockTwits）抓取讨论，结合 Yahoo Finance 行情，
实时推测**热门上涨股**，在网页 Dashboard 展示，并通过 **Telegram** 推送每日 AI 报告和盘中异动提醒。

**Claude 只在生成报告时调用一次。** 新闻抓取、行情和热度评分每 15 分钟自动刷新，这部分完全免费，不调用 Claude。
到了每天的报告时间（或你手动调 API），系统才把当时的热门股数据交给 Claude 分析一次，生成完整报告并推送。

> ⚠️ 本项目仅用于信息聚合与研究，输出内容不构成任何投资建议。

系统有两个页面：

- **热门雷达**（`/`）：从新闻和社交媒体找出热门股，每天生成 AI 报告。
- **图表 · 策略**（`/chart`）：类似 TradingView 的 K 线图表，支持技术指标、自定义策略、回测，并按你的策略自动给出买卖信号推送到 Telegram。

## 图表 · 策略平台

打开 `http://localhost:8000/chart`。

**图表**
- K 线 + 成交量，周期 1 分 / 5 分 / 15 分 / 1 小时 / 日 / 周。图表库是 TradingView 开源的 [Lightweight Charts](https://github.com/tradingview/lightweight-charts)（已打包进项目，不依赖外网 CDN）。
- 图表类型：蜡烛图、空心蜡烛、美国线、折线、面积、平均 K 线（Heikin Ashi）；价格轴可切换普通 / 对数 / 百分比。
- **指标管理**（“指标”菜单）：同一指标可以加多个、各自设参数和颜色（例如 EMA 20 + EMA 50 + EMA 200），随时删除；设置会记在浏览器里。
  - 主图：EMA、SMA、WMA、布林带、肯特纳通道、VWAP、SuperTrend、唐奇安通道、一目均衡表、抛物线 SAR。
  - 副图：RSI、MACD、KD、Stoch RSI、ATR、ADX/DMI、CCI、威廉 %R、MFI、ROC、OBV；成交量均线叠加在量柱上。
- **画线工具**（图表左侧工具栏）：趋势线、射线、延长线、水平线、垂直线、矩形、斐波那契回撤、文字标注、测量（涨跌幅 + K 线数）。
  - 点选后可拖动整条线或端点，可改颜色 / 粗细 / 虚线、锁定、删除；磁铁模式吸附到 OHLC；一键隐藏 / 清空；“图形列表”管理全部画线。
  - 画线按代码保存在服务器（SQLite），换设备打开也在。
  - 水平线可一键变成**价格提醒**（🔔），触发后走 Telegram。
  - 快捷键：`Alt+T` 趋势线、`Alt+H` 水平线、`Alt+V` 垂直线、`Alt+F` 斐波那契、`Esc` 取消、`Delete` 删除选中。
- **对比**：叠加其他代码（例如 SPY、QQQ），自动切到百分比坐标比较走势。
- **K 线回放**：从任意位置开始逐根 / 自动播放（1x–10x），用来练习看盘和人工验证策略，回放中未来数据不可见。
- **数据表**：把当前图表的 OHLCV 和所有指标值列成表，可导出 CSV（Excel 直接打开不乱码）。
- **股票筛选器**：对自选 + 热门股（或你填的代码）计算价格、涨跌、量比、1 周 ~ 1 年表现、RSI、ADX、ATR%、距 50/200 日线、距 52 周高低点；表头点击排序，可用规则语法过滤（例如 `rsi(close, 14) < 35 and close > sma(close, 200)`），点行打开图表。
- **指标脚本编辑器**（工具 → 指标脚本编辑器）：类似 Pine Script 写自己的指标，保存后像内置指标一样加到图上，可叠加主图或放副图；单独一行的数字（如 `length = 20`）自动变成可在“指标”菜单里调的参数。

  ```
  length = 20
  basis = sma(close, length)
  dev = 2 * stdev(close, length)
  plot(basis, "中轨", "#2962ff")
  plot(basis + dev, "上轨")
  plotshape(crossover(close, basis + dev), "突破", "up")
  ```
  语句：`变量 = 表达式`、`plot(值, "标题", "#颜色", "line|histogram|dots|area")`、`hline(值, "标题")`、`plotshape(条件, "标题", "up|down")`。表达式和策略规则同一套（含 `ref_close("SPY")`、`close[1]`、`a if 条件 else b`），内置 4 个示例。
- **期权链**：按到期日列出认购 / 认沽的最新价、买卖价、涨跌、隐含波动率、成交量、持仓量，实值高亮、平值分隔；汇总 Put/Call 比（成交、持仓）、最大痛点、平值 IV。缓存 15 分钟。
- **财经日历**：按周查看经济数据（CPI、非农、利率决议等，实际 / 预期 / 前值）和财报日历（盘前 / 盘后、预期与实际 EPS），自选股高亮，点击跳到图表。
- **基本面**：公司简介、估值（市值、市盈率、市销率、市净率、EV/EBITDA、PEG）、盈利能力（毛利率、净利率、ROE）、财务健康、股息、做空比例、机构持股、分析师目标价和评级；季度 / 年度利润表、资产负债表、现金流量表（带同比增速和柱状图）；历次财报的预期 vs 实际 EPS；热门雷达收录的该股新闻。数据来自 Yahoo Finance，缓存 12 小时。
- **事件标记**：K 线下方标出财报 (E)、分红 (D)、拆股 (S)，可用“事件”按钮开关。
- **多图布局**（`/multi`）：1 / 2 / 3 / 4 / 6 / 8 图，每个格子独立选代码和周期；可同步十字光标、周期、时间范围；布局记在浏览器里，点 ↗ 打开完整图表。
- 十字光标显示 OHLC、涨跌幅、成交量和所有指标值；数据每 30 秒自动刷新，K 线未收盘时会标“K 线进行中”。
- 自选列表：美股、港股（`0700.HK`）、A 股（`600519.SS`）、加密货币（`BTC-USD`，仅 Yahoo 数据源）。

**我的持仓 + 交易日志**（右侧「持仓」页）
- 记录你在任何券商的买入 / 卖出 / 分红（时间、价格、手续费），按平均成本算持仓、浮动和已实现盈亏、今日盈亏、仓位占比。
- 持仓市值曲线，对比“同样的买卖金额投到 SPY”会怎样，一眼看出自己选股是否跑赢大盘。
- 每笔交易可写日志和标签（为什么买、计划、复盘），可导出 CSV；你的买卖点会标在 K 线上（紫色“我买 / 我卖”）。
- 卖出数量超过当时持仓会被拒绝；持仓数据需要 API_TOKEN 才能查看（设置了的话）。

**做多 / 做空仓位工具**（画线工具栏 ⬆ ⬇，快捷键 Alt+L / Alt+S）
- 在图上点一下：自动生成开仓、止损（默认 2%）、目标（默认 2R）三条线，可拖动调整。
- 实时显示涨跌幅、盈亏比，以及按“资金 × 风险%”算出的股数、金额、潜在盈亏（资金和风险% 在属性栏里改，浏览器记住）。

**市场概览**（`/market`）
- 主要指数（标普、纳指、道指、罗素、VIX、美债、黄金、比特币）、11 个行业 ETF 热力图、自选 + 热门股热力图，可切换今日 / 1 周 / 1 月 / 3 月 / 年初至今 / 1 年。
- 市场宽度：涨跌家数、站上 50 / 200 日线比例、接近 52 周新高 / 新低、RSI 超买超卖数量；涨幅榜 / 跌幅榜。

**盘前简报**（Telegram）
- 工作日 `BRIEF_TIMES`（默认 20:45，按 `TIMEZONE`）发给 `TELEGRAM_CHAT_IDS`：大盘、我的持仓、自选异动、今明两天自选 / 持仓的财报、当天重要美国经济数据、近期策略信号。
- 随时在 Telegram 发 `/brief` 查看，`/portfolio` 查看持仓。含持仓信息，只发给管理员，不发给普通订阅者。

**行情数据**
- 默认 Yahoo Finance（免费，可能有延迟；1 分钟线只有最近 7 天，5/15 分钟线最近 60 天，1 小时线最近 2 年）。
- 填 `MASSIVE_API_KEY`（Massive，原 Polygon.io）后改用 Massive 行情，实时性取决于你的订阅档位。
- K 线缓存在 SQLite，之后只增量拉取。

**策略**

规则写法类似 Pine Script，每条规则是一个真/假表达式：

```
做多开仓：crossover(ema(close, fast), ema(close, slow)) and rsi(close, 14) < 70
做多平仓：crossunder(ema(close, fast), ema(close, slow))
参数：    fast=12, slow=26
```

- 行情变量：`open high low close volume hl2 hlc3 ohlc4`；`close[1]` 是前一根 K 线的值。
- 函数：`stdev sum cum nz barssince valuewhen log sqrt pow round sma ema wma rsi macd macd_signal macd_hist bb_upper bb_mid bb_lower stoch_k stoch_d atr adx plus_di minus_di cci willr mfi roc psar kc_upper kc_lower vwap supertrend supertrend_dir obv highest lowest crossover crossunder prev change rising falling abs max min`。
- 运算：`and or not`、比较、加减乘除。表达式在服务器上用白名单解析，不能执行任意代码。
- **引用其他代码**（同周期）：`ref_close("SPY")`、`ref_high/ref_low/ref_open/ref_volume`。最常用的是大盘过滤：`ref_close("SPY") > sma(ref_close("SPY"), 200)`，只在大盘处于长期上升趋势时开仓。
- **按风险定仓位**：填“单笔风险 %”（例如 1），系统按“碰到止损约亏账户 1%”自动算股数（需要设止损），“每笔占账户 %”变成仓位上限。回测、模拟账户和券商下单都按这个算。
- 风控：止损 %、止盈 %、ATR 倍数止损；可选做空规则（多空反手）。
- 内置 9 个模板可直接改：均线金叉、RSI 超卖反弹、MACD 趋势、唐奇安突破（海龟）、海龟突破 + 大盘过滤、RSI(2) 回调买入 + 大盘过滤、布林带放量突破、SuperTrend 多空、强势股回踩。

**稳健性验证报告**

```bash
python -m stockpulse validate                                  # 默认 4 个策略 × 14 只大盘股/ETF
python -m stockpulse validate --symbols SPY,QQQ,AAPL --strategies donchian_breakout,rsi2_reversion
```

对每个策略 × 每个代码回测全部历史，并单独统计最后 30% 的样本外表现；汇总盈利代码占比、跑赢买入持有占比、回撤是否更小。报告保存到 `data/reports/validation_日期.md`。为了公平比较信号质量，报告里每笔都按全仓计算。

**回测**
- 在当前图表的代码和周期上回测，结果显示在图表下方：总收益、买入持有对比、年化、最大回撤、夏普、索提诺、胜率、盈亏比、交易明细、净值曲线；买卖点标在 K 线上。
- 撮合规则（避免“偷看未来”）：每根 K 线**收盘**计算信号，**下一根 K 线开盘**成交；止损/止盈按当根最高最低价判断，跳空穿过止损价则按开盘价成交；默认每边手续费 5bp + 滑点 5bp（`COMMISSION_BPS` / `SLIPPAGE_BPS`）。

**实时信号**
- 保存策略时填上要监控的代码，勾选“启用实时信号”。
- 系统每 `SIGNAL_REFRESH_MINUTES`（默认 5 分钟）拉取新 K 线，用同一套回测逻辑跑一遍：最新一根**已收盘** K 线产生的买卖指令就是信号，同时检查止损/止盈是否被触发。
- 新信号写入数据库（同一根 K 线不会重复）、显示在「信号」页、推送到 Telegram（含收盘价、参考止损/止盈）。Telegram 里发 `/signals` 查看最近信号。
- 「信号」页还列出每个策略 × 代码当前的模拟持仓、开仓价、浮盈和止损位。

**策略扫描器**
- 在「策略」页点“扫描：谁现在有信号”，用当前编辑的策略扫一遍自选股和热门雷达里的股票（最多 60 个）。
- 结果按“下一根 K 线要开仓的 → 持仓中的 → 回测收益”排序；每行有当前状态、待执行信号和参考止损、回测收益/买入持有/最大回撤/胜率。
- 顶部汇总：多少代码盈利、多少跑赢买入持有、回撤中位数。只在一两只股票上赚钱的策略，在这里一眼就能看出来。

**条件提醒（类似 TradingView Alert）**
- 「提醒」页：价格上穿/下穿、收盘高于/低于、RSI 高于/低于、单根涨跌幅、成交量放大，或用规则语法写任意条件。
- 可选“只提醒一次”（触发后自动关闭）或“每根 K 线都提醒”。价格类提醒在图表上画虚线。
- 与策略信号一起每 `SIGNAL_REFRESH_MINUTES` 检查一次，按 K 线收盘判断；触发后推送 Telegram，`/alerts` 查看生效中的提醒。

**参数优化（走前验证）**
- 「策略」页 →「参数优化」：每行写一个参数的范围，如 `fast=5:30:5`、`slow=20:100:10`（最多 300 组）。
- 两种方法：
  - **走前验证（推荐）**：用前一段数据选出最优参数，在紧接着的下一段数据上实盘般交易，再往后滚动，共 N 折。把每一折的“下一段”拼起来，就是不含偷看的真实表现。
  - **训练/测试切分**：前 70% 选参数，后 30% 检验。
- 结果给出样本外收益、同期买入持有、盈利窗口数、参数稳定性；两个参数时显示热力图，看最优点周围是不是一片都好（稳健）还是孤立的尖峰（过拟合）。
- 自动提示过拟合迹象：样本外夏普远低于样本内、每折选出的参数都不同、跑不赢买入持有等。
- “把推荐参数填进策略”一键回填，回测确认后再保存。

**模拟账户**
- 把策略的“信号出现时”设为“同时在模拟账户下单”并启用实时信号。
- 信号在下一根 K 线开盘成交（含手续费、滑点），按策略的止损/止盈自动平仓；每笔占账户净值的比例可设（默认 10%）。
- 「账户」页：净值、收益、现金、已实现/浮动盈亏、胜率、持仓、平仓记录、订单、每日净值曲线；可重置初始资金。
- 成交和平仓推送到 Telegram（只发给 `TELEGRAM_CHAT_IDS` 里的管理员）；Telegram 里 `/account` 查看账户。

**盘中实时提醒**
- 价格类提醒（上穿/下穿/高于/低于）可勾选“⚡ 盘中实时”：价格一触及就推送，不等 K 线收盘。
- 有 `MASSIVE_API_KEY` 时用 Massive WebSocket 秒级推送；否则每 `REALTIME_POLL_SECONDS` 秒轮询一次最新价（Yahoo 或 Massive REST）。
- “每根 K 线都提醒”的实时提醒触发后 15 分钟内不重复。

**券商自动下单（Alpaca，可选，默认关闭）**

把策略的“信号出现时”设为“⚠ 券商自动下单（Alpaca）”并启用实时信号后，信号会自动发到你的 Alpaca 账户。

开启步骤：
1. 在 [Alpaca](https://alpaca.markets) 注册，先在 **Paper Trading**（模拟盘）里生成 API Key。
2. `.env` 填 `BROKER=alpaca`、`ALPACA_KEY_ID`、`ALPACA_SECRET_KEY`，保持 `ALPACA_PAPER=true`，并确认已设置 `API_TOKEN`，然后重启。
3. 「账户」页的“券商自动交易”区块显示“运行中”即可。先在模拟盘跑一段时间。
4. 真要用真钱：换成实盘 Key，设 `ALPACA_PAPER=false`，**并且**设 `LIVE_TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY`。少了最后这一行，实盘模式会拒绝所有下单。

保护措施（全部通过才会下单）：
- 只交易策略里列出的代码；单笔金额 ≤ `MAX_ORDER_NOTIONAL`（默认 $1000，再乘以“每笔占账户 %”取较小值）；每天开仓 ≤ `MAX_DAILY_ORDERS`；同时持仓 ≤ `MAX_OPEN_POSITIONS`（未成交的开仓单也算）；默认不做空。
- 必须设置 `API_TOKEN`，否则自动交易保持关闭（防止别人通过网页改策略）。
- 同一个信号只会下一次单（本地记录 + 券商端 `client_order_id` 双重去重）。
- 开仓（多或空）时如果策略有止损，会一起挂一张券商端止损单（OTO），即使本程序停了也有保护；策略的止损信号交给这张止损单处理，其他平仓信号会先撤掉它再平仓。
- 一键暂停：网页「账户」页“暂停”，或 Telegram 发 `/pause`（`/resume` 恢复，仅 `TELEGRAM_CHAT_IDS` 里的私聊可用；群聊里还需要发送者在 `TELEGRAM_ADMIN_IDS` 里）。暂停只阻止新开仓，平仓信号照常执行，避免策略已平仓而账户还持有。
- 紧急清仓：网页“紧急清仓”（需输入 FLATTEN），撤销所有挂单、平掉全部持仓并暂停。
- 每笔下单/拒绝/失败都记录在「账户」页，并推送给管理员。

注意：
- 市价单在盘外提交时会排到下一个交易时段开盘执行；日线策略的信号在美东 16:15 后产生，所以通常是次日开盘成交，和回测假设一致。
- 平仓（SELL/COVER）会平掉该代码在 Alpaca 账户里的**全部**持仓。同一个代码不要同时被多个券商策略交易，也不要和手动交易混用。
- 程序只根据你设定的规则下单，不保证盈利。实盘前请先用模拟账户和 Alpaca 模拟盘验证。

## 工作原理（热门雷达）

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
| `/chart` | 图表 · 策略页面 |
| `/multi` | 多图布局 |
| `/market` | 市场概览 |
| `GET /api/portfolio`、`POST /api/portfolio/tx`、`PUT/DELETE /api/portfolio/tx/{id}` | 持仓与交易日志（需 token） |
| `GET /api/market` | 指数、板块、市场宽度、涨跌榜 |
| `GET /api/brief`、`POST /api/brief/send` | 盘前简报（需 token） |
| `GET /api/fundamentals?symbol=AAPL` | 基本面、三张报表、财报 / 分红事件、相关新闻 |
| `GET /api/options?symbol=AAPL&expiry=2026-11-20` | 期权链 + Put/Call 比、最大痛点 |
| `GET /api/calendar?start=2026-10-19&days=7` | 经济数据和财报日历 |
| `GET/POST /api/scripts`、`DELETE /api/scripts/{id}` | 指标脚本（保存需 token） |
| `GET /api/chart?symbol=AAPL&interval=1d&indicators=ema:20,rsi:14,macd` | K 线 + 指标数据 |
| `GET /api/strategy/meta` | 内置模板、可用函数、周期 |
| `GET/POST /api/strategies`、`DELETE /api/strategies/{id}` | 策略列表 / 保存 / 删除 |
| `POST /api/strategies/validate` | 检查策略规则是否有效 |
| `POST /api/backtest` | 回测：`{"strategy": {...} 或 "strategy_id": 1, "symbol": "AAPL", "interval": "1d"}` |
| `GET /api/signals`、`GET /api/positions`、`POST /api/signals/run` | 信号列表 / 模拟持仓 / 立即检查 |
| `GET/PUT /api/watchlist` | 自选列表 |
| `POST /api/scan` | 策略扫描：`{"strategy": {...}, "universe": "both"/"watchlist"/"hot"}` 或指定 `"symbols"` |
| `GET/POST /api/alerts`、`DELETE /api/alerts/{id}`、`POST /api/alerts/run` | 条件提醒 |
| `POST /api/optimize` | 参数优化：`{"strategy": {...}, "symbol", "grid": {"fast": "5:30:5"}, "mode": "walk_forward"/"split", "objective"}` |
| `GET /api/paper`、`POST /api/paper/reset`、`POST /api/paper/process` | 模拟账户 |
| `GET /api/realtime` | 盘中实时监控状态 |
| `GET /api/broker`、`POST /api/broker/pause`、`POST /api/broker/flatten` | 券商状态 / 暂停或恢复 / 紧急清仓（需 `{"confirm": "FLATTEN"}`） |

设置 `API_TOKEN` 后，所有写操作（生成报告、保存/删除策略、修改自选、立即检查）都要带 `Authorization: Bearer <token>`；网页第一次遇到时会提示输入并记住。

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
  market_data.py    K 线数据（Yahoo / Massive）、缓存、判断 K 线是否收盘
  indicators.py     技术指标
  strategy/         策略：dsl.py 规则语言、presets.py 模板、backtest.py 回测、signals.py 实时信号、
                    scanner.py 多代码扫描、alerts.py 条件提醒、optimize.py 参数优化、paper.py 模拟账户
  realtime.py       盘中实时价格提醒（WebSocket / 轮询）
  broker.py         Alpaca 下单与风控闸门
  telegram_bot.py   推送与机器人命令
  scheduler.py      定时任务
  web/              FastAPI；static/index.html 热门雷达，static/chart.html 图表策略页，static/multi.html 多图，trading.py 图表/策略 API
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

- 回测结果不代表未来收益。参数在一段历史上调得越“好”，实盘越可能失效；建议在多个代码、多段时间上验证。
- 实时信号以 K 线收盘为准：日线信号在美东 16:15 之后产生，止损在盘中被触发时，要等那根 K 线收盘才推送。
- Yahoo 数据有延迟且偶尔缺失；需要更及时的数据请用 Massive。
- 热度 ≠ 涨幅。社交媒体热度很容易被炒作带动，评分只代表“正在被关注且舆论偏正面”，不预测价格。
- 免费数据源有频率限制，偶尔会失败；单个源失败不影响整轮分析。
- Yahoo 行情有延迟，不适合日内交易。
- 和 TradingView 相比没有：社区观点、公开脚本库这类社交功能。指标脚本是 Pine Script 的子集：没有循环、数组、`var` 状态变量、自定义函数和 `strategy.*` 下单语句（下单逻辑用策略规则写）。
