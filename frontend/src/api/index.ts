import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

export const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('pms_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
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
