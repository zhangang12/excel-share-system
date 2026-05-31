<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { Refresh, Lock, DataLine, FolderOpened } from '@element-plus/icons-vue'
import { permApi, type PermMatrix, type MatrixField } from '@/api/permissions'
import FieldPermissionDialog from '@/components/FieldPermissionDialog.vue'

const matrix = ref<PermMatrix | null>(null)
const loading = ref(false)
const activeView = ref<'overview' | 'datasheets'>('overview')

const dialogVisible = ref(false)
const dialogField = ref<{ id: number; name: string; scope: 'overview' | 'datasheet' } | null>(null)

async function load() {
  loading.value = true
  try { matrix.value = await permApi.getMatrix() } finally { loading.value = false }
}

onMounted(load)

const roles = computed(() => matrix.value?.roles || [])

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
  if (!v) load()  // 关闭后刷新
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
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-card v-loading="loading">
      <el-radio-group v-model="activeView" size="default" style="margin-bottom: 16px">
        <el-radio-button value="overview"><el-icon><DataLine /></el-icon> 项目一览字段</el-radio-button>
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
      <div v-if="activeView === 'overview'">
        <el-empty v-if="!matrix?.overview.length" description="项目一览还没有自定义字段" />
        <table v-else class="matrix">
          <thead>
            <tr>
              <th class="th-field">字段</th>
              <th v-for="r in roles" :key="r.code" class="th-role">{{ r.name }}</th>
              <th class="th-action"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in visibleFields(matrix.overview)" :key="f.field_id">
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

      <!-- 项目进度字段矩阵 -->
      <div v-else>
        <el-empty v-if="!matrix?.datasheets.length" description="还没有项目数据表" />
        <div v-for="ds in matrix?.datasheets" :key="ds.datasheet_id" class="ds-block">
          <div class="ds-title">
            <span class="ds-proj">{{ ds.project_code }} · {{ ds.project_name }}</span>
            <span class="ds-name">{{ ds.datasheet_name }}</span>
          </div>
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
.matrix {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.matrix th, .matrix td {
  padding: 10px 12px;
  text-align: center;
  border-right: 1px solid var(--border-light);
}
.matrix th:last-child, .matrix td:last-child { border-right: none; }
.matrix thead th {
  background: #f9fafb;
  color: var(--text-2);
  font-weight: 600;
  font-size: 12px;
}
.th-field { width: 200px; text-align: left !important; }
.th-role { min-width: 80px; }
.th-action { width: 88px; }
.matrix tbody tr:not(:last-child) { border-bottom: 1px solid var(--border-light); }
.matrix tbody tr:hover { background: #fafbfc; }

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
