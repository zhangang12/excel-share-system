/**
 * H5 专用 http 客户端。
 *
 * 刻意不复用 @/api/index.ts：那个文件 import 了 element-plus 的 ElMessage，
 * 一引就把整个 element-plus（以及顺带的 vxe-table）拖进 H5 包里。
 * H5 是给手机 4G 用的，只有登录和助手两页，不该背这个体积。
 *
 * 错误提示交给各页面自己按设计稿渲染，不弹全局 toast。
 */
import axios from 'axios'

export const http = axios.create({ baseURL: '/api', timeout: 60000 })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('pms_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => r,
  (err) => {
    // 401 一律回登录页；其余错误原样抛给调用方，由页面把 detail 显示出来
    if (err?.response?.status === 401) {
      localStorage.removeItem('pms_token')
      localStorage.removeItem('pms_user')
      if (!location.hash.startsWith('#/login')) location.hash = '#/login'
    }
    return Promise.reject(err)
  },
)

/** 从后端错误里取可读文案；取不到就给一句兜底，绝不显示 "[object Object]" */
export function errText(e: any, fallback = '操作失败'): string {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d) && d[0]?.msg) return String(d[0].msg)
  return e?.message || fallback
}
