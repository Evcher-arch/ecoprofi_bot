import bridge from '@vkontakte/vk-bridge';
import './style.css';

const app = document.querySelector<HTMLElement>('#app');
if (!app) throw new Error('missing app');
const root: HTMLElement = app;

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text: string,
  className = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

root.append(
  element('h1', 'Найти лекцию по Экобезу'),
  element('p', 'Изолированная проверка входа Mini App. Поиск и база данных не подключены.'),
);
const status = element('p', 'Инициализация VK Bridge…');
status.setAttribute('role', 'status');
status.setAttribute('aria-live', 'polite');
root.append(status);

const links = element('section', '', 'links');
links.append(element('h2', 'Контрольные переходы'));
for (const [label, href, note] of [
  ['Контрольное видео', 'https://vk.ru/video-95098598_456240761', 'Проверить открытие и доступность записи'],
  ['Контрольная презентация', 'https://vk.ru/away.php?to=https%3A%2F%2Fvk.com%2Fdoc177068782_686984961%3Fhash%3DVshmSrdDlBxhYFFj1HpbGsxpLMcnmHWeHbjVAIhIzlw%26dl%3DibixdjK2xQexwxWFYIvWQF8CHIZpwVWJ33kkrZpOyF8%26api%3D1%26no_preview%3D1', 'Сверить открытие документа и видимую страницу'],
  ['Контрольное письмо', 'https://vk.ru/away.php?to=https%3A%2F%2Fvk.com%2Fdoc177068782_672441089%3Fhash%3DOdDF8Fi1ttE4o2cnVLuhHvHXz07z1ZNQmfGwal0miJw%26dl%3DrJpSa6J5g6BHOFBznWWeB4y4CQ16OQQWDkHa5SlGHLH%26api%3D1%26no_preview%3D1', 'Сверить реквизиты с оригиналом'],
] as const) {
  const row = element('p', '');
  const link = element('a', label, '');
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  row.append(link, element('small', ` ${note}`));
  links.append(row);
}
root.append(links);

async function start(): Promise<void> {
  const launchQuery = window.location.search.startsWith('?')
    ? window.location.search.slice(1)
    : window.location.search;
  if (!launchQuery) {
    status.textContent = 'Откройте приложение из сообщества VK. Прямой вход без параметров не проверяется.';
    return;
  }
  try {
    await bridge.send('VKWebAppInit');
    const response = await fetch('./api/probe/session', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ launch_query: launchQuery }),
    });
    const result: { verified?: boolean; error?: { code?: string } } = await response.json();
    if (!response.ok || result.verified !== true) {
      throw new Error(result.error?.code || 'request_failed');
    }
    status.textContent = 'Запуск и подпись подтверждены. Проверка повтора…';
    const repeat = await fetch('./api/probe/verify', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ launch_query: launchQuery }),
    });
    if (!repeat.ok) {
      throw new Error('repeat_rejected');
    }
    status.textContent = 'Запуск, подпись и повтор подтверждены. Проверьте ссылки выше.';
    status.dataset.result = 'verified';
  } catch (error) {
    const code = error instanceof Error ? error.message : 'request_failed';
    status.textContent = `Вход не подтверждён (${code}). Откройте приложение заново из VK.`;
    status.dataset.result = 'rejected';
  }
}

void start();
