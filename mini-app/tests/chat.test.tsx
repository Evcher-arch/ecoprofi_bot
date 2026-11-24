import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Chat } from '../src/components/Chat';
import { api } from '../src/api';
import type { History } from '../src/types';

const session = { token: 'token', expires_at: '2026-11-23T20:00:00Z', dialog_id: 'd1', user_id: 1, limits: { question_chars: 2000 } };

function history(state: string): History {
  return { dialog_id: 'd1', next_after_sequence: 1, has_more: false, items: [{
    dialog_id: 'd1', request_id: 'r1', sequence: 1, state: state as never,
    operation_id: 'op', question: 'Вопрос', retry_allowed: false,
    answer: state === 'completed' ? { outcome: 'found', text: 'Ответ', cards: [] } : undefined,
  }] };
}

describe('состояния чата', () => {
  it('queued/running/found', async () => {
    for (const state of ['queued', 'running', 'completed']) {
      vi.spyOn(api, 'history').mockResolvedValue(history(state));
      const { unmount } = render(<Chat session={session} />);
      await waitFor(() => expect(api.history).toHaveBeenCalled());
      if (state === 'completed') expect(await screen.findByText('Ответ')).toBeInTheDocument();
      else expect(await screen.findByText('Поиск материалов…')).toBeInTheDocument();
      unmount();
    }
  });

  it('счётчик code points и запрет длинного вопроса', async () => {
    vi.spyOn(api, 'history').mockResolvedValue({ dialog_id: 'd1', items: [], next_after_sequence: 0, has_more: false });
    render(<Chat session={session} />);
    const input = await screen.findByLabelText('Вопрос');
    fireEvent.change(input, { target: { value: '😀'.repeat(2001) } });
    expect(screen.getByText('2001/2000')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled();
  });
});
