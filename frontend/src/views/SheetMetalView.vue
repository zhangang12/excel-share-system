<script setup lang="ts">
// 🆕 v3 M05 钣金组：项目图纸包下载 + 钣金装配表只读引用
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, Download, Refresh, CircleCheck } from '@element-plus/icons-vue'
import { http } from '@/api'
import { downloadAttachment } from '@/api/orders'
import { datasheetsApi } from '@/api/datasheets'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'

interface Att { id: number; name: string }
interface Row {
  project_id: number; code: string; name: string; designer?: string | null
  sheetmetal_datasheet_id?: number | null; sheetmetal_done: boolean; pkg_files: Att[]
}

const loading = ref(false)
const rows = ref<Row[]>([])

async function load() {
  loading.value = true
  try { rows.value = (await http.get<Row[]>('/sheetmetal/projects')).data }
  finally { loading.value = false }
}
onMounted(load)

// 钣金装配表只读预览
const viewVisible = ref(false)
const viewLoading = ref(false)
const viewTitle = ref('')
const viewFields = ref<{ id: number; name: string }[]>([])
const viewRecords = ref<any[]>([])
async function viewSheet(row: Row) {
  if (!row.sheetmetal_datasheet_id) { ElMessage.info('该项目暂无钣金装配表'); return }
  viewTitle.value = `${row.code} · 钣金装配表（只读引用）`
  viewVisible.value = true
  viewLoading.value = true
  try {
    const [fs, recs] = await Promise.all([
      datasheetsApi.listFields(row.sheetmetal_datasheet_id),
      datasheetsApi.listRecords(row.sheetmetal_datasheet_id),
    ])
    viewFields.value = fs.map((f: any) => ({ id: f.id, name: f.name }))
    viewRecords.value = recs
  } finally { viewLoading.value = false }
}
function cellVal(rec: any, fid: number) {
  return rec.values?.[String(fid)] ?? ''
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>钣金组</h1>
        <div class="desc">设计部接单后上传的 PDF 图纸包在此下载；钣金装配表为设计数据表的只读引用</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="项目" width="120">
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
        <el-table-column label="设计师" width="90">
          <template #default="{ row }">{{ row.designer || '—' }}</template>
        </el-table-column>
        <el-table-column label="钣金装配表(引用)" width="150">
          <template #default="{ row }">
            <el-button v-if="row.sheetmetal_datasheet_id" size="small" link type="primary" @click="viewSheet(row)">
              钣金装配表<el-icon v-if="row.sheetmetal_done" color="var(--success,#10b981)" style="margin-left:4px"><CircleCheck /></el-icon>
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="PDF 图纸包(下载)" min-width="220">
          <template #default="{ row }">
            <el-tag v-for="f in row.pkg_files" :key="f.id" size="small" effect="plain"
                    class="fc" @click="downloadAttachment(f)">
              <el-icon><Document /></el-icon>{{ f.name }}<el-icon class="dl"><Download /></el-icon>
            </el-tag>
            <span v-if="!row.pkg_files.length" class="muted">待设计部上传</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120" align="center" fixed="right">
          <template #default="{ row }">
            <StatusPill :variant="row.pkg_files.length ? 'success' : 'muted'"
                        :text="row.pkg_files.length ? `可下载·${row.pkg_files.length}` : '待图纸包'" />
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无承接项目" />
    </el-card>

    <el-dialog v-model="viewVisible" :title="viewTitle" width="880px" class="v3-scroll-dialog">
      <div v-loading="viewLoading">
        <el-alert type="info" :closable="false" style="margin-bottom:10px"
                  title="只读引用——钣金装配表数据由设计部维护，钣金组不可编辑" />
        <el-table :data="viewRecords" border size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column type="index" label="#" width="50" />
          <el-table-column v-for="f in viewFields" :key="f.id" :label="f.name" min-width="110">
            <template #default="{ row }">{{ cellVal(row, f.id) }}</template>
          </el-table-column>
        </el-table>
        <EmptyHint v-if="!viewLoading && !viewRecords.length" text="钣金装配表暂无数据" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
.fc { cursor: pointer; margin: 2px 4px 2px 0; }
.fc .dl { margin-left: 4px; }
</style>
