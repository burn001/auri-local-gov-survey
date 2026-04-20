import { defineConfig } from 'vite';

export default defineConfig({
  base: '/auri-local-gov-survey/',
  build: {
    outDir: 'dist',
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
    },
  },
});
