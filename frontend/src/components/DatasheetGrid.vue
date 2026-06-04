<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Setting, Edit, Search } from '@element-plus/icons-vue'
import { datasheetsApi } from '@/api/datasheets'
import { permApi } from '@/api/permissions'
import { useAuthStore } from '@/stores/auth'
// 字段权限统一在「权限管理 → 权限矩阵」页配置，不再挂在表头
import { useRealtime } from '@/composables/useRealtime'
import { useTableHeight } from '@/composables/useTableHeight'
// 单元格手动公式（=A2+B2）功能已禁用；保留 utils/formula.ts 文件以便后续重启。
// 系统自动公式（preamble 的"货期/已过时间/倒计时"）走 preambleCell/preambleFormula，
// 与单元格公式无关，继续工作。
import { projectsApi } from '@/api/projects'
import type { DataField, DataRecord, Project } from '@/types'

const props = defineProps<{
  datasheetId: number
  canEdit: boolean
  headerLines?: string[][] | null
  // 项目级元数据：项目头表的所有 sheet 共享同一份数据
  project?: Project | null
}>()
const emit = defineEmits<{
  // 项目头表某字段（meta 来源）被更新
  'header-updated': [{ key: string; value: string | null }]
  // 项目自身字段（name / code 等）被更新
  'project-field-updated': [{ field: 'name' | 'code'; value: string }]
}>()

const keyword = ref("")
const fields = ref<DataField[]>([])
const records = ref<DataRecord[]>([])
const loading = ref(false)
const auth = useAuthStore()
const myPerms = ref<Record<string, { can_view: boolean; can_edit: boolean }>>({})
const isAdmin = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))

// 分页 + 适应屏幕
const pageSize = ref(20)
const currentPage = ref(1)
const fitScreen = ref(localStorage.getItem('pms_datasheet_fit') !== '0')
function onFitScreenChange(v: boolean) {
  localStorage.setItem('pms_datasheet_fit', v ? '1' : '0')
}

// 表格自带 # 行号列，名为"序号" / "#" / "No" 的字段视为冗余，自动隐藏
const ROWNUM_FIELD_NAMES = new Set(['序号', '#', 'no', 'no.', '序', '行号', 'index'])
function isRownumField(f: DataField): boolean {
  return ROWNUM_FIELD_NAMES.has((f.name || '').trim().toLowerCase())
}

const visibleFields = computed(() =>
  fields.value.filter(f =>
    myPerms.value[String(f.id)]?.can_view !== false && !isRownumField(f),
  )
)
function fieldEditable(f: DataField): boolean {
  if (!props.canEdit) return false
  return myPerms.value[String(f.id)]?.can_edit !== false
}

async function load() {
  loading.value = true
  try {
    const [fs, rs, perms] = await Promise.all([
      datasheetsApi.listFields(props.datasheetId),
      datasheetsApi.listRecords(props.datasheetId),
      permApi.myDatasheetPerms(props.datasheetId),
    ])
    fields.value = fs
    records.value = rs
    myPerms.value = perms
    currentPage.value = 1
  } finally { loading.value = false }
}

watch(() => props.datasheetId, () => load(), { immediate: true })

// 表格固定高度自适应视口（让横/纵滚动条始终在视口内）
const tableRef = ref()
const { height: tableHeight } = useTableHeight(tableRef)

const { onlineCount, connected } = useRealtime(`/ws/datasheets/${props.datasheetId}`, (ev) => {
  if (ev.record_id && ev.by_user_id !== auth.user?.id) {
    const idx = records.value.findIndex(r => r.id === ev.record_id)
    if (idx >= 0) {
      const newValues = { ...records.value[idx].values }
      const fid = String(ev.field_id)
      if (ev.value === null || ev.value === '') delete newValues[fid]
      else newValues[fid] = ev.value as never
      records.value[idx] = { ...records.value[idx], values: newValues }
    }
  }
})

const filteredRecords = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return records.value
  return records.value.filter(r => {
    const hay = Object.values(r.values || {})
      .map(v => Array.isArray(v) ? v.join(',') : String(v ?? ''))
      .join(' ').toLowerCase()
    return hay.includes(k)
  })
})

const pagedRecords = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredRecords.value.slice(start, start + pageSize.value)
})

