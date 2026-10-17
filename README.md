# Installment & Expense Telegram Bot

A serverless Telegram bot (Cloudflare Workers + D1, free tier) that tracks your monthly
installments/loans — how much to pay, what remains, how much of it is interest — reminds you
before every due date, and logs daily expenses.

## Features
- **Loans**: reducing-balance (default) or flat-rate interest; 0% (BNPL / phone plans) supported.
- Per loan: monthly amount, total interest, interest paid vs left, remaining principal, next due date, upcoming schedule.
- **Reminders** (daily cron): 3 days before, 1 day before, on the day, then daily while overdue — each with a one-tap "✅ Mark paid" button.
- **Expenses**: type `12.5 lunch` to log; `/today`, `/month` (by category), `/budget`, `/undo`.

## Commands
| Command | Purpose |
|---|---|
| `/addloan <name> <amount> <rate%> <months> <dueDay> [flat] [paid=N]` | e.g. `/addloan Car 50000 3.5 60 15`, `/addloan Phone 3600 0 12 5 paid=4` |
| `/loans`, `/loan <id>`, `/schedule <id>` | overview, details, next 12 payments |
| `/pay <id>`, `/unpay <id>`, `/delloan <id>` | manage |
| `12.5 food lunch` or `/exp ...` | log an expense |
| `/today`, `/month`, `/undo`, `/budget <amt>` | expense reports |

Use `_` for spaces in loan names. Check `/loans` against your bank statement — banks round slightly
differently, and extra/early repayments are not modelled (use `/pay` as each installment is made).

## Deploy (~10 minutes)
1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token.
2. `npm install`
3. `npx wrangler d1 create installment-bot` → paste the `database_id` into `wrangler.toml`.
4. `npm run db:init`
5. Edit `wrangler.toml` vars: `CURRENCY`, `TZ_OFFSET_HOURS`, `REMINDER_HOUR`.
6. Secrets:
   ```
   npx wrangler secret put BOT_TOKEN
   npx wrangler secret put WEBHOOK_SECRET      # any random string
   npx wrangler secret put ALLOWED_CHAT_ID     # optional; message the bot once to see your chat id
   ```
7. `npm run deploy` → note the `https://installment-bot.<you>.workers.dev` URL.
8. Point Telegram at it:
   ```
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d url=https://installment-bot.<you>.workers.dev/webhook \
     -d secret_token=<WEBHOOK_SECRET>
   ```
9. Send `/start` to your bot.

## iPhone app (installable web app)
The same Worker serves a mobile app: loan progress bars, principal-vs-interest split, payment
schedule, one-tap "Mark paid", expense entry, category breakdown and budget.
1. Set two more secrets: `ALLOWED_CHAT_ID` (required) and `APP_PASSCODE` (a long random string).
2. Open `https://installment-bot.<you>.workers.dev/` in **Safari** on your iPhone, enter the passcode.
3. Tap Share → **Add to Home Screen**. It now opens full-screen like a native app.

The passcode is stored only on your phone; use a long one (e.g. `openssl rand -base64 24`).

## Develop
`npm test` runs the loan-maths and bot tests (Node 22+, no network needed).
