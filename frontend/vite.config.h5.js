import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

/**
 * H5 独立构建 —— 与网页端 / 桌面客户端完全分开。
 *
 *   npm run build       → dist/      网页端（不含任何 H5 代码）
 *   npm run build:h5    → dist-h5/   H5（不含 element-plus / vxe-table）
 *
 * 两条构建线互不引用：H5 的源码只在 src/h5/ 下，且不 import 任何 @/ 路径，
 * 所以改 H5 不会让网页端产物有任何变化，反过来也一样。
 *
 * base='/h5/'：外层 nginx 以 /h5/ 前缀托管，资源必须走这个前缀才找得到。
 */
export default defineConfig({
  plugins: [vue()],
  root: __dirname,
  base: '/h5/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist-h5',
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL('./h5.html', import.meta.url)),
    },
    // 只有两页，产物很小；把告警阈值调低，体积一旦失控立刻能看见
    chunkSizeWarningLimit: 300,
  },
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API || 'http://backend:8000',
        changeOrigin: true,
      },
    },
  },
})
