<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, Upload, Setting, Search, Download } from '@element-plus/icons-vue'
import axios from 'axios'
import { overviewApi } from '@/api/overview'
import { projectsApi } from '@/api/projects'
import { permApi } from '@/api/permissions'
// 字段权限统一在「权限管理 → 权限矩阵」页配置，不再挂在表头
import { useRealtime } from '@/composables/useRealtime'
import { useTableHeight } from '@/composables/useTableHeight'
import { useAuthStore } from '@/stores/auth'
import type { OverviewField, OverviewRow } from '@/types'

const router = useRouter()
const auth = useAuthStore()
const keyword = ref("")
const fields = ref<OverviewField[]>([])
const rows = ref<OverviewRow[]>([])
const loading = ref(false)
const myPerms = ref<Record<string, { can_view: boolean; can_edit: boolean }>>({})

// 表格固定高度自适应视口（让横/纵滚动条始终在视口内）
const tableRef = ref()
const { height: tableHeight } = useTableHeight(tableRef)

// 分页
const pageSize = ref(20)
const currentPage = ref(1)

const isAdmin = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))
const visibleFields = computed(() =>
  fields.value.filter(f => myPerms.value[String(f.id)]?.can_view !== false)
)
function fieldEditable(f: OverviewField): boolean {
  return myPerms.value[String(f.id)]?.can_edit !== false
}

// ===== 项目一览固定模板列（与后端 sheet_templates.OVERVIEW_FIELDS 一致）=====
// 一览的数据与项目详情完全独立——meta 列存 row.extra['__o__<label>']。
type OverviewTplCol = {
  label: string
  source: 'code' | 'name' | 'status' | 'meta' | 'derived'
  derived?: 'duration' | 'elapsed' | 'remaining' | 'design_days'
  // fallbackDerived: source='meta' 但 meta 无值时，按这个派生公式算
  // 即"可编辑但有默认公式"——用户填了就用填的，没填就显示算出的值
  fallbackDerived?: 'duration' | 'elapsed' | 'remaining' | 'design_days'
  editable: boolean
  widthPct: number
}
const OVERVIEW_FIELDS: OverviewTplCol[] = [
  { label: '项目编号',     source: 'code',    editable: false, widthPct: 7 },
  { label: '项目名称',     source: 'name',    editable: true,  widthPct: 14 },
  { label: '状态',         source: 'status',  editable: true,  widthPct: 7 },
  { label: '签订日期',     source: 'meta',    editable: true,  widthPct: 7 },
  { label: '交货日期',     source: 'meta',    editable: true,  widthPct: 7 },
  { label: '销售',         source: 'meta',    editable: true,  widthPct: 6 },
  { label: '设计师',       source: 'meta',    editable: true,  widthPct: 6 },
  { label: '制图开始',     source: 'meta',    editable: true,  widthPct: 7 },
  { label: '制图结束',     source: 'meta',    editable: true,  widthPct: 7 },
  // 制图用时：可手填覆盖公式，公式 = 制图结束 - 制图开始
  { label: '制图用时',     source: 'meta',    fallbackDerived: 'design_days', editable: true, widthPct: 6 },
  { label: '电工',         source: 'meta',    editable: true,  widthPct: 6 },
  { label: '货期',         source: 'derived', derived: 'duration',  editable: false, widthPct: 5 },
  { label: '已过时间',     source: 'derived', derived: 'elapsed',   editable: false, widthPct: 6 },
  { label: '剩余制作时间', source: 'derived', derived: 'remaining', editable: false, widthPct: 6 },
]

const STATUS_OPTIONS_NEW = ['进行中', '已完成']

// 按列名给紧凑/默认两套最小宽度（14 寸 1366px 全屏下 14 列全部可见）
function overviewColMinWidth(col: OverviewTplCol): number {
  if (fitScreen.value) {
    switch (col.label) {
      case '项目编号': return 80
      case '项目名称': return 140
      case '状态':     return 78
      case '销售':     return 58
      case '设计师':   return 60
      case '电工':     return 50
      case '货期':     return 48
      case '签订日期':
      case '交货日期':
      case '制图开始':
      case '制图结束':
      case '完成日期':
      case '出货日期': return 76
      case '制图用时':
      case '已过时间':
      case '剩余制作时间': return 60
      default:         return 70
    }
  }
  // 默认（适应屏幕关闭，宽屏舒适模式）
  switch (col.label) {
    case '项目编号': return 110
    case '项目名称': return 180
    case '签订日期':
    case '交货日期':
    case '制图开始':
    case '制图结束':
    case '完成日期':
    case '出货日期': return 100
    default:         return 90
  }
}

