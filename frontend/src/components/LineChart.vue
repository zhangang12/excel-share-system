<script setup lang="ts">
// 🆕 需求十二：自建轻量 SVG 折线/曲线图（无第三方依赖，主题自适应，支持多系列+悬浮提示）
import { ref, computed } from 'vue'

interface Series { name: string; points: (number | null)[]; color?: string }
const props = withDefaults(defineProps<{
  labels: string[]
  series: Series[]
  height?: number
  smooth?: boolean
  yUnit?: string
  moneyFmt?: (v: number) => string
}>(), { height: 300, smooth: true, yUnit: '' })

const PALETTE = ['#2563eb', '#16a34a', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#65a30d']
const W = 820
const H = computed(() => props.height)
const PAD = { l: 62, r: 18, t: 16, b: 34 }
const plotW = computed(() => W - PAD.l - PAD.r)
const plotH = computed(() => H.value - PAD.t - PAD.b)

const fmt = (v: number) => (props.moneyFmt ? props.moneyFmt(v) : (v >= 10000 ? (v / 10000).toFixed(1) + '万' : String(Math.round(v))))

const maxY = computed(() => {
  let m = 0
  for (const s of props.series) for (const p of s.points) if (p != null && p > m) m = p
  if (m <= 0) return 1
  // nice ceiling
  const pow = Math.pow(10, Math.floor(Math.log10(m)))
  const n = m / pow
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
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

// x 轴标签抽稀（超过 12 个隔点显示）
const xTicks = computed(() => {
  const every = n.value > 12 ? Math.ceil(n.value / 12) : 1
  return props.labels.map((l, i) => ({ i, label: l, show: i % every === 0 || i === n.value - 1 }))
})

function pathFor(s: Series): string {
  const pts: [number, number][] = []
  s.points.forEach((p, i) => { if (p != null) pts.push([xFor(i), yFor(p)]) })
  if (!pts.length) return ''
  if (!props.smooth || pts.length < 3) return 'M' + pts.map(p => `${p[0]},${p[1]}`).join(' L')
  // Catmull-Rom → cubic bezier 平滑曲线
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
const wrapRef = ref<HTMLElement>()
function onMove(e: MouseEvent) {
  if (!wrapRef.value || n.value === 0) return
  const rect = wrapRef.value.getBoundingClientRect()
  const xPx = ((e.clientX - rect.left) / rect.width) * W
  if (n.value <= 1) { hoverIdx.value = 0; return }
  const rel = (xPx - PAD.l) / plotW.value
  let idx = Math.round(rel * (n.value - 1))
  idx = Math.max(0, Math.min(n.value - 1, idx))
  hoverIdx.value = idx
}
function onLeave() { hoverIdx.value = null }
const tipLeftPct = computed(() => hoverIdx.value == null ? 0 : (xFor(hoverIdx.value) / W) * 100)
const empty = computed(() => !props.series.some(s => s.points.some(p => p != null && p > 0)))
</script>

<template>
  <div class="lc-wrap" ref="wrapRef">
    <div v-if="empty" class="lc-empty">暂无数据</div>
    <template v-else>
      <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="lc-svg"
           @mousemove="onMove" @mouseleave="onLeave">
        <!-- 横向网格 + y 轴刻度 -->
        <g class="lc-grid">
          <line v-for="(g, i) in gridLines" :key="'g' + i" :x1="PAD.l" :x2="W - PAD.r" :y1="g.y" :y2="g.y" />
        </g>
        <g class="lc-ylabel">
          <text v-for="(g, i) in gridLines" :key="'yl' + i" :x="PAD.l - 8" :y="g.y + 4" text-anchor="end">{{ g.label }}</text>
        </g>
        <!-- x 轴标签 -->
        <g class="lc-xlabel">
          <text v-for="t in xTicks.filter(t => t.show)" :key="'x' + t.i" :x="xFor(t.i)" :y="H - 12" text-anchor="middle">{{ t.label }}</text>
        </g>
        <!-- hover 竖线 -->
        <line v-if="hoverIdx != null" class="lc-hover-line" :x1="xFor(hoverIdx)" :x2="xFor(hoverIdx)" :y1="PAD.t" :y2="H - PAD.b" />
        <!-- 折线 -->
        <path v-for="(s, si) in series" :key="'p' + si" :d="pathFor(s)" fill="none" :stroke="colorOf(s, si)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
        <!-- 数据点 -->
        <template v-for="(s, si) in series" :key="'d' + si">
          <circle v-for="(p, i) in s.points" :key="i" v-show="p != null && (hoverIdx === i || n <= 24)"
                  :cx="xFor(i)" :cy="yFor(p || 0)" :r="hoverIdx === i ? 4 : 2.5" :fill="colorOf(s, si)" />
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
          <span class="lc-dot" :style="{ background: colorOf(s, si) }"></span>{{ s.name }}
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lc-wrap { position: relative; width: 100%; }
.lc-svg { width: 100%; display: block; }
.lc-grid line { stroke: var(--el-border-color-lighter); stroke-width: 1; }
.lc-ylabel text, .lc-xlabel text { fill: var(--el-text-color-secondary); font-size: 11px; }
.lc-hover-line { stroke: var(--el-border-color); stroke-width: 1; stroke-dasharray: 3 3; }
.lc-legend { display: flex; flex-wrap: wrap; gap: 14px; justify-content: center; margin-top: 6px; font-size: 12px; color: var(--el-text-color-regular); }
.lc-leg { display: inline-flex; align-items: center; gap: 5px; }
.lc-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.lc-tip { position: absolute; top: 6px; transform: translateX(-50%); background: var(--el-bg-color-overlay, #fff);
  border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.12); pointer-events: none; min-width: 130px; z-index: 5; }
.lc-tip-title { font-weight: 600; margin-bottom: 4px; color: var(--el-text-color-primary); }
.lc-tip-row { display: flex; align-items: center; gap: 6px; line-height: 1.7; }
.lc-tip-name { color: var(--el-text-color-secondary); margin-right: auto; }
.lc-empty { text-align: center; color: var(--el-text-color-secondary); padding: 40px 0; font-size: 13px; }
</style>
