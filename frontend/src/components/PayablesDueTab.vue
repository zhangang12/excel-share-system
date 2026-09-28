<script setup lang="ts">
/**
 * 🆕 2026-09-29 采购管理 →「应付到期」：已到货、还没付清的采购明细，按「哪天该付」排好。
 *
 * 起因：有人提「账期比如写了 30 天，货到了以后要提醒该付款了，给采购部一张表」。
 * 老板拍板：从**到货**那天算；**先不推送，只要这张表**。
 * 系统里原本就有一半 —— 供应商有账期天数，财务「资金面板」也按「到货日 + 账期」算过到期日，
 * 但只在财务那一页，采购看不到。口径在后端 app/payables.py，与资金面板共用，两边数字一致。
 *
 * 单独做成组件、不塞进 PurchaseMgmtView：那个文件已经四千多行了。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Search } from '@element-plus/icons-vue'
import { http } from '@/api'
import { fmtMoney } from '@/utils/format'
import EmptyHint from '@/components/EmptyHint.vue'

interface DueRow {
  item_id: number; supplier: string; settlement_type: string; credit_days: number | null
  po_no: string; project_code: string; item_name: string; spec: string; buyer: string
  payment_method: string; received_amount: number; paid_amount: number; outstanding: number
  arrival_date: string; due_date: string | null; days_left: number | null
  bucket: 'overdue' | 'week' | 'month' | 'later' | 'nocredit'; bucket_label: string
  request_state: string
}
interface BucketSum { label: string; count: number; suppliers: number; amount: number }

const loading = ref(false)
const rows = ref<DueRow[]>([])
const summary = ref<Record<string, BucketSum>>({})
const today = ref('')
const bucket = ref<string>('')          // '' = 全部
const q = ref('')
const view = ref<'item' | 'supplier'>('supplier')

const BUCKETS: { key: string; tone: string }[] = [
  { key: 'overdue', tone: 'danger' },
  { key: 'week', tone: 'warning' },
  { key: 'month', tone: 'primary' },
  { key: 'later', tone: 'info' },
  { key: 'nocredit', tone: 'info' },
]

async function load() {
  loading.value = true
  try {
    const { data } = await http.get('/purchase-mgmt/payables-due')
    rows.value = data.rows || []
    summary.value = data.summary || {}
    today.value = data.today || ''
  } catch { /* 失败由拦截器提示 */ } finally {
    loading.value = false
  }
}
defineExpose({ load })
onMounted(load)

/** 与后端导出同一套筛选（purchase_mgmt_router._due_filter）：屏幕上看到什么，导出来就是什么 */
const filtered = computed(() => {
  const kw = q.value.trim().toLowerCase()
  return rows.value.filter((r) => (!bucket.value || r.bucket === bucket.value)
    && (!kw || [r.supplier, r.po_no, r.project_code, r.item_name, r.spec, r.buyer]
      .some((x) => (x || '').toLowerCase().includes(kw))))
})

/** 按供应商汇总：付款是按供应商付的，排付款计划看这个视图更顺手 */
interface SupRow {
  supplier: string; settlement: string; nearest_due: string | null; nearest_left: number | null
  overdue: number; week: number; total: number; count: number; in_flight: number; nocredit: boolean
  worst_days: number
}
const bySupplier = computed<SupRow[]>(() => {
  const m = new Map<string, SupRow>()
  for (const r of filtered.value) {
    let g = m.get(r.supplier)
    if (!g) {
      g = { supplier: r.supplier,
            settlement: r.credit_days != null ? `${r.settlement_type || '账期'} ${r.credit_days} 天` : (r.settlement_type || '—'),
            nearest_due: null, nearest_left: null, overdue: 0, week: 0, total: 0, count: 0,
            in_flight: 0, nocredit: r.bucket === 'nocredit', worst_days: 0 }
      m.set(r.supplier, g)
    }
    g.total += r.outstanding
    g.count += 1
    if (r.bucket === 'overdue') {
      g.overdue += r.outstanding
      g.worst_days = Math.max(g.worst_days, -(r.days_left || 0))
    }
    if (r.bucket === 'week') g.week += r.outstanding
    if (r.request_state !== '未请款') g.in_flight += 1
    if (r.due_date && (!g.nearest_due || r.due_date < g.nearest_due)) {
      g.nearest_due = r.due_date
      g.nearest_left = r.days_left
    }
  }
  // 最早该付的在最上面；账期未填的放最后
  return [...m.values()].sort((a, b) => {
    if (!a.nearest_due && !b.nearest_due) return b.total - a.total
    if (!a.nearest_due) return 1
    if (!b.nearest_due) return -1
    return a.nearest_due < b.nearest_due ? -1 : a.nearest_due > b.nearest_due ? 1 : 0
  })
})

