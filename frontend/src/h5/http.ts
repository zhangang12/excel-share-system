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
import { API_BASE } from './apiBase'

// baseURL 不写死 '/api'：APP 里页面在 http://localhost，API 在服务器上，
// 相对路径会打到本地包里去（404）。见 apiBase.ts。
export const http = axios.create({ baseURL: API_BASE, timeout: 60000 })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('pms_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => {
    // 🆕 反馈#425：后端在令牌剩余不足一半时用响应头下发新令牌，悄悄换掉——用着就不掉线（与 @/api/index.ts 同口径）
    const t = r?.headers?.['x-pms-refresh-token']
    if (typeof t === 'string' && t) {
      try { localStorage.setItem('pms_token', t) } catch { /* 存储不可用则沿用旧令牌 */ }
    }
    return r
  },
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

  // 🆕 2026-09-23 推广到全公司前：网络类错误 axios 给的是英文
  //    （"Network Error" / "timeout of 60000ms exceeded" / "Request aborted"）。
  //    车间和工地上信号本来就差，这类提示出现得最多，而一线看到英文只会当成系统坏了。
  //    这里只翻译**确定含义**的那几种，其余仍走后端 detail —— 别自作主张改写业务报错。
  const code = String(e?.code || '')
  const msg = String(e?.message || '')
  if (code === 'ECONNABORTED' || /timeout/i.test(msg)) {
    return '网络太慢，这次没等到结果。换个信号好的地方再试一次。'
  }
  if (code === 'ERR_NETWORK' || /Network Error/i.test(msg)) {
    return '连不上服务器。检查一下手机网络，或者稍后再试。'
  }
  if (code === 'ERR_CANCELED' || /aborted|canceled/i.test(msg)) return '已取消'
  const st = e?.response?.status
  if (st === 401) return '登录已过期，请重新登录'
  if (st === 403) return '你的账号没有这项权限'
  if (st === 404) return '没找到这条数据，可能已被删除'
  if (st === 413) return '文件太大，传不上去'
  if (st && st >= 500) return '服务器出错了，已记录。稍后再试，或把这一步告诉管理员。'
  return msg || fallback
}