// ===== 项目头表的派生列依赖：跨天响应式 =====
// 用一个响应式 key 表示"今天是哪一天"。每分钟轮询一次系统时间，
// 如果日期串变了（跨过 0 点）就更新这个 key，触发所有依赖它的
// 公式列重新渲染。这样浏览器开整天不动也会自动跳天。
const todayKey = ref(new Date().toDateString())
let _todayTimer: number | null = null
onMounted(() => {
  _todayTimer = window.setInterval(() => {
    const k = new Date().toDateString()
    if (k !== todayKey.value) todayKey.value = k
  }, 60_000)  // 60 秒检查一次足够，跨天最坏延迟 1 分钟
})
onBeforeUnmount(() => {
  if (_todayTimer !== null) {
    window.clearInterval(_todayTimer)
    _todayTimer = null
  }
})

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

// ============= 项目头表（固定 13 列，所有 sheet 共享一份数据） =============
type HeaderColumn = {
  label: string
  source: 'index' | 'code' | 'name' | 'meta' | 'derived'
  metaKey?: string
  derivedKey?: 'duration' | 'elapsed' | 'remaining'
  editable: boolean
  widthPct: number  // 占整张项目头表宽度的百分比，总和约 100
}

const COMPANY_TITLE = '同辉智能装备（无锡）有限公司   (注解：图纸编号 字母+2 位数/材料编号 2 位数/完成度 进行中/完成 填充/钣金字母 B 打头，机加工 J 打头，外购 W 打头)'

const HEADER_COLUMNS: HeaderColumn[] = [
  { label: '序号',     source: 'index',   editable: false, widthPct: 4 },
  { label: '项目编号', source: 'code',    editable: true,  widthPct: 8 },   // 改 Project.code
  { label: '设备名称', source: 'name',    editable: true,  widthPct: 18 },  // 改 Project.name
  { label: '数量',     source: 'meta',    metaKey: '数量',     editable: true, widthPct: 5 },
  { label: '制表日期', source: 'meta',    metaKey: '制表日期', editable: true, widthPct: 9 },
  { label: '销售',     source: 'meta',    metaKey: '销售',     editable: true, widthPct: 6 },
  { label: '设计师',   source: 'meta',    metaKey: '设计师',   editable: true, widthPct: 6 },
  { label: '电器',     source: 'meta',    metaKey: '电器',     editable: true, widthPct: 6 },
  { label: '下单日期', source: 'meta',    metaKey: '下单日期', editable: true, widthPct: 9 },
  { label: '交货日期', source: 'meta',    metaKey: '交货日期', editable: true, widthPct: 9 },
  { label: '货期',     source: 'derived', derivedKey: 'duration',  editable: false, widthPct: 5 },
  { label: '已过时间', source: 'derived', derivedKey: 'elapsed',   editable: false, widthPct: 6 },
  { label: '倒计时',   source: 'derived', derivedKey: 'remaining', editable: false, widthPct: 9 },
]

function projectHeaderValue(col: HeaderColumn, rowSeq = 1): string {
  const p = props.project
  if (!p) return ''
  if (col.source === 'index') return String(rowSeq)
  if (col.source === 'code') return p.code || ''
  if (col.source === 'name') return p.name || ''
  if (col.source === 'meta' && col.metaKey) {
    return smartFormatValue(p.header_meta?.[col.metaKey])
  }
  if (col.source === 'derived') {
    void todayKey.value  // 响应式依赖：跨天自动重算
    const orderDate = parseLooseDate(p.header_meta?.['下单日期'])
    const deliverDate = parseLooseDate(p.header_meta?.['交货日期'])
    const today = new Date()
    if (col.derivedKey === 'duration') {
      if (orderDate && deliverDate) return String(daysBetween(deliverDate, orderDate))
    } else if (col.derivedKey === 'elapsed') {
      if (orderDate) return String(daysBetween(today, orderDate))
    } else if (col.derivedKey === 'remaining') {
      if (deliverDate) return String(daysBetween(deliverDate, today))
    }
  }
  return ''
}

function projectHeaderFormula(col: HeaderColumn): string {
  if (col.source === 'derived') {
    if (col.derivedKey === 'duration')  return '= 交货日期 - 下单日期'
    if (col.derivedKey === 'elapsed')   return '= TODAY() - 下单日期'
    if (col.derivedKey === 'remaining') return '= 交货日期 - TODAY()'
  }
  return ''
}

