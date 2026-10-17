const api = (env, method, body) =>
  fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

export const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

export function sendMessage(env, chatId, text, extra = {}) {
  return api(env, 'sendMessage', { chat_id: chatId, text, parse_mode: 'HTML', ...extra });
}

export function answerCallback(env, id, text) {
  return api(env, 'answerCallbackQuery', { callback_query_id: id, text });
}
