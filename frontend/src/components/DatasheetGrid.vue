<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Setting, Edit, Lock, Search } from '@element-plus/icons-vue'
import { datasheetsApi } from '@/api/datasheets'
import { permApi } from '@/api/permissions'
import { useAuthStore } from '@/stores/auth'
import FieldPermissionDialog from '@/components/FieldPermissionDialog.vue'
import { useRealtime } from '@/composables/useRealtime'
import { useTableHeight } from '@/composables/useTableHeight'
// 单元格手动公式（=A2+B2）功能已禁用；保留 utils/formula.ts 文件以便后续重启。
// 系统自动公式（preamble 的"货期/已过时间/倒计时"）走 preambleCell/preambleFormula，
// 与单元格公式无关，继续工作。
import type { DataField, DataRecord, FieldType } from '@/types'

const props = defineProps<{
  datasheetId: number
  canEdit: boolean
  headerLines?: string[][] | null
}>()

const keyword = ref("")
const fields = ref<DataField[]>([])
const records = ref<DataRecord[]>([])
const loading = ref(false)
const auth = useAuthStore()
const myPerms = ref<Record<string, { can_view: boolean; can_edit: boolean }>>({})
const isAdmin = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))
const canManagePerm = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))

// 分页 + 适应屏幕
const pageSize = ref(20)
const currentPage = ref(1)
const fitScreen = ref(localStorage.getItem('pms_datasheet_fit') !== '0')
function onFitScreenChange(v: boolean) {
  localStorage.setItem('pms_datasheet_fit', v ? '1' : '0')
}

