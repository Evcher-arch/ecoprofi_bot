import type { ChatRequest, History, Session } from './types';

const PREFIX = '/api/miniapp/v1';
const TIMEOUT = 15_000;

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public retryable = false) {
    super(message);
  }
}

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), TIMEOUT);
  try {
    const response = await fetch(`${PREFIX}${path}`, {
      ...options,
      signal: controller.signal,
      credentials: 'same-origin',
      headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
    if (response.status === 204) return undefined as T;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = data.error || {};
      throw new ApiError(response.status, error.code || 'service_unavailable',
        error.message || 'Сервис временно недоступен.', Boolean(error.retryable));
    }
    return data as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

export const api = {
  login: (launchQuery: string) => request<Session>('/session', {
    method: 'POST', body: JSON.stringify({ launch_query: launchQuery }),
  }),
  resume: (launchQuery: string, token: string) => request<Omit<Session, 'token'>>('/session/resume', {
    method: 'POST', body: JSON.stringify({ launch_query: launchQuery }),
  }, token),
  logout: (token: string) => request<void>('/session', { method: 'DELETE' }, token),
  newDialog: (dialogId: string, token: string) => request<{ dialog_id: string }>('/dialogs', {
    method: 'POST', body: JSON.stringify({ dialog_id: dialogId }),
  }, token),
  history: (dialogId: string, token: string, after = 0, limit = 50) =>
    request<History>(`/dialogs/${dialogId}/history?after_sequence=${after}&limit=${limit}`, {}, token),
  submit: (dialogId: string, requestId: string, text: string, token: string) =>
    request<ChatRequest>(`/dialogs/${dialogId}/requests`, {
      method: 'POST', body: JSON.stringify({ request_id: requestId, text }),
    }, token),
  status: (dialogId: string, requestId: string, token: string) =>
    request<ChatRequest>(`/dialogs/${dialogId}/requests/${requestId}`, {}, token),
};
