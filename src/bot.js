import { buildSchedule, summarize, dueDate, ymd, daysBetween, startYmFor, parseMoney } from './finance.js';
import { esc } from './telegram.js';

// ---------- helpers ----------

export function localToday(env, now = Date.now()) {
  const d = new Date(now + Number(env.TZ_OFFSET_HOURS ?? 0) * 3600e3);
  return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours() };
}

export const money = (env, cents) =>
  `${env.CURRENCY ?? '$'}${(cents / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const scheduleOf = (l) =>
  buildSchedule({ principalCents: l.principal_cents, annualRate: l.annual_rate, months: l.months, mode: l.mode });

const rel = (days) =>
  days < 0 ? `⚠️ overdue by ${-days}d` : days === 0 ? '🔔 due TODAY' : days === 1 ? 'due tomorrow' : `in ${days} days`;

function loanLine(env, l, today) {
  const s = summarize(scheduleOf(l), l.paid_count);
  if (s.done) return `✅ <b>#${l.id} ${esc(l.name)}</b> — fully paid`;
  const due = dueDate(l.start_ym, l.due_day, l.paid_count + 1);
  return (
    `<b>#${l.id} ${esc(l.name)}</b> — ${l.paid_count}/${l.months} paid\n` +
    `   Next: <b>${money(env, s.next.payment)}</b> on ${ymd(due)} (${rel(daysBetween(today, due))})\n` +
    `   Remaining principal: ${money(env, s.remainingPrincipal)} · interest left: ${money(env, s.remainingInterest)}`
  );
}

const getLoan = (env, chatId, id) =>
  env.DB.prepare('SELECT * FROM loans WHERE id=? AND chat_id=?').bind(id, chatId).first();

// ---------- loans ----------

const HELP = `<b>Installment & expense bot</b>

<b>Loans / installments</b>
/addloan &lt;name&gt; &lt;amount&gt; &lt;rate%&gt; &lt;months&gt; &lt;dueDay&gt; [flat] [paid=N]
  e.g. <code>/addloan Car 50000 3.5 60 15</code>
  e.g. <code>/addloan Phone 3600 0 12 5 paid=4</code>
  Use <code>flat</code> if your lender quotes a flat rate (interest on the full amount); default is reducing balance. Use _ for spaces in the name.
/loans — everything due + remaining
/loan &lt;id&gt; — details, total interest paid/left
/schedule &lt;id&gt; — upcoming payments
/pay &lt;id&gt; — mark the next installment as paid
/unpay &lt;id&gt; — undo the last payment mark
/delloan &lt;id&gt;

<b>Daily expenses</b>
Just type: <code>12.5 lunch</code> or <code>45 transport grab to office</code>
/today · /month · /undo
/budget &lt;amount&gt; — monthly spending limit (loan installments counted separately)

I remind you 3 days before, 1 day before, on the due day, and daily when overdue.`;

async function addLoan(env, chatId, args) {
  const [rawName, amt, rate, months, day, ...opts] = args;
  const principal = parseMoney(amt);
  const rateN = Number(rate);
  const monthsN = Number(months);
  const dayN = Number(day);
  if (!rawName || !principal || !(rateN >= 0) || !Number.isInteger(monthsN) || monthsN < 1 || monthsN > 600 ||
      !Number.isInteger(dayN) || dayN < 1 || dayN > 31 || rate === undefined) {
    return 'Usage: <code>/addloan &lt;name&gt; &lt;amount&gt; &lt;rate%&gt; &lt;months&gt; &lt;dueDay&gt; [flat] [paid=N]</code>\nExample: <code>/addloan Car 50000 3.5 60 15</code>';
  }
  let mode = 'reducing';
  let paid = 0;
  for (const o of opts) {
    if (o.toLowerCase() === 'flat') mode = 'flat';
    else if (/^paid=\d+$/i.test(o)) paid = Number(o.split('=')[1]);
    else return `Unknown option <code>${esc(o)}</code>`;
  }
  if (paid > monthsN) return 'paid= cannot exceed months.';
  const name = rawName.replace(/_/g, ' ');
  const startYm = startYmFor(localToday(env), dayN, paid);
  const res = await env.DB.prepare(
    'INSERT INTO loans (chat_id,name,principal_cents,annual_rate,months,mode,due_day,start_ym,paid_count) VALUES (?,?,?,?,?,?,?,?,?)'
  ).bind(chatId, name, principal, rateN, monthsN, mode, dayN, startYm, paid).run();
  const id = res.meta.last_row_id;
  return `Added loan #${id}.\n\n` + (await loanDetail(env, chatId, id));
}

