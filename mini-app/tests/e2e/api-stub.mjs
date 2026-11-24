// Изолированная подмена API Mini App для Playwright. Искусственные данные; не живой VK.
import http from 'node:http';
import { randomUUID } from 'node:crypto';

const sessions = new Map();
const requests = new Map();

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(body === undefined ? '' : JSON.stringify(body));
}

function answerFor(text) {
  if (text.includes('уточн')) return { outcome: 'clarify', text: 'Уточните, пожалуйста: какая лекция?', cards: [] };
  return {
    outcome: 'found', text: 'Вот что нашлось в доступных вам материалах:', cards: [{
      type: 'episode', material_id: 1, title: 'Контрольная запись', lecture: { title: 'Лекция 1', date: null },
      explanation: 'Синтетическое пояснение', url: 'https://vk.ru/video-1_2?t=1563s',
      excerpt: 'синтетическая выдержка', start_ms: 1563000, end_ms: 1590000,
    }],
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  let body = '';
  for await (const chunk of req) body += chunk;
  const data = body ? JSON.parse(body) : {};
  const token = (req.headers.authorization || '').replace('Bearer ', '');
  const session = sessions.get(token);
  if (url.pathname === '/health') return send(res, 200, { ok: true });
  if (url.pathname === '/api/miniapp/v1/session' && req.method === 'POST') {
    if (!data.launch_query?.includes('sign=')) return send(res, 401, { error: { code: 'invalid_launch', message: 'Откройте приложение заново.' } });
    const created = { token: randomUUID(), expires_at: new Date(Date.now() + 8 * 3600e3).toISOString(),
      dialog_id: randomUUID(), user_id: 1, limits: { question_chars: 2000 } };
    sessions.set(created.token, { ...created, items: [] });
    return send(res, 201, created);
  }
  if (url.pathname === '/api/miniapp/v1/session/resume') {
    if (!session) return send(res, 401, { error: { code: 'session_expired', message: 'Сессия истекла.' } });
    return send(res, 200, { expires_at: session.expires_at, dialog_id: session.dialog_id, user_id: 1, limits: session.limits });
  }
  if (!session) return send(res, 401, { error: { code: 'invalid_session', message: 'Войдите заново.' } });
  if (url.pathname === '/api/miniapp/v1/dialogs' && req.method === 'POST') {
    session.dialog_id = data.dialog_id; session.items = [];
    return send(res, 201, { dialog_id: data.dialog_id, state: 'active' });
  }
  const match = url.pathname.match(/^\/api\/miniapp\/v1\/dialogs\/([^/]+)\/(history|requests)(?:\/([^/]+))?$/);
  if (!match || match[1] !== session.dialog_id) return send(res, 404, { error: { code: 'not_found', message: 'Не найдено' } });
  if (match[2] === 'history') return send(res, 200, { dialog_id: match[1], items: session.items, next_after_sequence: session.items.length, has_more: false });
  if (req.method === 'POST') {
    const item = { dialog_id: match[1], request_id: data.request_id, sequence: session.items.length + 1, state: 'queued',
      operation_id: 'stub', question: data.text, retry_allowed: false };
    session.items.push(item);
    requests.set(data.request_id, { item, ready: Date.now() + 500 });
    return send(res, 202, item);
  }
  const stored = requests.get(match[3]);
  if (!stored) return send(res, 404, { error: { code: 'not_found', message: 'Не найдено' } });
  if (Date.now() >= stored.ready && stored.item.state === 'queued') {
    Object.assign(stored.item, { state: 'completed', answer: answerFor(stored.item.question) });
  }
  return send(res, 200, stored.item);
});

server.listen(8788, '127.0.0.1');
