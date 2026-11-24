import { Card, Link, Paragraph, Text } from '@vkontakte/vkui';
import type { MaterialCard as Material } from '../types';

const HOSTS = new Set(['vk.com', 'vk.ru', 'vkvideo.ru', 'm.vk.com', 'm.vk.ru']);

export function safeUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:') return null;
    if ([...HOSTS].some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`))) return url.href;
  } catch { /* невалидная ссылка не показывается */ }
  return null;
}

function clock(ms: number): string {
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function details(card: Material): string[] {
  const rows: string[] = [];
  if (card.lecture?.title) rows.push(`Лекция: ${card.lecture.title}`);
  if (card.type === 'episode' && card.start_ms != null && card.end_ms != null) {
    rows.push(`Эпизод ${clock(card.start_ms)}–${clock(card.end_ms)}`);
  }
  if (card.page != null) rows.push(`Страница ${card.page}${card.pages_total ? ` из ${card.pages_total}` : ''}`);
  if (card.recognized) rows.push('Текст распознан');
  if (card.requisites) {
    rows.push(`Письмо № ${card.requisites.number}`);
    if (card.requisites.date) rows.push(`Дата: ${card.requisites.date}`);
    if (card.requisites.sender) rows.push(`Отправитель: ${card.requisites.sender}`);
  } else if (card.requisites_note) rows.push(card.requisites_note);
  if (card.type === 'material') rows.push('Материал целиком');
  if (card.type === 'navigation') rows.push('Запись целиком');
  return rows;
}

export function MaterialCard({ card }: { card: Material }) {
  const href = safeUrl(card.url);
  return (
    <Card mode="outline" style={{ padding: 14, marginTop: 10 }}>
      <Paragraph weight="1">{card.title}</Paragraph>
      {details(card).map((line) => <Text key={line} style={{ display: 'block', marginTop: 4 }}>{line}</Text>)}
      {card.excerpt && <Paragraph style={{ marginTop: 8 }}>«{card.excerpt}»</Paragraph>}
      <Paragraph style={{ marginTop: 8 }}>{card.explanation}</Paragraph>
      {href && <Link href={href} target="_blank" rel="noopener noreferrer">Открыть источник</Link>}
    </Card>
  );
}
