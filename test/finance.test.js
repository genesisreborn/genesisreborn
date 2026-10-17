import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSchedule, summarize, dueDate, startYmFor, daysBetween, parseMoney } from '../src/finance.js';

test('reducing: standard 12k @ 12% / 12m', () => {
  const rows = buildSchedule({ principalCents: 1200000, annualRate: 12, months: 12 });
  assert.equal(rows.length, 12);
  assert.equal(rows[0].payment, 106619); // 1066.19
  assert.equal(rows[0].interest, 12000);
  assert.equal(rows[11].balance, 0);
  assert.equal(rows.reduce((a, r) => a + r.principal, 0), 1200000);
});

test('zero interest splits evenly and ends at 0', () => {
  const rows = buildSchedule({ principalCents: 100000, annualRate: 0, months: 3 });
  assert.deepEqual(rows.map(r => r.payment), [33333, 33333, 33334]);
  assert.equal(rows[2].balance, 0);
  assert.equal(summarize(rows, 0).totalInterest, 0);
});

test('flat: interest on original principal', () => {
  const rows = buildSchedule({ principalCents: 1000000, annualRate: 5, months: 24, mode: 'flat' });
  const s = summarize(rows, 0);
  assert.equal(s.totalInterest, 100000);
  assert.equal(s.totalPayable, 1100000);
  assert.equal(rows[23].balance, 0);
});

test('summary tracks paid vs remaining', () => {
  const rows = buildSchedule({ principalCents: 1200000, annualRate: 12, months: 12 });
  const s = summarize(rows, 3);
  assert.equal(s.remainingPrincipal, rows[2].balance);
  assert.equal(s.next.n, 4);
  assert.equal(s.paidInterest + s.remainingInterest, s.totalInterest);
  assert.equal(summarize(rows, 12).done, true);
});

test('due dates clamp to month end and roll over years', () => {
  const start = 2026 * 12 + 0; // Jan 2026
  assert.deepEqual(dueDate(start, 31, 2), { y: 2026, m: 2, d: 28 });
  assert.deepEqual(dueDate(start, 15, 13), { y: 2027, m: 1, d: 15 });
});

test('startYmFor accounts for passed due day and paid count', () => {
  const today = { y: 2026, m: 10, d: 17 };
  assert.equal(startYmFor(today, 20, 0), 2026 * 12 + 9);  // due this month
  assert.equal(startYmFor(today, 5, 0), 2026 * 12 + 10);  // next month
  assert.equal(startYmFor(today, 17, 0), 2026 * 12 + 9);  // due today still counts
  assert.equal(startYmFor(today, 20, 4), 2026 * 12 + 5);
});

test('daysBetween & parseMoney', () => {
  assert.equal(daysBetween({ y: 2026, m: 10, d: 17 }, { y: 2026, m: 10, d: 20 }), 3);
  assert.equal(parseMoney('12.5'), 1250);
  assert.equal(parseMoney('abc'), null);
  assert.equal(parseMoney('0'), null);
});
