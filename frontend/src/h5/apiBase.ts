/**
 * API 根地址 —— 网页版与 APP 唯一的分叉点。
 *
 *   网页版（服务器 /h5/）      页面和 API 同源，用相对路径 '/api'
 *   APP（Capacitor 本地包）    页面在 http://localhost，API 在服务器上，**必须写绝对地址**
 *
 * 值在**构建时**注入（`VITE_API_BASE`），不在运行时探测：
 * 探测要多一次首屏往返，而且探错了整个 APP 直接不可用——这种事不该留给运行时。
 *
 * ⚠️ 改成绝对地址之后请求就是跨域的，后端 CORS 必须放行 APP 的 origin
 *   （见 backend/app/main.py 的 _app_origins）。少了那一步，APP 里每个接口都是
 *   CORS 报错，而浏览器控制台在手机上你还看不见。
 */
export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) || '/api'

/** 拼 API 地址。path 以 / 开头，例：api('/agent/chat/stream') */
export const api = (path: string) => `${API_BASE}${path}`

/** 是否跑在原生壳里（Capacitor 会往 window 上挂这个对象） */
export const isNativeApp = typeof window !== 'undefined'
  && !!(window as any).Capacitor?.isNativePlatform?.()
