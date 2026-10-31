// 🆕 反馈#444 出差时长。出发/结束都是北京时间「YYYY-MM-DD HH:mm」（墙上时间，不带时区），
// 按 UTC 解析只为算差值——避开本机时区和夏令时，两头用同一种算法，差值就是对的。

function parse(s?: string | null): number {
  const m = (s || '').match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : NaN
}

/** 出差分钟数；缺一头或格式不对返回 null */
export function tripMinutes(start?: string | null, end?: string | null): number | null {
  const d = (parse(end) - parse(start)) / 60000
  return Number.isFinite(d) ? d : null
}

/** 「2 天 11 小时 30 分」；结束不晚于出发返回空串 */
export function fmtTripDuration(start?: string | null, end?: string | null): string {
  const m = tripMinutes(start, end)
  if (m == null || m <= 0) return ''
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mi = Math.round(m % 60)
  return [d && `${d} 天`, h && `${h} 小时`, mi && `${mi} 分`].filter(Boolean).join(' ')
}

/** 「10/28 07:30 – 10/30 19:00」 */
export function fmtTripRange(start?: string | null, end?: string | null): string {
  const f = (s?: string | null) => (s || '').slice(5, 16).replace('-', '/')
  return start && end ? `${f(start)} – ${f(end)}` : ''
}
