import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Chat } from '../src/components/Chat';
import { NEW_DIALOG_HINT, OUTCOME_HINTS, QUESTION_PLACEHOLDER } from '../src/components/SearchTips';
import { api } from '../src/api';
import type { Answer, History } from '../src/types';

const session = { token: 'token', expires_at: '2026-11-23T20:00:00Z', dialog_id: 'd1', user_id: 1, limits: { question_chars: 2000 } };

function history(outcome: Answer['outcome']): History {
  return { dialog_id: 'd1', next_after_sequence: 1, has_more: false, items: [{
    dialog_id: 'd1', request_id: 'r1', sequence: 1, state: 'completed', operation_id: 'op',
    question: 'Вопрос', retry_allowed: false, answer: { outcome, text: `Ответ ${outcome}`, cards: [] },
  }] };
}

describe('рекомендации по поиску (FR-15)', () => {
  it('пример в поле, подпись к новому диалогу, блок свёрнут и раскрывается', async () => {
    vi.spyOn(api, 'history').mockResolvedValue({ dialog_id: 'd1', items: [], next_after_sequence: 0, has_more: false });
    render(<Chat session={session} />);
    const input = await screen.findByLabelText('Вопрос');
    expect(input).toHaveAttribute('placeholder', QUESTION_PLACEHOLDER);
    expect(screen.getByText(NEW_DIALOG_HINT)).toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: 'Как искать' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText(/Пишите 2–4 ключевых слова/)).not.toBeVisible();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/Пишите 2–4 ключевых слова/)).toBeVisible();
    expect(screen.getByText(/«ВБР»/)).toBeVisible();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it.each(['clarify', 'empty'] as const)('дополнение под ответом %s', async (outcome) => {
    vi.spyOn(api, 'history').mockResolvedValue(history(outcome));
    render(<Chat session={session} />);
    expect(await screen.findByText(`Ответ ${outcome}`)).toBeInTheDocument();
    expect(screen.getByText(OUTCOME_HINTS[outcome]!)).toBeInTheDocument();
  });

  it('у найденного ответа дополнения нет', async () => {
    vi.spyOn(api, 'history').mockResolvedValue(history('found'));
    render(<Chat session={session} />);
    expect(await screen.findByText('Ответ found')).toBeInTheDocument();
    expect(screen.queryByText(OUTCOME_HINTS.clarify!)).not.toBeInTheDocument();
    expect(screen.queryByText(OUTCOME_HINTS.empty!)).not.toBeInTheDocument();
  });
});