async function loanDetail(env, chatId, id) {
  const l = await getLoan(env, chatId, id);
  if (!l) return 'Loan not found. See /loans.';
  const rows = scheduleOf(l);
  const s = summarize(rows, l.paid_count);
  const today = localToday(env);
  const lines = [
    `<b>#${l.id} ${esc(l.name)}</b> (${l.annual_rate}% ${l.mode === 'flat' ? 'flat' : 'p.a. reducing'}, ${l.months} months, due day ${l.due_day})`,
    `Monthly installment: <b>${money(env, rows[0].payment)}</b>`,
    `Principal: ${money(env, l.principal_cents)} · Total interest: ${money(env, s.totalInterest)} · Total payable: ${money(env, s.totalPayable)}`,
    '',
    `Paid: ${l.paid_count}/${l.months} → ${money(env, s.paidPayments)} (interest portion ${money(env, s.paidInterest)})`,
    `Remaining: ${l.months - l.paid_count} payments → <b>${money(env, s.remainingPayments)}</b>`,
    `   of which principal ${money(env, s.remainingPrincipal)}, interest ${money(env, s.remainingInterest)}`,
  ];
  if (!s.done) {
    const due = dueDate(l.start_ym, l.due_day, l.paid_count + 1);
    lines.push('', `Next: ${money(env, s.next.payment)} on ${ymd(due)} (${rel(daysBetween(today, due))})`);
  } else lines.push('', '✅ Fully paid!');
  return lines.join('\n');
}

async function listLoans(env, chatId) {
  const { results } = await env.DB.prepare('SELECT * FROM loans WHERE chat_id=? ORDER BY id').bind(chatId).all();
  if (!results.length) return 'No loans yet. Add one with /addloan (see /help).';
  const today = localToday(env);
  let monthly = 0, left = 0, interestLeft = 0;
  const lines = results.map((l) => {
    const s = summarize(scheduleOf(l), l.paid_count);
    if (!s.done) { monthly += s.next.payment; left += s.remainingPayments; interestLeft += s.remainingInterest; }
    return loanLine(env, l, today);
  });
  return lines.join('\n\n') +
    `\n\n<b>Total monthly commitment:</b> ${money(env, monthly)}\n<b>Total still to pay:</b> ${money(env, left)} (interest ${money(env, interestLeft)})`;
}

async function showSchedule(env, chatId, id) {
  const l = await getLoan(env, chatId, id);
  if (!l) return 'Loan not found. See /loans.';
  const rows = scheduleOf(l).slice(l.paid_count, l.paid_count + 12);
  if (!rows.length) return 'Fully paid — nothing upcoming.';
  const body = rows.map((r) =>
    `${String(r.n).padStart(3)} ${ymd(dueDate(l.start_ym, l.due_day, r.n))} ${(r.payment / 100).toFixed(2).padStart(10)} ${(r.interest / 100).toFixed(2).padStart(9)} ${(Math.max(r.balance, 0) / 100).toFixed(2).padStart(11)}`
  ).join('\n');
  return `<b>${esc(l.name)}</b> — next ${rows.length} payments\n<pre>  # due date       payment  interest     balance\n${body}</pre>`;
}

export async function markPaid(env, chatId, id, expectedN) {
  const l = await getLoan(env, chatId, id);
  if (!l) return 'Loan not found.';
  if (l.paid_count >= l.months) return 'Already fully paid.';
  if (expectedN && l.paid_count + 1 !== expectedN) return 'That installment was already marked paid.';
  const row = scheduleOf(l)[l.paid_count];
  await env.DB.prepare('UPDATE loans SET paid_count=paid_count+1 WHERE id=?').bind(id).run();
  const s = summarize(scheduleOf(l), l.paid_count + 1);
  return `✅ Recorded payment ${row.n}/${l.months} for <b>${esc(l.name)}</b>: ${money(env, row.payment)} (interest ${money(env, row.interest)}).\n` +
    (s.done ? '🎉 Loan fully paid!' : `Remaining: ${money(env, s.remainingPayments)} · principal left ${money(env, s.remainingPrincipal)}`);
}

