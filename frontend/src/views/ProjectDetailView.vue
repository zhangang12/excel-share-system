<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Plus, Delete, User as UserIcon, Edit, Document, Upload, Download, CopyDocument } from '@element-plus/icons-vue'
import axios from 'axios'
import { projectsApi } from '@/api/projects'
import { datasheetsApi } from '@/api/datasheets'
import { adminApi } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import DatasheetGrid from '@/components/DatasheetGrid.vue'
import ClonePermissionsDialog from '@/components/ClonePermissionsDialog.vue'
import WorkflowGraph from '@/components/WorkflowGraph.vue'
import { collabApi, ASSEMBLY_SHEETS, type Workflow } from '@/api/collab'
import type { Project, ProjectMember, User, Datasheet } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const pid = computed(() => Number(route.params.id))
const project = ref<Project | null>(null)
const members = ref<ProjectMember[]>([])
const datasheets = ref<Datasheet[]>([])
const activeSheetId = ref<number | null>(null)
const activeSheetHeaderLines = computed<string[][] | null>(() => {
  const ds = datasheets.value.find(d => d.id === activeSheetId.value)
  return ds?.header_lines || null
})
const activeTab = ref('data')

const canEdit = computed(() => {
  if (!auth.user || !project.value) return false
  if (auth.user.role_code === 'admin' || auth.user.role_code === 'manager') return true
  const me = members.value.find(m => m.user_id === auth.user!.id)
  return !!me && me.permission === 'edit'
})

const canManage = computed(() =>
  auth.user?.role_code === 'admin' || auth.user?.role_code === 'manager'
)

// 权限克隆：admin 或 manager 可见（与后端 require_admin_or_manager 对齐）
const canClonePerm = computed(() =>
  auth.user?.role_code === 'admin' || auth.user?.role_code === 'manager'
)
const cloneDialogVisible = ref(false)

async function loadProject() {
  try { project.value = await projectsApi.get(pid.value) }
  catch { router.push({ name: 'projects' }) }
}

// 项目头表某字段被子组件保存成功 → 同步更新本地 project.overview_meta
// （项目头表已镜像「项目一览」，写入的是一览存储 __o__，读的是 overview_meta）
// 这样切换 sheet / 同时打开多个 sheet 时都能看到新值
function onHeaderUpdated(payload: { key: string; value: string | null }) {
  if (!project.value) return
  const meta = { ...(project.value.overview_meta || {}) }
  if (payload.value === null || payload.value === '') {
    delete meta[payload.key]
  } else {
    meta[payload.key] = payload.value
  }
  project.value = { ...project.value, overview_meta: meta }
}

// 项目自身字段（name / code）被改 → 同步本地 project
function onProjectFieldUpdated(payload: { field: 'name' | 'code'; value: string }) {
  if (!project.value) return
  project.value = { ...project.value, [payload.field]: payload.value }
}
async function loadMembers() { members.value = await projectsApi.listMembers(pid.value) }

async function loadDatasheets() {
  datasheets.value = await datasheetsApi.list(pid.value)
  if (datasheets.value.length && !activeSheetId.value) {
    activeSheetId.value = datasheets.value[0].id
  }
  if (activeSheetId.value && !datasheets.value.find(d => d.id === activeSheetId.value)) {
    activeSheetId.value = datasheets.value[0]?.id || null
  }
}

// ========== 🆕 v3 M12 部门协作 tab ==========
const workflow = ref<Workflow | null>(null)
const wfLoading = ref(false)
const STATUS_OPTIONS = ['进行中', '已完成', '已归档']
// 四表校验 slots（钣金装配/标准件清单/外协外购/原料下料单 — 仅模板四表，不含电工采购单第5表）
const FOUR_SHEETS = ['钣金装配', '标准件清单', '外协外购', '原料下料单']
const fourSheetStatus = computed(() =>
  FOUR_SHEETS.map(name => {
    const d = datasheets.value.find(x => x.name === name)
    return { name, imported: !!d?.imported, did: d?.id }
  })
)
// 当前激活表是否为装配前置三表（显示 done-flag banner）
const activeSheet = computed(() => datasheets.value.find(d => d.id === activeSheetId.value) || null)
const isPrecheckSheet = computed(() => !!activeSheet.value && ASSEMBLY_SHEETS.includes(activeSheet.value.name))
const canMarkDone = computed(() =>
  ['admin', 'manager', 'pm_lead', 'designer', 'design_lead'].includes(auth.user?.role_code || ''))

