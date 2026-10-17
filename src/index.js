import { handleText, markPaid, buildReminders, recordReminded, reminderText, reminderKeyboard, localToday } from './bot.js';
import { sendMessage, answerCallback } from './telegram.js';

const allowed = (env, chatId) => !env.ALLOWED_CHAT_ID || String(chatId) === String(env.ALLOWED_CHAT_ID);

async function handleUpdate(env, update) {
  const cb = update.callback_query;
  if (cb) {
    const chatId = cb.message?.chat.id;
    if (!chatId || !allowed(env, chatId)) return;
    const m = /^pay:(\d+):(\d+)$/.exec(cb.data ?? '');
    if (m) {
      const text = await markPaid(env, chatId, Number(m[1]), Number(m[2]));
      await answerCallback(env, cb.id, 'Done');
      await sendMessage(env, chatId, text);
    }
    return;
  }
  const msg = update.message;
  if (!msg?.text) return;
  if (!allowed(env, msg.chat.id)) {
    await sendMessage(env, msg.chat.id, `This bot is private. Your chat id is <code>${msg.chat.id}</code>.`);
    return;
  }
  await sendMessage(env, msg.chat.id, await handleText(env, msg.chat.id, msg.text));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === 'POST' && url.pathname === '/webhook') {
      if (request.headers.get('x-telegram-bot-api-secret-token') !== env.WEBHOOK_SECRET) {
        return new Response('forbidden', { status: 403 });
      }
      const update = await request.json();
      ctx.waitUntil(handleUpdate(env, update).catch((e) => console.error('update failed', e)));
      return new Response('ok');
    }
    return new Response('installment-bot is running');
  },

  async scheduled(event, env) {
    if (localToday(env, event.scheduledTime).h !== Number(env.REMINDER_HOUR ?? 9)) return;
    const byChat = await buildReminders(env, event.scheduledTime);
    for (const [chatId, items] of byChat) {
      if (!allowed(env, chatId)) continue;
      const res = await sendMessage(env, chatId, reminderText(env, items), { reply_markup: reminderKeyboard(items) });
      if (res.ok) await recordReminded(env, items);
    }
  },
};