const filteredTotal = computed(() => filtered.value.reduce((s, r) => s + r.outstanding, 0))

function leftText(d: number | null): string {
  if (d == null) return '—'
  if (d < 0) return `已过期 ${-d} 天`
  if (d === 0) return '今天到期'
  return `还剩 ${d} 天`
}
function leftType(d: number | null): 'danger' | 'warning' | 'info' | 'success' {
  if (d == null) return 'info'
  if (d < 0) return 'danger'
  if (d <= 7) return 'warning'
  return 'success'
}

function toggleBucket(k: string) {
  bucket.value = bucket.value === k ? '' : k
}

async function exportXlsx() {
  try {
    const res = await http.get('/purchase-mgmt/payables-due/export', {
      params: { bucket: bucket.value, q: q.value.trim() }, responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `应付到期表_${today.value}.xlsx`
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { ElMessage.error('导出失败') }
}
</script>

<template>
  <div v-loading="loading" class="due-wrap">
    <!-- 五个分档，点一下就只看那一档，再点取消 -->
    <div class="due-cards">
      <div v-for="b in BUCKETS" :key="b.key" class="due-card" :class="[b.tone, { on: bucket === b.key }]"
           @click="toggleBucket(b.key)">
        <div class="dc-label">{{ summary[b.key]?.label || b.key }}</div>
        <div class="dc-amt">{{ fmtMoney(summary[b.key]?.amount || 0) }}</div>
        <div class="dc-sub">{{ summary[b.key]?.suppliers || 0 }} 家 · {{ summary[b.key]?.count || 0 }} 条</div>
      </div>
    </div>

    <div class="due-tip">
      到期日 = <b>到货日期 + 供应商账期天数</b>（{{ today }} 起算）。期初余额里已含的不重复算，与「供应商账目」同口径。
      <template v-if="summary.nocredit?.count">
        有 <b>{{ summary.nocredit.suppliers }}</b> 家供应商没填账期天数，算不出哪天该付 —— 到「采购部」的供应商资料里补上就会进来。
      </template>
    </div>

    <div class="due-bar">
      <el-radio-group v-model="view" size="small">
        <el-radio-button value="supplier">按供应商</el-radio-button>
        <el-radio-button value="item">按明细</el-radio-button>
      </el-radio-group>
      <el-input v-model="q" size="small" clearable :prefix-icon="Search" style="width: 260px"
                placeholder="供应商 / 采购单号 / 项目 / 物料 / 采购员" />
      <span class="due-sum">
        <template v-if="bucket">筛选：{{ summary[bucket]?.label }} · </template>
        共 {{ filtered.length }} 条，欠款 <b>{{ fmtMoney(filteredTotal) }}</b>
      </span>
      <el-button size="small" :icon="Download" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <!-- 按供应商 -->
    <el-table v-if="view === 'supplier'" :data="bySupplier" stripe size="small" border
              max-height="calc(100vh - 380px)" show-overflow-tooltip>
      <el-table-column prop="supplier" label="供应商" min-width="220" />
      <el-table-column prop="settlement" label="账期" width="110" />
      <!-- 日期 + 「已过期 54 天」标签要 200px 才放得下；不开溢出省略，截掉标签就等于没看到最要紧的那句 -->
      <el-table-column label="最早到期" width="205" :show-overflow-tooltip="false">
        <template #default="{ row }">
          <template v-if="row.nearest_due">
            {{ row.nearest_due }}
            <el-tag size="small" :type="leftType(row.nearest_left)" effect="plain">{{ leftText(row.nearest_left) }}</el-tag>
          </template>
          <el-tag v-else size="small" type="info" effect="plain">账期未填</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="已过期" width="150" align="right">
        <template #default="{ row }">
          <span v-if="row.overdue" class="danger"><b>{{ fmtMoney(row.overdue) }}</b></span>
          <span v-else class="muted">—</span>
          <div v-if="row.worst_days" class="muted small">最久 {{ row.worst_days }} 天</div>
        </template>
      </el-table-column>
      <el-table-column label="7 天内到期" width="120" align="right">
        <template #default="{ row }"><span :class="{ warn: row.week }">{{ row.week ? fmtMoney(row.week) : '—' }}</span></template>
      </el-table-column>
      <el-table-column label="欠款合计" width="130" align="right">
        <template #default="{ row }"><b>{{ fmtMoney(row.total) }}</b></template>
      </el-table-column>
      <el-table-column prop="count" label="明细" width="70" align="center" />
      <el-table-column label="已提请款" width="90" align="center">
        <template #default="{ row }">{{ row.in_flight ? `${row.in_flight} 条` : '—' }}</template>
      </el-table-column>
    </el-table>

    <!-- 按明细 -->
    <el-table v-else :data="filtered" stripe size="small" border
              max-height="calc(100vh - 380px)" show-overflow-tooltip>
      <el-table-column label="到期日" width="205" :show-overflow-tooltip="false">
        <template #default="{ row }">
          <template v-if="row.due_date">
            {{ row.due_date }}
            <el-tag size="small" :type="leftType(row.days_left)" effect="plain">{{ leftText(row.days_left) }}</el-tag>
          </template>
          <el-tag v-else size="small" type="info" effect="plain">账期未填</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="supplier" label="供应商" min-width="180" />
      <el-table-column prop="po_no" label="采购单号" width="150" />
      <el-table-column prop="project_code" label="项目" width="105" />
      <el-table-column label="物料" min-width="150">
        <template #default="{ row }">{{ row.item_name }}<span v-if="row.spec" class="muted small"> · {{ row.spec }}</span></template>
      </el-table-column>
      <el-table-column prop="buyer" label="采购员" width="80" />
      <el-table-column prop="arrival_date" label="到货日" width="100" />
      <el-table-column label="账期" width="80" align="center">
        <template #default="{ row }">{{ row.credit_days != null ? `${row.credit_days} 天` : '—' }}</template>
      </el-table-column>
      <el-table-column label="欠款" width="115" align="right">
        <template #default="{ row }"><b :class="{ danger: row.bucket === 'overdue' }">{{ fmtMoney(row.outstanding) }}</b></template>
      </el-table-column>
      <el-table-column label="请款" width="100" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.request_state !== '未请款'" size="small" type="success" effect="plain">{{ row.request_state }}</el-tag>
          <span v-else class="muted small">未请款</span>
        </template>
      </el-table-column>
    </el-table>

    <EmptyHint v-if="!loading && !filtered.length"
               :text="rows.length ? '这一档没有' : '没有到了货还没付清的采购'" size="sm" />
  </div>
</template>

<style scoped>
.due-wrap { padding-top: 4px; }
.due-cards { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 10px; }
.due-card {
  border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 10px 12px;
  cursor: pointer; background: var(--el-bg-color); transition: border-color .15s, box-shadow .15s;
}
.due-card:hover { border-color: var(--el-color-primary-light-5); }
.due-card.on { box-shadow: 0 0 0 2px var(--el-color-primary-light-5); border-color: var(--el-color-primary); }
.dc-label { font-size: 12px; color: var(--el-text-color-secondary); }
.dc-amt { font-size: 18px; font-weight: 700; margin-top: 2px; font-variant-numeric: tabular-nums; }
.dc-sub { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.due-card.danger .dc-amt { color: var(--el-color-danger); }
.due-card.warning .dc-amt { color: var(--el-color-warning); }
.due-card.primary .dc-amt { color: var(--el-color-primary); }
.due-tip {
  font-size: 12px; line-height: 1.8; color: var(--el-text-color-regular);
  background: var(--el-fill-color-lighter); border-radius: 6px; padding: 6px 10px; margin-bottom: 10px;
}
.due-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.due-sum { font-size: 13px; color: var(--el-text-color-regular); margin-left: auto; }
.danger { color: var(--el-color-danger); }
.warn { color: var(--el-color-warning); font-weight: 600; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
@media (max-width: 900px) { .due-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
