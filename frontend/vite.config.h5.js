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
/**
 * dev 下 Vite 只会为 HTML 请求吐 root 的 index.html —— 那是桌面版入口，
 * 于是 http://localhost:5199/h5/ 打开的是桌面 app（白屏，因为它没有 /h5 路由）。
 * rollupOptions.input 只在 build 时生效，救不了 dev。
 * 这个小中间件把 /h5/ 重写到 /h5.html，让开发地址和生产地址都是 /h5/，不用记两套。
 */
const serveH5Entry = () => ({
  name: 'h5-dev-entry',
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      // 目标要带 base 前缀：dev 下真正能取到入口的路径是 /h5/h5.html
      if (req.url === '/h5/' || req.url === '/h5' || req.url === '/') req.url = '/h5/h5.html'
      next()
    })
  },
})

export default defineConfig({
  plugins: [vue(), serveH5Entry()],
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
