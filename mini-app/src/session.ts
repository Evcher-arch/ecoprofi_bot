import type { Session } from './types';

const KEY = 'ecoprofi.miniapp.session.v1';
let memory: Session | null = null;

export function loadSession(): Session | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw) {
      const saved = JSON.parse(raw) as Session;
      if (saved.token && saved.dialog_id && saved.expires_at) return saved;
    }
  } catch { /* sessionStorage может быть недоступен в WebView */ }
  return memory;
}

export function saveSession(session: Session): void {
  memory = session;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify({
      token: session.token, expires_at: session.expires_at, dialog_id: session.dialog_id,
      user_id: session.user_id, limits: session.limits,
    }));
  } catch { /* остаётся хранение в памяти */ }
}

export function clearSession(): void {
  memory = null;
  clearPending();
  try { window.sessionStorage.removeItem(KEY); } catch { /* noop */ }
}

// ID незавершённого обращения — единственное, что хранится о вопросе; сам текст не сохраняется.
const PENDING = 'ecoprofi.miniapp.pending.v1';
let pendingMemory: { dialog_id: string; request_id: string } | null = null;

export function savePending(dialogId: string, requestId: string): void {
  pendingMemory = { dialog_id: dialogId, request_id: requestId };
  try { window.sessionStorage.setItem(PENDING, JSON.stringify(pendingMemory)); } catch { /* память */ }
}

export function loadPending(dialogId: string): string | null {
  let value = pendingMemory;
  try {
    const raw = window.sessionStorage.getItem(PENDING);
    if (raw) value = JSON.parse(raw);
  } catch { /* память */ }
  return value && value.dialog_id === dialogId ? value.request_id : null;
}

export function clearPending(): void {
  pendingMemory = null;
  try { window.sessionStorage.removeItem(PENDING); } catch { /* noop */ }
}

export function launchQuery(): string {
  return window.location.search.startsWith('?') ? window.location.search.slice(1) : window.location.search;
}
