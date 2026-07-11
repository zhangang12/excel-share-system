<script setup lang="ts">
/**
 * SheetmetalGrid — 钣金装配表行内可编辑网格
 * 供钣金组（SheetMetalView）和装配组（DeptWorkbenchView）共用。
 *
 * 读取：使用标准 datasheets 读端点（sheetmetal/assembler 已在 project_members 可读）
 * 写入：使用 produce-edit 专用端点（绕开详单闸门）
 */
import { ref, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete } from '@element-plus/icons-vue'
import { http } from '@/api'
import { datasheetsApi } from '@/api/datasheets'

const props = defineProps<{
  datasheetId: number
  projectCode: string
  canEdit: boolean       // 管理员/主管/被派单的钣金或装配角色传 true
}>()

interface Field { id: number; name: string }
interface SheetRow { id: number; values: Record<string, unknown>; sort_order: number }

const fields = ref<Field[]>([])
const records = ref<SheetRow[]>([])
const loading = ref(false)

// 编辑中的单元格
const editingCell = ref<{ rowId: number; fieldId: number } | null>(null)
const editingValue = ref('')
const inputRef = ref<any[]>([])

// 🆕 单元格类型：日期列(名称以"日期"结尾)→日期选择器；进度列→下拉；其余→带"下拉复制已有值"的输入
function isDateField(f: Field) { return (f.name || '').trim().endsWith('日期') }
function isProgressField(f: Field) { return PROGRESS_FIELD_NAMES.has((f.name || '').trim()) }
// 该列已填过的去重取值（下拉复制用,最多 20 个）
function columnValues(f: Field): { value: string }[] {
  const seen = new Set<string>()
  for (const r of records.value) {
    const v = getCellVal(r, f.id).trim()
    if (v) seen.add(v)
    if (seen.size >= 20) break
  }
  return Array.from(seen).map(v => ({ value: v }))
}
function fetchColumnSuggestions(f: Field) {
  return (query: string, cb: (list: { value: string }[]) => void) => {
    const all = columnValues(f)
    cb(query ? all.filter(x => x.value.toLowerCase().includes(query.toLowerCase())) : all)
  }
}

// 🆕 向下填充：把该单元格的值复制到下方所有「空」单元格（类 Excel 下拉复制）
const filling = ref(false)
async function fillDown(row: SheetRow, f: Field) {
  const val = getCellVal(row, f.id).trim()
  if (!val || filling.value) return
  const idx = records.value.findIndex(r => r.id === row.id)
  const targets = records.value.slice(idx + 1).filter(r => !getCellVal(r, f.id).trim())
  if (!targets.length) { ElMessage.info('下方没有空单元格可填充'); return }
  try {
    await ElMessageBox.confirm(`把「${val}」向下复制到本列下方 ${targets.length} 个空单元格？`, '向下填充', { type: 'info' })
  } catch { return }
  filling.value = true
  try {
    for (const t of targets) {
      await datasheetsApi.produceUpdateCell(props.datasheetId, t.id, f.id, val)
      const i = records.value.findIndex(r => r.id === t.id)
      if (i >= 0) records.value[i] = { ...records.value[i], values: { ...records.value[i].values, [String(f.id)]: val } }
    }
    ElMessage.success(`已填充 ${targets.length} 格`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '填充失败，部分已保存')
    load()
  } finally { filling.value = false }
}

async function load() {
  loading.value = true
  try {
    const [fs, rs] = await Promise.all([
      datasheetsApi.listFields(props.datasheetId),
      datasheetsApi.listRecords(props.datasheetId),
    ])
    fields.value = fs.map((f: any) => ({ id: f.id, name: f.name }))
    records.value = rs
  } finally {
    loading.value = false
  }
}

watch(() => props.datasheetId, () => load(), { immediate: true })

function getCellVal(row: SheetRow, fid: number): string {
  const v = row.values?.[String(fid)]
  return v == null ? '' : String(v)
}

