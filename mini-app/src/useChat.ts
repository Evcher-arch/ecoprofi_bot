import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { clearPending, loadPending, savePending } from './session';
import type { ChatRequest, History } from './types';

interface ChatApi {
  history: typeof api.history;
  submit: typeof api.submit;
  status: typeof api.status;
  newDialog: typeof api.newDialog;
}

const noop = () => {};
// Дольше этого вкладка была скрыта — прежняя выдача не показывается до проверки допуска.
const HIDE_AFTER_MS = 10 * 60 * 1000;

export function useChat(dialogId: string, token: string, client: ChatApi,
  onDialogProp?: (id: string) => void, onExpiredProp?: () => void) {
  // Колбэки через ref: их смена не перезапускает загрузку истории.
  const callbacks = useRef({ onDialog: onDialogProp || noop, onExpired: onExpiredProp || noop });
  callbacks.current = { onDialog: onDialogProp || noop, onExpired: onExpiredProp || noop };
  const onDialog = useCallback((id: string) => callbacks.current.onDialog(id), []);
  const onExpired = useCallback(() => callbacks.current.onExpired(), []);
  const [items, setItems] = useState<ChatRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const polling = useRef<number | null>(null);
  // ID создаётся до отправки и сохраняется до однозначного исхода: сетевой повтор использует его же.
  // text = null: ID восстановлен после перезагрузки, исходный текст не хранился.
  const unconfirmed = useRef<{ id: string; text: string | null } | null>(null);

  const load = useCallback(async (after = 0, silent = false) => {
    const current = ++generation.current;
    if (!silent) setLoading(true);
    try {
      const page = await client.history(dialogId, token, after, 50);
      if (current !== generation.current || page.dialog_id !== dialogId) return;
      setItems((previous) => after ? [...previous, ...page.items] : page.items);
      setHasMore(page.has_more);
      setError(null);
      if (!after) {
        // После перезагрузки: судьбу последнего отправленного вопроса определяет сервер.
        const pendingId = loadPending(dialogId);
        const known = page.items.find((item) => item.request_id === pendingId);
        if (pendingId && !known && !page.has_more) {
          unconfirmed.current = { id: pendingId, text: null };
          setError('Отправка последнего вопроса не подтверждена. Введите его снова — дубля не будет.');
        } else if (known && !['queued', 'running'].includes(known.state)) {
          clearPending();
        }
      }
    } catch (cause) {
      if (current !== generation.current) return;
      setError(cause instanceof ApiError ? cause.message : 'Не удалось загрузить историю.');
      if (cause instanceof ApiError && [401, 403, 503].includes(cause.status)) setItems([]);
      if (cause instanceof ApiError && cause.status === 401) onExpired();
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [dialogId, token, client, onExpired]);

  useEffect(() => {
    setItems([]);
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  useEffect(() => {
    if (polling.current) window.clearTimeout(polling.current);
    const pending = items.find((item) => ['queued', 'running'].includes(item.state));
    if (!pending || document.hidden) return;
    const currentDialog = dialogId;
    polling.current = window.setTimeout(async () => {
      try {
        const updated = await client.status(currentDialog, pending.request_id, token);
        if (updated.dialog_id !== currentDialog) return;
        if (!['queued', 'running'].includes(updated.state)) clearPending();
        setItems((previous) => previous.map((item) => item.request_id === updated.request_id ? updated : item));
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : 'Ошибка соединения.');
        if (cause instanceof ApiError && [401, 403, 503].includes(cause.status)) setItems([]);
      }
    }, 2000);
    return () => { if (polling.current) window.clearTimeout(polling.current); };
  }, [items, dialogId, token, client]);

  const submit = useCallback(async (text: string, existing?: ChatRequest): Promise<boolean> => {
    if (busy || !text.trim() || Array.from(text).length > 2000) return false;
    setBusy(true);
    setError(null);
    const saved = unconfirmed.current;
    const requestId = existing?.request_id
      || (saved && (saved.text === null || saved.text === text) ? saved.id : crypto.randomUUID());
    unconfirmed.current = { id: requestId, text };
    savePending(dialogId, requestId);
    try {
      const created = await client.submit(dialogId, requestId, text, token);
      unconfirmed.current = null;
      if (!['queued', 'running'].includes(created.state)) clearPending();
      if (created.dialog_id !== dialogId) return false;
      setItems((previous) => {
        const rest = previous.filter((item) => item.request_id !== requestId);
        return [...rest, created].sort((a, b) => a.sequence - b.sequence);
      });
      return true;
    } catch (cause) {
      if (cause instanceof ApiError) {   // сервер ответил: исход однозначен
        unconfirmed.current = null;
        clearPending();
      }
      setError(cause instanceof ApiError ? cause.message : 'Отправка не подтверждена. Повторите — вопрос не задублируется.');
      if (cause instanceof ApiError && cause.status === 401) onExpired();
      return false;
    } finally {
      setBusy(false);
    }
  }, [busy, client, dialogId, token, onExpired]);

  const pendingDialog = useRef<string | null>(null);
  const newDialog = useCallback(async () => {
    // Повтор нажатия во время запроса использует тот же ID; поздние ответы старого диалога отбрасываются.
    const id = pendingDialog.current || crypto.randomUUID();
    pendingDialog.current = id;
    generation.current += 1;
    setItems([]);
    try {
      await client.newDialog(id, token);
      pendingDialog.current = null;
      unconfirmed.current = null;
      clearPending();
      onDialog(id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Не удалось начать новый диалог.');
      if (cause instanceof ApiError && cause.status === 401) onExpired();
    }
  }, [client, token, onDialog, onExpired]);

  const hiddenAt = useRef<number | null>(null);
  useEffect(() => {
    // Возврат скрытой вкладки: диалог остаётся на экране, допуск и история перепроверяются
    // в фоне; отказ (401/403/503) скрывает выдачу в load. После долгого отсутствия прежняя
    // выдача скрывается до ответа сервера.
    const visible = () => {
      if (document.hidden) {
        hiddenAt.current = Date.now();
        return;
      }
      const away = hiddenAt.current === null ? 0 : Date.now() - hiddenAt.current;
      hiddenAt.current = null;
      if (away > HIDE_AFTER_MS) {
        setItems([]);
        void load();
      } else {
        void load(0, true);
      }
    };
    document.addEventListener('visibilitychange', visible);
    return () => document.removeEventListener('visibilitychange', visible);
  }, [load]);

  return { items, loading, error, hasMore, busy, submit, newDialog,
    reload: () => load(), loadMore: () => load(items.at(-1)?.sequence || 0) };
}
