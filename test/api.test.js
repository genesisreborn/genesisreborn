import test from 'node:test';
import assert from 'node:assert/strict';
import { fakeD1 } from './helpers.js';
import { handleApi } from '../src/api.js';

const env = () => ({ DB: fakeD1(), CURRENCY: 'MYR ', TZ_OFFSET_HOURS: '8', ALLOWED_CHAT_ID: '7', APP_PASSCODE: 'secret-pass' });
const req = (path, body, pass = 'secret-pass') => new Request('http://x' + path, {
  method: body ? 'POST' : 'GET', headers: { authorization: 'Bearer ' + pass }, body: body ? JSON.stringify(body) : undefined });

test('rejects bad passcode', async () => {
  assert.equal((await handleApi(req('/api/state', null, 'nope'), env())).status, 401);
});

test('add loan, pay, expense through the API', async () => {
  const e = env();
  let j = await (await handleApi(req('/api/loan', { name: 'My Car', amount: '12000', rate: '12', months: '12', dueDay: '15', paid: '0' }), e)).json();
  assert.equal(j.ok, true);
  assert.equal(j.state.loans[0].name, 'My Car');
  assert.equal(j.state.loans[0].payment, 106619);
  j = await (await handleApi(req('/api/pay', { id: 1 }), e)).json();
  assert.equal(j.state.loans[0].paid, 1);
  j = await (await handleApi(req('/api/expense', { amount: '12.5', category: 'food', note: 'lunch' }), e)).json();
  assert.equal(j.state.spend.today, 1250);
  const bad = await handleApi(req('/api/loan', { name: 'x', amount: 'abc' }), e);
  assert.equal(bad.status, 400);
});

import { createHmac } from 'node:crypto';
import { validInitData } from '../src/api.js';

function signInit(token, user, authDate = Math.floor(Date.now() / 1000)) {
  const p = new URLSearchParams({ auth_date: String(authDate), query_id: 'AAA', user: JSON.stringify(user) });
  const check = [...p.entries()].map(([k, v]) => `${k}=${v}`).sort().join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(token).digest();
  p.set('hash', createHmac('sha256', secret).update(check).digest('hex'));
  return p.toString();
}

test('Telegram initData validation', async () => {
  const good = signInit('123:TOKEN', { id: 7, first_name: 'A' });
  assert.equal(await validInitData(good, '123:TOKEN', '7'), true);
  assert.equal(await validInitData(good, '123:TOKEN', '8'), false, 'other user');
  assert.equal(await validInitData(good, '999:OTHER', '7'), false, 'wrong bot');
  assert.equal(await validInitData(good + 'x', '123:TOKEN', '7'), false, 'tampered');
  const old = signInit('123:TOKEN', { id: 7 }, Math.floor(Date.now() / 1000) - 90000);
  assert.equal(await validInitData(old, '123:TOKEN', '7'), false, 'expired');
});

test('API accepts valid initData header', async () => {
  const e = { ...env(), BOT_TOKEN: '123:TOKEN' };
  const r = new Request('http://x/api/state', { headers: { 'x-telegram-init-data': signInit('123:TOKEN', { id: 7 }) } });
  assert.equal((await handleApi(r, e)).status, 200);
});
