import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  // Прокси только для локальной разработки и e2e с подменой API; в production — nginx-шлюз.
  server: { host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: { '/api/miniapp/v1': 'http://127.0.0.1:8788' } },
  build: { outDir: 'dist', emptyOutDir: true },
});