// 进度列整格着色（完成绿 / 进行中红），口径与项目详单 DatasheetGrid 一致
const PROGRESS_FIELD_NAMES = new Set(['进度', '进度100%', '完成度', '状态', '完工度'])
const STATUS_DONE_WORDS = new Set(['完成', '已完成', '完工', '已结束'])
const STATUS_DOING_WORDS = new Set([
  '进行中', '正在做', '处理中', '在做', '未开始', '待开始', '待处理', '未开工',
  '延期', '逾期', '超期', '暂停', '搁置', '挂起', '取消', '作废', '待审核', '审核中',
])
function cellStateClass(field: Field, row: SheetRow): string {
  if (!PROGRESS_FIELD_NAMES.has((field.name || '').trim())) return ''
  const v = getCellVal(row, field.id).trim()
  if (!v) return ''
  if (STATUS_DONE_WORDS.has(v)) return 'smg-done'
  if (STATUS_DOING_WORDS.has(v)) return 'smg-doing'
  return ''
}

function startEdit(row: SheetRow, field: Field) {
  if (!props.canEdit) return
  editingCell.value = { rowId: row.id, fieldId: field.id }
  editingValue.value = getCellVal(row, field.id)
  nextTick(() => inputRef.value[0]?.focus?.())
}

function isEditing(row: SheetRow, field: Field) {
  return editingCell.value?.rowId === row.id && editingCell.value?.fieldId === field.id
}

async function saveEdit(row: SheetRow, field: Field) {
  const newVal = editingValue.value.trim()
  const oldVal = getCellVal(row, field.id)
  editingCell.value = null
  if (newVal === oldVal) return
  try {
    await datasheetsApi.produceUpdateCell(props.datasheetId, row.id, field.id, newVal)
    // 乐观更新本地数据
    const idx = records.value.findIndex(r => r.id === row.id)
    if (idx >= 0) {
      const newValues = { ...records.value[idx].values }
      if (newVal === '') delete newValues[String(field.id)]
      else newValues[String(field.id)] = newVal
      records.value[idx] = { ...records.value[idx], values: newValues }
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
    load()
  }
}

function cancelEdit() {
  editingCell.value = null
}

function onKeydown(e: KeyboardEvent, row: SheetRow, field: Field) {
  if (e.key === 'Enter') { e.preventDefault(); saveEdit(row, field) }
  if (e.key === 'Escape') cancelEdit()
  if (e.key === 'Tab') { e.preventDefault(); saveEdit(row, field) }
}

async function addRow() {
  try {
    const newRec = await datasheetsApi.produceCreateRecord(props.datasheetId) as SheetRow
    records.value.push(newRec)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '新增失败')
  }
}

