<script setup lang="ts">
// 🆕 自建轻量 SVG 折线图（无第三方依赖，主题自适应，多系列 + 悬浮十字提示）
// 财务口径改造：①按容器实宽 1:1 渲染(去掉 preserveAspectRatio=none 的拉伸畸变，圆点不再变椭圆)
//   ②默认直线段(不平滑，避免曲线过冲显示出没发生过的金额) ③经校验的分类配色 ④基线/网格更克制。
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

interface Series { name: string; points: (number | null)[]; color?: string }
const props = withDefaults(defineProps<{
  labels: string[]
  series: Series[]
  height?: number
  smooth?: boolean
  yUnit?: string
  moneyFmt?: (v: number) => string
}>(), { height: 260, smooth: false, yUnit: '' })

// 经 dataviz 校验器验证(light+dark 六项全过)的分类配色，按固定顺序取用（蓝/琥珀/绿/…）
const PALETTE = ['#2a78d6', '#eda100', '#008300', '#4a3aa7', '#e34948', '#0891b2', '#e87ba4', '#eb6834']

const wrapRef = ref<HTMLElement>()
const W = ref(760)                       // 随容器实宽变化 → 1:1 渲染，不拉伸
let ro: ResizeObserver | null = null
onMounted(() => {
  const upd = () => { if (wrapRef.value) W.value = Math.max(320, Math.round(wrapRef.value.clientWidth)) }
  upd()
  ro = new ResizeObserver(upd)
  if (wrapRef.value) ro.observe(wrapRef.value)
})
onBeforeUnmount(() => { ro?.disconnect(); ro = null })

const H = computed(() => props.height)
const PAD = { l: 60, r: 16, t: 14, b: 30 }
const plotW = computed(() => W.value - PAD.l - PAD.r)
const plotH = computed(() => H.value - PAD.t - PAD.b)

const fmt = (v: number) => (props.moneyFmt ? props.moneyFmt(v) : (v >= 10000 ? (v / 10000).toFixed(1) + '万' : String(Math.round(v))))

const maxY = computed(() => {
  let m = 0
  for (const s of props.series) for (const p of s.points) if (p != null && p > m) m = p
  if (m <= 0) return 1
  const pow = Math.pow(10, Math.floor(Math.log10(m)))
  const nn = m / pow
  const nice = nn <= 1 ? 1 : nn <= 2 ? 2 : nn <= 5 ? 5 : 10
  return nice * pow
})

const n = computed(() => props.labels.length)
function xFor(i: number) {
  if (n.value <= 1) return PAD.l + plotW.value / 2
  return PAD.l + (plotW.value * i) / (n.value - 1)
}
function yFor(v: number) { return PAD.t + plotH.value * (1 - v / maxY.value) }

const gridLines = computed(() => {
  const steps = 4
  const out: { y: number; label: string }[] = []
  for (let i = 0; i <= steps; i++) {
    const v = (maxY.value * i) / steps
    out.push({ y: yFor(v), label: fmt(v) })
  }
  return out
})

const xTicks = computed(() => {
  const every = n.value > 12 ? Math.ceil(n.value / 12) : 1
  return props.labels.map((l, i) => ({ i, label: l, show: i % every === 0 || i === n.value - 1 }))
})

function pathFor(s: Series): string {
  const pts: [number, number][] = []
  s.points.forEach((p, i) => { if (p != null) pts.push([xFor(i), yFor(p)]) })
  if (!pts.length) return ''
  if (!props.smooth || pts.length < 3) return 'M' + pts.map(p => `${p[0]},${p[1]}`).join(' L')
  let d = `M${pts[0][0]},${pts[0][1]}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i]
    const p1 = pts[i]
    const p2 = pts[i + 1]
    const p3 = pts[i + 2] || p2
    const c1x = p1[0] + (p2[0] - p0[0]) / 6
    const c1y = p1[1] + (p2[1] - p0[1]) / 6
    const c2x = p2[0] - (p3[0] - p1[0]) / 6
    const c2y = p2[1] - (p3[1] - p1[1]) / 6
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`
  }
  return d
}
function colorOf(s: Series, i: number) { return s.color || PALETTE[i % PALETTE.length] }

const hoverIdx = ref<number | null>(null)
function onMove(e: MouseEvent) {
  if (!wrapRef.value || n.value === 0) return
  const rect = wrapRef.value.getBoundingClientRect()
  const xPx = ((e.clientX - rect.left) / rect.width) * W.value
  if (n.value <= 1) { hoverIdx.value = 0; return }
  const rel = (xPx - PAD.l) / plotW.value
  let idx = Math.round(rel * (n.value - 1))
  idx = Math.max(0, Math.min(n.value - 1, idx))
  hoverIdx.value = idx
}
function onLeave() { hoverIdx.value = null }
const tipLeftPct = computed(() => hoverIdx.value == null ? 0 : (xFor(hoverIdx.value) / W.value) * 100)
const empty = computed(() => !props.series.some(s => s.points.some(p => p != null && p > 0)))
const baselineY = computed(() => yFor(0))
</script>

