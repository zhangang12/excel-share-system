/**
 * 智能体回复的 Markdown 渲染 —— **网页版/桌面客户端与 H5/APP 共用这一份**。
 *
 * 为什么要共用（2026-09-24 用户截图报的 bug）：
 * 语义着色 `[[danger:…]]` 是「语义着色 + UI 表格」那次加的（682b24c），
 * 后端 render.py 开始吐标记、H5 加了插件来翻译，**而 AgentView.vue 自己 new 了一个
 * MarkdownIt，没人想起来给它也装一份**。结果网页版和桌面客户端里每一张表都漏出
 * `[[danger:30 天]]` `[[muted:—]]` 这样的裸标记 —— 而且是**每个空单元格**都漏
 * （render.py 把空值统一渲染成 `[[muted:—]]`），一张 30 行的表就是几十处。
 * 后来的图表（519247a，```pmschart 围栏）是同一个故事：只加在了 H5。
 *
 * 所以这里不只是把插件抽出来，而是把**整个 MarkdownIt 实例的构造**抽出来：
 * 只要两边都调 `createAgentMd()`，就不可能再出现「一边认、一边不认」。
 * 以后后端再加新围栏/新标记，改这一个文件两端同时生效。
 *
 * ⚠️ 本文件在 src/shared/ 下，规矩是**只许依赖 markdown-it，不许引任何 UI 框架**
 *    （element-plus / vxe-table 一个都不行）。H5 包是给手机 4G 用的，只有登录和助手
 *    两页，背不动网页端那套依赖。破这条规矩 = H5 体积当场翻倍。
 *    vite.config.h5.js 里「H5 源码只在 src/h5/ 下」那条注释因此放宽为
 *    「H5 只 import src/h5/ 与 src/shared/」，后者由本条规矩保证零负担。
 */
import MarkdownIt from 'markdown-it'

/**
 * 语义着色档位白名单。与后端 `backend/app/agent/render.py` 的 `_tone_of` 一一对应：
 *   danger  已超期 / 有缺口
 *   warn    快到期 / 今天到期 / 有在途未完成
 *   good    正常（目前只在图表里用）
 *   muted   空值占位（render.py 把所有空单元格渲染成 `[[muted:—]]`）
 * ⚠️ 白名单之外的写法一律**当普通文字**，不做任何解析。
 */
export const TONES = new Set(['danger', 'warn', 'good', 'muted'])

/** SVG / 属性里出现的每一段来自数据的文字都必须过这里 */
export function esc(s: string): string {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string))
}

/**
 * `[[danger:已过 55 天]]` → `<span class="agent-tone agent-tone--danger">已过 55 天</span>`
 *
 * **为什么要绕这一圈**：颜色只能由前端加，因为后端不许吐 HTML（见 createAgentMd 的红线）。
 * 后端只发一个**语义**（这条是危险/警告/正常），由这里翻成受控的 span。
 *
 * 安全性：
 *   · 档位取自白名单，class 名不可能被数据控制；
 *   · 文本用 `text` token 推进去，由 markdown-it 自己转义 —— 数据里带
 *     `<img onerror=...>` 也只会变成字面量；
 *   · 白名单外的写法原样当普通文字。
 */
export function tonePlugin(md: MarkdownIt): void {
  md.inline.ruler.before('emphasis', 'agenttone', (state, silent) => {
    const src = state.src
    const start = state.pos
    if (src.charCodeAt(start) !== 0x5B || src.charCodeAt(start + 1) !== 0x5B) return false
    const end = src.indexOf(']]', start + 2)
    if (end < 0) return false
    const body = src.slice(start + 2, end)
    // ⚠️ 分隔符**必须是冒号，不能用竖线**：这些标记要放进 markdown 表格的
    //   单元格里，而竖线是表格的列分隔符 —— 用 `|` 会把一行切成好几列，
    //   整张表当场散掉（实测）。
    const sep = body.indexOf(':')
    if (sep <= 0) return false
    const tone = body.slice(0, sep)
    if (!TONES.has(tone)) return false          // 白名单之外一律不认
    const text = body.slice(sep + 1)
    if (!text) return false
    if (!silent) {
      let t = state.push('html_inline', '', 0)
      t.content = `<span class="agent-tone agent-tone--${tone}">`
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
 * ⚠️ 为什么不让后端直接吐 SVG：同下面那条红线。后端一旦开始吐标签，
 *    `html:false` 就形同虚设 —— 而工具数据里带着用户自由填的备注、
 *    物料名、OA 详情，`<img src=x onerror=…>` 完全可能混在里面。
 *    这里 SVG 的**结构由代码写死**，来自数据的只有数字和文字，文字一律走 esc()。
 *
 * ⚠️ JSON 解析失败/字段不对 → **什么都不画**，绝不把原文回吐。
 *    半张图比没有图更让人困惑，露出 JSON 更糟。
 */
// 颜色写成 `var(--token, #hex)`：H5 里走它自己的设计令牌，网页版没有这些令牌就
// 落到十六进制兜底 —— 一份代码，两边都对，不用为配色再分叉一次。
const CHART_TONES: Record<string, string> = {
  danger: 'var(--h5-danger, #C4362F)',
  warn: 'var(--h5-warn, #A96A08)',
  good: 'var(--h5-good, #2A7A52)',
  muted: 'var(--h5-ink-4, #B4BAC2)',
}

interface ChartRow { label: string; value: number; text: string; tone: string }

/** 发散条形图：中线是今天，左边超期、右边还剩。 */
function barChart(data: any): string {
  const rows: ChartRow[] = (data.rows || []).filter(
    (r: any) => r && typeof r.value === 'number' && Number.isFinite(r.value))
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

export function chartPlugin(md: MarkdownIt): void {
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

/**
 * 造一个配好的 MarkdownIt。**两个前端都必须用它，别自己 new** —— 自己 new
 * 就是这次 bug 的成因。
 *
 * ⚠️ `html: false` 与下游的 v-html 是**成对**的，谁也不能单独改：
 * markdown-it 在 html:false 时把原始 HTML 转义成文本，v-html 才是安全的。
 * 一旦为了「让模型画个好看的卡片」把它开成 true，或者绕过 render 直接
 * v-html 模型输出，XSS 防线当场归零（手册 3.4.2）。
 *
 * 攻击面不只是「模型自己乱写」：工具返回的数据里带着用户自由填写的
 * 备注、OA 详情、记录值，里面完全可以有 <img src=x onerror=...>，
 * 模型很可能原样复述出来。
 *
 * ⚠️ `linkify: false` 也是刻意的（原则三）：模型给的 URL 不该可点。
 *    网页版原来是 true —— 现在统一到 false，两端一致。
 */
export function createAgentMd(): MarkdownIt {
  return new MarkdownIt({
    html: false,      // ← 与两端的 v-html 成对，不要单独改
    linkify: false,   // 模型给的 URL 不该可点
    breaks: true,     // 单换行即换行，符合聊天场景的书写习惯
  }).use(tonePlugin).use(chartPlugin)
}

const md = createAgentMd()

/** 两个前端的唯一渲染入口。 */
export function renderAgentMd(text: string): string {
  return md.render(text || '')
}
