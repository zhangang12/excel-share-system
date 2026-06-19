<script setup lang="ts">
// 采购部项目列表：按采购员子角色显示两套列
//   外协采购员(buyer_outsource)：项目编号/项目名称/设计师/外协加工表/不锈钢原料下料单/CAD激光图纸/外购附图
//   标准件采购员(buyer_standard)：项目编号/项目名称/电工采购单/标准件清单
//   未细分 buyer / 管理层 / 双角色：两套列全显示
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh, View } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
import { datasheetsApi } from '@/api/datasheets'
import EmptyHint from '@/components/EmptyHint.vue'

interface Att { id: number; name: string }
interface Row {
  project_id: number; code: string; name: string; designer?: string | null
  outsource_sheet_id?: number | null; material_sheet_id?: number | null
  elec_po_sheet_id?: number | null; standard_sheet_id?: number | null
  cad_laser_files: Att[]; outsource_img_files: Att[]
}

const auth = useAuthStore()
const loading = ref(false)
const rows = ref<Row[]>([])

// 列可见性：统一采购部角色，按账号名区分分工
const isFangbusen = computed(() => auth.user?.username === 'fangbusen')
const isLixinxin  = computed(() => auth.user?.username === 'lixinxin')
const seeAll = computed(() => !isFangbusen.value && !isLixinxin.value)
const showOutsource = computed(() => seeAll.value || isFangbusen.value)
const showStandard  = computed(() => seeAll.value || isLixinxin.value)

async function load() {
  loading.value = true
  try { rows.value = (await http.get<Row[]>('/purchase/projects')).data }
  finally { loading.value = false }
}
onMounted(load)

