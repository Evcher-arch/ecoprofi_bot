import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  testMatch: '*.spec.ts',
  use: { baseURL: 'http://127.0.0.1:5173', viewport: { width: 390, height: 844 } },
  webServer: [
    { command: 'node tests/e2e/api-stub.mjs', url: 'http://127.0.0.1:8788/health', reuseExistingServer: false },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: false },
  ],
});
