<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { Refresh, Lock, DataLine, FolderOpened } from '@element-plus/icons-vue'
import {
  permApi, type MatrixField,
  type OverviewMatrix, type DatasheetProject, type DatasheetMatrix,
} from '@/api/permissions'
import FieldPermissionDialog from '@/components/FieldPermissionDialog.vue'

const activeView = ref<'overview' | 'datasheets'>('overview')

// 🆕 异步分片：概览矩阵进页即拉(轻量)；进度矩阵按项目按需拉
const overview = ref<OverviewMatrix | null>(null)
const loadingOverview = ref(false)

const dsProjects = ref<DatasheetProject[]>([])
const loadingProjects = ref(false)
const selectedProject = ref<number | undefined>(undefined)
const dsMatrix = ref<DatasheetMatrix | null>(null)
const loadingDs = ref(false)

const dialogVisible = ref(false)
const dialogField = ref<{ id: number; name: string; scope: 'overview' | 'datasheet' } | null>(null)

async function loadOverview() {
  loadingOverview.value = true
  try { overview.value = await permApi.getOverviewMatrix() } finally { loadingOverview.value = false }
}

async function loadProjects() {
  loadingProjects.value = true
  try {
    dsProjects.value = await permApi.getDatasheetProjects()
    // 默认选中第一个项目，避免 tab 空白
    if (!selectedProject.value && dsProjects.value.length) {
      selectedProject.value = dsProjects.value[0].project_id
      await loadDatasheetMatrix(selectedProject.value)
    }
  } finally { loadingProjects.value = false }
}

async function loadDatasheetMatrix(pid: number) {
  loadingDs.value = true
  try { dsMatrix.value = await permApi.getDatasheetMatrix(pid) } finally { loadingDs.value = false }
}

function onSelectProject(pid: number) {
  selectedProject.value = pid
  loadDatasheetMatrix(pid)
}

function refresh() {
  if (activeView.value === 'overview') loadOverview()
  else if (selectedProject.value) loadDatasheetMatrix(selectedProject.value)
  else loadProjects()
}

onMounted(loadOverview)

// 首次切到「项目进度字段」tab 时才拉项目列表（懒加载）
watch(activeView, (v) => {
  if (v === 'datasheets' && !dsProjects.value.length && !loadingProjects.value) loadProjects()
})

// 表头角色：概览 tab 用概览矩阵的角色，进度 tab 用该项目矩阵的角色
const roles = computed(() =>
  (activeView.value === 'overview' ? overview.value?.roles : dsMatrix.value?.roles) || []
)

// 表格自带 # 行号列，名为"序号" / "#" / "No" 的字段视为冗余，矩阵中也隐藏
const ROWNUM_FIELD_NAMES = new Set(['序号', '#', 'no', 'no.', '序', '行号', 'index'])
function isRownumField(name: string): boolean {
  return ROWNUM_FIELD_NAMES.has((name || '').trim().toLowerCase())
}
function visibleFields(fields: MatrixField[]) {
  return fields.filter(f => !isRownumField(f.field_name))
}

function openConfig(field: MatrixField, scope: 'overview' | 'datasheet') {
  dialogField.value = { id: field.field_id, name: field.field_name, scope }
  dialogVisible.value = true
}

function onDialogClose(v: boolean) {
  dialogVisible.value = v
  if (!v) {
    // 关闭后只刷新当前分片，避免重拉全量
    if (dialogField.value?.scope === 'overview') loadOverview()
    else if (selectedProject.value) loadDatasheetMatrix(selectedProject.value)
  }
}

function cellClass(cell: { can_view: boolean; can_edit: boolean; customized: boolean }) {
  if (!cell.customized) return 'cell-default'
  if (!cell.can_view) return 'cell-deny'
  if (!cell.can_edit) return 'cell-readonly'
  return 'cell-full'
}