async function unpay(env, chatId, id) {
  const l = await getLoan(env, chatId, id);
  if (!l) return 'Loan not found.';
  if (l.paid_count === 0) return 'Nothing to undo.';
  await env.DB.prepare('UPDATE loans SET paid_count=paid_count-1 WHERE id=?').bind(id).run();
  return `↩️ Payment ${l.paid_count} of ${esc(l.name)} un-marked.`;
}

async function delLoan(env, chatId, id) {
  const r = await env.DB.prepare('DELETE FROM loans WHERE id=? AND chat_id=?').bind(id, chatId).run();
  if (r.meta.changes) await env.DB.prepare('DELETE FROM reminder_log WHERE loan_id=?').bind(id).run();
  return r.meta.changes ? `Deleted loan #${id}.` : 'Loan not found.';
}

// ---------- expenses ----------

async function addExpense(env, chatId, text) {
  const [amt, cat, ...note] = text.split(/\s+/);
  const cents = parseMoney(amt);
  if (!cents) return null;
  const t = localToday(env);
  const category = (cat ?? 'other').toLowerCase();
  await env.DB.prepare('INSERT INTO expenses (chat_id,amount_cents,category,note,spent_on) VALUES (?,?,?,?,?)')
    .bind(chatId, cents, category, note.join(' '), ymd(t)).run();
  const day = await env.DB.prepare('SELECT SUM(amount_cents) s FROM expenses WHERE chat_id=? AND spent_on=?').bind(chatId, ymd(t)).first();
  return `💸 ${money(env, cents)} → ${esc(category)}${note.length ? ` (${esc(note.join(' '))})` : ''}\nToday so far: <b>${money(env, day.s)}</b>`;
}

async function breakdown(env, chatId, from, to, title) {
  const { results } = await env.DB.prepare(
    'SELECT category, SUM(amount_cents) s, COUNT(*) c FROM expenses WHERE chat_id=? AND spent_on>=? AND spent_on<=? GROUP BY category ORDER BY s DESC'
  ).bind(chatId, from, to).all();
  if (!results.length) return `${title}: no expenses recorded.`;
  const total = results.reduce((a, r) => a + r.s, 0);
  return { total, text: `<b>${title}</b>\n` + results.map((r) => `• ${esc(r.category)}: ${money(env, r.s)} (${r.c})`).join('\n') + `\n<b>Total: ${money(env, total)}</b>` };
}

async function today(env, chatId) {
  const t = ymd(localToday(env));
  const r = await breakdown(env, chatId, t, t, `Today (${t})`);
  return typeof r === 'string' ? r : r.text;
}

async function month(env, chatId) {
  const t = localToday(env);
  const from = ymd({ ...t, d: 1 });
  const r = await breakdown(env, chatId, from, ymd(t), `This month (${from.slice(0, 7)})`);
  if (typeof r === 'string') return r;
  const set = await env.DB.prepare('SELECT monthly_budget_cents b FROM settings WHERE chat_id=?').bind(chatId).first();
  let out = r.text;
  if (set?.b) {
    const left = set.b - r.total;
    const dim = new Date(Date.UTC(t.y, t.m, 0)).getUTCDate();
    out += `\nBudget ${money(env, set.b)}: ${left >= 0 ? `${money(env, left)} left (~${money(env, Math.floor(left / (dim - t.d + 1)))}/day for the rest of the month)` : `⚠️ over by ${money(env, -left)}`}`;
  }
  return out;
}

async function undoExpense(env, chatId) {
  const last = await env.DB.prepare('SELECT id,amount_cents,category FROM expenses WHERE chat_id=? ORDER BY id DESC LIMIT 1').bind(chatId).first();
  if (!last) return 'No expenses to undo.';
  await env.DB.prepare('DELETE FROM expenses WHERE id=?').bind(last.id).run();
  return `↩️ Removed ${money(env, last.amount_cents)} (${esc(last.category)}).`;
}

