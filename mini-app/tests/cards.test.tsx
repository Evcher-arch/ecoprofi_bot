import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MaterialCard, safeUrl } from '../src/components/MaterialCard';
import type { MaterialCard as Material } from '../src/types';

const base: Material = {
  type: 'navigation', material_id: 1, title: '<без HTML>', lecture: null,
  explanation: 'Проверка', url: 'https://vk.ru/video-1_2',
};

describe('карточки источников', () => {
  it('шесть типов и безопасный текст', () => {
    for (const type of ['episode', 'page', 'letter', 'document', 'material', 'navigation'] as const) {
      const { unmount } = render(<MaterialCard card={{ ...base, type, start_ms: 1000, end_ms: 2000,
        page: 2, pages_total: 8, recognized: true, requisites: null, requisites_note: 'не подтверждено' }} />);
      expect(screen.getByText('<без HTML>')).toBeInTheDocument();
      expect(document.querySelector('script')).toBeNull();
      unmount();
    }
  });

  it('подтверждённые реквизиты и null не выдумываются', () => {
    render(<MaterialCard card={{ ...base, type: 'letter', requisites: null, requisites_note: 'Реквизиты не подтверждены' }} />);
    expect(screen.getByText('Реквизиты не подтверждены')).toBeInTheDocument();
    expect(screen.queryByText(/Письмо №/)).not.toBeInTheDocument();
  });

  it('HTTPS allowlist', () => {
    expect(safeUrl('https://vk.ru/video-1_2')).toContain('vk.ru');
    expect(safeUrl('https://sub.vkvideo.ru/video')).toContain('vkvideo');
    expect(safeUrl('javascript:alert(1)')).toBeNull();
    expect(safeUrl('https://evil.test/vk.ru')).toBeNull();
    expect(safeUrl('https://vk.ru.evil.test/')).toBeNull();
  });
});