// 权限管理对话框
const permDialogVisible = ref(false)
const permDialogField = ref<DataField | null>(null)
function openPermDialog(f: DataField) {
  permDialogField.value = f
  permDialogVisible.value = true
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

const FIELD_TYPE_META: Record<FieldType, { label: string; color: string }> = {
  text: { label: '文本', color: '#6b7280' },
  number: { label: '数字', color: '#10b981' },
  date: { label: '日期', color: '#f59e0b' },
  select: { label: '单选', color: '#8b5cf6' },
  multi_select: { label: '多选', color: '#ec4899' },
  person: { label: '人员', color: '#0ea5e9' },
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

// ===== Preamble（项目信息行）自动计算 =====
// 识别"货期 / 已过时间 / 倒计时"等列，按"下单日期" + "交货日期" + TODAY 实时算
const DATE_DERIVED_COLS = new Set(['货期', '已过时间', '已经过时间', '倒计时', '剩余天数', '剩余'])
const DATE_KEYS = {
  order: ['下单日期', '下单时间', '下单'],
  deliver: ['交货日期', '交付日期', '交期', '交货时间'],
}

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

// 在 header 行（idx=1）中按候选名找列索引
function findHeaderIdx(headerRow: string[], candidates: string[]): number {
  for (let i = 0; i < headerRow.length; i++) {
    const k = String(headerRow[i] || '').trim()
    if (candidates.includes(k)) return i
  }
  return -1
}

function preambleCell(rowIdx: number, colIdx: number): string {
  const lines = props.headerLines || []
  const raw = lines[rowIdx]?.[colIdx]
  const rawStr = raw == null ? '' : String(raw)
  // 只对"值行"（idx=2）做自动计算；其他行原样返回
  if (rowIdx !== 2) return rawStr

  const headerRow = (lines[1] || []).map(c => String(c ?? ''))
  const valueRow = (lines[2] || []).map(c => String(c ?? ''))
  const header = (headerRow[colIdx] || '').trim()
  if (!DATE_DERIVED_COLS.has(header)) return rawStr

  const orderIdx = findHeaderIdx(headerRow, DATE_KEYS.order)
  const deliverIdx = findHeaderIdx(headerRow, DATE_KEYS.deliver)
  const orderDate = orderIdx >= 0 ? parseLooseDate(valueRow[orderIdx]) : null
  const deliverDate = deliverIdx >= 0 ? parseLooseDate(valueRow[deliverIdx]) : null
  // 关键：读 todayKey 建立 Vue 响应式依赖
  // 即使浏览器开着不动，每 60 秒定时器跨天后会更新 todayKey，触发本函数重算
  void todayKey.value
  const today = new Date()

  if (header === '货期') {
    if (orderDate && deliverDate) return String(daysBetween(deliverDate, orderDate))
    return rawStr
  }
  if (header === '已过时间' || header === '已经过时间') {
    if (orderDate) return String(daysBetween(today, orderDate))
    return rawStr
  }
  if (header === '倒计时' || header === '剩余天数' || header === '剩余') {
    if (deliverDate) return String(daysBetween(deliverDate, today))
    return rawStr
  }
  return rawStr
}

function preambleCellClass(rowIdx: number, colIdx: number): string {
  if (rowIdx !== 2) return ''
  const headerRow = ((props.headerLines || [])[1] || []).map(c => String(c ?? ''))
  const header = (headerRow[colIdx] || '').trim()
  if (header === '倒计时' || header === '剩余天数' || header === '剩余') {
    const v = parseInt(preambleCell(rowIdx, colIdx))
    if (!isNaN(v)) {
      if (v < 0) return 'preamble-overdue'   // 已逾期
      if (v <= 3) return 'preamble-urgent'   // 紧迫
      if (v <= 7) return 'preamble-warning'  // 警告
    }
  }
  return ''
}

// 返回某列的"公式定义"文本；非公式列返回 ''
function preambleFormula(colIdx: number): string {
  const headerRow = ((props.headerLines || [])[1] || []).map(c => String(c ?? ''))
  const header = (headerRow[colIdx] || '').trim()
  if (header === '货期') return '= 交货日期 - 下单日期'
  if (header === '已过时间' || header === '已经过时间') return '= TODAY() - 下单日期'
  if (header === '倒计时' || header === '剩余天数' || header === '剩余') return '= 交货日期 - TODAY()'
  return ''
}

function getCellValue(record: DataRecord, f: DataField) {
  return record.values?.[String(f.id)]
}

/** 单元格显示值（纯文本 / 数组），不再做公式求值 */
function displayCellValue(_record: DataRecord, f: DataField): {
  text: string; isError: boolean; isEmpty: boolean
} {
  const raw = getCellValue(_record, f)
  if (Array.isArray(raw)) {
    return raw.length
      ? { text: raw.join('、'), isError: false, isEmpty: false }
      : { text: '-', isError: false, isEmpty: true }
  }
  if (raw == null || raw === '') return { text: '-', isError: false, isEmpty: true }
  return { text: String(raw), isError: false, isEmpty: false }
}

// 列宽自适应
function colWidth(f: DataField): number {
  const headerLen = (f.name || '').length
  let maxLen = headerLen
  for (const r of pagedRecords.value) {
    const d = displayCellValue(r, f)
    const s = d.text || ''
    if (s.length > maxLen) maxLen = s.length
  }
  let w = Math.max(maxLen * 13, headerLen * 13) + 40
  if (fitScreen.value) {
    w = Math.min(w, 130)
  } else {
    w = Math.min(w, 260)
  }
  return Math.max(80, w)
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

async function deleteRow(rowId: number) {
  await ElMessageBox.confirm('删除这一行？', '确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
  }).catch(() => 'cancel').then(async (r) => {
    if (r === 'cancel') return
    await datasheetsApi.deleteRecord(rowId)
    await load()
  })
}
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

    <!-- 来自 Excel 的前几行标题区（只读；货期/已过时间/倒计时 用公式实时算） -->
    <div v-if="props.headerLines && props.headerLines.length" class="preamble">
      <table>
        <tr v-for="(line, idx) in props.headerLines" :key="idx"
            :class="{
              'preamble-info-head': idx === 1,
              'preamble-info-value': idx === 2,
            }">
          <!-- 第一行（公司标题）：跨所有列居中 -->
          <template v-if="idx === 0">
            <td :colspan="line.length" class="preamble-title">
              {{ line.filter(c => c).join(' ') }}
            </td>
          </template>
          <!-- 表头行（idx=1）：公式列在表头后挂 fx 紫色徽标 -->
          <template v-else-if="idx === 1">
            <td v-for="(_cell, ci) in line" :key="ci">
              <span>{{ preambleCell(idx, ci) }}</span>
              <el-tooltip v-if="preambleFormula(ci)" :content="preambleFormula(ci)" placement="top">
                <span class="preamble-fx-badge">fx</span>
              </el-tooltip>
            </td>
          </template>
          <!-- 值行（idx=2）：公式列结果用紫色 + 虚线下划线 + tooltip 显示公式 -->
          <template v-else>
            <td v-for="(_cell, ci) in line" :key="ci" :class="preambleCellClass(idx, ci)">
              <el-tooltip v-if="preambleFormula(ci)" placement="top">
                <template #content>
                  <div style="line-height:1.7">
                    <div style="font-weight:600">{{ preambleFormula(ci) }}</div>
                    <div style="font-size:11px;opacity:.7">基于"下单日期"、"交货日期"、今天实时计算</div>
                  </div>
                </template>
                <span class="preamble-fx-value">{{ preambleCell(idx, ci) }}</span>
              </el-tooltip>
              <template v-else>{{ preambleCell(idx, ci) }}</template>
            </td>
          </template>
        </tr>
      </table>
    </div>

    <el-table ref="tableRef" :data="pagedRecords" border stripe :size="fitScreen ? 'small' : 'default'"
              style="width: 100%" :height="tableHeight"
              v-loading="loading"
              :empty-text="loading ? '加载中…' : (fields.length === 0 ? '请先添加字段（列）' : '暂无数据，点添加行开始录入')">
      <el-table-column type="index" label="#" width="50" align="center"
                       :index="(i: number) => (currentPage - 1) * pageSize + i + 1" />

      <el-table-column v-for="f in visibleFields" :key="f.id" :label="f.name"
                       :min-width="colWidth(f)" show-overflow-tooltip>
        <template #header>
          <span class="field-header">
            <span class="field-type-dot" :style="{ background: FIELD_TYPE_META[f.type].color }"></span>
            <el-tooltip :content="f.name" placement="top" :show-after="300" :hide-after="0">
              <span class="field-name">{{ f.name }}</span>
            </el-tooltip>
            <span v-if="canManagePerm" class="field-actions" @click.stop>
              <button class="perm-btn" @click="openPermDialog(f)" title="配置该列的角色权限">
                <el-icon><Lock /></el-icon>
              </button>
            </span>
          </span>
        </template>
        <template #default="{ row }">
          <template v-if="isEditing(row, f)">
            <el-input v-model="editingValue" autofocus class="cell-edit-input"
                      @blur="saveEdit(row, f)" @keyup.enter="saveEdit(row, f)" @keyup.escape="cancelEdit" />
          </template>
          <template v-else>
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

      <el-table-column v-if="canEdit" label="操作" :width="fitScreen ? 70 : 80" align="center" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="danger" :icon="Delete" link @click="deleteRow(row.id)" />
        </template>
      </el-table-column>
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

    <FieldPermissionDialog
      v-if="permDialogField"
      v-model="permDialogVisible"
      :field-id="permDialogField.id"
      :field-name="permDialogField.name"
      scope="datasheet"
    />
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

.field-header { display: inline-flex; align-items: center; gap: 5px; width: 100%; }
.field-type-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.field-name {
  flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.field-actions {
  display: inline-flex;
  flex-shrink: 0;
  margin-left: 2px;
}
.perm-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px 4px;
  color: #b45309;
  background: #fef3c7;
  border: 1px solid #fcd34d;
  border-radius: 5px;
  cursor: pointer;
  transition: all .15s;
}
.perm-btn:hover {
  background: #f59e0b;
  color: white;
  border-color: #f59e0b;
  transform: translateY(-1px);
  box-shadow: 0 2px 6px rgba(245, 158, 11, .3);
}
.perm-btn .el-icon { font-size: 14px; }
.action-icon:hover { color: var(--primary); }
.action-icon.danger:hover { color: var(--danger); }

.cell {
  display: inline-block; min-width: 60px; min-height: 32px;
  padding: 6px 8px;
  line-height: 20px;
}
.cell.editable {
  cursor: cell; border-radius: 3px;
}
.cell.editable:hover {
  background: rgba(37,99,235,.08);
  outline: 1px dashed var(--primary);
}
/* 单元格手动公式相关样式（.cell.formula / .formula-help）已随功能下线移除 */

/* ===== 表格底色 + 加粗边框 + 圆角 ===== */
:deep(.el-table) {
  --el-table-border-color: #d0d5dd;
  --el-table-border: 2px solid #d0d5dd;
  --el-table-header-bg-color: #f4f6fb;
  border-radius: 10px;
}
:deep(.el-table .el-table__inner-wrapper) {
  border-radius: 10px;
  overflow: hidden;
}
:deep(.el-table th.el-table__cell) {
  background: #f4f6fb !important;
  color: #1f2d3d;
  font-weight: 600;
}
:deep(.el-table td.el-table__cell),
:deep(.el-table th.el-table__cell) {
  border-right: 2px solid #d0d5dd !important;
  border-bottom: 2px solid #d0d5dd !important;
}
:deep(.el-table--border),
:deep(.el-table--border .el-table__inner-wrapper) {
  border-color: #d0d5dd !important;
}
:deep(.el-table tbody tr td.el-table__cell) {
  background: #ffffff;
}
:deep(.el-table .el-table__row--striped td.el-table__cell) {
  background: #f8fafc !important;
}
:deep(.el-table tbody tr:hover td.el-table__cell) {
  background: #eef4ff !important;
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

.pager { padding: 12px 14px; text-align: right; }

.action-icon.perm { color: var(--primary); }
.action-icon.perm:hover { background: var(--primary); color: white; }
.action-icon.danger:hover { background: var(--danger); color: white; }

.preamble {
  background: #f8fafc;
  border-bottom: 2px solid var(--primary);
  padding: 0;
  overflow-x: auto;
}
.preamble table { border-collapse: collapse; width: 100%; }
.preamble td {
  padding: 9px 12px;
  border: 2px solid #d0d5dd;
  color: var(--text-1);
  white-space: nowrap;
  background: #ffffff;
  font-size: 12.5px;
  text-align: center;
}
/* 行 1：公司标题大字 + 横跨所有列（用 first-child td 单独样式） */
.preamble-title {
  padding: 10px 14px !important;
  font-size: 15px;
  font-weight: 600;
  color: var(--primary);
  background: linear-gradient(90deg, #eff6ff 0%, white 100%) !important;
  text-align: center !important;
  border-left: none !important;
  border-right: none !important;
}
/* 项目信息表头行（第 2 行） */
.preamble tr.preamble-info-head td {
  background: #f1f5f9 !important;
  color: var(--text-2);
  font-weight: 500;
}
/* 项目信息值行（第 3 行） */
.preamble tr.preamble-info-value td {
  font-weight: 400;
  color: var(--text-1);
  background: white;
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

/* 小屏笔记本 / 平板：压缩工具栏、标签条、Excel 标题区，给表格腾高度 */
@media (max-height: 800px) {
  .grid-toolbar { padding: 6px 10px; gap: 8px; }
  .datasheet-tabs { padding: 4px 8px; }
  .ds-tab { padding: 5px 10px; }
  .preamble td { padding: 4px 8px; font-size: 11.5px; }
  .preamble-title { padding: 5px 12px !important; font-size: 13px; }
  .cell { min-height: 26px; padding: 4px 6px; }
  .pager { padding: 8px 12px; }
}
</style>