// 数据表「下载」
async function downloadSheet(did: number | null | undefined, label: string) {
  if (!did) { ElMessage.info(`该项目暂无「${label}」`); return }
  try {
    const res = await http.get(`/datasheets/${did}/export`, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a'); a.href = url; a.download = `${label}.xlsx`; a.click()
    URL.revokeObjectURL(url)
  } catch { ElMessage.error('下载失败') }
}

// 数据表「预览」
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewTitle = ref('')
const previewFields = ref<{ id: number; name: string }[]>([])
const previewRecords = ref<any[]>([])
const previewColWidth = computed(() => {
  const n = previewFields.value.length
  if (!n) return 120
  const usable = (typeof window !== 'undefined' ? window.innerWidth : 1280) - 50 - 32
  return Math.max(80, Math.floor(usable / n))
})

async function openPreview(did: number | null | undefined, title: string) {
  if (!did) { ElMessage.info(`该项目暂无「${title}」`); return }
  previewTitle.value = title
  previewVisible.value = true
  previewLoading.value = true
  try {
    const [fs, recs] = await Promise.all([
      datasheetsApi.listFields(did),
      datasheetsApi.listRecords(did),
    ])
    previewFields.value = fs.map((f: any) => ({ id: f.id, name: f.name }))
    previewRecords.value = recs
  } finally { previewLoading.value = false }
}

function cellVal(rec: any, fid: number) {
  return rec.values?.[String(fid)] ?? ''
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购部</h1>
        <div class="desc">按项目汇总采购所需数据表（支持预览/下载）与设计师推送的图纸；不同采购员看到对应列</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="50" fixed />
        <el-table-column label="项目编号" width="120" fixed>
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" min-width="160" show-overflow-tooltip />
        <el-table-column v-if="showOutsource" label="设计师" width="90">
          <template #default="{ row }">{{ row.designer || '—' }}</template>
        </el-table-column>

        <!-- 外协采购列 -->
        <el-table-column v-if="showOutsource" label="外协加工表" min-width="150">
          <template #default="{ row }">
            <div class="btn-group">
              <el-button size="small" link type="primary" :disabled="!row.outsource_sheet_id"
                         @click="openPreview(row.outsource_sheet_id, `${row.code} · 外协加工表`)">
                <el-icon><View /></el-icon>预览
              </el-button>
              <el-button size="small" link :disabled="!row.outsource_sheet_id"
                         @click="downloadSheet(row.outsource_sheet_id, '外协加工')">
                <el-icon><Download /></el-icon>
              </el-button>
              <span v-if="!row.outsource_sheet_id" class="muted">—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="showOutsource" label="不锈钢原料下料单" min-width="170">
          <template #default="{ row }">
            <div class="btn-group">
              <el-button size="small" link type="primary" :disabled="!row.material_sheet_id"
                         @click="openPreview(row.material_sheet_id, `${row.code} · 不锈钢原料下料单`)">
                <el-icon><View /></el-icon>预览
              </el-button>
              <el-button size="small" link :disabled="!row.material_sheet_id"
                         @click="downloadSheet(row.material_sheet_id, '不锈钢原料下料单')">
                <el-icon><Download /></el-icon>
              </el-button>
              <span v-if="!row.material_sheet_id" class="muted">—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="showOutsource" label="CAD激光图纸(设计推送)" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="f in row.cad_laser_files" :key="f.id" size="small" effect="plain" class="fc" @click="downloadAttachment(f)">
              {{ f.name }}<el-icon class="dl"><Download /></el-icon>
            </el-tag>
            <span v-if="!row.cad_laser_files.length" class="muted">待设计推送</span>
          </template>
        </el-table-column>
        <el-table-column v-if="showOutsource" label="外购附图(设计推送)" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="f in row.outsource_img_files" :key="f.id" size="small" effect="plain" class="fc" @click="downloadAttachment(f)">
              {{ f.name }}<el-icon class="dl"><Download /></el-icon>
            </el-tag>
            <span v-if="!row.outsource_img_files.length" class="muted">待设计推送</span>
          </template>
        </el-table-column>

        <!-- 标准件采购列 -->
        <el-table-column v-if="showStandard" label="电工采购单" min-width="150">
          <template #default="{ row }">
            <div class="btn-group">
              <el-button size="small" link type="primary" :disabled="!row.elec_po_sheet_id"
                         @click="openPreview(row.elec_po_sheet_id, `${row.code} · 电工采购单`)">
                <el-icon><View /></el-icon>预览
              </el-button>
              <el-button size="small" link :disabled="!row.elec_po_sheet_id"
                         @click="downloadSheet(row.elec_po_sheet_id, '电工采购单')">
                <el-icon><Download /></el-icon>
              </el-button>
              <span v-if="!row.elec_po_sheet_id" class="muted">—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="showStandard" label="标准件清单" min-width="150">
          <template #default="{ row }">
            <div class="btn-group">
              <el-button size="small" link type="primary" :disabled="!row.standard_sheet_id"
                         @click="openPreview(row.standard_sheet_id, `${row.code} · 标准件清单`)">
                <el-icon><View /></el-icon>预览
              </el-button>
              <el-button size="small" link :disabled="!row.standard_sheet_id"
                         @click="downloadSheet(row.standard_sheet_id, '标准件清单')">
                <el-icon><Download /></el-icon>
              </el-button>
              <span v-if="!row.standard_sheet_id" class="muted">—</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无项目" />
    </el-card>

    <!-- 数据表预览弹窗（全屏只读） -->
    <el-dialog v-model="previewVisible" :title="previewTitle" fullscreen destroy-on-close>
      <el-table :data="previewRecords" stripe v-loading="previewLoading"
                max-height="calc(100vh - 130px)" :scrollbar-always-on="true" size="small">
        <el-table-column
          v-for="f in previewFields" :key="f.id"
          :label="f.name" :min-width="previewColWidth">
          <template #default="{ row }">{{ cellVal(row, f.id) }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
.fc { cursor: pointer; margin: 2px 4px 2px 0; }
.fc .dl { margin-left: 4px; }
.btn-group { display: flex; align-items: center; gap: 2px; }
</style>
