import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

export const http = axios.create({
  // 🆕 桌面客户端打包时以 VITE_API_BASE（如 http://8.141.123.141）构建，直连后端；
  //   浏览器构建不设该变量，baseURL 仍是 '/api'（走 Vite 代理 / nginx），行为与原来一致。
  baseURL: (import.meta.env.VITE_API_BASE ?? '') + '/api',
  timeout: 30000,
})

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('pms_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  // 🆕 桌面客户端在线统计：preload 注入 window.pmsDesktop（仅桌面端有），带统计头，
  //   后端中间件按 device_id upsert（60s 节流）；浏览器无此全局，不加头、行为不变。
  const dk = window.pmsDesktop
  if (dk?.isDesktop) {
    config.headers['X-PMS-Client'] = `desktop/${dk.version}`
    config.headers['X-PMS-Device'] = dk.deviceId
    try {
      const u = JSON.parse(localStorage.getItem('pms_user') || 'null')
      if (u?.username) config.headers['X-PMS-User'] = String(u.username)
    } catch { /* pms_user 解析失败则不带用户名头，不影响请求 */ }
  }
  // 🆕 #188：文件下载(blob)不设前端超时——打包 zip/CAD 图纸等大文件在慢网络下
  //   远超全局 30s，被 axios 掐断后表现为「请求超时/打包下载失败」。
  //   服务端 nginx proxy_read_timeout=300s 仍然兜底，不会无限挂起。
  if (config.responseType === 'blob') config.timeout = 0
  return config
})

function extractErrorMessage(err: AxiosError): string {
  // 1. 网络层错误（没收到任何响应）
  if (!err.response) {
    if (err.code === 'ECONNABORTED' || /timeout/i.test(err.message)) {
      return '请求超时，请检查网络或重试'
    }
    if (err.code === 'ERR_NETWORK' || /network error/i.test(err.message)) {
      return '网络连接失败，请检查后端是否启动'
    }
    if (err.code === 'ERR_CANCELED') {
      return ''  // 主动取消，不弹
    }
    return `网络错误：${err.message || '未知'}`
  }

  // 2. 拿到响应了，按状态码归类
  const status = err.response.status
  const data = err.response.data as unknown

  // 优先解析后端 detail
  if (data && typeof data === 'object') {
    const detail = (data as { detail?: unknown }).detail
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => {
          const loc = Array.isArray(d.loc)
            ? d.loc.filter((x) => x !== 'body' && x !== 'query' && x !== 'path').join('.')
            : ''
          return loc ? `${loc}: ${d.msg}` : d.msg ?? ''
        })
        .filter(Boolean)
        .join('；')
    }
    if (typeof detail === 'string') return detail
    const msg = (data as { message?: unknown }).message
    if (typeof msg === 'string') return msg
  }
  if (typeof data === 'string' && data) return data

  // 没拿到具体消息，按状态码兜底
  const STATUS_MAP: Record<number, string> = {
    400: '请求参数错误',
    401: '未登录或登录已过期',
    403: '无权操作',
    404: '资源不存在',
    405: '不支持该操作',
    409: '数据冲突',
    413: '上传文件过大',
    422: '校验失败',
    429: '请求过于频繁，请稍后再试',
    500: '服务器内部错误',
    502: '服务暂不可达',
    503: '服务暂时不可用',
    504: '后端响应超时',
  }
  return STATUS_MAP[status] || `请求失败（HTTP ${status}）`
}

http.interceptors.response.use(
  (res) => res,
  (err: AxiosError) => {
    const status = err.response?.status
    const url = err.config?.url || ''
    const isLoginRequest = url.includes('/auth/login')

    if (status === 401 && !isLoginRequest) {
      // 已登录态过期 → 清认证、跳登录
      localStorage.removeItem('pms_token')
      localStorage.removeItem('pms_user')
      if (location.pathname !== '/login') {
        ElMessage.warning('登录已过期，请重新登录')
        location.href = '/login'
      }
    } else {
      const msg = extractErrorMessage(err)
      if (msg) ElMessage.error(msg)
    }
    return Promise.reject(err)
  },
)
