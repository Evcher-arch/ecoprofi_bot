import { useState } from 'react';
import { Button, Card, Paragraph, Spinner, Textarea, Title } from '@vkontakte/vkui';
import { MaterialCard } from './MaterialCard';
import { NEW_DIALOG_HINT, OutcomeHint, QUESTION_PLACEHOLDER, SearchTips } from './SearchTips';
import type { Session } from '../types';
import { useChat } from '../useChat';
import { api } from '../api';

// Ответ, ожидание и сообщения отделяются от вопроса участника линией и отступом.
const SEPARATED = {
  marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--vkui--color_separator_primary)',
} as const;

interface Props {
  session: Session;
  onDialog?: (id: string) => void;
  onExpired?: () => void;
}

export function Chat({ session, onDialog, onExpired }: Props) {
  const [text, setText] = useState('');
  const { items, loading, error, busy, submit, newDialog } =
    useChat(session.dialog_id, session.token, api, onDialog, onExpired);
  const length = Array.from(text).length;

  async function send() {
    if (!text.trim() || length > 2000 || busy) return;
    // Текст очищается только после принятия вопроса сервером; при ошибке остаётся редактируемым.
    if (await submit(text)) setText('');
  }

  return (
    <main style={{ maxWidth: 760, margin: '0 auto', padding: 16, boxSizing: 'border-box' }}>
      <Title level="1">Найти лекцию по Экобезу</Title>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, margin: '12px 0' }}>
        <Button mode="secondary" onClick={() => void newDialog()}>Новый диалог</Button>
        <Paragraph style={{ opacity: 0.75 }}>{NEW_DIALOG_HINT}</Paragraph>
      </div>
      <section aria-label="История диалога">
        {loading && <Spinner aria-label="Загрузка истории" />}
        {error && <Paragraph role="alert">{error}</Paragraph>}
        {items.map((item) => (
          <Card key={item.request_id} mode="outline" style={{ padding: 14, marginBottom: 12 }}>
            <Paragraph weight="2">{item.question}</Paragraph>
            {(item.state === 'queued' || item.state === 'running') && (
              <div role="status" aria-live="polite"
                style={{ ...SEPARATED, display: 'flex', alignItems: 'center', gap: 10, opacity: 0.8 }}>
                <Spinner size="s" aria-label="Идёт поиск" style={{ flex: 'none', width: 'auto', margin: 0 }} />
                <Paragraph>Поиск материалов…</Paragraph>
              </div>
            )}
            {item.answer && <div style={SEPARATED}>
              <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.answer.text}</Paragraph>
              <OutcomeHint outcome={item.answer.outcome} />
              {item.answer.cards.map((card) => <MaterialCard key={card.material_id} card={card} />)}
            </div>}
            {item.message && <Paragraph style={SEPARATED}>{item.message}</Paragraph>}
            {item.retry_allowed && <Button mode="secondary" onClick={() => void submit(item.question, item)}>Повторить</Button>}
          </Card>
        ))}
      </section>
      <form onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <Textarea
          aria-label="Вопрос"
          placeholder={QUESTION_PLACEHOLDER}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <Paragraph aria-live="polite">{length}/2000</Paragraph>
        <SearchTips />
        <Button size="l" stretched type="submit" disabled={!text.trim() || length > 2000 || busy}>
          {busy ? 'Отправка…' : 'Отправить'}
        </Button>
      </form>
    </main>
  );
}
