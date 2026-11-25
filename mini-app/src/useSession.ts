import { useCallback, useEffect, useState } from 'react';
import bridge from '@vkontakte/vk-bridge';
import { api, ApiError } from './api';
import { clearSession, launchQuery, loadSession, saveSession } from './session';
import type { Session } from './types';

export type SessionState =
  | { status: 'loading'; session: null; error: null }
  | { status: 'ready'; session: Session; error: null }
  | { status: 'unverified' | 'denied' | 'expired' | 'error'; session: null; error: string };

async function initBridge(): Promise<void> {
  // VKWebAppInit обязателен, но его сбой не подменяет подтверждение: подпись проверяет сервер.
  await Promise.race([
    bridge.send('VKWebAppInit').catch(() => undefined),
    new Promise((resolve) => window.setTimeout(resolve, 3000)),
  ]);
}

export function useSession() {
  const [state, setState] = useState<SessionState>({ status: 'loading', session: null, error: null });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setState({ status: 'loading', session: null, error: null });
    const query = launchQuery();
    if (!query) {
      setState({ status: 'unverified', session: null, error: 'Откройте приложение из сообщества.' });
      return () => { active = false; };
    }
    void (async () => {
      await initBridge();
      const saved = loadSession();
      if (saved) {
        try {
          const resumed = await api.resume(query, saved.token);
          const session = { ...saved, ...resumed, token: saved.token };
          saveSession(session);
          if (active) setState({ status: 'ready', session, error: null });
          return;
        } catch (error) {
          clearSession();
          // 401 при resume означает новый запуск (другая подпись) или истёкшую сессию:
          // не показываем ошибку, а сразу входим по свежим параметрам запуска.
          if (error instanceof ApiError && error.status === 403) throw error;
        }
      }
      const session = await api.login(query);
      saveSession(session);
      if (active) setState({ status: 'ready', session, error: null });
    })().catch((error: unknown) => {
      if (!active) return;
      if (error instanceof ApiError && error.status === 403) {
        clearSession();
        setState({ status: 'denied', session: null, error: error.message });
      } else if (error instanceof ApiError && error.status === 401) {
        clearSession();
        setState({ status: 'expired', session: null, error: error.message });
      } else {
        setState({ status: 'error', session: null, error: 'Не удалось проверить доступ. Повторите попытку.' });
      }
    });
    return () => { active = false; };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  const setDialog = useCallback((dialogId: string) => {
    setState((current) => {
      if (current.status !== 'ready') return current;
      const session = { ...current.session, dialog_id: dialogId };
      saveSession(session);
      return { status: 'ready', session, error: null };
    });
  }, []);
  const expire = useCallback(() => {
    clearSession();
    setState({ status: 'expired', session: null, error: 'Сессия истекла. Откройте приложение заново.' });
  }, []);
  return { ...state, retry, setDialog, expire };
}
