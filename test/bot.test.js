import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { handleText, buildReminders, recordReminded } from '../src/bot.js';

// Minimal D1 shim over node:sqlite
function fakeD1() {
  const db = new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('../schema.sql', import.meta.url), 'utf8'));
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    first: async () => db.prepare(sql).get(...args) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...args) }),
    run: async () => {
      const r = db.prepare(sql).run(...args);
      return { meta: { last_row_id: Number(r.lastInsertRowid), changes: r.changes } };
    },
  });
  return { prepare: (sql) => stmt(sql) };
}

const env = () => ({ DB: fakeD1(), CURRENCY: '$', TZ_OFFSET_HOURS: '0' });
const C = 1;

test('add loan, pay, remaining', async () => {
  const e = env();
  const added = await handleText(e, C, '/addloan Car 12000 12 12 15');
  assert.match(added, /Added loan #1/);
  assert.match(added, /1,066\.19/);
  assert.match(await handleText(e, C, '/pay 1'), /Recorded payment 1\/12/);
  assert.match(await handleText(e, C, '/loans'), /1\/12 paid/);
  assert.match(await handleText(e, C, '/unpay 1'), /un-marked/);
  assert.match(await handleText(e, C, '/schedule 1'), /<pre>/);
});

test('expenses, budget, undo', async () => {
  const e = env();
  assert.match(await handleText(e, C, '12.5 lunch with team'), /Today so far: <b>\$12\.50/);
  await handleText(e, C, '7 coffee');
  await handleText(e, C, '/budget 500');
  const m = await handleText(e, C, '/month');
  assert.match(m, /Total: \$19\.50/);
  assert.match(m, /Budget \$500\.00/);
  assert.match(await handleText(e, C, '/undo'), /Removed \$7\.00/);
});

test('reminders fire once per offset', async () => {
  const e = env();
  const now = Date.now();
  const d = new Date(now + 3 * 86400e3);
  await handleText(e, C, `/addloan Phone 1200 0 12 ${d.getUTCDate()}`);
  const first = await buildReminders(e, now);
  assert.equal(first.get(C).length, 1);
  await recordReminded(e, first.get(C));
  assert.equal((await buildReminders(e, now)).size, 0);
});

test('bad input is rejected', async () => {
  const e = env();
  assert.match(await handleText(e, C, '/addloan Car abc 3 12 15'), /Usage/);
  assert.match(await handleText(e, C, 'hello'), /didn't get that/);
});
