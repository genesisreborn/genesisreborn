---
name: stockpulse-verify
description: Run, test and visually verify the StockPulse app (FastAPI + static HTML pages) in demo mode without network access. Use after changing anything under stockpulse/ — Python modules, API routes, or the chart / market / multi / index pages — and before committing.
---

# Verifying StockPulse changes

StockPulse is a FastAPI app (`stockpulse/web/app.py`) serving static pages from `stockpulse/web/static/`
(`index.html` news radar, `market.html` market overview, `chart.html` TradingView-style chart + strategies,
`multi.html` multi-chart grid; `drawings.js` is the chart's drawing layer). Demo mode generates deterministic
fake bars, fundamentals, options and calendars, so everything runs offline.

## 1. Tests

```bash
timeout 120 .venv/bin/python -m pytest -q -p no:cacheprovider
```

All tests must pass. Add a regression test for every bug you fix and every new endpoint; API tests use
`TestClient(create_app(settings, start_scheduler=False))` with `Settings()` set to `demo_mode=True`, a temp
`db_path`, and empty keys (see `tests/test_charting.py::settings`).

## 2. Demo server

Use a scratch DB so the real `data/` database is never touched, and track the PID (never `pkill -f`
with a pattern — it can kill your own shell):

```bash
S=<scratch dir>
DEMO_MODE=true DB_PATH=$S/demo.db nohup .venv/bin/python -m uvicorn stockpulse.web.app:create_app \
  --factory --port 8077 > $S/srv.log 2>&1 & echo $! > $S/srv.pid
# ... verify ...
kill $(cat $S/srv.pid)
```

## 3. Browser check (Playwright, preinstalled Chromium)

Do not run `playwright install`. Drive pages with node:

```js
const { chromium } = require('playwright-core');   // run with NODE_PATH=$(npm root -g)
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const p = await b.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
p.on('pageerror', e => errs.push(e.message));
p.on('dialog', d => d.accept());                      // prompts / confirms in the UI
await p.goto('http://localhost:8077/chart?symbol=NVDA&interval=1d');
```

- Collect `pageerror` and console errors; a 404 for `/favicon.ico` is expected, anything else is a bug.
- Panels such as 数据表 / 筛选器 / 基本面 / 期权链 / 日历 / 脚本 live inside the **工具 ▾** menu: click
  `#toolBtn` first, then the item id (`#tableBtn`, `#screenBtn`, `#fundBtn`, `#chainBtn`, `#calBtn`, `#scriptBtn`).
- Side tabs: `[data-tab="watch|strategy|signals|alerts|portfolio|account"]`.
- Drawing tools: `[data-tool="trend|hline|fib|rect|measure|text|long|short"]`, then click inside `#chartArea`.
- Take screenshots and look at them: layout regressions (overlapping labels, clipped panels) don't throw.
- Also check a phone viewport (390×844): `document.documentElement.scrollWidth` must equal 390.

## 4. Conventions worth keeping

- Every mutating route takes `_=require_token`; routes that expose holdings (`/api/portfolio*`, `/api/brief`)
  require it for reads too. Free-form JSON bodies get `@bad_input_is_400`.
- Never return NaN/inf in JSON; filter with `math.isfinite`.
- In the frontend, every API string goes through `esc()`, URLs through `safeUrl()`, and async loads check
  that the symbol/interval is still current before rendering (see `loadChart`'s `chartGen`).
- No look-ahead in strategy code: signals on bar close, fills at the next bar's open.