async function setBudget(env, chatId, amt) {
  const c = parseMoney(amt);
  if (!c) return 'Usage: <code>/budget 1500</code>';
  await env.DB.prepare('INSERT INTO settings (chat_id,monthly_budget_cents) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET monthly_budget_cents=excluded.monthly_budget_cents').bind(chatId, c).run();
  return `Monthly budget set to ${money(env, c)}.`;
}

// ---------- dispatcher ----------

export async function handleText(env, chatId, text) {
  text = text.trim();
  if (!text.startsWith('/')) {
    return (await addExpense(env, chatId, text)) ??
      'I didn\'t get that. Log an expense like <code>12.5 lunch</code>, or see /help.';
  }
  const [cmdRaw, ...args] = text.split(/\s+/);
  const cmd = cmdRaw.slice(1).split('@')[0].toLowerCase();
  const id = Number(args[0]);
  const needId = (fn) => (Number.isInteger(id) && id > 0 ? fn() : `Usage: <code>/${cmd} &lt;loan id&gt;</code> (see /loans)`);
  switch (cmd) {
    case 'start': case 'help': return HELP;
    case 'addloan': return addLoan(env, chatId, args);
    case 'loans': return listLoans(env, chatId);
    case 'loan': return needId(() => loanDetail(env, chatId, id));
    case 'schedule': return needId(() => showSchedule(env, chatId, id));
    case 'pay': return needId(() => markPaid(env, chatId, id));
    case 'unpay': return needId(() => unpay(env, chatId, id));
    case 'delloan': return needId(() => delLoan(env, chatId, id));
    case 'exp': return (await addExpense(env, chatId, args.join(' '))) ?? 'Usage: <code>/exp 12.5 food lunch</code>';
    case 'today': return today(env, chatId);
    case 'month': return month(env, chatId);
    case 'undo': return undoExpense(env, chatId);
    case 'budget': return setBudget(env, chatId, args[0]);
    default: return 'Unknown command. See /help.';
  }
}

// ---------- reminders (called from cron) ----------

const REMIND_AT = new Set([3, 1, 0]);

export async function buildReminders(env, now = Date.now()) {
  const t = localToday(env, now);
  const { results } = await env.DB.prepare('SELECT * FROM loans').all();
  const byChat = new Map();
  for (const l of results) {
    if (l.paid_count >= l.months) continue;
    const n = l.paid_count + 1;
    const due = dueDate(l.start_ym, l.due_day, n);
    const days = daysBetween(t, due);
    if (!REMIND_AT.has(days) && days >= 0) continue;
    const seen = await env.DB.prepare('SELECT 1 x FROM reminder_log WHERE loan_id=? AND installment=? AND day_offset=?').bind(l.id, n, days).first();
    if (seen) continue;
    const pay = scheduleOf(l)[l.paid_count];
    const list = byChat.get(l.chat_id) ?? [];
    list.push({ l, n, days, due, pay });
    byChat.set(l.chat_id, list);
  }
  return byChat;
}

export async function recordReminded(env, items) {
  for (const { l, n, days } of items)
    await env.DB.prepare('INSERT OR IGNORE INTO reminder_log (loan_id,installment,day_offset) VALUES (?,?,?)').bind(l.id, n, days).run();
}

export function reminderText(env, items) {
  const lines = items.map(({ l, n, days, due, pay }) => {
    const s = summarize(scheduleOf(l), l.paid_count);
    return `• <b>${esc(l.name)}</b> #${n}/${l.months}: pay <b>${money(env, pay.payment)}</b> (interest ${money(env, pay.interest)}) — ${rel(days)} (${ymd(due)})\n   Remaining after this: ${money(env, s.remainingPayments - pay.payment)}`;
  });
  return `💳 <b>Payment reminder</b>\n${lines.join('\n')}`;
}

export const reminderKeyboard = (items) => ({
  inline_keyboard: items.map(({ l, n }) => [{ text: `✅ Mark ${l.name} #${n} paid`, callback_data: `pay:${l.id}:${n}` }]),
});
