import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({
    plugins: [vue()],
    // 桌面客户端（Electron file:// 加载）构建时设了 VITE_API_BASE，
    // 资源必须走相对路径 './'，否则 file:// 下 /assets/... 会解析到磁盘根目录导致白屏；
    // 浏览器/docker 构建不设 VITE_API_BASE，保持 '/' 不变（history 路由深链接不受影响）。
    base: process.env.VITE_API_BASE ? './' : '/',
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
