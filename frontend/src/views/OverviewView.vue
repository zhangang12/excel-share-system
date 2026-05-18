<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, Upload, Setting, Lock, Search, Download } from '@element-plus/icons-vue'
import axios from 'axios'
import { overviewApi } from '@/api/overview'
import { projectsApi } from '@/api/projects'
import { permApi } from '@/api/permissions'
import FieldPermissionDialog from '@/components/FieldPermissionDialog.vue'
import { useRealtime } from '@/composables/useRealtime'
import { useTableHeight } from '@/composables/useTableHeight'
import { useAuthStore } from '@/stores/auth'
import type { OverviewField, OverviewRow, FieldType } from '@/types'

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
const canManagePerm = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))
const visibleFields = computed(() =>
  fields.value.filter(f => myPerms.value[String(f.id)]?.can_view !== false)
)
function fieldEditable(f: OverviewField): boolean {
  return myPerms.value[String(f.id)]?.can_edit !== false
}

const permDialogVisible = ref(false)
const permDialogField = ref<OverviewField | null>(null)
function openPermDialog(f: OverviewField) {
  permDialogField.value = f
  permDialogVisible.value = true
}

const FIELD_TYPE_META: Record<FieldType, { label: string; color: string }> = {
  text: { label: '文本', color: '#6b7280' },
  number: { label: '数字', color: '#10b981' },
  date: { label: '日期', color: '#f59e0b' },
  select: { label: '单选', color: '#8b5cf6' },
  multi_select: { label: '多选', color: '#ec4899' },
  person: { label: '人员', color: '#0ea5e9' },
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

// 过滤 + 分页
const filteredRows = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return rows.value
  return rows.value.filter(r => {
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
      <el-input v-model="keyword" placeholder="搜索任意列..." style="width: 240px"
                size="large" clearable :prefix-icon="Search" @input="currentPage = 1" />
<el-button v-if="isAdmin" :icon="Setting" size="large" @click="openAddField">添加列</el-button>
      <el-button :icon="Download" size="large" @click="onExport">导出</el-button>
      <label v-if="isAdmin" class="el-button el-button--primary el-button--large" style="margin: 0">
        <el-icon style="margin-right:6px"><Upload /></el-icon>
        <span>导入汇总表</span>
        <input type="file" accept=".xlsx,.xlsm,.xls" hidden @change="onImportFile" />
      </label>
    </div>

    <el-card v-loading="loading">
      <el-table ref="tableRef" :data="pagedRows" border stripe :size="fitScreen ? 'small' : 'default'"
                style="width: 100%" :height="tableHeight"
                :empty-text="loading ? '加载中…' : '无数据'"
                :default-sort="{ prop: 'code', order: 'ascending' }">
        <el-table-column type="index" label="#" width="55" align="center"
                         :index="(i: number) => (currentPage - 1) * pageSize + i + 1" />
        <el-table-column prop="code" label="项目编号" :width="fitScreen ? 110 : 140" fixed="left" sortable>
          <template #default="{ row }">
            <a class="proj-link" @click.stop="openProject(row.id)">{{ row.code }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" :min-width="fitScreen ? 130 : 200" show-overflow-tooltip sortable>
          <template #default="{ row }">
            <span class="proj-name" @click.stop="openProject(row.id)">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" :width="fitScreen ? 100 : 116" sortable>
          <template #default="{ row }">
            <el-select v-if="isAdmin" :model-value="row.status" size="small" style="width: 100%"
                       @update:model-value="(v: any) => changeStatus(row, v as string)">
              <el-option v-for="s in STATUS_OPTIONS" :key="s" :value="s" :label="s" />
            </el-select>
            <el-tag v-else :type="(STATUS_COLOR[row.status] || 'info') as any" effect="light" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="manager_name" label="项目经理" :width="fitScreen ? 90 : 110">
          <template #default="{ row }">
            <span v-if="row.manager_name">{{ row.manager_name }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>

        <!-- 自定义列 -->
        <el-table-column v-for="f in visibleFields" :key="f.id" :label="f.name"
                         :min-width="colWidth(f)" show-overflow-tooltip>
          <template #header>
            <span class="field-header">
              <span class="field-type-dot" :style="{ background: FIELD_TYPE_META[f.type].color }"></span>
              <span class="field-name">{{ f.name }}</span>
              <span v-if="canManagePerm" class="field-actions" @click.stop>
                <button class="perm-btn" @click="openPermDialog(f)" title="配置该列的角色权限">
                  <el-icon><Lock /></el-icon>
                </button>
              </span>
            </span>
          </template>
          <template #default="{ row }">
            <!-- 编辑态：统一文本输入 -->
            <template v-if="isEditing(row, f)">
              <el-input v-model="editingValue" autofocus class="cell-edit-input"
                        @blur="saveEdit(row, f)" @keyup.enter="saveEdit(row, f)" @keyup.escape="cancelEdit" />
            </template>
            <!-- 显示态：纯文本（兼容旧 array 数据） -->
            <template v-else>
              <span class="cell" :class="{ editable: fieldEditable(f) }" @click="startEdit(row, f)">
                <template v-if="Array.isArray(getCellValue(row, f))">
                  <span v-if="(getCellValue(row, f) as unknown[]).length">{{ (getCellValue(row, f) as unknown[]).join('、') }}</span>
                  <span v-else class="muted">-</span>
                </template>
                <template v-else>
                  <span v-if="getCellValue(row, f) != null && getCellValue(row, f) !== ''">{{ getCellValue(row, f) }}</span>
                  <span v-else class="muted">-</span>
                </template>
              </span>
            </template>
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

    <FieldPermissionDialog
      v-if="permDialogField"
      v-model="permDialogVisible"
      :field-id="permDialogField.id"
      :field-name="permDialogField.name"
      scope="overview"
    />
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

.pager { padding: 16px 0; text-align: right; }

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

/* 小屏笔记本 / 平板：压缩单元格与分页器 */
@media (max-height: 800px) {
  .cell { min-height: 26px; padding: 4px 6px; }
  .pager { padding: 8px 0; }
}
</style>