// 整列着色：状态列的 td 按 row.status 直接染色
function cellClassName({ row, column }: any): string {
  const label = column?.label || ''
  if (label === '状态') {
    if (row.status === '已完成') return 'cell-row-done'
    if (row.status === '进行中') return 'cell-row-doing'
  }
  return ''
}

// 日期解析与天数差（与 DatasheetGrid 一致）
function parseLooseDate(s: unknown): Date | null {
  if (!s && s !== 0) return null
  const str = String(s).trim()
  const m = /^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})/.exec(str)
  if (!m) return null
  const d = new Date(+m[1], +m[2] - 1, +m[3])
  return isNaN(d.getTime()) ? null : d
}
function daysBetween(a: Date, b: Date): number {
  const a0 = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())
  const b0 = Date.UTC(b.getFullYear(), b.getMonth(), b.getDate())
  return Math.round((a0 - b0) / 86400000)
}

// 每分钟刷新一次响应式 today，让派生列跨天自动跳
const todayKey = ref(new Date().toDateString())
let _todayTimer: number | null = null
onMounted(() => {
  _todayTimer = window.setInterval(() => {
    const k = new Date().toDateString()
    if (k !== todayKey.value) todayKey.value = k
  }, 60_000)
})
import { onBeforeUnmount } from 'vue'
onBeforeUnmount(() => { if (_todayTimer !== null) window.clearInterval(_todayTimer) })

// 一览专属前缀 __o__，与项目详情的 __h__ 完全独立
const OVERVIEW_PREFIX = '__o__'

function rowMetaValue(row: OverviewRow, key: string): string {
  return String(row.extra?.[`${OVERVIEW_PREFIX}${key}`] ?? '')
}

// 派生公式：基于一览自己的 __o__ 日期 key 实时算
function computeDerived(row: OverviewRow, kind: string): string {
  void todayKey.value
  const signed = parseLooseDate(rowMetaValue(row, '签订日期'))
  const deliver = parseLooseDate(rowMetaValue(row, '交货日期'))
  const designStart = parseLooseDate(rowMetaValue(row, '制图开始'))
  const designEnd = parseLooseDate(rowMetaValue(row, '制图结束'))
  const today = new Date()
  switch (kind) {
    // 货期 = 交货日期 - 签订日期
    case 'duration':    return signed && deliver ? String(daysBetween(deliver, signed)) : ''
    // 已过时间 = TODAY() - 签订日期
    case 'elapsed':     return signed            ? String(daysBetween(today, signed))   : ''
    // 剩余制作时间 = 交货日期 - TODAY()（数学上等价于 货期 - 已过时间）
    case 'remaining':   return deliver           ? String(daysBetween(deliver, today))  : ''
    // 制图用时 = 制图结束 - 制图开始
    case 'design_days': return designStart && designEnd ? String(daysBetween(designEnd, designStart)) : ''
  }
  return ''
}

// 模板列的显示值
function templateCellValue(row: OverviewRow, col: OverviewTplCol): string {
  if (col.source === 'code') return row.code || ''
  if (col.source === 'name') return row.name || ''
  if (col.source === 'status') return row.status || ''
  if (col.source === 'meta') {
    const v = rowMetaValue(row, col.label)
    if (v) return smartFormatValue(v)
    // 用户没手填 → 尝试 fallback 派生公式（如"制图用时"）
    if (col.fallbackDerived) return computeDerived(row, col.fallbackDerived)
    return ''
  }
  if (col.source === 'derived' && col.derived) {
    // 已完成项目的"剩余制作时间"和"已过时间"不再算（业务上无意义）
    if (row.status === '已完成' && (col.derived === 'remaining' || col.derived === 'elapsed')) {
      return ''
    }
    return computeDerived(row, col.derived)
  }
  return ''
}

