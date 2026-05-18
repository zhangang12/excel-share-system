import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({
    plugins: [vue()],
    resolve: {
        alias: {
            '@': fileURLToPath(new URL('./src', import.meta.url)),
        },
    },
    server: {
        host: '0.0.0.0',
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://backend:8000',
                changeOrigin: true,
                timeout: 60000,
                proxyTimeout: 60000,
                configure: function (proxy, _options) {
                    proxy.on('error', function (err, _req, _res) {
                        console.log('[vite-proxy] /api error:', err.message);
                    });
                },
            },
            '/ws': {
                target: 'ws://backend:8000',
                ws: true,
                changeOrigin: true,
                configure: function (proxy, _options) {
                    proxy.on('error', function (err, _req, _res) {
                        console.log('[vite-proxy] /ws error:', err.message);
                    });
                },
            },
        },
    },
});