function projectHeaderClass(col: HeaderColumn): string {
  if (col.source === 'derived' && col.derivedKey === 'remaining') {
    const v = parseInt(projectHeaderValue(col))
    if (!isNaN(v)) {
      if (v < 0) return 'preamble-overdue'
      if (v <= 3) return 'preamble-urgent'
      if (v <= 7) return 'preamble-warning'
    }
  }
  return ''
}

// 项目头单元格编辑
// 注意：用 label 字符串做标识，不要直接存 HeaderColumn 对象引用 ——
// Vue 3 ref 对对象值会自动 reactive proxy 包装，导致
// editingHeader.value === col 永远 false（proxy ≠ raw object）。
const editingHeaderLabel = ref<string>('')
const editingHeaderValue = ref<string>('')

function isEditingHeader(col: HeaderColumn): boolean {
  return editingHeaderLabel.value === col.label
}
function isHeaderCellEditable(col: HeaderColumn): boolean {
  return !!col.editable && !!props.canEdit && !projectHeaderFormula(col)
}
function startEditHeader(col: HeaderColumn) {
  if (!isHeaderCellEditable(col)) return
  editingHeaderLabel.value = col.label
  editingHeaderValue.value = projectHeaderValue(col)
}
function onHeaderCellClick(col: HeaderColumn) {
  if (isHeaderCellEditable(col) && !isEditingHeader(col)) {
    startEditHeader(col)
  }
}
function cancelEditHeader() { editingHeaderLabel.value = '' }
async function saveHeader() {
  const label = editingHeaderLabel.value
  editingHeaderLabel.value = ''
  if (!label || !props.project) return
  const col = HEADER_COLUMNS.find(c => c.label === label)
  if (!col) return
  const newVal = editingHeaderValue.value.trim()
  const oldVal = projectHeaderValue(col)
  if (newVal === oldVal) return
  try {
    if (col.source === 'meta' && col.metaKey) {
      // 写入 Project.extra 的 __h__<key>
      await projectsApi.updateHeaderCell(props.project.id, col.metaKey, newVal || null)
      emit('header-updated', { key: col.metaKey, value: newVal || null })
    } else if (col.source === 'name') {
      // 写入 Project.name
      if (!newVal) { ElMessage.warning('设备名称不能为空'); return }
      await projectsApi.update(props.project.id, { name: newVal })
      emit('project-field-updated', { field: 'name', value: newVal })
    } else if (col.source === 'code') {
      // 写入 Project.code（后端会校验唯一性）
      if (!newVal) { ElMessage.warning('项目编号不能为空'); return }
      await projectsApi.update(props.project.id, { code: newVal })
      emit('project-field-updated', { field: 'code', value: newVal })
    } else {
      return
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

function getCellValue(record: DataRecord, f: DataField) {
  return record.values?.[String(f.id)]
}

/** 显示用格式化：
 *  - "整数.0/.00" → 去尾巴（"6.0" → "6"）
 *  - "YYYY-MM-DD 00:00:00" 或带 T 的同款 → 截掉时间部分（只剩日期）
 *  - "2.5米"、"1+4 (备)"、"1.5" 等保留不动
 */
function smartFormatValue(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') return String(v)
  let s = String(v)
  // 1) 日期带零点时间：去掉时间部分
  const dateOnly = /^(\d{4}-\d{1,2}-\d{1,2})[T ]00:00:00(\.\d+)?Z?$/.exec(s)
  if (dateOnly) s = dateOnly[1]
  // 2) 整数.0 → 去尾巴
  const intDot0 = /^(-?\d+)\.0+$/.exec(s)
  if (intDot0) s = intDot0[1]
  return s
}

/** 单元格显示值（纯文本 / 数组），不再做公式求值 */
function displayCellValue(_record: DataRecord, f: DataField): {
  text: string; isError: boolean; isEmpty: boolean
} {
  const raw = getCellValue(_record, f)
  if (Array.isArray(raw)) {
    return raw.length
      ? { text: raw.map(x => smartFormatValue(x)).join('、'), isError: false, isEmpty: false }
      : { text: '-', isError: false, isEmpty: true }
  }
  if (raw == null || raw === '') return { text: '-', isError: false, isEmpty: true }
  return { text: smartFormatValue(raw), isError: false, isEmpty: false }
}

// cellStatusClass 已下线 —— 之前按"值"识别会误染其他字段（如"仓库签字"
// 字段值是"完成"也会变绿）。现在统一由 datasheetCellClass 按"字段名 +
// 白名单值"严格判断，只对"进度"等列整格着色。

// 判断字段是否是"进度"列（编辑时显示下拉，限定选项）
const PROGRESS_FIELD_NAMES = new Set([
  '进度', '进度100%', '完成度', '状态', '完工度',
])
function isProgressField(f: DataField): boolean {
  return PROGRESS_FIELD_NAMES.has((f.name || '').trim())
}
const PROGRESS_OPTIONS = ['完成', '进行中']

// 整列着色：进度列的 td 按值染色（与项目一览状态列一致）
// 用 columnIndex 精确定位字段：columnIndex 0 是 # 自动行号列，从 1 起对应 visibleFields[0..]
// 严格匹配已知状态词，其他值（日期、人名、数字）一律不染色 —— 避免误伤
const STATUS_DONE_WORDS = new Set(['完成', '已完成', '完工', '已结束'])
const STATUS_DOING_WORDS = new Set([
  '进行中', '正在做', '处理中', '在做',
  '未开始', '待开始', '待处理', '未开工',
  '延期', '逾期', '超期',
  '暂停', '搁置', '挂起',
  '取消', '作废',
  '待审核', '审核中',
])
function datasheetCellClass({ row, columnIndex }: any): string {
  const fieldIdx = columnIndex - 1
  if (fieldIdx < 0 || fieldIdx >= visibleFields.value.length) return ''
  const f = visibleFields.value[fieldIdx]
  if (!f || !isProgressField(f)) return ''
  const v = String(row.values?.[String(f.id)] ?? '').trim()
  if (!v || v === '-') return ''
  if (STATUS_DONE_WORDS.has(v)) return 'cell-row-done'
  if (STATUS_DOING_WORDS.has(v)) return 'cell-row-doing'
  return ''  // 其他值（如日期）不染色
}

// el-select 渲染后自动 focus + 弹出 dropdown（automatic-dropdown 配合）
// 这样用户单击单元格 → 下拉立刻打开，看到两个选项
function onProgressSelectMount(el: any) {
  if (!el) return
  // nextTick 后 focus，触发 automatic-dropdown 打开 dropdown
  setTimeout(() => {
    try { el.focus?.() } catch { /* ignore */ }
  }, 0)
}

// 列宽自适应（紧凑版：每字符 ~10px，14 寸笔记本全屏可显）
function colWidth(f: DataField): number {
  const headerLen = (f.name || '').length
  let maxLen = headerLen
  for (const r of pagedRecords.value) {
    const d = displayCellValue(r, f)
    const s = d.text || ''
    if (s.length > maxLen) maxLen = s.length
  }
  let w = Math.max(maxLen * 10, headerLen * 10) + 16
  if (fitScreen.value) {
    w = Math.min(w, 95)   // 紧凑模式：上限 95
  } else {
    w = Math.min(w, 200)
  }
  return Math.max(56, w)  // 最小 56
}

// 单元格编辑
const editingCell = ref<{ rowId: number; fieldId: number } | null>(null)
const editingValue = ref<any>(null)

function startEdit(r: DataRecord, f: DataField) {
  if (!fieldEditable(f)) return
  editingCell.value = { rowId: r.id, fieldId: f.id }
  // 统一用文本编辑：array 转字符串以便用户编辑
  const v = getCellValue(r, f)
  if (v == null) editingValue.value = ''
  else if (Array.isArray(v)) editingValue.value = (v as unknown[]).join('、')
  else editingValue.value = String(v)
}

function isEditing(r: DataRecord, f: DataField) {
  return editingCell.value?.rowId === r.id && editingCell.value?.fieldId === f.id
}

async function saveEdit(r: DataRecord, f: DataField) {
  const newVal = editingValue.value
  const oldVal = getCellValue(r, f)
  editingCell.value = null
  if (JSON.stringify(newVal) === JSON.stringify(oldVal)) return
  try {
    await datasheetsApi.updateCell(r.id, f.id, newVal)
    const idx = records.value.findIndex(x => x.id === r.id)
    if (idx >= 0) {
      const newValues = { ...records.value[idx].values }
      const fid = String(f.id)
      if (newVal === null || newVal === '') delete newValues[fid]
      else newValues[fid] = newVal
      records.value[idx] = { ...records.value[idx], values: newValues }
    }
  } catch { /* */ }
}

function cancelEdit() { editingCell.value = null }

// 字段管理
const fieldDialogVisible = ref(false)
const editingField = ref<DataField | null>(null)
const fieldForm = ref({ name: '' })

function openEditField(f: DataField) {
  editingField.value = f
  fieldForm.value = { name: f.name }
  fieldDialogVisible.value = true
}

async function submitField() {
  if (!fieldForm.value.name.trim()) { ElMessage.warning('请填写字段名'); return }
  try {
    if (editingField.value) {
      await datasheetsApi.updateField(editingField.value.id, {
        name: fieldForm.value.name,
      })
    } else {
      await datasheetsApi.createField(props.datasheetId, {
        name: fieldForm.value.name, type: 'text',
      })
    }
    fieldDialogVisible.value = false
    ElMessage.success('已保存')
    await load()
  } catch { /* */ }
}

async function deleteField(f: DataField) {
  await ElMessageBox.confirm(`删除字段「${f.name}」？该字段所有行的数据都会丢失。`, '确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
  }).catch(() => 'cancel').then(async (r) => {
    if (r === 'cancel') return
    await datasheetsApi.deleteField(f.id)
    ElMessage.success('已删除')
    await load()
  })
}

async function addRow() {
  await datasheetsApi.createRecord(props.datasheetId, {})
  await load()
}

// deleteRow 已下线（行删除按钮已从 UI 隐藏）
</script>

<template>
  <div class="grid-wrap">
    <div class="grid-toolbar">
<el-button v-if="canEdit" type="primary" :icon="Plus" @click="addRow">添加行</el-button>
      
      <span style="flex: 1"></span>
      <el-tooltip :content="connected ? '实时同步已连接 · ' + onlineCount + ' 人在线' : '实时同步已断开（5 秒后自动重连）'">
        <span class="rt-status" :class="connected ? 'on' : 'off'">
          <span class="dot"></span>{{ connected ? '实时' : '离线' }}
        </span>
      </el-tooltip>
      <el-tooltip content="适应屏幕：所有列尽量在一屏展示（窄列）">
        <el-switch v-model="fitScreen" active-text="适应屏幕" size="small"
                   @change="onFitScreenChange" />
      </el-tooltip>
      <el-input v-model="keyword" placeholder="搜索..." style="width: 200px"
                clearable :prefix-icon="Search" @input="currentPage = 1" />
      <span class="muted small">{{ filteredRecords.length }} / {{ records.length }} 行</span>
    </div>

    <!-- 项目头表：固定 13 列，所有 sheet 共享一份；数量/销售/...等可编辑 -->
    <div v-if="props.project" class="preamble">
      <table class="preamble-table">
        <colgroup>
          <col v-for="col in HEADER_COLUMNS" :key="'cg-' + col.label"
               :style="{ width: col.widthPct + '%' }" />
        </colgroup>
        <!-- 第 1 行：公司标题（硬编码） -->
        <tr>
          <td :colspan="HEADER_COLUMNS.length" class="preamble-title">
            {{ COMPANY_TITLE }}
          </td>
        </tr>
        <!-- 第 2 行：表头 -->
        <tr class="preamble-info-head">
          <td v-for="col in HEADER_COLUMNS" :key="'h-' + col.label">
            <span>{{ col.label }}</span>
            <el-tooltip v-if="projectHeaderFormula(col)" :content="projectHeaderFormula(col)" placement="top">
              <span class="preamble-fx-badge">fx</span>
            </el-tooltip>
          </td>
        </tr>
        <!-- 第 3 行：值 -->
        <tr class="preamble-info-value">
          <td v-for="col in HEADER_COLUMNS" :key="'v-' + col.label"
              :class="[
                projectHeaderClass(col),
                { 'preamble-cell-editable': isHeaderCellEditable(col) && !isEditingHeader(col) },
              ]"
              @click="onHeaderCellClick(col)"
              @dblclick="onHeaderCellClick(col)">
            <!-- 编辑态 -->
            <el-input v-if="isEditingHeader(col)"
                      v-model="editingHeaderValue" size="small" autofocus
                      class="header-edit-input"
                      @blur="saveHeader"
                      @keyup.enter="saveHeader"
                      @keyup.escape="cancelEditHeader"
                      @click.stop />
            <!-- 显示态：派生列（带 fx 提示），不可编辑 -->
            <el-tooltip v-else-if="projectHeaderFormula(col)" placement="top">
              <template #content>
                <div style="line-height:1.7">
                  <div style="font-weight:600">{{ projectHeaderFormula(col) }}</div>
                  <div style="font-size:11px;opacity:.7">基于"下单日期"、"交货日期"、今天实时计算</div>
                </div>
              </template>
              <span class="preamble-fx-value">{{ projectHeaderValue(col) }}</span>
            </el-tooltip>
            <!-- 显示态：可编辑列 / 只读列 -->
            <template v-else>
              <template v-if="projectHeaderValue(col)">{{ projectHeaderValue(col) }}</template>
              <span v-else class="header-cell-empty">
                {{ isHeaderCellEditable(col) ? '点击填写' : '-' }}
              </span>
            </template>
          </td>
        </tr>
      </table>
    </div>

    <el-table ref="tableRef" :data="pagedRecords" border stripe :size="fitScreen ? 'small' : 'default'"
              style="width: 100%" :height="tableHeight"
              v-loading="loading"
              :cell-class-name="datasheetCellClass"
              :empty-text="loading ? '加载中…' : (fields.length === 0 ? '请先添加字段（列）' : '暂无数据，点添加行开始录入')">
      <el-table-column type="index" label="#" width="38" align="center" fixed="left"
                       :index="(i: number) => (currentPage - 1) * pageSize + i + 1" />

      <el-table-column v-for="f in visibleFields" :key="f.id" :label="f.name"
                       :min-width="colWidth(f)" show-overflow-tooltip
                       align="center" header-align="center">
        <template #header>
          <span class="field-header">
            <el-tooltip :content="f.name" placement="top" :show-after="300" :hide-after="0">
              <span class="field-name">{{ f.name }}</span>
            </el-tooltip>
          </span>
        </template>
        <template #default="{ row }">
          <template v-if="isEditing(row, f)">
            <!-- 进度列：固定下拉框（完成 / 进行中），存量旧值显示为禁用项让用户重新选 -->
            <el-select v-if="isProgressField(f)"
                       v-model="editingValue" size="small"
                       class="cell-edit-select"
                       automatic-dropdown
                       :ref="(el: any) => onProgressSelectMount(el)"
                       @change="saveEdit(row, f)"
                       @blur="saveEdit(row, f)"
                       @keyup.escape="cancelEdit">
              <el-option v-for="opt in PROGRESS_OPTIONS" :key="opt" :label="opt" :value="opt" />
              <el-option v-if="editingValue && !PROGRESS_OPTIONS.includes(editingValue)"
                         :label="editingValue + ' （旧值，请重新选择）'"
                         :value="editingValue" disabled />
            </el-select>
            <el-input v-else v-model="editingValue" autofocus class="cell-edit-input"
                      @blur="saveEdit(row, f)" @keyup.enter="saveEdit(row, f)" @keyup.escape="cancelEdit" />
          </template>
          <template v-else>
            <!-- 不再用 cellStatusClass 给 span 着色（避免误染"仓库签字"等
                 字段中文值是"完成"的情况）；整格着色由 :cell-class-name
                 +datasheetCellClass 完成，仅对"进度"列生效 -->
            <span class="cell"
                  :class="{ editable: fieldEditable(f) }"
                  @click="startEdit(row, f)">
              <template v-if="displayCellValue(row, f).isEmpty">
                <span class="muted">-</span>
              </template>
              <template v-else>
                {{ displayCellValue(row, f).text }}
              </template>
            </span>
          </template>
        </template>
      </el-table-column>

      <!-- "操作"删除列已隐藏（行删除可去后台维护） -->
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :total="filteredRecords.length"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
      />
    </div>

    <!-- 字段对话框 -->
    <el-dialog v-model="fieldDialogVisible" :title="editingField ? '编辑字段' : '添加字段'" width="500px">
      <el-form label-position="top">
        <el-form-item label="字段名 *">
          <el-input v-model="fieldForm.name" size="large" placeholder="如：供应商" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fieldDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitField">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.grid-wrap { background: white; border-radius: var(--radius); overflow: hidden; }
.grid-toolbar {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
}
.muted { color: var(--text-3); }
.small { font-size: 12px; }

.rt-status {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 500;
  background: #f3f4f6; color: var(--text-3);
}
.rt-status .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.rt-status.on { background: #ecfdf5; color: var(--success); }
.rt-status.on .dot { animation: ds-pulse 1.6s ease-in-out infinite; }
.rt-status.off { background: #fef2f2; color: var(--danger); }
@keyframes ds-pulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }

.field-header {
  display: inline-flex; align-items: center; gap: 5px; width: 100%;
}
.field-name {
  flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  /* 字段表头：粗体 + 深色，与 preamble 表头风格一致 */
  font-weight: 700;
  color: #0f172a;
  font-size: 12.5px;
  letter-spacing: 0.2px;
}

.cell {
  display: inline-block;
  min-width: 40px;
  min-height: 22px;
  padding: 2px 4px;
  line-height: 18px;
  font-size: 12.5px;
  font-weight: 600;
  color: #0f172a;
  text-align: center;
}
.cell.editable {
  cursor: cell; border-radius: 3px;
}
.cell.editable:hover {
  background: rgba(37,99,235,.10);
  outline: 1px dashed var(--primary);
}

/* .cell.status-done / status-doing 已下线，避免按值染色误染其他字段 */

/* 进度列整格着色：覆盖斑马纹和 hover */
:deep(.el-table td.el-table__cell.cell-row-done),
:deep(.el-table tbody tr td.el-table__cell.cell-row-done),
:deep(.el-table tbody tr:hover td.el-table__cell.cell-row-done),
:deep(.el-table .el-table__row--striped td.el-table__cell.cell-row-done) {
  background: #d1fae5 !important;
}
:deep(.el-table td.el-table__cell.cell-row-doing),
:deep(.el-table tbody tr td.el-table__cell.cell-row-doing),
:deep(.el-table tbody tr:hover td.el-table__cell.cell-row-doing),
:deep(.el-table .el-table__row--striped td.el-table__cell.cell-row-doing) {
  background: #fee2e2 !important;
}
/* 进度列内的文字也加深加粗 */
:deep(.cell-row-done .cell) { color: #065f46 !important; font-weight: 800 !important; }
:deep(.cell-row-doing .cell) { color: #991b1b !important; font-weight: 800 !important; }
/* 编辑态 select 透明白底，让格底色显出 */
:deep(.cell-row-done .el-select__wrapper),
:deep(.cell-row-doing .el-select__wrapper) {
  background: rgba(255, 255, 255, 0.55) !important;
  box-shadow: none !important;
  border: 1px solid rgba(0, 0, 0, .15) !important;
}
/* 单元格手动公式相关样式（.cell.formula / .formula-help）已随功能下线移除 */

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
  font-weight: 700;
  font-size: 12.5px;
  padding: 3px 0 !important;
  height: auto !important;
}
:deep(.el-table th.el-table__cell .cell) {
  padding: 0 4px !important;
  line-height: 1.3;
  font-weight: 700;
}
:deep(.el-table td.el-table__cell) {
  padding: 2px 0 !important;
  height: auto !important;
  text-align: center !important;
}
/* 强制 Element Plus 外层 .cell 包裹层（td 直接子级）内联内容居中，
   使数据表所有字段值水平居中展示 */
:deep(.el-table td.el-table__cell > .cell) {
  text-align: center !important;
}
:deep(.el-table td.el-table__cell .cell) {
  padding: 0 4px !important;
  line-height: 1.3;
}
:deep(.el-table td.el-table__cell),
:deep(.el-table th.el-table__cell) {
  border-right: 2px solid #94a3b8 !important;
  border-bottom: 2px solid #94a3b8 !important;
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
/* 进度列下拉编辑：紧凑、与单元格宽度匹配 */
.cell-edit-select { width: 100%; }
.cell-edit-select :deep(.el-select__wrapper) {
  min-height: 24px;
  padding: 0 6px;
  box-shadow: 0 0 0 2px var(--primary) inset !important;
  background: #f5f9ff;
  font-size: 12.5px;
  font-weight: 700;
}

.pager { padding: 12px 14px; text-align: right; }

.action-icon.danger:hover { background: var(--danger); color: white; }

.preamble {
  background: #e2e8f0;
  border: 2px solid #64748b;
  border-bottom: 3px solid var(--primary);
  border-radius: 8px 8px 0 0;
  padding: 0;
  overflow-x: auto;
  margin-bottom: -1px;
}
.preamble table.preamble-table {
  border-collapse: collapse;
  width: 100%;
  /* 关键：fixed 让列宽完全由 colgroup 决定，编辑时塞 el-input 也不会变宽 */
  table-layout: fixed;
}
.preamble td {
  padding: 3px 5px;
  border: 2px solid #94a3b8;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  background: #ffffff;
  font-size: 12.5px;
  font-weight: 600;
  text-align: center;
  line-height: 1.4;
}
/* 行 1：公司标题大字 + 横跨所有列（用 first-child td 单独样式） */
.preamble-title {
  padding: 5px 10px !important;
  font-size: 13.5px;
  font-weight: 700;
  color: var(--primary);
  background: linear-gradient(90deg, #dbeafe 0%, #eff6ff 50%, #dbeafe 100%) !important;
  text-align: center !important;
  border-left: none !important;
  border-right: none !important;
  border-bottom: 3px solid var(--primary) !important;
  letter-spacing: 0.3px;
}
/* 项目信息表头行（第 2 行） */
.preamble tr.preamble-info-head td {
  background: linear-gradient(180deg, #cbd5e1 0%, #94a3b8 100%) !important;
  color: #0f172a;
  font-weight: 700;
  border-color: #64748b !important;
  padding: 3px 5px;
}
/* 项目信息值行（第 3 行） */
.preamble tr.preamble-info-value td {
  font-weight: 600;
  color: #0f172a;
  background: #f8fafc;
  border-color: #94a3b8 !important;
  padding: 3px 5px;
}
/* 倒计时按紧迫程度着色 */
.preamble td.preamble-warning {
  color: #b45309 !important;
  background: #fffbeb !important;
  font-weight: 600 !important;
}
.preamble td.preamble-urgent {
  color: #b91c1c !important;
  background: #fee2e2 !important;
  font-weight: 700 !important;
}
.preamble td.preamble-overdue {
  color: #ffffff !important;
  background: #dc2626 !important;
  font-weight: 700 !important;
}
/* fx 公式列：表头紫色徽标 */
.preamble-fx-badge {
  display: inline-block;
  background: linear-gradient(135deg, #8b5cf6, #6d28d9);
  color: white;
  font-size: 9px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 4px;
  font-style: italic;
  vertical-align: middle;
  letter-spacing: 0.5px;
  cursor: help;
  box-shadow: 0 1px 2px rgba(109, 40, 217, .3);
}
/* fx 公式列：值用紫色虚线下划线提示"可悬停看公式" */
.preamble-fx-value {
  cursor: help;
  border-bottom: 1px dashed currentColor;
  padding-bottom: 1px;
}
/* 可编辑的项目头单元格：整个 td 可点击 + cursor + 悬停高亮 */
.preamble td.preamble-cell-editable {
  cursor: cell !important;
  transition: background-color .12s, outline-color .12s;
}
.preamble td.preamble-cell-editable:hover {
  background: rgba(37, 99, 235, .14) !important;
  outline: 1.5px dashed var(--primary);
  outline-offset: -2px;
}
.header-cell-empty {
  color: #94a3b8;
  font-weight: 400;
  font-style: italic;
}
.header-edit-input {
  width: 100%;
  /* el-input 默认 width 100% 撑满 td，但因 table-layout: fixed，td 宽度不变 */
}
.header-edit-input :deep(.el-input__wrapper) {
  padding: 0 4px;
  border-radius: 3px;
  box-shadow: 0 0 0 2px var(--primary) inset;
  background: #f5f9ff;
  min-width: 0;
}
.header-edit-input :deep(.el-input__inner) {
  height: 22px;
  font-size: 12.5px;
  font-weight: 600;
  text-align: center;
  /* 输入框内容超出时滚动而非撑开 */
  width: 100%;
  min-width: 0;
}

/* 小屏笔记本（14 寸常见 1366×768）：进一步紧凑 */
@media (max-height: 800px), (max-width: 1440px) {
  .grid-toolbar { padding: 4px 8px; gap: 6px; }
  .preamble td { padding: 2px 4px; font-size: 11.5px; }
  .preamble-title { padding: 3px 8px !important; font-size: 12.5px; }
  .cell { min-height: 20px; padding: 1px 3px; font-size: 12px; }
  .pager { padding: 4px 8px; }
}
</style>
