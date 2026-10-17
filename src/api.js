import { buildSchedule, summarize, dueDate, ymd, daysBetween } from './finance.js';
import { handleText, localToday, scheduleOf } from './bot.js';

const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } });

function authed(request, env) {
  const given = (request.headers.get('authorization') ?? '').replace(/^Bearer /, '');
  const want = env.APP_PASSCODE ?? '';
  if (!want || given.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= given.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

async function state(env, chatId) {
  const t = localToday(env);
  const { results: loans } = await env.DB.prepare('SELECT * FROM loans WHERE chat_id=? ORDER BY id').bind(chatId).all();
  const out = loans.map((l) => {
    const rows = scheduleOf(l);
    const s = summarize(rows, l.paid_count);
    const due = s.done ? null : dueDate(l.start_ym, l.due_day, l.paid_count + 1);
    return {
      id: l.id, name: l.name, rate: l.annual_rate, mode: l.mode, months: l.months, paid: l.paid_count,
      principal: l.principal_cents, payment: rows[0].payment, done: s.done,
      nextPayment: s.next?.payment ?? 0, nextDue: due && ymd(due), daysUntil: due && daysBetween(t, due),
      totalInterest: s.totalInterest, paidInterest: s.paidInterest, remainingInterest: s.remainingInterest,
      remainingPrincipal: s.remainingPrincipal, remainingTotal: s.remainingPayments,
      curve: rows.map((r) => [r.principal, r.interest]),
      schedule: rows.slice(l.paid_count, l.paid_count + 12).map((r) => ({ ...r, due: ymd(dueDate(l.start_ym, l.due_day, r.n)) })),
    };
  });
  const today = ymd(t);
  const monthFrom = ymd({ ...t, d: 1 });
  const { results: cats } = await env.DB.prepare(
    'SELECT category, SUM(amount_cents) s FROM expenses WHERE chat_id=? AND spent_on>=? GROUP BY category ORDER BY s DESC').bind(chatId, monthFrom).all();
  const todayRow = await env.DB.prepare('SELECT COALESCE(SUM(amount_cents),0) s FROM expenses WHERE chat_id=? AND spent_on=?').bind(chatId, today).first();
  const { results: recent } = await env.DB.prepare(
    'SELECT id, amount_cents, category, note, spent_on FROM expenses WHERE chat_id=? ORDER BY id DESC LIMIT 15').bind(chatId).all();
  const from14 = new Date(Date.UTC(t.y, t.m - 1, t.d - 13));
  const { results: dayRows } = await env.DB.prepare(
    'SELECT spent_on, SUM(amount_cents) s FROM expenses WHERE chat_id=? AND spent_on>=? GROUP BY spent_on')
    .bind(chatId, ymd({ y: from14.getUTCFullYear(), m: from14.getUTCMonth() + 1, d: from14.getUTCDate() })).all();
  const byDay = Object.fromEntries(dayRows.map((r) => [r.spent_on, r.s]));
  const daily = Array.from({ length: 14 }, (_, i) => {
    const d = new Date(Date.UTC(t.y, t.m - 1, t.d - 13 + i));
    const key = ymd({ y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() });
    return { date: key, cents: byDay[key] ?? 0 };
  });
  const set = await env.DB.prepare('SELECT monthly_budget_cents b FROM settings WHERE chat_id=?').bind(chatId).first();
  return {
    currency: env.CURRENCY ?? '$', today,
    loans: out,
    totals: {
      monthly: out.reduce((a, l) => a + (l.done ? 0 : l.nextPayment), 0),
      remaining: out.reduce((a, l) => a + l.remainingTotal, 0),
      remainingInterest: out.reduce((a, l) => a + l.remainingInterest, 0),
    },
    spend: { daily, today: todayRow.s, month: cats.reduce((a, c) => a + c.s, 0), categories: cats, recent, budget: set?.b ?? null },
  };
}

export async function handleApi(request, env) {
  if (!authed(request, env)) return json({ error: 'unauthorized' }, 401);
  const chatId = Number(env.ALLOWED_CHAT_ID);
  if (!chatId) return json({ error: 'ALLOWED_CHAT_ID secret is not set' }, 500);
  const { pathname } = new URL(request.url);
  if (request.method === 'GET' && pathname === '/api/state') return json(await state(env, chatId));
  if (request.method !== 'POST') return json({ error: 'not found' }, 404);

  const b = await request.json().catch(() => ({}));
  const clean = (s) => String(s ?? '').trim().replace(/\s+/g, '_');
  let reply;
  switch (pathname) {
    case '/api/pay': reply = await handleText(env, chatId, `/pay ${Number(b.id)}`); break;
    case '/api/unpay': reply = await handleText(env, chatId, `/unpay ${Number(b.id)}`); break;
    case '/api/delloan': reply = await handleText(env, chatId, `/delloan ${Number(b.id)}`); break;
    case '/api/loan':
      reply = await handleText(env, chatId,
        `/addloan ${clean(b.name)} ${b.amount} ${b.rate} ${b.months} ${b.dueDay}${b.flat ? ' flat' : ''}${Number(b.paid) ? ` paid=${Number(b.paid)}` : ''}`);
      break;
    case '/api/expense':
      reply = await handleText(env, chatId, `/exp ${b.amount} ${clean(b.category) || 'other'} ${String(b.note ?? '').trim()}`);
      break;
    case '/api/undo': reply = await handleText(env, chatId, '/undo'); break;
    case '/api/budget': reply = await handleText(env, chatId, `/budget ${b.amount}`); break;
    default: return json({ error: 'not found' }, 404);
  }
  const failed = /^(Usage|Unknown|Loan not found|Nothing|No expenses|I didn)/.test(reply);
  return json({ ok: !failed, message: reply.replace(/<[^>]+>/g, ''), state: await state(env, chatId) }, failed ? 400 : 200);
}
