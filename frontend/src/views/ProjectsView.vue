<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Search } from '@element-plus/icons-vue'
import { projectsApi } from '@/api/projects'
import { useAuthStore } from '@/stores/auth'
import { useTableHeight } from '@/composables/useTableHeight'
import type { Project } from '@/types'

const router = useRouter()
const auth = useAuthStore()
const list = ref<Project[]>([])
const loading = ref(false)
const keyword = ref('')
const codeFilter = ref('')
const statusFilter = ref('')
const tableRef = ref()
const { height: tableHeight } = useTableHeight(tableRef)

const isAdmin = computed(() =>
  ['admin', 'manager'].includes(auth.user?.role_code || ''),
)
const canCreate = isAdmin
const canDelete = isAdmin

// 分页
const pageSize = ref(20)
const currentPage = ref(1)
const fitScreen = ref(localStorage.getItem('pms_projects_fit') !== '0')
function onFitScreenChange(v: boolean) {
  localStorage.setItem('pms_projects_fit', v ? '1' : '0')
}

// ===== 状态下拉与着色 =====
const STATUS_OPTIONS_NEW = ['进行中', '已完成']
function cellClassName({ row, column }: any): string {
  const label = column?.label || ''
  if (label === '状态') {
    if (row.status === '已完成') return 'cell-row-done'
    if (row.status === '进行中') return 'cell-row-doing'
  }
  return ''
}
async function changeStatus(row: Project, status: string) {
  if (row.status === status) return
  try {
    await projectsApi.update(row.id, { status })
    const idx = list.value.findIndex(r => r.id === row.id)
    if (idx >= 0) list.value[idx] = { ...list.value[idx], status }
    ElMessage.success('状态已更新')
  } catch { /* */ }
}

// ===== 一览模板列（与 OverviewView 完全一致）=====
type TplCol = {
  label: string
  source: 'code' | 'name' | 'status' | 'meta' | 'derived'
  derived?: 'duration' | 'elapsed' | 'remaining' | 'design_days'
  fallbackDerived?: 'duration' | 'elapsed' | 'remaining' | 'design_days'
  editable: boolean
}
const TPL_FIELDS: TplCol[] = [
  { label: '项目编号',     source: 'code',    editable: false },
  { label: '项目名称',     source: 'name',    editable: true },
  { label: '状态',         source: 'status',  editable: true },
  { label: '签订日期',     source: 'meta',    editable: true },
  { label: '交货日期',     source: 'meta',    editable: true },
  { label: '销售',         source: 'meta',    editable: true },
  { label: '设计师',       source: 'meta',    editable: true },
  { label: '制图开始',     source: 'meta',    editable: true },
  { label: '制图结束',     source: 'meta',    editable: true },
  { label: '制图用时',     source: 'meta',    fallbackDerived: 'design_days', editable: true },
  { label: '电工',         source: 'meta',    editable: true },
  { label: '货期',         source: 'derived', derived: 'duration',  editable: false },
  { label: '已过时间',     source: 'derived', derived: 'elapsed',   editable: false },
  { label: '剩余制作时间', source: 'derived', derived: 'remaining', editable: false },
]

// 一览专属前缀（与项目详情的 __h__ 解耦）
const OVERVIEW_PREFIX = '__o__'
function rowMetaValue(row: Project, key: string): string {
  return String((row as any).extra?.[`${OVERVIEW_PREFIX}${key}`] ?? '')
}

