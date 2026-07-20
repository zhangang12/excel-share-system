/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const component: DefineComponent<{}, {}, any>
  export default component
}

// 🆕 桌面客户端（Electron）preload 注入的全局；仅桌面端存在，浏览器为 undefined
interface Window {
  pmsDesktop?: {
    isDesktop: boolean
    version: string
    deviceId: string
  }
}
