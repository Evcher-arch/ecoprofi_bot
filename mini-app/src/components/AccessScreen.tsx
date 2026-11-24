import { Button, Card, Paragraph, Spinner, Title } from '@vkontakte/vkui';

interface Props {
  status: 'loading' | 'unverified' | 'denied' | 'expired' | 'error';
  error?: string;
  onRetry: () => void;
}

export function AccessScreen({ status, error, onRetry }: Props) {
  if (status === 'loading') return <Spinner size="l" aria-label="Проверка входа" />;
  const denied = status === 'denied';
  return (
    <Card mode="shadow" style={{ padding: 20 }}>
      <Title level="2">Найти лекцию по Экобезу</Title>
      <Paragraph>{error || 'Откройте приложение из сообщества «Экопрофи».'}</Paragraph>
      {denied && (
        <Button mode="secondary" href="https://vk.ru/ecoprofi" target="_blank" rel="noopener noreferrer">
          Перейти в клуб Экопрофи
        </Button>
      )}
      <Button onClick={onRetry} style={{ marginTop: 12 }}>
        {denied ? 'Проверить доступ снова' : 'Повторить вход'}
      </Button>
    </Card>
  );
}
