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
const inputRef = ref<HTMLInputElement[]>([])

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

function startEdit(row: SheetRow, field: Field) {
  if (!props.canEdit) return
  editingCell.value = { rowId: row.id, fieldId: field.id }
  editingValue.value = getCellVal(row, field.id)
  nextTick(() => inputRef.value[0]?.focus())
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
        点击单元格即可编辑，Enter/Tab 保存，Esc 取消；修改实时同步至项目详单
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
              :class="{ 'smg-editable': canEdit, 'smg-editing': isEditing(row, f) }"
              @click="startEdit(row, f)"
            >
              <input
                v-if="isEditing(row, f)"
                ref="inputRef"
                v-model="editingValue"
                class="smg-input"
                @blur="saveEdit(row, f)"
                @keydown="onKeydown($event, row, f)"
              />
              <span v-else class="smg-cell-text">{{ getCellVal(row, f.id) || ' ' }}</span>
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

.smg-table-wrap {
  overflow-x: hidden;
  overflow-y: auto;
  max-height: calc(100vh - 280px);
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}
.smg-table {
  width: 100%; border-collapse: collapse;
  font-size: 13px; table-layout: fixed;
}
.smg-th {
  position: sticky; top: 0; z-index: 1;
  background: var(--el-color-primary-dark-2, #1e3a5f);
  color: #fff; font-weight: 500;
  padding: 8px 10px; text-align: left; white-space: nowrap;
  border-right: 1px solid rgba(255,255,255,.15);
}
.smg-th:last-child { border-right: none; }
.smg-idx { width: 44px; text-align: center; }
.smg-op { width: 52px; text-align: center; }

.smg-td {
  padding: 0; border-bottom: 1px solid var(--el-border-color-light);
  border-right: 1px solid var(--el-border-color-lighter);
  vertical-align: middle;
}
.smg-td:last-child { border-right: none; }
.smg-row:last-child .smg-td { border-bottom: none; }
.smg-row:nth-child(even) { background: var(--el-fill-color-lighter, #f9fafb); }
.smg-row:hover { background: var(--el-fill-color-light, #f0f4ff); }

.smg-editable { cursor: pointer; }
.smg-editable:hover { background: #eff6ff !important; }
.smg-editing { background: #fff !important; padding: 0 !important; }

.smg-cell-text {
  display: block; padding: 7px 10px;
  white-space: pre-wrap; word-break: break-all;
}
.smg-input {
  width: 100%; height: 100%;
  border: 2px solid var(--el-color-primary);
  padding: 5px 8px; font-size: 13px;
  outline: none; background: #fff;
  box-sizing: border-box;
}

.smg-empty {
  text-align: center; color: var(--el-text-color-secondary);
  padding: 32px; font-size: 13px;
}
.smg-footer {
  font-size: 12px; color: var(--el-text-color-secondary);
  text-align: right; padding-right: 4px;
}
</style>
