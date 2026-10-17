CREATE TABLE IF NOT EXISTS loans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  principal_cents INTEGER NOT NULL,
  annual_rate REAL NOT NULL,          -- percent p.a., 0 for interest-free
  months INTEGER NOT NULL,
  mode TEXT NOT NULL DEFAULT 'reducing', -- 'reducing' | 'flat'
  due_day INTEGER NOT NULL,
  start_ym INTEGER NOT NULL,          -- year*12 + (month-1) of installment #1
  paid_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_loans_chat ON loans(chat_id);

CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  category TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  spent_on TEXT NOT NULL              -- local date YYYY-MM-DD
);
CREATE INDEX IF NOT EXISTS idx_exp_chat_date ON expenses(chat_id, spent_on);

CREATE TABLE IF NOT EXISTS settings (
  chat_id INTEGER PRIMARY KEY,
  monthly_budget_cents INTEGER
);

CREATE TABLE IF NOT EXISTS reminder_log (
  loan_id INTEGER NOT NULL,
  installment INTEGER NOT NULL,
  day_offset INTEGER NOT NULL,        -- days until due (negative = overdue)
  PRIMARY KEY (loan_id, installment, day_offset)
);
