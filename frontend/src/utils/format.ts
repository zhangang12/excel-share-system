// 🆕 v4 中文格式化工具：金额/日期/数字 适配中文使用习惯。

/** 金额完整显示: ¥1,280,000 (千分位, zh-CN locale) */
export function fmtMoney(n?: number | null, dash = '—'): string {
  if (n == null || n === 0 || Number.isNaN(n)) return dash
  return '¥' + Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

/** KPI 大字金额: 自动转「万」「亿」，节省空间。
 *  例: 1280000 -> "¥128.00万"; 128000000 -> "¥1.28亿"; <1万 -> "¥9,888"  */
export function fmtAmountShort(n?: number | null, dash = '—'): string {
  if (n == null || n === 0 || Number.isNaN(n)) return dash
  const v = Number(n)
  if (Math.abs(v) >= 1e8) return `¥${(v / 1e8).toFixed(2)}亿`
  if (Math.abs(v) >= 1e4) return `¥${(v / 1e4).toFixed(2)}万`
  return '¥' + v.toLocaleString('zh-CN')
}

/** 千分位整数(无¥): 用于"项目数 / 笔数"等非金额场景 */
export function fmtInt(n?: number | null, dash = '—'): string {
  if (n == null || Number.isNaN(n)) return dash
  return Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

/** 日期: 默认 "2026-06-15"; mode="cn" → "2026年6月15日"; mode="md" → "06月15日" */
export function fmtDate(s?: string | null, mode: 'iso' | 'cn' | 'md' = 'iso', dash = '—'): string {
  if (!s) return dash
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s.toString()
  const y = d.getFullYear()
  const m = d.getMonth() + 1
  const day = d.getDate()
  if (mode === 'cn') return `${y}年${m}月${day}日`
  if (mode === 'md') return `${String(m).padStart(2, '0')}月${String(day).padStart(2, '0')}日`
  return `${y}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

/** 日期+时间: "2026-06-15 14:30" */
export function fmtDateTime(s?: string | null, dash = '—'): string {
  if (!s) return dash
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s.toString()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 相对时间(中文): "刚刚 / 5 分钟前 / 3 小时前 / 昨天 / 3 天前 / 2026-06-15" */
export function fmtRelative(s?: string | null, dash = '—'): string {
  if (!s) return dash
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s.toString()
  const diff = Date.now() - d.getTime()
  const min = Math.floor(diff / 60000)
  const hour = Math.floor(diff / 3.6e6)
  const day = Math.floor(diff / 8.64e7)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  if (hour < 24) return `${hour} 小时前`
  if (day === 1) return '昨天'
  if (day < 7) return `${day} 天前`
  return fmtDate(s, 'iso')
}

/** 百分比: 88.5 -> "88.5%"; null -> "—" */
export function fmtPercent(n?: number | null, digits = 0, dash = '—'): string {
  if (n == null || Number.isNaN(n)) return dash
  return Number(n).toFixed(digits) + '%'
}