async function loadWorkflow() {
  wfLoading.value = true
  try { workflow.value = await collabApi.workflow(pid.value) }
  catch { workflow.value = null }
  finally { wfLoading.value = false }
}

async function onCollabTab(name: string) {
  if (name === 'collab' && !workflow.value) await loadWorkflow()
}

async function setProjStatus(status: string) {
  if (!project.value || project.value.status === status) return
  await projectsApi.update(pid.value, { status })
  project.value = { ...project.value, status }
  ElMessage.success('项目状态已更新')
}

async function toggleSheetDone(did?: number, cur?: boolean) {
  if (!did) return
  await collabApi.setDoneFlag(did, !cur)
  ElMessage.success(!cur ? '已标记完成' : '已取消完成')
  await loadDatasheets()
}


async function renameDatasheet(d: Datasheet) {
  const r = await ElMessageBox.prompt('新名称', '重命名数据表', {
    inputValue: d.name, confirmButtonText: '确定', cancelButtonText: '取消',
  }).catch(() => null)
  if (!r) return
  await datasheetsApi.rename(d.id, r.value)
  ElMessage.success('已重命名')
  await loadDatasheets()
}

async function deleteDatasheet(d: Datasheet) {
  await ElMessageBox.confirm(`删除数据表「${d.name}」？所有字段与数据都会丢失！`, '危险操作', {
    type: 'error', confirmButtonText: '删除', cancelButtonText: '取消',
    confirmButtonClass: 'el-button--danger',
  }).catch(() => 'cancel').then(async (r) => {
    if (r === 'cancel') return
    await datasheetsApi.remove(d.id)
    ElMessage.success('已删除')
    await loadDatasheets()
  })
}

// 成员管理（批量添加）
const memberDialogVisible = ref(false)
const allUsers = ref<User[]>([])
const newMember = ref({
  user_ids: [] as number[],
  permission: 'edit' as 'edit'|'view',
})
const adding = ref(false)

async function openAddMember() {
  try {
    allUsers.value = await adminApi.listUsers()
    newMember.value = { user_ids: [], permission: 'edit' }
    memberDialogVisible.value = true
  } catch {
    ElMessage.warning('需要管理员权限加载用户列表')
  }
}

const availableUsers = computed(() => {
  const ids = new Set(members.value.map(m => m.user_id))
  return allUsers.value.filter(u => !ids.has(u.id) && u.is_active)
})

function selectAllAvailable() {
  newMember.value.user_ids = availableUsers.value.map(u => u.id)
}
function clearSelection() {
  newMember.value.user_ids = []
}

async function submitAddMember() {
  if (!newMember.value.user_ids.length) {
    ElMessage.warning('请至少选择一个用户')
    return
  }
  adding.value = true
  try {
    const created = await projectsApi.addMembersBatch(
      pid.value, newMember.value.user_ids, newMember.value.permission,
    )
    memberDialogVisible.value = false
    ElMessage.success(`已添加 ${created.length} 个成员`)
    await loadMembers()
  } finally {
    adding.value = false
  }
}

async function changePermission(m: ProjectMember, perm: 'edit'|'view') {
  if (m.permission === perm) return
  await projectsApi.updateMember(pid.value, m.id, perm)
  ElMessage.success('权限已更新')
  await loadMembers()
}

async function removeMember(m: ProjectMember) {
  await ElMessageBox.confirm(`移除「${m.full_name || m.username}」？`, '确认', {
    type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消',
  }).catch(() => 'cancel').then(async (r) => {
    if (r === 'cancel') return
    await projectsApi.removeMember(pid.value, m.id)
    ElMessage.success('已移除')
    await loadMembers()
  })
}


