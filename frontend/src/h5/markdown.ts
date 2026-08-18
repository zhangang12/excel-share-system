/**
 * 助手回复的 Markdown 渲染。
 *
 * ⚠️ `html: false` 与下游的 v-html 是**成对**的，谁也不能单独改：
 * markdown-it 在 html:false 时把原始 HTML 转义成文本，v-html 才是安全的。
 * 一旦为了「让模型画个好看的卡片」把它开成 true，或者绕过 render 直接
 * v-html 模型输出，XSS 防线当场归零（手册 3.4.2）。
 *
 * 攻击面不只是「模型自己乱写」：工具返回的数据里带着用户自由填写的
 * 备注、OA 详情、记录值，里面完全可以有 <img src=x onerror=...>，
 * 模型很可能原样复述出来。
 */
import MarkdownIt from 'markdown-it'

/**
 * 语义着色标记：`[[danger:已过 55 天]]` → 红字。
 *
 * **为什么要绕这一圈**：颜色只能由前端加，因为后端不许吐 HTML（见上面那条红线）。
 * 所以后端只发一个**语义**标记（这条是危险/警告/正常），由这里翻成受控的 span。
 *
 * 安全性：
 *   · 档位取自**白名单**，class 名不可能被数据控制；
 *   · 文本用 `text` token 推进去，由 markdown-it 自己转义 —— 数据里带
 *     `<img onerror=...>` 也只会变成字面量；
 *   · 白名单外的写法**原样当普通文字**，不做任何解析。
 *
 * ⚠️ 前端老包遇到新标记会**原样显示 `[[danger:…]]`**。这是刻意的取舍：
 *   宁可露出一段丑字符，也不要为了兼容去放开 html。H5 与后端同一次部署，
 *   APP 侧会热更新，实际窗口很短。
 */
const TONES = new Set(['danger', 'warn', 'good', 'muted'])

function tonePlugin(md: MarkdownIt): void {
  md.inline.ruler.before('emphasis', 'h5tone', (state, silent) => {
    const src = state.src
    const start = state.pos
    if (src.charCodeAt(start) !== 0x5B || src.charCodeAt(start + 1) !== 0x5B) return false
    const end = src.indexOf(']]', start + 2)
    if (end < 0) return false
    const body = src.slice(start + 2, end)
    // ⚠️ 分隔符**必须是冒号，不能用竖线**：这些标记要放进 markdown 表格的
    //   单元格里，而竖线是表格的列分隔符 —— 用 `|` 会把一行切成好几列，
    //   整张表当场散掉（实测）。
    const bar = body.indexOf(':')
    if (bar <= 0) return false
    const tone = body.slice(0, bar)
    if (!TONES.has(tone)) return false          // 白名单之外一律不认
    const text = body.slice(bar + 1)
    if (!text) return false
    if (!silent) {
      let t = state.push('html_inline', '', 0)
      t.content = `<span class="h5-tone h5-tone--${tone}">`
      t = state.push('text', '', 0)
      t.content = text                           // ← text token 会被转义，安全
      t = state.push('html_inline', '', 0)
      t.content = '</span>'
    }
    state.pos = end + 2
    return true
  })
}

/* ══════════════════════════════ 图 ══════════════════════════════
 *
 * 后端发 ```pmschart 围栏 + 一段**纯数据 JSON**，SVG 在这里拼。
 *
 * ⚠️ 为什么不让后端直接吐 SVG：同上面那条红线。后端一旦开始吐标签，
 *    `html:false` 就形同虚设 —— 而工具数据里带着用户自由填的备注、
 *    物料名、OA 详情，`<img src=x onerror=…>` 完全可能混在里面。
 *    这里 SVG 的**结构由代码写死**，来自数据的只有数字和文字，
 *    文字一律走 esc() 转义。
 *
 * ⚠️ JSON 解析失败/字段不对 → **什么都不画**，绝不把原文回吐。
 *    半张图比没有图更让人困惑，露出 JSON 更糟。
 */
const CHART_TONES: Record<string, string> = {
  danger: 'var(--h5-danger)', warn: 'var(--h5-warn)',
  good: 'var(--h5-good)', muted: 'var(--h5-ink-4)',
}

interface ChartRow { label: string; value: number; text: string; tone: string }

/** SVG 里出现的每一段文字都必须过这里 */
function esc(s: string): string {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string))
}

/**
 * 发散条形图：中线是今天，左边超期、右边还剩。
 * 「哪些项目快到期了」这问题，一张图比十几行数字快得多。
 */
function barChart(data: { title?: string; zero?: string; rows: ChartRow[] }): string {
  const rows = data.rows.filter(
    (r) => r && typeof r.value === 'number' && Number.isFinite(r.value))
  if (rows.length < 2) return ''
  const W = 320, LAB = 84, VAL = 62, ROW = 26, PAD = 8
  const mid = LAB + (W - LAB - VAL) / 2
  const half = (W - LAB - VAL) / 2 - 4
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 1)
  const top = data.title ? 20 : 4
  const H = top + rows.length * ROW + PAD

  const parts: string[] = []
  if (data.title) {
    parts.push(`<text x="0" y="12" class="pc-title">${esc(data.title)}</text>`)
  }
  // 中线（今天）
  parts.push(`<line x1="${mid}" y1="${top - 2}" x2="${mid}" y2="${H - PAD + 2}" class="pc-axis"/>`)
  rows.forEach((r, i) => {
    const y = top + i * ROW
    const w = Math.max(2, (Math.abs(r.value) / max) * half)
    const neg = r.value < 0
    const x = neg ? mid - w : mid
    const fill = CHART_TONES[r.tone] || CHART_TONES.muted
    parts.push(
      `<text x="0" y="${y + 14}" class="pc-lab">${esc(r.label)}</text>`,
      `<rect x="${x.toFixed(1)}" y="${y + 5}" width="${w.toFixed(1)}" height="12" rx="3" fill="${fill}"/>`,
      `<text x="${W}" y="${y + 15}" class="pc-val" fill="${fill}">${esc(r.text)}</text>`)
  })
  if (data.zero) {
    parts.push(`<text x="${mid}" y="${H - 1}" class="pc-zero">${esc(data.zero)}</text>`)
  }
  return `<div class="pmschart"><svg viewBox="0 0 ${W} ${H + (data.zero ? 6 : 0)}" `
       + `role="img" aria-label="${esc(data.title || '图')}">${parts.join('')}</svg></div>`
}

function chartPlugin(md: MarkdownIt): void {
  const fallback = md.renderer.rules.fence!
  md.renderer.rules.fence = (tokens, idx, opts, env, self) => {
    const t = tokens[idx]
    if ((t.info || '').trim() !== 'pmschart') return fallback(tokens, idx, opts, env, self)
    try {
      const data = JSON.parse(t.content)
      if (!data || !Array.isArray(data.rows)) return ''
      return barChart(data)          // 目前只有 bar 一种；不认的 kind 就不画
    } catch {
      return ''                      // ⚠️ 坏数据什么都不画，绝不回吐原文
    }
  }
}

const md = new MarkdownIt({
  html: false,      // ← 与 H5ChatView 里的 v-html 成对，不要单独改
  linkify: false,   // H5 里不做自动链接：模型给的 URL 不该可点（原则三）
  breaks: true,     // 单换行即换行，符合聊天场景的书写习惯
}).use(tonePlugin).use(chartPlugin)

export function renderMd(text: string): string {
  return md.render(text || '')
}