<template>
  <div class="lc-wrap" ref="wrapRef">
    <div v-if="empty" class="lc-empty">暂无数据</div>
    <template v-else>
      <svg :viewBox="`0 0 ${W} ${H}`" :width="W" :height="H" class="lc-svg"
           @mousemove="onMove" @mouseleave="onLeave">
        <!-- 横向网格 -->
        <g class="lc-grid">
          <line v-for="(g, i) in gridLines" :key="'g' + i" :x1="PAD.l" :x2="W - PAD.r" :y1="g.y" :y2="g.y" />
        </g>
        <!-- 基线(x 轴) -->
        <line class="lc-axis" :x1="PAD.l" :x2="W - PAD.r" :y1="baselineY" :y2="baselineY" />
        <!-- y 轴刻度 -->
        <g class="lc-ylabel">
          <text v-for="(g, i) in gridLines" :key="'yl' + i" :x="PAD.l - 8" :y="g.y + 4" text-anchor="end">{{ g.label }}</text>
        </g>
        <!-- x 轴标签 -->
        <g class="lc-xlabel">
          <text v-for="t in xTicks.filter(t => t.show)" :key="'x' + t.i" :x="xFor(t.i)" :y="H - 10" text-anchor="middle">{{ t.label }}</text>
        </g>
        <!-- hover 竖线 -->
        <line v-if="hoverIdx != null" class="lc-hover-line" :x1="xFor(hoverIdx)" :x2="xFor(hoverIdx)" :y1="PAD.t" :y2="baselineY" />
        <!-- 折线 -->
        <path v-for="(s, si) in series" :key="'p' + si" :d="pathFor(s)" fill="none" :stroke="colorOf(s, si)"
              stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
        <!-- 数据点：默认克制(≤24点显示小点)，hover 放大 + 白描边 -->
        <template v-for="(s, si) in series" :key="'d' + si">
          <circle v-for="(p, i) in s.points" :key="i" v-show="p != null && (hoverIdx === i || n <= 24)"
                  :cx="xFor(i)" :cy="yFor(p || 0)" :r="hoverIdx === i ? 4.5 : 2.5" :fill="colorOf(s, si)"
                  :stroke="hoverIdx === i ? 'var(--el-bg-color, #fff)' : 'none'" :stroke-width="hoverIdx === i ? 2 : 0" />
        </template>
      </svg>
      <!-- 悬浮提示 -->
      <div v-if="hoverIdx != null" class="lc-tip" :style="{ left: tipLeftPct + '%' }">
        <div class="lc-tip-title">{{ labels[hoverIdx] }}</div>
        <div v-for="(s, si) in series" :key="si" class="lc-tip-row">
          <span class="lc-dot" :style="{ background: colorOf(s, si) }"></span>
          <span class="lc-tip-name">{{ s.name }}</span>
          <b>{{ moneyFmt ? moneyFmt(s.points[hoverIdx] || 0) : (s.points[hoverIdx] ?? '—') }}{{ yUnit }}</b>
        </div>
      </div>
      <!-- 图例 -->
      <div class="lc-legend">
        <span v-for="(s, si) in series" :key="si" class="lc-leg">
          <span class="lc-swatch" :style="{ background: colorOf(s, si) }"></span>{{ s.name }}
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lc-wrap { position: relative; width: 100%; }
.lc-svg { width: 100%; display: block; }
.lc-grid line { stroke: var(--el-border-color-lighter); stroke-width: 1; }
.lc-axis { stroke: var(--el-border-color); stroke-width: 1; }
.lc-ylabel text, .lc-xlabel text { fill: var(--el-text-color-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.lc-hover-line { stroke: var(--el-border-color); stroke-width: 1; stroke-dasharray: 3 3; }
.lc-legend { display: flex; flex-wrap: wrap; gap: 16px; justify-content: center; margin-top: 8px; font-size: 12px; color: var(--el-text-color-regular); }
.lc-leg { display: inline-flex; align-items: center; gap: 6px; }
.lc-swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.lc-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.lc-tip { position: absolute; top: 6px; transform: translateX(-50%); background: var(--el-bg-color-overlay, #fff);
  border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 8px 11px; font-size: 12px;
  box-shadow: 0 6px 20px rgba(0,0,0,.14); pointer-events: none; min-width: 140px; z-index: 5; }
.lc-tip-title { font-weight: 600; margin-bottom: 5px; color: var(--el-text-color-primary); }
.lc-tip-row { display: flex; align-items: center; gap: 7px; line-height: 1.8; }
.lc-tip-row b { font-variant-numeric: tabular-nums; }
.lc-tip-name { color: var(--el-text-color-secondary); margin-right: auto; }
.lc-empty { text-align: center; color: var(--el-text-color-secondary); padding: 40px 0; font-size: 13px; }
</style>