// ========== Excel 导入导出 ==========
const importing = ref(false)
async function onImportFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const ok = await ElMessageBox.confirm(
    `导入「${file.name}」？\n\n⚠ 此操作会：\n• 删除本项目所有现有数据表（含字段、行、字段权限）\n• 然后从 Excel 重新建表\n\n其他项目不受影响。`,
    '全量导入确认',
    { confirmButtonText: '清空并导入', cancelButtonText: '取消', type: 'warning' }
  ).catch(() => false)
  if (!ok) { input.value = ''; return }
  importing.value = true
  try {
    const fd = new FormData(); fd.append('file', file)
    const token = localStorage.getItem('pms_token') || ''
    const r = await axios.post(`/api/projects/${pid.value}/import-excel`, fd, {
      headers: { Authorization: `Bearer ${token}` },
    })
    ElMessage.success(r.data.message || '导入成功')
    await loadDatasheets()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || e.message || '导入失败')
  } finally {
    importing.value = false
    input.value = ''
  }
}

async function exportCurrent() {
  if (!activeSheetId.value) { ElMessage.warning('请先选择数据表'); return }
  const token = localStorage.getItem('pms_token') || ''
  const res = await fetch(`/api/datasheets/${activeSheetId.value}/export`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  if (!res.ok) { ElMessage.error('导出失败'); return }
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8'')?([^;\n]+)/i)
  const fname = m ? decodeURIComponent(m[1].replace(/"/g,'')) : 'export.xlsx'
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = fname
  document.body.appendChild(a); a.click(); a.remove()
}

async function exportAll() {
  const token = localStorage.getItem('pms_token') || ''
  const res = await fetch(`/api/projects/${pid.value}/export`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  if (!res.ok) { ElMessage.error('导出失败'); return }
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8'')?([^;\n]+)/i)
  const fname = m ? decodeURIComponent(m[1].replace(/"/g,'')) : 'project.xlsx'
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = fname
  document.body.appendChild(a); a.click(); a.remove()
}

onMounted(async () => {
  await loadProject()
  await Promise.all([loadMembers(), loadDatasheets()])
})
</script>

<template>
  <div v-if="project">
    <div class="page-header">
      <el-button :icon="ArrowLeft" @click="router.push({ name: 'projects' })">返回</el-button>
      <div>
        <h1>{{ project.name }}</h1>
        <div class="desc">
          <b>{{ project.code }}</b> ·
          <el-tag size="small" effect="light">{{ project.status }}</el-tag>
          · 项目经理：{{ project.manager_name || '-' }}
        </div>
      </div>
      <div class="spacer"></div>
      <el-tooltip v-if="canClonePerm" content="把其他项目的字段权限配置克隆到当前项目">
        <el-button :icon="CopyDocument" @click="cloneDialogVisible = true">克隆权限</el-button>
      </el-tooltip>
      <label class="el-button" :class="canEdit ? 'el-button--primary' : ''" v-if="canEdit" style="margin: 0">
        <el-icon style="margin-right:6px"><Upload /></el-icon>
        <span>导入 Excel</span>
        <input type="file" accept=".xlsx,.xlsm,.xls" hidden @change="onImportFile" />
      </label>
      <el-button :icon="Download" @click="exportAll">导出全部</el-button>
    </div>

    <el-tabs v-model="activeTab" class="proj-tabs" @tab-change="onCollabTab">
      <!-- 数据 tab -->
      <el-tab-pane label="进度表" name="data">
        <div class="datasheet-tabs" v-if="datasheets.length || canEdit">
          <div
            v-for="d in datasheets" :key="d.id"
            class="ds-tab" :class="{ active: activeSheetId === d.id }"
            @click="activeSheetId = d.id"
          >
            <el-icon><Document /></el-icon>
            <span>{{ d.name }}</span>
            <span v-if="canEdit && activeSheetId === d.id && d.name !== '电工采购单'" class="ds-tab-actions" @click.stop>
              <el-icon class="ds-action" @click="renameDatasheet(d)" title="重命名"><Edit /></el-icon>
              <el-icon class="ds-action danger" @click="deleteDatasheet(d)" title="删除"><Delete /></el-icon>
            </span>
          </div>
        </div>

        <!-- 🆕 v3 §十七 装配前置三表完成标记 banner（仅钣金装配/标准件清单/外协外购显示） -->
        <div v-if="isPrecheckSheet && activeSheet" class="sheet-banner">
          <span>🆕 本表完成状态：</span>
          <el-tag :type="activeSheet.done_flag ? 'success' : 'primary'" size="small">
            {{ activeSheet.done_flag ? '已完成' : '进行中' }}
          </el-tag>
          <span class="muted small">所有项完成即标「已完成」，自动同步装配组工作台</span>
          <span class="spacer" style="flex:1"></span>
          <el-button v-if="canMarkDone" size="small"
                     @click="toggleSheetDone(activeSheet.id, activeSheet.done_flag)">
            {{ activeSheet.done_flag ? '↩ 标记为进行中' : '✓ 标记为已完成' }}
          </el-button>
        </div>

        <DatasheetGrid
          v-if="activeSheetId"
          :key="activeSheetId"
          :datasheet-id="activeSheetId"
          :can-edit="canEdit"
          :header-lines="activeSheetHeaderLines"
          :project="project"
          @header-updated="onHeaderUpdated"
          @project-field-updated="onProjectFieldUpdated"
        />
        <el-empty v-else description="还没有数据表，请上传 Excel 模版">
        </el-empty>
      </el-tab-pane>

      <!-- 成员 tab -->
      <el-tab-pane :label="`成员 (${members.length})`" name="members">
        <el-card>
          <template #header>
            <div style="display:flex;align-items:center">
              <span><el-icon><UserIcon /></el-icon> 项目成员</span>
              <span style="flex:1"></span>
              <el-button v-if="canManage" type="primary" size="small" :icon="Plus" @click="openAddMember">
                添加成员
              </el-button>
            </div>
          </template>
          <el-empty v-if="!members.length" description="尚未添加成员" />
          <el-table v-else :data="members" size="large" stripe>
            <el-table-column label="用户" min-width="200">
              <template #default="{ row }">
                <div style="font-weight:600">{{ row.full_name || row.username }}</div>
                <div class="muted small">@{{ row.username }}</div>
              </template>
            </el-table-column>
            <el-table-column label="角色" width="120">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ row.role_name }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="项目内权限" width="180">
              <template #default="{ row }">
                <el-select v-if="canManage" :model-value="row.permission"
                  @update:model-value="(v: any) => changePermission(row, v as any)"
                  size="small" style="width: 120px">
                  <el-option label="编辑" value="edit" />
                  <el-option label="只读" value="view" />
                </el-select>
                <el-tag v-else size="small" :type="row.permission === 'edit' ? 'success' : 'info'">
                  {{ row.permission === 'edit' ? '编辑' : '只读' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="right">
              <template #default="{ row }">
                <el-button v-if="canManage" size="small" type="danger" :icon="Delete" @click="removeMember(row)" />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- 🆕 v3 M12 部门协作 tab（叠加，不改原进度表/成员） -->
      <el-tab-pane label="部门协作 🆕" name="collab">
        <el-card style="margin-bottom:14px" shadow="never">
          <template #header>📦 项目状态 / 发货</template>
          <div class="collab-status">
            <span>项目状态：<el-tag size="small" effect="light">{{ project.status }}</el-tag></span>
            <span v-if="workflow">发货：
              <el-tag size="small" :type="workflow.ship_status === 'shipped' ? 'success' : 'warning'">
                {{ workflow.ship_status === 'shipped' ? '已发货' : '待发货' }}
              </el-tag>
            </span>
            <span v-if="canManage" style="margin-left:auto">
              管理层改状态：
              <el-select :model-value="project.status" size="small" style="width:130px"
                         @update:model-value="setProjStatus">
                <el-option v-for="s in STATUS_OPTIONS" :key="s" :label="s" :value="s" />
              </el-select>
            </span>
          </div>
        </el-card>

        <el-card style="margin-bottom:14px" shadow="never">
          <template #header>📑 四个数据表（设计完成前置校验）</template>
          <div class="sheet-slots">
            <div v-for="s in fourSheetStatus" :key="s.name" class="slot" :class="{ up: s.imported }">
              <div class="nm">{{ s.name }}</div>
              <div class="st" :class="s.imported ? 'ok' : 'no'">{{ s.imported ? '✅ 已导入' : '⬜ 未导入' }}</div>
            </div>
          </div>
          <div class="muted small" style="margin-top:8px">通过页头「导入 Excel」上传四表（含四个 sheet 一次导入）；此处仅显示校验状态。</div>
        </el-card>

        <el-card shadow="never" v-loading="wfLoading">
          <template #header>🔀 全流程工作流（并行 / 串行）</template>
          <WorkflowGraph v-if="workflow" :wf="workflow" />
          <el-empty v-else description="加载中…" :image-size="60" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 克隆权限对话框（仅 manager） -->
    <ClonePermissionsDialog
      v-if="canClonePerm && project"
      v-model="cloneDialogVisible"
      :target-project-id="project.id"
      :target-project-name="`${project.code} · ${project.name}`"
    />

    <!-- 添加成员对话框（多选） -->
    <el-dialog v-model="memberDialogVisible" title="添加项目成员" width="520px">
      <el-form label-position="top">
        <el-form-item>
          <template #label>
            <span>选择用户</span>
            <span class="muted small" style="margin-left:8px">
              已选 {{ newMember.user_ids.length }} / 可选 {{ availableUsers.length }}
            </span>
            <el-link type="primary" :underline="false" style="margin-left:12px"
                     @click="selectAllAvailable" v-if="availableUsers.length">全选</el-link>
            <el-link type="info" :underline="false" style="margin-left:8px"
                     @click="clearSelection" v-if="newMember.user_ids.length">清空</el-link>
          </template>
          <el-select v-model="newMember.user_ids" multiple filterable
                     collapse-tags collapse-tags-tooltip
                     size="large" style="width: 100%"
                     placeholder="搜索用户名 / 姓名，可多选">
            <el-option v-for="u in availableUsers" :key="u.id" :value="u.id"
                       :label="`${u.full_name || u.username} (${u.username})`" />
          </el-select>
        </el-form-item>
        <el-form-item label="项目内权限">
          <el-radio-group v-model="newMember.permission">
            <el-radio value="edit">可编辑</el-radio>
            <el-radio value="view">只读</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAddMember" :loading="adding"
                   :disabled="!newMember.user_ids.length">
          添加 {{ newMember.user_ids.length || '' }} 个成员
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted { color: var(--text-3); }
.small { font-size: 12px; }
.proj-tabs :deep(.el-tabs__nav) { padding-left: 0; }
.proj-tabs :deep(.el-tabs__item) { font-size: 14px; }

.datasheet-tabs {
  display: flex; gap: 4px;
  padding: 8px 12px;
  background: white;
  border-radius: var(--radius) var(--radius) 0 0;
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.ds-tab {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 14px;
  font-size: 13px; color: var(--text-2);
  border-radius: var(--radius-sm);
  cursor: pointer;
  white-space: nowrap;
  transition: all .15s;
}
.ds-tab:hover { background: var(--primary-light); color: var(--primary); }
.ds-tab.active { background: var(--primary-light); color: var(--primary); font-weight: 500; }
.ds-tab .el-icon { font-size: 14px; }
.ds-tab-actions { display: inline-flex; gap: 4px; margin-left: 4px; }
.ds-action { font-size: 13px; padding: 2px; }
.ds-action:hover { color: var(--primary); }
.ds-action.danger:hover { color: var(--danger); }

.ds-add {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px;
  font-size: 13px; color: var(--text-3);
  background: transparent; border: 1px dashed var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.ds-add:hover { color: var(--primary); border-color: var(--primary); background: var(--primary-light); }

/* 🆕 v3 M12 协作 tab + 装配前置 banner */
.sheet-banner {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 12px; margin-bottom: 10px;
  background: var(--el-fill-color-light); border-radius: 8px; font-size: 13px;
}
.collab-status { display: flex; align-items: center; gap: 22px; flex-wrap: wrap; font-size: 14px; }
.sheet-slots { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.slot {
  border: 1px solid var(--el-border-color); border-radius: 10px;
  padding: 14px; text-align: center; background: #fff;
}
.slot.up { background: #f0fdf4; border-color: #86efac; }
.slot .nm { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
.slot .st { font-size: 12px; }
.slot .st.ok { color: #16a34a; }
.slot .st.no { color: var(--text-3); }
</style>