// 跨天自动刷新
const todayKey = ref(new Date().toDateString())
let _todayTimer: number | null = null
onMounted(() => {
  _todayTimer = window.setInterval(() => {
    const k = new Date().toDateString()
    if (k !== todayKey.value) todayKey.value = k
  }, 60_000)
})
onBeforeUnmount(() => { if (_todayTimer !== null) window.clearInterval(_todayTimer) })

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
function computeDerived(row: Project, kind: string): string {
  void todayKey.value
  const signed = parseLooseDate(rowMetaValue(row, '签订日期'))
  const deliver = parseLooseDate(rowMetaValue(row, '交货日期'))
  const designStart = parseLooseDate(rowMetaValue(row, '制图开始'))
  const designEnd = parseLooseDate(rowMetaValue(row, '制图结束'))
  const today = new Date()
  switch (kind) {
    case 'duration':    return signed && deliver ? String(daysBetween(deliver, signed)) : ''
    case 'elapsed':     return signed            ? String(daysBetween(today, signed))   : ''
    case 'remaining':   return deliver           ? String(daysBetween(deliver, today))  : ''
    case 'design_days': return designStart && designEnd ? String(daysBetween(designEnd, designStart)) : ''
  }
  return ''
}
function smartFormatValue(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') return String(v)
  const s = String(v)
  const m = /^(-?\d+)\.0+$/.exec(s)
  return m ? m[1] : s
}
function templateCellValue(row: Project, col: TplCol): string {
  if (col.source === 'code') return row.code || ''
  if (col.source === 'name') return row.name || ''
  if (col.source === 'status') return row.status || ''
  if (col.source === 'meta') {
    const v = rowMetaValue(row, col.label)
    if (v) return smartFormatValue(v)
    if (col.fallbackDerived) return computeDerived(row, col.fallbackDerived)
    return ''
  }
  if (col.source === 'derived' && col.derived) {
    return computeDerived(row, col.derived)
  }
  return ''
}
function templateCellClass(row: Project, col: TplCol): string {
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

// ===== 单元格编辑 =====
const editingTplLabel = ref<string>('')
const editingTplRowId = ref<number>(0)
const editingTplValue = ref<string>('')
function isEditingTpl(row: Project, col: TplCol): boolean {
  return editingTplRowId.value === row.id && editingTplLabel.value === col.label
}
function isTplCellEditable(col: TplCol): boolean {
  return !!col.editable && !!isAdmin.value
}
function startEditTpl(row: Project, col: TplCol) {
  if (!isTplCellEditable(col)) return
  editingTplRowId.value = row.id
  editingTplLabel.value = col.label
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
async function saveEditTpl(row: Project, col: TplCol) {
  const newVal = (editingTplValue.value || '').trim()
  const oldVal = col.source === 'meta'
    ? rowMetaValue(row, col.label)
    : (col.source === 'name' ? (row.name || '') : '')
  cancelEditTpl()
  if (newVal === oldVal) return
  try {
    if (col.source === 'name') {
      if (!newVal) { ElMessage.warning('项目名称不能为空'); return }
      await projectsApi.update(row.id, { name: newVal })
      const idx = list.value.findIndex(r => r.id === row.id)
      if (idx >= 0) list.value[idx] = { ...list.value[idx], name: newVal }
    } else if (col.source === 'meta') {
      await projectsApi.updateHeaderCell(row.id, col.label, newVal || null, true)
      const idx = list.value.findIndex(r => r.id === row.id)
      if (idx >= 0) {
        const extra: any = { ...(list.value[idx] as any).extra }
        const key = `${OVERVIEW_PREFIX}${col.label}`
        if (!newVal) delete extra[key]
        else extra[key] = newVal
        list.value[idx] = { ...list.value[idx], extra } as any
      }
    }
    ElMessage.success('已保存')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

// ===== 数据 / 过滤 =====
async function load() {
  loading.value = true
  try {
    list.value = await projectsApi.list(keyword.value || undefined, statusFilter.value || undefined)
  } finally { loading.value = false }
}

const filteredList = computed(() => {
  let result = list.value
  if (statusFilter.value) {
    result = result.filter(r => r.status === statusFilter.value)
  }
  const cf = codeFilter.value.trim().toLowerCase()
  if (cf) result = result.filter(r => (r.code || '').toLowerCase().includes(cf))
  const k = keyword.value.trim().toLowerCase()
  if (k) {
    result = result.filter(r => {
      const hay = (r.code + ' ' + r.name + ' ' + (r.status || '') + ' ' +
        Object.values((r as any).extra || {}).map(v => String(v ?? '')).join(' ')
      ).toLowerCase()
      return hay.includes(k)
    })
  }
  return result
})

const pagedList = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredList.value.slice(start, start + pageSize.value)
})

function openProject(id: number) {
  router.push({ name: 'project-detail', params: { id } })
}

// ===== 新建项目 =====
const dialogVisible = ref(false)
const form = reactive({
  code: '', name: '', description: '', status: '进行中',
})
function openCreate() {
  Object.assign(form, { code: '', name: '', description: '', status: '进行中' })
  dialogVisible.value = true
}
async function submit() {
  if (!form.code.trim()) { ElMessage.warning('请填写项目编号'); return }
  if (!form.name.trim()) { ElMessage.warning('请填写项目名称'); return }
  try {
    await projectsApi.create({
      code: form.code.trim(), name: form.name.trim(),
      description: form.description, status: form.status,
    })
    dialogVisible.value = false
    ElMessage.success('已新建')
    await load()
  } catch { /* */ }
}

// ===== 删除项目 =====
async function remove(p: Project) {
  const r = await ElMessageBox.prompt(
    `确认删除项目「${p.name}」？请输入项目编号 ${p.code} 以确认：`,
    '删除确认',
    {
      type: 'warning',
      inputPattern: new RegExp(`^${p.code}$`),
      inputErrorMessage: '编号不匹配',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    },
  ).catch(() => null)
  if (!r) return
  await projectsApi.remove(p.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>项目列表</h1>
        <div class="desc">点项目编号进入维护进度表</div>
      </div>
      <div class="spacer"></div>
      <el-tooltip content="适应屏幕：所有列尽量在一屏展示（窄列）">
        <el-switch v-model="fitScreen" active-text="适应屏幕" @change="onFitScreenChange" />
      </el-tooltip>
      <el-select v-model="statusFilter" placeholder="全部状态" size="large"
                 style="width: 130px" clearable @change="currentPage = 1">
        <el-option value="进行中" label="进行中">
          <span class="status-dot status-dot-doing"></span> 进行中
        </el-option>
        <el-option value="已完成" label="已完成">
          <span class="status-dot status-dot-done"></span> 已完成
        </el-option>
      </el-select>
      <el-input v-model="codeFilter" placeholder="项目编号筛选（如 2026）"
                style="width: 200px" size="large" clearable
                @input="currentPage = 1" />
      <el-input v-model="keyword" placeholder="搜索任意列..." style="width: 200px"
                size="large" clearable :prefix-icon="Search" @input="currentPage = 1" />
      <el-button v-if="canCreate" type="primary" :icon="Plus" size="large" @click="openCreate">
        新建项目
      </el-button>
    </div>

    <el-card v-loading="loading">
      <el-table ref="tableRef" :data="pagedList" border stripe :size="fitScreen ? 'small' : 'default'"
                style="width: 100%" :height="tableHeight"
                :empty-text="loading ? '加载中…' : '暂无项目'"
                :cell-class-name="cellClassName">
        <el-table-column type="index" label="#" width="55" align="center" fixed="left"
                         :index="(i: number) => (currentPage - 1) * pageSize + i + 1" />
        <el-table-column v-for="col in TPL_FIELDS" :key="col.label"
                         :label="col.label"
                         :min-width="fitScreen ? 80 : 110"
                         :fixed="col.source === 'code' ? 'left' : undefined"
                         show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="col.source === 'status'">
              <el-select v-if="isAdmin" :model-value="row.status" size="small" style="width: 100%"
                         @update:model-value="(v: any) => changeStatus(row, v as string)">
                <el-option v-for="s in STATUS_OPTIONS_NEW" :key="s" :value="s" :label="s" />
                <el-option v-if="row.status && !STATUS_OPTIONS_NEW.includes(row.status)"
                           :label="row.status + '（旧值）'" :value="row.status" disabled />
              </el-select>
              <el-tag v-else
                      :type="row.status === '已完成' ? 'success' : (row.status === '进行中' ? 'danger' : 'info')"
                      effect="dark" size="small">
                {{ row.status }}
              </el-tag>
            </template>
            <el-input v-else-if="isEditingTpl(row, col)"
                      v-model="editingTplValue" autofocus size="small"
                      class="cell-edit-input"
                      @blur="saveEditTpl(row, col)"
                      @keyup.enter="saveEditTpl(row, col)"
                      @keyup.escape="cancelEditTpl" />
            <a v-else-if="col.source === 'code'" class="proj-link"
               @click.stop="openProject(row.id)">{{ row.code }}</a>
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
        <!-- 删除操作列 -->
        <el-table-column v-if="canDelete" label="操作" width="70" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" :icon="Delete" link @click.stop="remove(row)" />
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="filteredList.length"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- 新建项目对话框 -->
    <el-dialog v-model="dialogVisible" title="新建项目" width="500px">
      <el-form label-position="top">
        <el-form-item label="项目编号 *">
          <el-input v-model="form.code" size="large" placeholder="如 2026-040" />
        </el-form-item>
        <el-form-item label="项目名称 *">
          <el-input v-model="form.name" size="large" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="form.status" size="large" style="width:100%">
            <el-option label="进行中" value="进行中" />
            <el-option label="已完成" value="已完成" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submit">新建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted { color: var(--text-3); }
.pager { padding: 16px 0; text-align: right; }

.proj-link {
  color: var(--primary); font-weight: 600;
  cursor: pointer; text-decoration: none;
}
.proj-link:hover { text-decoration: underline; }

.cell {
  display: inline-block; min-width: 40px; min-height: 22px;
  padding: 2px 4px; line-height: 18px;
  font-size: 12.5px; font-weight: 600; color: #0f172a;
}
.cell.editable { cursor: cell; border-radius: 3px; }
.cell.editable:hover {
  background: rgba(37,99,235,.10);
  outline: 1px dashed var(--primary);
}
.cell.cell-warning { color: #b45309 !important; font-weight: 700; }
.cell.cell-urgent { color: #b91c1c !important; font-weight: 700; }
.cell.cell-overdue {
  color: #ffffff !important; background: #dc2626 !important;
  font-weight: 700; padding: 0 4px; border-radius: 3px;
}

/* 状态筛选 dropdown 里的小圆点 */
.status-dot {
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 6px; vertical-align: middle;
}
.status-dot-doing { background: #ef4444; }
.status-dot-done { background: #10b981; }

/* 状态列整格着色（与 OverviewView 一致） */
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
:deep(.cell-row-done .el-select__wrapper),
:deep(.cell-row-doing .el-select__wrapper) {
  background: rgba(255, 255, 255, 0.55) !important;
  box-shadow: none !important;
  border: 1px solid rgba(0, 0, 0, .15) !important;
}
:deep(.cell-row-done .el-select__wrapper .el-select__selected-item),
:deep(.cell-row-done .el-select__wrapper input) {
  color: #065f46 !important; font-weight: 700 !important;
}
:deep(.cell-row-doing .el-select__wrapper .el-select__selected-item),
:deep(.cell-row-doing .el-select__wrapper input) {
  color: #991b1b !important; font-weight: 700 !important;
}

/* 表格底色 + 加粗边框（与 OverviewView 一致） */
:deep(.el-table) {
  --el-table-border-color: #94a3b8;
  --el-table-header-bg-color: #cbd5e1;
  border-radius: 10px;
  border: 2px solid #64748b;
}
:deep(.el-table .el-table__inner-wrapper) {
  border-radius: 10px; overflow: hidden;
}
:deep(.el-table th.el-table__cell) {
  background: linear-gradient(180deg, #cbd5e1 0%, #94a3b8 100%) !important;
  color: #0f172a; font-weight: 700;
  font-size: 12.5px; padding: 3px 0 !important;
}
:deep(.el-table td.el-table__cell),
:deep(.el-table th.el-table__cell) {
  border-right: 2px solid #94a3b8 !important;
  border-bottom: 2px solid #94a3b8 !important;
}
:deep(.el-table td.el-table__cell) { padding: 2px 0 !important; }

/* 编辑框 */
.cell-edit-input :deep(.el-input__wrapper) {
  padding: 0 6px;
  box-shadow: 0 0 0 2px var(--primary) inset;
  background: #f5f9ff;
}
.cell-edit-input :deep(.el-input__inner) {
  height: 22px; font-size: 12.5px; font-weight: 600; text-align: center;
}
</style>
