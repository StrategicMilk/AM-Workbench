import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'path';

const kernelApiOrigin = process.env.VETINARI_KERNEL_API_ORIGIN ?? 'http://127.0.0.1:5000';

export default defineConfig({
  plugins: [svelte()],
  root: '.',
  resolve: {
    alias: {
      '$lib': resolve('./src/lib'),
      '$components': resolve('./src/components'),
      '$views': resolve('./src/views'),
    },
  },
  build: {
    outDir: '../static/svelte',
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      input: resolve('./src/main.js'),
      output: {
        entryFileNames: 'js/[name].js',
        chunkFileNames: 'js/[name]-[hash].js',
        assetFileNames: (info) => {
          if (info.name && info.name.endsWith('.css')) {
            return 'css/[name][extname]';
          }
          return 'assets/[name]-[hash][extname]';
        },
        manualChunks(id) {
          const normalized = id.replace(/\\/g, '/');
          if (normalized.includes('/node_modules/chart.js/')) return 'vendor-chart';
          if (normalized.includes('/node_modules/marked/')) return 'vendor-marked';
          if (normalized.includes('/node_modules/highlight.js/')) return 'vendor-hljs';
          if (normalized.includes('/src/views/')) {
            const viewName = normalized
              .split('/src/views/')[1]
              .replace(/\.[^.]+$/, '')
              .replace(/[^A-Za-z0-9_-]/g, '-')
              .toLowerCase();
            return `view-${viewName}`;
          }
          if (normalized.includes('/src/lib/components/workbench/')) return 'workbench-components';
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': kernelApiOrigin,
      '/health': kernelApiOrigin,
      '/static': kernelApiOrigin,
    },
  },
});
