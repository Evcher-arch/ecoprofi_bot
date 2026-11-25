import { useId, useState } from 'react';
import { Button, Paragraph } from '@vkontakte/vkui';
import type { Answer } from '../types';

// Тексты FR-15 — дословно из specs/004-vk-mini-app/contracts/frontend-gateway.md#рекомендации-по-поиску.
export const QUESTION_PLACEHOLDER = 'Например: «4-ОС сборы», «инвентаризация выбросов», «2909-р»';
export const NEW_DIALOG_HINT = 'Начните заново, если меняете тему.';

export const OUTCOME_HINTS: Partial<Record<Answer['outcome'], string>> = {
  clarify: 'Можно ответить коротко — например, «для воздуха» или «форма 2-ТП»: уточнение добавится '
    + 'к прошлому вопросу. Если тема новая, просто задайте новый вопрос.',
  empty: 'Попробуйте другие слова: сокращение вместо полного названия, номер документа или название формы.',
};

const TIPS: { title: string; text: string }[] = [
  { title: 'Пишите 2–4 ключевых слова, а не целую фразу.',
    text: '«плата за сбросы» сработает лучше, чем «подскажите, как правильно считать плату, если у нас есть '
      + 'сбросы»: чем длиннее вопрос, тем больше его слов должно встретиться в одном фрагменте лекции.' },
  { title: 'Используйте слова, которые звучат в лекциях.',
    text: 'Поиск находит сказанное дословно, с учётом окончаний («выброс», «выбросы», «выбросов» — одно слово). '
      + 'Синонимы и пересказ он узнаёт хуже.' },
  { title: 'Пробуйте и сокращение, и полное название.',
    text: 'Не нашлось «водные биоресурсы» — попробуйте «ВБР», и наоборот. То же для «НВОС» / «негативное '
      + 'воздействие», «ПЭК» / «производственный экологический контроль».' },
  { title: 'Номер документа или формы — как в оригинале:',
    text: '«2909-р», «4-ОС», «2-ТП (воздух)», «приказ 369». По номеру материал находится даже без совпадения в тексте.' },
  { title: 'Одна тема — один вопрос.',
    text: '«выбросы и отходы и сбросы» лучше разбить на три отдельных вопроса.' },
];

export function OutcomeHint({ outcome }: { outcome: Answer['outcome'] }) {
  const hint = OUTCOME_HINTS[outcome];
  return hint ? <Paragraph className="outcome-hint" style={{ marginTop: 8, opacity: 0.75 }}>{hint}</Paragraph> : null;
}

export function SearchTips() {
  const [open, setOpen] = useState(false);
  const panel = useId();
  return (
    <section style={{ margin: '8px 0 12px' }}>
      <Button mode="tertiary" aria-expanded={open} aria-controls={panel} onClick={() => setOpen((value) => !value)}>
        Как искать
      </Button>
      <ol id={panel} hidden={!open} style={{ margin: '8px 0 0', paddingLeft: 20, overflowWrap: 'anywhere' }}>
        {TIPS.map((tip) => (
          <li key={tip.title} style={{ marginBottom: 8 }}>
            <Paragraph><strong>{tip.title}</strong> {tip.text}</Paragraph>
          </li>
        ))}
      </ol>
    </section>
  );
}
