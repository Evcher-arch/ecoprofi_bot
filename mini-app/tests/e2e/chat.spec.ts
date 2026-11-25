import { expect, test } from '@playwright/test';

const LAUNCH = '/?vk_app_id=54916430&vk_user_id=1&vk_ts=1&sign=synthetic';

test('прямой запуск без параметров закрыт', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText(/Откройте приложение из сообщества/)).toBeVisible();
  await expect(page.getByLabel('Вопрос')).toHaveCount(0);
});

test('вход → уточнение → карточка → новый диалог', async ({ page }) => {
  await page.goto(LAUNCH);
  const input = page.getByLabel('Вопрос');
  await expect(input).toBeVisible({ timeout: 15000 });
  await input.fill('нужно уточнение');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByText('Уточните, пожалуйста: какая лекция?')).toBeVisible({ timeout: 10000 });
  await input.fill('загрязняющих веществ');
  await input.press('Enter');
  await expect(page.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  await expect(page.getByText('Эпизод 26:03–26:30')).toBeVisible();
  const link = page.getByRole('link', { name: 'Открыть источник' });
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  await page.getByRole('button', { name: 'Новый диалог' }).click();
  await expect(page.getByText('Контрольная запись')).toHaveCount(0, { timeout: 10000 });
  await page.getByRole('button', { name: 'Как искать' }).click();
  await expect(page.getByText(/Пишите 2–4 ключевых слова/)).toBeVisible();
  const width = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  expect(width).toBe(true);
});

test('две независимые сессии не смешиваются', async ({ browser }) => {
  const first = await (await browser.newContext()).newPage();
  const second = await (await browser.newContext()).newPage();
  await first.goto(LAUNCH);
  await second.goto(LAUNCH);
  await first.getByLabel('Вопрос').fill('загрязняющих веществ');
  await first.getByRole('button', { name: 'Отправить' }).click();
  await expect(first.getByText('Контрольная запись')).toBeVisible({ timeout: 10000 });
  await expect(second.getByLabel('Вопрос')).toBeVisible();
  await expect(second.getByText('Контрольная запись')).toHaveCount(0);
});
