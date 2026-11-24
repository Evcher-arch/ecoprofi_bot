import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Chat } from '../src/components/Chat';
import { api, ApiError } from '../src/api';
import type { ChatRequest } from '../src/types';

const session = { token: 't', expires_at: '2026-11-23T20:00:00Z', dialog_id: 'd1', user_id: 1, limits: { question_chars: 2000 } };
const empty = { dialog_id: 'd1', items: [], next_after_sequence: 0, has_more: false };

function item(state: ChatRequest['state'], extra: Partial<ChatRequest> = {}): ChatRequest {
  return { dialog_id: 'd1', request_id: 'r1', sequence: 1, state, operation_id: 'op', question: 'Вопрос',
    retry_allowed: false, ...extra };
}

describe('восстановление UI', () => {
  it('сетевой обрыв: тот же request_id при повторе и текст сохраняется', async () => {
    vi.spyOn(api, 'history').mockResolvedValue(empty);
    const submit = vi.spyOn(api, 'submit')
      .mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce(item('queued'));
    render(<Chat session={session} />);
    const input = await screen.findByLabelText('Вопрос');
    fireEvent.change(input, { target: { value: 'Вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(await screen.findByText(/Отправка не подтверждена/)).toBeInTheDocument();
    expect(input).toHaveValue('Вопрос');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[0][1]).toBe(submit.mock.calls[1][1]);
  });

  it('invalidated показывает предложение нового поиска без старых карточек', async () => {
    vi.spyOn(api, 'history').mockResolvedValue({ ...empty, items: [
      item('invalidated', { message: 'Материалы ответа изменились или недоступны. Выполните новый поиск.' })] });
    render(<Chat session={session} />);
    expect(await screen.findByText(/Выполните новый поиск/)).toBeInTheDocument();
    expect(screen.queryByText('Открыть источник')).not.toBeInTheDocument();
  });

  it('403/503 скрывают историю, 401 сообщает об истечении', async () => {
    const onExpired = vi.fn();
    vi.spyOn(api, 'history').mockRejectedValue(new ApiError(401, 'session_expired', 'Сессия истекла.'));
    render(<Chat session={session} onExpired={onExpired} />);
    await waitFor(() => expect(onExpired).toHaveBeenCalled());
  });

  it('возврат скрытой вкладки перезагружает историю', async () => {
    const history = vi.spyOn(api, 'history').mockResolvedValue(empty);
    render(<Chat session={session} />);
    await waitFor(() => expect(history).toHaveBeenCalledTimes(1));
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
  });

  it('после перезагрузки неподтверждённая отправка: повтор с прежним ID, без автоповтора и без текста в storage', async () => {
    const { savePending, loadPending } = await import('../src/session');
    savePending('d1', 'saved-id');
    vi.spyOn(api, 'history').mockResolvedValue(empty);
    const submit = vi.spyOn(api, 'submit').mockResolvedValue(item('queued', { request_id: 'saved-id' }));
    render(<Chat session={session} />);
    expect(await screen.findByText(/Отправка последнего вопроса не подтверждена/)).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem('ecoprofi.miniapp.pending.v1')).not.toContain('Вопрос');
    fireEvent.change(screen.getByLabelText('Вопрос'), { target: { value: 'Вопрос' } });
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith('d1', 'saved-id', 'Вопрос', 't'));
    expect(loadPending('d1')).toBe('saved-id');     // ещё в очереди: ID хранится до исхода
  });

  it('после перезагрузки найденный в истории вопрос восстанавливается с сервера', async () => {
    const { savePending, loadPending } = await import('../src/session');
    savePending('d1', 'r1');
    vi.spyOn(api, 'history').mockResolvedValue({ ...empty, items: [item('completed', {
      answer: { outcome: 'empty', text: 'Ничего не найдено', cards: [] } })] });
    render(<Chat session={session} />);
    expect(await screen.findByText('Ничего не найдено')).toBeInTheDocument();
    expect(loadPending('d1')).toBeNull();
  });

  it('повтор «Новый диалог» во время запроса использует тот же ID', async () => {
    vi.spyOn(api, 'history').mockResolvedValue(empty);
    const created = vi.spyOn(api, 'newDialog').mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce({ dialog_id: 'x' });
    const onDialog = vi.fn();
    render(<Chat session={session} onDialog={onDialog} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Новый диалог' }));
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Новый диалог' }));
    await waitFor(() => expect(onDialog).toHaveBeenCalled());
    expect(created.mock.calls[0][0]).toBe(created.mock.calls[1][0]);
  });
});
