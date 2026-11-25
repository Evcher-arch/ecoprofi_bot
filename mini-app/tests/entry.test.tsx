import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import { clearSession, saveSession } from '../src/session';

const bridge = vi.hoisted(() => ({ send: vi.fn() }));
vi.mock('@vkontakte/vk-bridge', () => ({ default: bridge }));
const session = {
  token: 'test-token', expires_at: '2026-11-23T20:00:00Z',
  dialog_id: '11111111-1111-1111-1111-111111111111', user_id: 12345,
  limits: { question_chars: 2000 },
};

function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } }));
}

describe('подтверждённый вход', () => {
  beforeEach(() => {
    clearSession();
    bridge.send.mockResolvedValue({ result: true });
    window.history.replaceState({}, '', '/?vk_app_id=54916430&vk_user_id=12345');
    vi.stubGlobal('fetch', vi.fn((url: RequestInfo | URL) => {
      if (String(url).endsWith('/history?after_sequence=0&limit=50')) {
        return response({ dialog_id: session.dialog_id, items: [], next_after_sequence: 0, has_more: false });
      }
      return response(session, 201);
    }));
  });

  it('Bridge и login предшествуют истории', async () => {
    render(<App />);
    expect(await screen.findByLabelText('Вопрос')).toBeInTheDocument();
    expect(bridge.send).toHaveBeenCalledWith('VKWebAppInit');
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('нет query — нет запроса и ручного VK ID', async () => {
    window.history.replaceState({}, '', '/');
    render(<App />);
    expect(await screen.findByText(/Откройте приложение из сообщества/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/VK ID/)).not.toBeInTheDocument();
  });

  it('resume до истории, bearer только в разрешённом sessionStorage', async () => {
    saveSession(session);
    render(<App />);
    expect(await screen.findByLabelText('Вопрос')).toBeInTheDocument();
    expect(window.sessionStorage.getItem('ecoprofi.miniapp.session.v1')).not.toContain('vk_user_id');
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/miniapp/v1/session/resume', expect.objectContaining({ method: 'POST' })));
  });

  it('новый запуск со старой сессией: resume 401 → сразу login без экрана ошибки', async () => {
    saveSession({ ...session, token: 'old-token' });
    vi.stubGlobal('fetch', vi.fn((url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith('/session/resume')) {
        return response({ error: { code: 'resume_mismatch', message: 'Откройте приложение заново из VK.' } }, 401);
      }
      if (path.endsWith('/history?after_sequence=0&limit=50')) {
        return response({ dialog_id: session.dialog_id, items: [], next_after_sequence: 0, has_more: false });
      }
      return response(session, 201);
    }));
    render(<App />);
    expect(await screen.findByLabelText('Вопрос')).toBeInTheDocument();
    expect(screen.queryByText(/Откройте приложение заново/)).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith('/api/miniapp/v1/session', expect.objectContaining({ method: 'POST' }));
    expect(window.sessionStorage.getItem('ecoprofi.miniapp.session.v1')).toContain('test-token');
  });

  it('ошибка допуска не открывает чат', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'not_subscribed', message: 'Нужна подписка' } }, 403)));
    render(<App />);
    expect(await screen.findByText('Нужна подписка')).toBeInTheDocument();
    expect(screen.queryByLabelText('Вопрос')).not.toBeInTheDocument();
  });
});
