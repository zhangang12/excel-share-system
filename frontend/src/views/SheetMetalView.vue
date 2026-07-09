<script setup lang="ts">
// v3 M05 钣金组：项目图纸包下载 + 钣金装配表可编辑引用
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, CircleCheck } from '@element-plus/icons-vue'
import { http } from '@/api'
import EmptyHint from '@/components/EmptyHint.vue'
import SheetmetalGrid from '@/components/SheetmetalGrid.vue'

interface Row {
  project_id: number; code: string; name: string; designer?: string | null
  sheetmetal_datasheet_id?: number | null; sheetmetal_done: boolean
}

const loading = ref(false)
const rows = ref<Row[]>([])

const curYear = String(new Date().getFullYear())
const yearFilter = ref(curYear)
const yearOptions = computed(() => { const y = parseInt(curYear); return [y - 1, y, y + 1].map(String) })
const projStatusFilter = ref('进行中')

async function load() {
  loading.value = true
  try {
    rows.value = (await http.get<Row[]>('/sheetmetal/projects', {
      params: { year: yearFilter.value, proj_status: projStatusFilter.value || undefined }
    })).data
  } finally { loading.value = false }
}
onMounted(load)

// 钣金装配表预览/编辑
const viewVisible = ref(false)
const viewTitle = ref('')
const viewRow = ref<Row | null>(null)

function viewSheet(row: Row) {
  if (!row.sheetmetal_datasheet_id) { ElMessage.info('该项目暂无钣金装配表'); return }
  viewTitle.value = `${row.code} · 钣金装配表`
  viewRow.value = row
  viewVisible.value = true
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>钣金组</h1>
        <div class="desc">钣金装配表可直接在此页面编辑维护，修改实时同步至项目详单</div>
      </div>
      <div class="spacer"></div>
      <el-select v-model="yearFilter" size="large" style="width:100px" @change="load">
        <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
      </el-select>
      <el-select v-model="projStatusFilter" size="large" style="width:100px" @change="load">
        <el-option label="进行中" value="进行中" />
        <el-option label="已完成" value="已完成" />
        <el-option label="全部" value="" />
      </el-select>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table show-overflow-tooltip :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="项目" width="120">
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
        <el-table-column label="设计师" width="90">
          <template #default="{ row }">{{ row.designer || '—' }}</template>
        </el-table-column>
        <el-table-column label="钣金装配表" width="150">
          <template #default="{ row }">
            <el-button v-if="row.sheetmetal_datasheet_id" size="small" link type="primary" @click="viewSheet(row)">
              编辑钣金装配表<el-icon v-if="row.sheetmetal_done" color="var(--success,#10b981)" style="margin-left:4px"><CircleCheck /></el-icon>
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无承接项目" />
    </el-card>

    <el-dialog v-model="viewVisible" :title="viewTitle" width="90vw" class="v3-scroll-dialog" destroy-on-close>
      <SheetmetalGrid
        v-if="viewRow?.sheetmetal_datasheet_id"
        :datasheetId="viewRow.sheetmetal_datasheet_id"
        :projectCode="viewRow.code"
        :canEdit="true"
      />
    </el-dialog>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
</style>
