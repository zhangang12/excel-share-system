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

/** 🆕 物料编码显示格式化: 存储值为 9 位数字(大类1+中类2+细分2+流水4),
 *  显示为「大类-中类细分-流水」加短横线,如 101010001 → 1-0101-0001。
 *  仅格式化标准 9 位纯数字编码;手工填的/历史非标编码原样返回(向后兼容、零风险,不改存储值)。 */
export function fmtMatCode(code?: string | null, dash = ''): string {
  if (!code) return dash
  const s = String(code).trim()
  if (/^\d{9}$/.test(s)) return `${s.slice(0, 1)}-${s.slice(1, 5)}-${s.slice(5)}`
  return s
}

/** 🆕 反馈#387「字体重复」：规格里同一段被重复拼接。
 *  现场：成本审计→无价入库 显示「传动外协 · J01-油缸法兰 · J01-油缸法兰」。
 *  来源是合并收货把多行的规格用「·」串起来，同名零件就串出重复段。
 *  这里按「·」拆开去重（顺序保持首次出现的顺序，不排序——排序会打乱人习惯的阅读次序）。 */
export function dedupSpec(spec?: string | null): string {
  const parts = String(spec ?? '').split('·').map(p => p.trim()).filter(Boolean)
  return [...new Set(parts)].join(' · ')
}

/** 🆕 #387：列表里「名称 · 规格」的规格段。规格与名称相同、或名称里已经含了它，就不再重复显示。
 *  返回空字符串表示「不用显示规格」——调用方用 v-if 控制那个 span。 */
export function specOf(name?: string | null, spec?: string | null): string {
  const n = String(name ?? '').trim()
  const s = dedupSpec(spec)
  if (!s || s === n || (n && n.includes(s))) return ''
  return s
}
