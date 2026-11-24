import { expect, test } from '@playwright/test';

// Подмена API: искусственные параметры запуска; не живой VK и не реальная подпись.
const LAUNCH = '/?vk_app_id=54916430&vk_user_id=1&vk_ts=1&sign=synthetic';

test('перезагрузка возобновляет сессию и показывает ответ с сервера', async ({ page }) => {
  await page.goto(LAUNCH);
  await page.getByLabel('Вопрос').fill('загрязняющих веществ');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  await page.reload();
  await expect(page.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  const stored = await page.evaluate(() => JSON.stringify(window.sessionStorage));
  expect(stored).not.toContain('загрязняющих');
  expect(stored).not.toContain('sign=');
});

test('обрыв при отправке: текст остаётся, повтор не создаёт дубль', async ({ page }) => {
  await page.goto(LAUNCH);
  const input = page.getByLabel('Вопрос');
  await expect(input).toBeVisible({ timeout: 15000 });
  let first = true;
  await page.route('**/requests', async (route) => {
    if (first && route.request().method() === 'POST') { first = false; return route.abort('connectionreset'); }
    return route.continue();
  });
  await input.fill('загрязняющих веществ');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByText(/Отправка не подтверждена/)).toBeVisible();
  await expect(input).toHaveValue('загрязняющих веществ');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  await expect(page.getByText('загрязняющих веществ', { exact: true })).toHaveCount(1);
});

test('истёкшая сессия требует нового входа без старой истории', async ({ page }) => {
  await page.goto(LAUNCH);
  await expect(page.getByLabel('Вопрос')).toBeVisible({ timeout: 15000 });
  await page.route('**/history**', (route) => route.fulfill({ status: 401, contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'session_expired', message: 'Сессия истекла.' } }) }));
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByText(/Сессия истекла/)).toBeVisible();
  await expect(page.getByLabel('Вопрос')).toHaveCount(0);
  expect(await page.evaluate(() => window.sessionStorage.length)).toBe(0);
});

test('сбой проверки подписки (503) скрывает материалы', async ({ page }) => {
  await page.goto(LAUNCH);
  await page.getByLabel('Вопрос').fill('загрязняющих веществ');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  await page.route('**/history**', (route) => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'membership_unavailable', message: 'Проверка подписки временно недоступна.' } }) }));
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByText('Проверка подписки временно недоступна.')).toBeVisible();
  await expect(page.getByText('Контрольная запись')).toHaveCount(0);
});
