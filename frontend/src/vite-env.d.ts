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
    /** Vue 挂载完成后通知主进程（关启动页、亮主窗口） */
    notifyReady?: () => void
    /** 主动检查更新（桌面端「检查更新」按钮） */
    checkUpdate?: () => void
    /** 订阅更新状态：checking / available / not-available / downloaded / error */
    onUpdateStatus?: (cb: (s: { status: string; version?: string; message?: string }) => void) => void
    /** 🆕 登录成功后静默检查更新（30 分钟节流，有新版静默下载后提示重启） */
    checkUpdateSilent?: () => void
    /** 🆕 登录前强制版本检查：true = 版本过低，主进程已切到强制更新页，别继续登录 */
    enforceVersion?: () => Promise<boolean>
  }
}