function templateCellClass(row: OverviewRow, col: OverviewTplCol): string {
  if (col.derived === 'remaining') {
    const v = parseInt(templateCellValue(row, col))
    if (!isNaN(v)) {
      if (v < 0) return 'cell-overdue'
      if (v <= 3) return 'cell-urgent'
      if (v <= 7) return 'cell-warning'
    }
  }
  return ''
}

// 模板列编辑：用 label 比对（避开 Vue ref 对象 proxy 陷阱）
const editingTplLabel = ref<string>('')
const editingTplRowId = ref<number>(0)
const editingTplValue = ref<string>('')
function isEditingTpl(row: OverviewRow, col: OverviewTplCol): boolean {
  return editingTplRowId.value === row.id && editingTplLabel.value === col.label
}
function isTplCellEditable(col: OverviewTplCol): boolean {
  return !!col.editable && !!isAdmin.value
}
function startEditTpl(row: OverviewRow, col: OverviewTplCol) {
  if (!isTplCellEditable(col)) return
  editingTplRowId.value = row.id
  editingTplLabel.value = col.label
  // meta 列编辑时显示用户实际存的值（无值就空），不带公式 fallback；
  // name 列显示 row.name；其他列走 templateCellValue
  if (col.source === 'meta') {
    editingTplValue.value = rowMetaValue(row, col.label)
  } else if (col.source === 'name') {
    editingTplValue.value = row.name || ''
  } else {
    editingTplValue.value = templateCellValue(row, col)
  }
}
function cancelEditTpl() {
  editingTplRowId.value = 0
  editingTplLabel.value = ''
}
async function saveEditTpl(row: OverviewRow, col: OverviewTplCol) {
  const newVal = (editingTplValue.value || '').trim()
  const oldVal = templateCellValue(row, col)
  cancelEditTpl()
  if (newVal === oldVal) return
  try {
    if (col.source === 'name') {
      if (!newVal) { ElMessage.warning('项目名称不能为空'); return }
      await projectsApi.update(row.id, { name: newVal })
      const idx = rows.value.findIndex(r => r.id === row.id)
      if (idx >= 0) rows.value[idx] = { ...rows.value[idx], name: newVal }
    } else if (col.source === 'meta') {
      // is_overview=true → 后端存到 __o__<label>（一览独立 key）
      await projectsApi.updateHeaderCell(row.id, col.label, newVal || null, true)
      const idx = rows.value.findIndex(r => r.id === row.id)
      if (idx >= 0) {
        const extra = { ...rows.value[idx].extra }
        const key = `${OVERVIEW_PREFIX}${col.label}`
        if (!newVal) delete extra[key]
        else extra[key] = newVal
        rows.value[idx] = { ...rows.value[idx], extra }
      }
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

const STATUS_COLOR: Record<string, string> = {
  '进行中': 'primary', '已完成': 'success', '已归档': 'info',
}
const STATUS_OPTIONS = ['进行中', '已完成', '已归档']

// 改项目状态（admin / manager）
async function changeStatus(row: OverviewRow, status: string) {
  if (row.status === status) return
  try {
    await projectsApi.update(row.id, { status })
    const idx = rows.value.findIndex(r => r.id === row.id)
    if (idx >= 0) rows.value[idx] = { ...rows.value[idx], status }
    ElMessage.success('状态已更新')
  } catch { /* */ }
}

// 导出项目一览为 Excel
async function onExport() {
  const token = localStorage.getItem('pms_token') || ''
  try {
    const res = await fetch('/api/overview/export', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!res.ok) { ElMessage.error('导出失败'); return }
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = '项目一览.xlsx'
    document.body.appendChild(a); a.click(); a.remove()
    URL.revokeObjectURL(a.href)
  } catch (e: any) {
    ElMessage.error(e.message || '导出失败')
  }
}

async function load() {
  loading.value = true
  try {
    const [b, perms] = await Promise.all([
      overviewApi.get(),
      permApi.myOverviewPerms(),
    ])
    fields.value = b.fields
    rows.value = b.rows
    myPerms.value = perms
  } finally { loading.value = false }
}

// 状态筛选（'' = 全部；'进行中' / '已完成'）
const statusFilter = ref<string>('')
function onStatusFilterChange() { currentPage.value = 1 }

// 项目编号筛选（包含匹配，不区分大小写）
const codeFilter = ref<string>('')

// 过滤 + 分页：状态 → 编号 → 关键词
const filteredRows = computed(() => {
  let result = rows.value
  if (statusFilter.value) {
    result = result.filter(r => r.status === statusFilter.value)
  }
  const cf = codeFilter.value.trim().toLowerCase()
  if (cf) {
    result = result.filter(r => (r.code || '').toLowerCase().includes(cf))
  }
  const k = keyword.value.trim().toLowerCase()
  if (!k) return result
  return result.filter(r => {
    const hay = (r.code + ' ' + r.name + ' ' + (r.status || '') + ' ' +
      Object.values(r.extra || {}).map(v => Array.isArray(v) ? v.join(',') : String(v ?? '')).join(' ')
    ).toLowerCase()
    return hay.includes(k)
  })
})

const pagedRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredRows.value.slice(start, start + pageSize.value)
})

function openProject(rowId: number) {
  router.push({ name: 'project-detail', params: { id: rowId } })
}

// ===== 新建项目（一览页主入口） =====
const createDialogVisible = ref(false)
const creating = ref(false)
const createForm = ref({
  code: '', name: '', status: '进行中',
})
function openCreateProject() {
  createForm.value = { code: '', name: '', status: '进行中' }
  createDialogVisible.value = true
}
async function submitCreateProject() {
  const f = createForm.value
  if (!f.code.trim()) { ElMessage.warning('请填写项目编号'); return }
  if (!f.name.trim()) { ElMessage.warning('请填写项目名称'); return }
  creating.value = true
  try {
    const p = await projectsApi.create({
      code: f.code.trim(),
      name: f.name.trim(),
      status: f.status,
    })
    createDialogVisible.value = false
    ElMessage.success(`已新建项目 ${p.code} · ${p.name}`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '新建失败')
  } finally {
    creating.value = false
  }
}

// 列宽自适应
const fitScreen = ref(localStorage.getItem('pms_overview_fit') !== '0')  // 默认开
function onFitScreenChange(v: boolean) {
  localStorage.setItem('pms_overview_fit', v ? '1' : '0')
}

function colWidth(f: OverviewField): number {
  // 看表头长度
  const headerLen = (f.name || '').length
  // 看当前页所有 cell 内容长度
  let maxLen = headerLen
  for (const r of pagedRows.value) {
    const v = r.extra?.[String(f.id)]
    let s = ''
    if (v == null) s = ''
    else if (Array.isArray(v)) s = v.join('、')
    else s = String(v)
    if (s.length > maxLen) maxLen = s.length
  }
  // 中文 ~14 px / 字符；列标题图标占 36 px
  let w = Math.max(maxLen * 13, headerLen * 13) + 40
  // 紧凑（适应屏幕）模式：每列上限缩小
  if (fitScreen.value) {
    w = Math.min(w, 130)
  } else {
    w = Math.min(w, 260)
  }
  return Math.max(80, w)
}

function getCellValue(row: OverviewRow, f: OverviewField) {
  return row.extra?.[String(f.id)]
}

/** 显示用：把字符串的"整数.0"后缀去掉，保留"2.5米"等 */
function smartFormatValue(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') return String(v)
  const s = String(v)
  const m = /^(-?\d+)\.0+$/.exec(s)
  return m ? m[1] : s
}

// 单元格编辑
const editingCell = ref<{ rowId: number; fieldId: number } | null>(null)
const editingValue = ref<any>(null)

function startEdit(row: OverviewRow, f: OverviewField) {
  if (!fieldEditable(f)) return
  editingCell.value = { rowId: row.id, fieldId: f.id }
  // 统一用文本编辑：array 转字符串以便用户编辑
  const v = getCellValue(row, f)
  if (v == null) editingValue.value = ''
  else if (Array.isArray(v)) editingValue.value = (v as unknown[]).join('、')
  else editingValue.value = String(v)
}

function isEditing(row: OverviewRow, f: OverviewField) {
  return editingCell.value?.rowId === row.id && editingCell.value?.fieldId === f.id
}

async function saveEdit(row: OverviewRow, f: OverviewField) {
  const newVal = editingValue.value
  const oldVal = getCellValue(row, f)
  editingCell.value = null
  if (JSON.stringify(newVal) === JSON.stringify(oldVal)) return
  try {
    await overviewApi.updateCell(row.id, f.id, newVal)
    // 更新本地
    const idx = rows.value.findIndex(r => r.id === row.id)
    if (idx >= 0) {
      const newExtra = { ...rows.value[idx].extra }
      const fid = String(f.id)
      if (newVal === null || newVal === '') delete newExtra[fid]
      else newExtra[fid] = newVal
      rows.value[idx] = { ...rows.value[idx], extra: newExtra }
    }
  } catch { /* */ }
}

function cancelEdit() {
  editingCell.value = null
}

// 字段管理对话框
const fieldDialogVisible = ref(false)
const editingField = ref<OverviewField | null>(null)
const fieldForm = ref({ name: '' })

function openAddField() {
  editingField.value = null
  fieldForm.value = { name: '' }
  fieldDialogVisible.value = true
}
function openEditField(f: OverviewField) {
  editingField.value = f
  fieldForm.value = { name: f.name }
  fieldDialogVisible.value = true
}

async function submitField() {
  if (!fieldForm.value.name.trim()) { ElMessage.warning('请填写字段名'); return }
  if (editingField.value) {
    await overviewApi.updateField(editingField.value.id, {
      name: fieldForm.value.name,
    })
  } else {
    await overviewApi.createField({ name: fieldForm.value.name, type: 'text' })
  }
  fieldDialogVisible.value = false
  ElMessage.success('已保存')
  await load()
}

// "添加项目"已回退，统一在「项目列表」页操作

async function deleteField(f: OverviewField) {
  await ElMessageBox.confirm(`删除列「${f.name}」？所有项目的该列数据都会清空。`, '确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
  }).catch(() => 'cancel').then(async (r) => {
    if (r === 'cancel') return
    await overviewApi.deleteField(f.id)
    ElMessage.success('已删除')
    await load()
  })
}