async function deleteRow(row: SheetRow) {
  try {
    await ElMessageBox.confirm('确认删除此行？', '删除', { type: 'warning', confirmButtonText: '删除' })
  } catch { return }
  try {
    await datasheetsApi.produceDeleteRecord(props.datasheetId, row.id)
    records.value = records.value.filter(r => r.id !== row.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

</script>

<template>
  <div class="smg-wrap" v-loading="loading">
    <!-- 工具栏 -->
    <div class="smg-toolbar">
      <span class="smg-tip" v-if="canEdit">
        点击单元格编辑：日期列弹日期选择、进度列下拉、其他列可下拉复制已有值；悬停单元格点 ⬇ 向下填充空格；Enter/Tab 保存，Esc 取消；实时同步项目详单
      </span>
      <span class="smg-tip smg-ro" v-else>只读引用</span>
      <div class="smg-actions">
        <el-button v-if="canEdit" size="small" :icon="Plus" @click="addRow">新增行</el-button>
      </div>
    </div>

    <!-- 可编辑表格 -->
    <div class="smg-table-wrap">
      <table class="smg-table">
        <thead>
          <tr>
            <th class="smg-th smg-idx">#</th>
            <th v-for="f in fields" :key="f.id" class="smg-th">{{ f.name }}</th>
            <th v-if="canEdit" class="smg-th smg-op">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!records.length">
            <td :colspan="fields.length + (canEdit ? 2 : 1)" class="smg-empty">
              暂无数据{{ canEdit ? '，点击「新增行」开始录入' : '' }}
            </td>
          </tr>
          <tr v-for="(row, idx) in records" :key="row.id" class="smg-row">
            <td class="smg-td smg-idx">{{ idx + 1 }}</td>
            <td
              v-for="f in fields" :key="f.id"
              class="smg-td"
              :class="[cellStateClass(f, row), { 'smg-editable': canEdit, 'smg-editing': isEditing(row, f) }]"
              @click="startEdit(row, f)"
            >
              <template v-if="isEditing(row, f)">
                <!-- 🆕 日期列:日期选择器 -->
                <el-date-picker
                  v-if="isDateField(f)"
                  ref="inputRef"
                  v-model="editingValue"
                  type="date" value-format="YYYY-MM-DD" size="small"
                  class="smg-editor" :clearable="true" placeholder="选择日期"
                  @change="saveEdit(row, f)"
                  @visible-change="(v: boolean) => { if (!v) saveEdit(row, f) }"
                />
                <!-- 🆕 进度列:下拉 -->
                <el-select
                  v-else-if="isProgressField(f)"
                  ref="inputRef"
                  v-model="editingValue"
                  size="small" class="smg-editor" automatic-dropdown
                  @change="saveEdit(row, f)"
                  @visible-change="(v: boolean) => { if (!v) saveEdit(row, f) }"
                >
                  <el-option value="完成" label="完成" />
                  <el-option value="进行中" label="进行中" />
                </el-select>
                <!-- 🆕 其余列:输入 + 下拉复制本列已有值 -->
                <el-autocomplete
                  v-else
                  ref="inputRef"
                  v-model="editingValue"
                  :fetch-suggestions="fetchColumnSuggestions(f)"
                  :trigger-on-focus="true"
                  class="smg-editor"
                  size="small"
                  placeholder="输入或下拉选已有值"
                  @select="saveEdit(row, f)"
                  @blur="saveEdit(row, f)"
                  @keydown="onKeydown($event, row, f)"
                />
              </template>
              <template v-else>
                <span class="smg-cell-text" :title="getCellVal(row, f.id)">{{ getCellVal(row, f.id) || ' ' }}</span>
                <!-- 🆕 填充柄:悬停出现,把该值向下复制到下方空单元格(类 Excel 下拉复制) -->
                <button
                  v-if="canEdit && getCellVal(row, f.id)"
                  class="smg-fill" title="向下填充到空单元格"
                  @click.stop="fillDown(row, f)"
                >⬇</button>
              </template>
            </td>
            <td v-if="canEdit" class="smg-td smg-op">
              <el-button size="small" link type="danger" :icon="Delete" @click.stop="deleteRow(row)" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="smg-footer">共 {{ records.length }} 行</div>
  </div>
</template>

<style scoped>
.smg-wrap { display: flex; flex-direction: column; gap: 8px; }

.smg-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0;
}
.smg-tip { font-size: 12px; color: var(--el-text-color-secondary); }
.smg-ro { color: var(--el-color-info); }
.smg-actions { display: flex; gap: 8px; }

/* ===== 表格：石板灰主题，与项目详单 DatasheetGrid 视觉一致 ===== */
.smg-table-wrap {
  /* 列放不下时横向滚动；外框 2px 深灰 + 圆角，整体更有分量 */
  overflow-x: auto;
  overflow-y: auto;
  max-height: calc(100vh - 280px);
  border: 2px solid #64748b;
  border-radius: 10px;
}
.smg-table {
  /* 列按内容自适应宽度，整表可超出容器后横向滚动 */
  width: max-content; min-width: 100%;
  border-collapse: separate; border-spacing: 0;
  font-size: 12.5px; table-layout: auto;
  color: #1e293b;
}
.smg-th, .smg-td {
  min-width: 58px; text-align: center;
  border-right: 2px solid #94a3b8;
  border-bottom: 2px solid #94a3b8;
}
.smg-th:last-child, .smg-td:last-child { border-right: none; }
.smg-row:last-child .smg-td { border-bottom: none; }

/* 表头：石板渐变 + 深色粗体 + 吸顶 */
.smg-th {
  position: sticky; top: 0; z-index: 2;
  background: linear-gradient(180deg, #cbd5e1 0%, #94a3b8 100%);
  color: #0f172a; font-weight: 700;
  padding: 7px 10px; white-space: nowrap;
  border-bottom: 2px solid #64748b;
}
.smg-idx, .smg-op { color: #475569; font-weight: 600; }
.smg-idx { width: 44px; min-width: 44px; }
.smg-op { width: 56px; min-width: 56px; }

/* 数据单元格：白底 / 斑马灰 / 悬停浅蓝 */
.smg-td { padding: 0; vertical-align: middle; background: #ffffff; }
.smg-row:nth-child(even) .smg-td { background: #e2e8f0; }
.smg-row:hover .smg-td { background: #dbeafe; }

.smg-editable { cursor: cell; }
.smg-editable:hover {
  background: rgba(37, 99, 235, .10) !important;
  outline: 1px dashed var(--el-color-primary, #2563eb);
  outline-offset: -2px;
}
.smg-editing { background: #f5f9ff !important; padding: 0 !important; }

.smg-cell-text {
  display: block; padding: 6px 10px;
  /* 单行显示，过长省略号；点击单元格查看/编辑完整内容，悬停 title 显示全文 */
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 260px;
  font-weight: 600; color: #0f172a;
}
.smg-input {
  width: 100%; height: 100%;
  border: 2px solid var(--el-color-primary, #2563eb);
  padding: 5px 8px; font-size: 12.5px;
  outline: none; background: #f5f9ff;
  box-sizing: border-box; text-align: center;
}
/* 🆕 单元格内编辑器(日期/下拉/自动补全)统一撑满 */
.smg-editor { width: 100%; min-width: 132px; }
.smg-editor :deep(.el-input__wrapper) { border-radius: 0; box-shadow: 0 0 0 2px var(--el-color-primary) inset; }
/* 🆕 填充柄:悬停单元格才出现 */
.smg-td { position: relative; }
.smg-fill {
  display: none; position: absolute; right: 2px; bottom: 2px; z-index: 1;
  width: 18px; height: 18px; line-height: 16px; padding: 0;
  border: 1px solid var(--el-color-primary, #2563eb); border-radius: 4px;
  background: #fff; color: var(--el-color-primary, #2563eb);
  font-size: 11px; cursor: pointer;
}
.smg-td:hover .smg-fill { display: block; }
.smg-fill:hover { background: var(--el-color-primary, #2563eb); color: #fff; }

/* 进度列整格着色：完成绿 / 进行中红（与项目详单一致） */
.smg-td.smg-done { background: #d1fae5 !important; }
.smg-td.smg-done .smg-cell-text { color: #065f46; font-weight: 800; }
.smg-td.smg-doing { background: #fee2e2 !important; }
.smg-td.smg-doing .smg-cell-text { color: #991b1b; font-weight: 800; }
.smg-row:hover .smg-td.smg-done { background: #b7f0d2 !important; }
.smg-row:hover .smg-td.smg-doing { background: #fbcfcf !important; }

.smg-empty {
  text-align: center; color: var(--el-text-color-secondary);
  padding: 32px; font-size: 13px; background: #fff;
}
.smg-footer {
  font-size: 12px; color: var(--el-text-color-secondary);
  text-align: right; padding-right: 4px;
}
</style>