function cellLabel(cell: { can_view: boolean; can_edit: boolean }) {
  if (!cell.can_view) return '不可见'
  if (!cell.can_edit) return '只读'
  return '可编辑'
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>权限总览</h1>
        <div class="desc">查看与配置所有字段对每个角色的访问权限</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" @click="refresh">刷新</el-button>
    </div>

    <el-card>
      <el-radio-group v-model="activeView" size="default" style="margin-bottom: 16px">
        <el-radio-button value="overview"><el-icon><DataLine /></el-icon> 项目目录字段</el-radio-button>
        <el-radio-button value="datasheets"><el-icon><FolderOpened /></el-icon> 项目进度字段</el-radio-button>
      </el-radio-group>

      <!-- 图例 -->
      <div class="legend">
        <span class="lg cell-default">默认（全可见可编辑）</span>
        <span class="lg cell-full">已配置：可编辑</span>
        <span class="lg cell-readonly">已配置：只读</span>
        <span class="lg cell-deny">已配置：不可见</span>
      </div>

      <!-- 项目一览字段矩阵 -->
      <div v-if="activeView === 'overview'" v-loading="loadingOverview">
        <el-empty v-if="!loadingOverview && !overview?.overview.length" description="项目目录还没有自定义字段" />
        <div v-else-if="overview?.overview.length" class="matrix-wrap">
        <table class="matrix">
          <thead>
            <tr>
              <th class="th-field">字段</th>
              <th v-for="r in roles" :key="r.code" class="th-role">{{ r.name }}</th>
              <th class="th-action"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in visibleFields(overview.overview)" :key="f.field_id">
              <td class="td-field">
                <b>{{ f.field_name }}</b>
                <span class="ftype">{{ f.field_type }}</span>
              </td>
              <td v-for="r in roles" :key="r.code" :class="cellClass(f.perms[r.code])" class="td-cell">
                {{ cellLabel(f.perms[r.code]) }}
              </td>
              <td>
                <el-button size="small" :icon="Lock" @click="openConfig(f, 'overview')">配置</el-button>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
      </div>

      <!-- 项目进度字段矩阵：先选项目，再异步拉该项目的数据表矩阵 -->
      <div v-else>
        <div class="ds-toolbar" v-loading="loadingProjects">
          <span class="ds-toolbar-label">选择项目：</span>
          <el-select
            :model-value="selectedProject"
            filterable
            placeholder="选择项目查看其进度字段权限"
            style="width: 360px"
            :loading="loadingProjects"
            @change="onSelectProject">
            <el-option
              v-for="p in dsProjects"
              :key="p.project_id"
              :label="`${p.project_code} · ${p.project_name}（${p.datasheet_count} 张表）`"
              :value="p.project_id" />
          </el-select>
          <span v-if="dsProjects.length" class="muted ds-count">共 {{ dsProjects.length }} 个项目</span>
        </div>

        <el-empty v-if="!loadingProjects && !dsProjects.length" description="还没有项目数据表" />
        <div v-else v-loading="loadingDs">
          <el-empty v-if="selectedProject && !loadingDs && !dsMatrix?.datasheets.length" description="该项目暂无数据表" />
          <div v-for="ds in dsMatrix?.datasheets" :key="ds.datasheet_id" class="ds-block">
          <div class="ds-title">
            <span class="ds-proj">{{ ds.project_code }} · {{ ds.project_name }}</span>
            <span class="ds-name">{{ ds.datasheet_name }}</span>
          </div>
          <div class="matrix-wrap">
          <table class="matrix">
            <thead>
              <tr>
                <th class="th-field">字段</th>
                <th v-for="r in roles" :key="r.code" class="th-role">{{ r.name }}</th>
                <th class="th-action"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in visibleFields(ds.fields)" :key="f.field_id">
                <td class="td-field">
                  <b>{{ f.field_name }}</b>
                  <span class="ftype">{{ f.field_type }}</span>
                </td>
                <td v-for="r in roles" :key="r.code" :class="cellClass(f.perms[r.code])" class="td-cell">
                  {{ cellLabel(f.perms[r.code]) }}
                </td>
                <td>
                  <el-button size="small" :icon="Lock" @click="openConfig(f, 'datasheet')">配置</el-button>
                </td>
              </tr>
            </tbody>
          </table>
          </div>
          </div>
        </div>
      </div>
    </el-card>

    <FieldPermissionDialog
      v-if="dialogField"
      v-model="dialogVisible"
      :field-id="dialogField.id"
      :field-name="dialogField.name"
      :scope="dialogField.scope"
      @update:model-value="onDialogClose"
    />
  </div>
</template>

<style scoped>
/* 🆕 矩阵列多时横向滚动（角色列可达 20+），滚动条常驻 */
.matrix-wrap {
  overflow-x: auto;
  max-width: 100%;
  scrollbar-width: thin;
}
.matrix-wrap::-webkit-scrollbar { height: 10px; }
.matrix-wrap::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 5px; }
.matrix-wrap::-webkit-scrollbar-track { background: #f1f5f9; }
.matrix {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  border-radius: 8px;
  overflow: hidden;
  border: 2px solid #64748b;
}
.matrix th, .matrix td {
  padding: 10px 12px;
  text-align: center;
  border-right: 2px solid #94a3b8;
}
.matrix th:last-child, .matrix td:last-child { border-right: none; }
.matrix thead th {
  background: linear-gradient(180deg, #cbd5e1 0%, #94a3b8 100%);
  color: #0f172a;
  font-weight: 700;
  font-size: 12.5px;
}
.th-field { width: 200px; text-align: left !important; }
.th-role { min-width: 80px; }
.th-action { width: 88px; }
.matrix tbody tr:not(:last-child) { border-bottom: 2px solid #94a3b8; }
.matrix tbody tr:hover { background: #dbeafe; }

.td-field { text-align: left !important; }
.td-field b { font-weight: 600; color: var(--text-1); }
.ftype {
  display: inline-block;
  margin-left: 6px;
  font-size: 11px;
  padding: 1px 6px;
  background: var(--primary-light);
  color: var(--primary);
  border-radius: 4px;
  font-weight: 500;
}

.td-cell {
  font-weight: 500;
  font-size: 12px;
}
.cell-default { color: var(--text-3); background: white; }
.cell-full { color: #065f46; background: #d1fae5; }
.cell-readonly { color: #92400e; background: #fef3c7; }
.cell-deny { color: #991b1b; background: #fee2e2; }

.legend {
  display: flex; gap: 14px; flex-wrap: wrap;
  margin-bottom: 16px;
  font-size: 12px;
}
.lg {
  padding: 3px 10px;
  border-radius: 4px;
}

/* 🆕 项目进度字段 tab 顶部项目选择器 */
.ds-toolbar {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 18px; padding-bottom: 16px;
  border-bottom: 1px dashed var(--border);
}
.ds-toolbar-label { font-size: 13px; color: var(--text-2); font-weight: 500; }
.ds-toolbar .ds-count { font-size: 12px; color: var(--text-3); }
.muted { color: var(--text-3); }

.ds-block {
  margin-bottom: 28px;
}
.ds-title {
  padding: 10px 14px;
  background: linear-gradient(90deg, var(--primary-light), white);
  border-radius: 8px 8px 0 0;
  border: 1px solid var(--border);
  border-bottom: 0;
  font-size: 13px;
}
.ds-title .ds-proj { color: var(--primary); font-weight: 600; margin-right: 12px; }
.ds-title .ds-name { color: var(--text-2); }
.ds-block .matrix { border-radius: 0 0 8px 8px; }
</style>