// Excel 导入
const importing = ref(false)
async function onImportFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const ok = await ElMessageBox.confirm(
    `导入「${file.name}」？\n\n⚠ 此操作会：\n• 删除项目一览所有自定义列（之前配的字段权限会丢）\n• 清空所有项目的一览数据\n• 然后从 Excel 重新导入\n\n项目本身、项目内的进度表数据不受影响。`,
    '全量导入确认',
    { confirmButtonText: '清空并导入', cancelButtonText: '取消', type: 'warning', dangerouslyUseHTMLString: false }
  ).catch(() => false)
  if (!ok) { input.value = ''; return }
  importing.value = true
  try {
    const fd = new FormData(); fd.append('file', file)
    const token = localStorage.getItem('pms_token') || ''
    const r = await axios.post('/api/overview/import', fd, {
      headers: { Authorization: `Bearer ${token}` },
    })
    ElMessage.success(r.data.message || '导入成功')
    await load()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || e.message || '导入失败')
  } finally {
    importing.value = false
    input.value = ''
  }
}

const { connected: rtConnected, onlineCount: rtOnlineCount } = useRealtime('/ws/overview', (ev) => {
  if (ev.project_id && ev.by_user_id !== auth.user?.id) {
    const idx = rows.value.findIndex(r => r.id === ev.project_id)
    if (idx >= 0) {
      const newExtra = { ...rows.value[idx].extra }
      const fid = String(ev.field_id)
      if (ev.value === null || ev.value === '') delete newExtra[fid]
      else newExtra[fid] = ev.value
      rows.value[idx] = { ...rows.value[idx], extra: newExtra }
    }
  }
})

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>项目一览</h1>
        <div class="desc">共 {{ rows.length }} 个项目 · {{ fields.length }} 个自定义列</div>
      </div>
      <div class="spacer"></div>
      <el-tooltip :content="rtConnected ? '实时同步已连接 · ' + rtOnlineCount + ' 人在线' : '实时同步已断开（5 秒后自动重连）'">
        <span class="rt-status" :class="rtConnected ? 'on' : 'off'">
          <span class="dot"></span>{{ rtConnected ? '实时' : '离线' }}
        </span>
      </el-tooltip>
      <el-tooltip content="适应屏幕：所有列尽量在一屏展示（窄列）；关闭则按内容长度自由展开">
        <el-switch v-model="fitScreen" active-text="适应屏幕"
                   @change="onFitScreenChange" />
      </el-tooltip>
      <el-select v-model="statusFilter" placeholder="全部状态" size="large"
                 style="width: 130px" clearable @change="onStatusFilterChange">
        <el-option label="进行中" value="进行中">
          <span class="status-dot status-dot-doing"></span> 进行中
        </el-option>
        <el-option label="已完成" value="已完成">
          <span class="status-dot status-dot-done"></span> 已完成
        </el-option>
      </el-select>
      <el-input v-model="codeFilter" placeholder="项目编号筛选（如 2026）"
                style="width: 200px" size="large" clearable
                @input="currentPage = 1" />
      <el-input v-model="keyword" placeholder="搜索任意列..." style="width: 200px"
                size="large" clearable :prefix-icon="Search" @input="currentPage = 1" />
      <!-- 一览列名已固定为 Excel 模板（与"2026 项目倒计时"对齐），"添加列"已下线 -->
      <el-button :icon="Download" size="large" @click="onExport">导出</el-button>
      <el-button v-if="isAdmin" type="primary" :icon="Plus" size="large" @click="openCreateProject">
        新建项目
      </el-button>
      <!-- 导入汇总表已隐藏（仍保留 onImportFile 函数和路由，可恢复）
      <label v-if="isAdmin" class="el-button el-button--primary el-button--large" style="margin: 0">
        <el-icon style="margin-right:6px"><Upload /></el-icon>
        <span>导入汇总表</span>
        <input type="file" accept=".xlsx,.xlsm,.xls" hidden @change="onImportFile" />
      </label>
      -->
    </div>

    <el-card v-loading="loading">
      <el-table ref="tableRef" :data="pagedRows" border stripe :size="fitScreen ? 'small' : 'default'"
                style="width: 100%" :height="tableHeight"
                :empty-text="loading ? '加载中…' : '无数据'"
                :default-sort="{ prop: 'code', order: 'ascending' }"
                :cell-class-name="cellClassName">
        <el-table-column type="index" label="#" :width="fitScreen ? 38 : 55" align="center" fixed="left"
                         :index="(i: number) => (currentPage - 1) * pageSize + i + 1" />
        <!-- 14 列固定模板（项目编号/项目名称/状态/签订日期/...），所有列居中 -->
        <el-table-column v-for="col in OVERVIEW_FIELDS" :key="col.label"
                         :label="col.label"
                         :min-width="overviewColMinWidth(col)"
                         :fixed="col.source === 'code' ? 'left' : undefined"
                         align="center"
                         header-align="center"
                         show-overflow-tooltip>
          <template #default="{ row }">
            <!-- 状态列：el-select 下拉（只显示 进行中/已完成；旧值显示禁用项）-->
            <template v-if="col.source === 'status'">
              <el-select v-if="isAdmin"
                         :model-value="row.status" size="small" style="width: 100%"
                         @update:model-value="(v: any) => changeStatus(row, v as string)">
                <el-option v-for="s in STATUS_OPTIONS_NEW" :key="s" :value="s" :label="s">
                  <span class="status-dot" :class="{
                    'status-dot-doing': s === '进行中',
                    'status-dot-done': s === '已完成',
                  }"></span> {{ s }}
                </el-option>
                <el-option v-if="row.status && !STATUS_OPTIONS_NEW.includes(row.status)"
                           :label="row.status + '（旧值）'" :value="row.status" disabled />
              </el-select>
              <el-tag v-else
                      :type="row.status === '已完成' ? 'success' : (row.status === '进行中' ? 'danger' : 'info')"
                      effect="dark" size="small">
                {{ row.status }}
              </el-tag>
            </template>
            <!-- meta 编辑态 -->
            <el-input v-else-if="isEditingTpl(row, col)"
                      v-model="editingTplValue" autofocus size="small"
                      class="cell-edit-input"
                      @blur="saveEditTpl(row, col)"
                      @keyup.enter="saveEditTpl(row, col)"
                      @keyup.escape="cancelEditTpl" />
            <!-- 项目编号链接 -->
            <a v-else-if="col.source === 'code'" class="proj-link"
               @click.stop="openProject(row.id)">{{ row.code }}</a>
            <!-- 其他列：值 + 可编辑高亮 -->
            <span v-else class="cell"
                  :class="[
                    templateCellClass(row, col),
                    { editable: isTplCellEditable(col) },
                  ]"
                  @click="startEditTpl(row, col)">
              <span v-if="templateCellValue(row, col)">{{ templateCellValue(row, col) }}</span>
              <span v-else class="muted">-</span>
            </span>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pager">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="filteredRows.length"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- 字段对话框 -->
    <el-dialog v-model="fieldDialogVisible" :title="editingField ? '编辑列' : '添加列'" width="500px">
      <el-form label-position="top">
        <el-form-item label="列名 *">
          <el-input v-model="fieldForm.name" size="large" placeholder="如：销售" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fieldDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitField">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新建项目对话框（与项目列表共享同一后端接口）-->
    <el-dialog v-model="createDialogVisible" title="新建项目" width="500px"
               :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="项目编号 *">
          <el-input v-model="createForm.code" size="large" placeholder="如 2026-040"
                    @keyup.enter="submitCreateProject" />
        </el-form-item>
        <el-form-item label="项目名称 *">
          <el-input v-model="createForm.name" size="large"
                    placeholder="如 500ML 双行星混合机"
                    @keyup.enter="submitCreateProject" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="createForm.status" size="large" style="width:100%">
            <el-option label="进行中" value="进行中" />
            <el-option label="已完成" value="已完成" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreateProject">
          新建并跳转
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.rt-status {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 12px; border-radius: 14px;
  font-size: 12px; font-weight: 500;
  background: #f3f4f6; color: var(--text-3);
}
.rt-status .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.rt-status.on { background: #ecfdf5; color: var(--success); }
.rt-status.on .dot { animation: pulse 1.6s ease-in-out infinite; }
.rt-status.off { background: #fef2f2; color: var(--danger); }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }

.proj-link {
  color: var(--primary); font-weight: 600;
  cursor: pointer; text-decoration: none;
}
.proj-link:hover { text-decoration: underline; }
.proj-name { cursor: pointer; }
.proj-name:hover { color: var(--primary); }

.muted { color: var(--text-3); }

.field-header { display: inline-flex; align-items: center; gap: 6px; width: 100%; }
.field-name {
  flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-weight: 700;
  color: #0f172a;
  font-size: 12.5px;
  letter-spacing: 0.2px;
}

.cell {
  display: inline-block; min-width: 60px; min-height: 32px;
  padding: 6px 8px;
  line-height: 20px;
  text-align: center;
}
.cell.editable {
  cursor: cell; border-radius: 3px;
}
.cell.editable:hover {
  background: rgba(37,99,235,.08);
  outline: 1px dashed var(--primary);
}

.pager { padding: 16px 0; text-align: right; }

/* 派生列紧迫度色（剩余制作时间）*/
.cell.cell-warning { color: #b45309 !important; font-weight: 700; }
.cell.cell-urgent { color: #b91c1c !important; font-weight: 700; }
.cell.cell-overdue {
  color: #ffffff !important; background: #dc2626 !important;
  font-weight: 700; padding: 0 4px; border-radius: 3px;
}

/* 状态列：整个单元格按状态染色（强制覆盖斑马纹/hover/默认底） */
:deep(.el-table td.el-table__cell.cell-row-done),
:deep(.el-table tbody tr td.el-table__cell.cell-row-done),
:deep(.el-table tbody tr:hover td.el-table__cell.cell-row-done),
:deep(.el-table .el-table__row--striped td.el-table__cell.cell-row-done) {
  background: #d1fae5 !important;  /* 绿 */
}
:deep(.el-table td.el-table__cell.cell-row-doing),
:deep(.el-table tbody tr td.el-table__cell.cell-row-doing),
:deep(.el-table tbody tr:hover td.el-table__cell.cell-row-doing),
:deep(.el-table .el-table__row--striped td.el-table__cell.cell-row-doing) {
  background: #fee2e2 !important;  /* 红 */
}
/* el-select 在状态格内：让 wrapper 透明，文字按状态色加深加粗 */
:deep(.cell-row-done .el-select__wrapper),
:deep(.cell-row-doing .el-select__wrapper) {
  background: rgba(255, 255, 255, 0.55) !important;
  box-shadow: none !important;
  border: 1px solid rgba(0, 0, 0, .15) !important;
}
:deep(.cell-row-done .el-select__wrapper .el-select__selected-item),
:deep(.cell-row-done .el-select__wrapper input) {
  color: #065f46 !important;
  font-weight: 700 !important;
}
:deep(.cell-row-doing .el-select__wrapper .el-select__selected-item),
:deep(.cell-row-doing .el-select__wrapper input) {
  color: #991b1b !important;
  font-weight: 700 !important;
}

/* 状态筛选 dropdown 里的小圆点 */
.status-dot {
  display: inline-block;
  width: 8px; height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.status-dot-doing { background: #ef4444; }
.status-dot-done { background: #10b981; }
.status-dot-archived { background: #94a3b8; }

/* ===== 表格底色 + 加粗边框 + 圆角（v2: 加重视觉分量） ===== */
:deep(.el-table) {
  --el-table-border-color: #94a3b8;
  --el-table-border: 2px solid #94a3b8;
  --el-table-header-bg-color: #cbd5e1;
  border-radius: 10px;
  border: 2px solid #64748b;
}
:deep(.el-table .el-table__inner-wrapper) {
  border-radius: 10px;
  overflow: hidden;
}
:deep(.el-table th.el-table__cell) {
  background: linear-gradient(180deg, #cbd5e1 0%, #94a3b8 100%) !important;
  color: #0f172a;
  font-weight: 800;
  text-align: center;
}
:deep(.el-table th.el-table__cell .cell) {
  font-weight: 800 !important;
  color: #0f172a !important;
  letter-spacing: 0.3px;
}
/* 单元格内容默认居中、字体加粗加深，对齐项目列表风格 */
:deep(.el-table td.el-table__cell) {
  text-align: center !important;
}
/* 关键：只命中 Element Plus 自己的外层 .cell 包裹层（td 的直接子级），
   强制其内部 inline/inline-block 内容（我们的 span.cell / a.proj-link / 状态下拉）水平居中。
   用 > 直接子选择器，避免把我们嵌套的 span.cell 也改成块级而破坏「已过期」红底徽章形态 */
:deep(.el-table td.el-table__cell > .cell) {
  text-align: center !important;
}
:deep(.el-table td.el-table__cell .cell) {
  color: #0f172a;
  font-weight: 600;
}
:deep(.el-table td.el-table__cell),
:deep(.el-table th.el-table__cell) {
  border-right: 2px solid #94a3b8 !important;
  border-bottom: 2px solid #94a3b8 !important;
}

/* 紧凑模式（fitScreen）：减小 padding + 字号，让 14 列在 14 寸笔记本全屏可见 */
:deep(.el-table--small .el-table__cell) {
  padding: 3px 0 !important;
  height: auto !important;
}
:deep(.el-table--small .el-table__cell .cell) {
  padding: 0 5px !important;
  line-height: 1.35 !important;
  font-size: 12.5px !important;
}
:deep(.el-table--small th.el-table__cell .cell) {
  font-weight: 700 !important;
  font-size: 12.5px !important;
  letter-spacing: 0.2px;
}
/* 紧凑模式下输入框/下拉/cell span 也跟着小 */
:deep(.el-table--small .el-select__wrapper) {
  min-height: 22px !important;
  padding: 0 6px !important;
  font-size: 12px !important;
}
:deep(.el-table--small .el-input__wrapper) {
  padding: 0 4px !important;
}
:deep(.el-table--small .el-input__inner) {
  height: 22px !important;
  font-size: 12px !important;
}
:deep(.el-table--border),
:deep(.el-table--border .el-table__inner-wrapper) {
  border-color: #64748b !important;
}
:deep(.el-table tbody tr td.el-table__cell) {
  background: #ffffff;
  color: #1e293b;
}
:deep(.el-table .el-table__row--striped td.el-table__cell) {
  background: #e2e8f0 !important;
}
:deep(.el-table tbody tr:hover td.el-table__cell) {
  background: #dbeafe !important;
}

/* ===== 编辑框加大 ===== */
.cell-edit-input :deep(.el-input__wrapper) {
  padding: 2px 10px;
  border-radius: 4px;
  box-shadow: 0 0 0 2px var(--primary) inset;
  background: #f5f9ff;
}
.cell-edit-input :deep(.el-input__inner) {
  height: 32px;
  font-size: 14px;
  color: #1f2d3d;
}

/* 小屏笔记本 / 平板：压缩单元格与分页器 */
@media (max-height: 800px) {
  .cell { min-height: 26px; padding: 4px 6px; }
  .pager { padding: 8px 0; }
}
</style>
