import test from 'node:test';
import assert from 'node:assert/strict';

test('официальный Python/PHP пример использует + для пробела', () => {
  assert.equal(new URLSearchParams([['vk_language', 'a b']]).toString(), 'vk_language=a+b');
});

test('официальный JavaScript пример использует %20', () => {
  assert.equal(encodeURIComponent('a b'), 'a%20b');
});
