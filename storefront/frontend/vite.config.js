import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev server only: /api/availability goes to inventory-api, everything else /api and /config to the
// storefront backend. In the deployed stack nginx does this routing (contracts §6).
const backend = process.env.STOREFRONT_BACKEND_URL || 'http://localhost:8000';
const inventory = process.env.INVENTORY_API_URL || 'http://localhost:8001';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api/availability': inventory,
      '/api': backend,
      '/config': backend,
      '/img': backend,
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.js'],
    globals: true,
  },
});
