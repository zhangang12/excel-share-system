<script setup lang="ts">
// 采购部项目列表：列只做「表格预览 + 附件上传状态」，右侧「打包下载」面板勾选表格/附件打 zip。
//   外协采购员(fangbusen)：外协加工表/不锈钢原料下料单/CAD激光图纸
//   标准件采购员(lixinxin)：电工采购单/标准件清单/外购附图
//   未细分 buyer / 管理层：全列显示
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh, View } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
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

const curYear = String(new Date().getFullYear())
const yearFilter = ref(curYear)
const yearOptions = computed(() => { const y = parseInt(curYear); return [y - 1, y, y + 1].map(String) })
const projStatusFilter = ref('进行中')

async function load() {
  loading.value = true
  try {
    rows.value = (await http.get<Row[]>('/purchase/projects', {
      params: { year: yearFilter.value, proj_status: projStatusFilter.value || undefined }
    })).data
  } finally { loading.value = false }
}
onMounted(load)

// ===== 数据表「预览」（只读全屏） =====
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

// ===== 右侧「打包下载」面板 =====
type SheetKey = 'outsource_sheet_id' | 'material_sheet_id' | 'elec_po_sheet_id' | 'standard_sheet_id'
const SHEET_DEFS: { key: SheetKey; label: string; group: 'outsource' | 'standard' }[] = [
  { key: 'outsource_sheet_id', label: '外协加工表', group: 'outsource' },
  { key: 'material_sheet_id', label: '不锈钢原料下料单', group: 'outsource' },
  { key: 'elec_po_sheet_id', label: '电工采购单', group: 'standard' },
  { key: 'standard_sheet_id', label: '标准件清单', group: 'standard' },
]

const dlVisible = ref(false)
const dlRow = ref<Row | null>(null)
const dlSelSheets = ref<number[]>([])
const dlSelAtts = ref<number[]>([])
const dlPacking = ref(false)

const dlSheets = computed(() => {
  const r = dlRow.value
  if (!r) return [] as { id: number; label: string }[]
  return SHEET_DEFS
    .filter(d => (d.group === 'outsource' ? showOutsource.value : showStandard.value))
    .map(d => ({ id: r[d.key] as number | null | undefined, label: d.label }))
    .filter((s): s is { id: number; label: string } => s.id != null)
})
const dlAtts = computed(() => {
  const r = dlRow.value
  if (!r) return [] as { id: number; name: string; kind: string }[]
  const out: { id: number; name: string; kind: string }[] = []
  if (showOutsource.value) r.cad_laser_files.forEach(f => out.push({ id: f.id, name: f.name, kind: 'CAD激光图纸' }))
  if (showStandard.value) r.outsource_img_files.forEach(f => out.push({ id: f.id, name: f.name, kind: '外购附图' }))
  return out
})
const dlTotal = computed(() => dlSheets.value.length + dlAtts.value.length)
const dlSelCount = computed(() => dlSelSheets.value.length + dlSelAtts.value.length)

function openDownload(row: Row) {
  dlRow.value = row
  dlSelSheets.value = dlSheets.value.map(s => s.id)   // 默认全选
  dlSelAtts.value = dlAtts.value.map(a => a.id)
  dlVisible.value = true
}
function toggleAllSheets(v: any) { dlSelSheets.value = v ? dlSheets.value.map(s => s.id) : [] }
function toggleAllAtts(v: any) { dlSelAtts.value = v ? dlAtts.value.map(a => a.id) : [] }

async function packDownload() {
  if (!dlRow.value) return
  if (!dlSelCount.value) { ElMessage.info('请至少勾选一项'); return }
  dlPacking.value = true
  try {
    const res = await http.post('/purchase/package', {
      project_id: dlRow.value.project_id,
      sheet_ids: dlSelSheets.value,
      attachment_ids: dlSelAtts.value,
    }, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a'); a.href = url
    a.download = `${dlRow.value.code}_采购资料.zip`; a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已打包下载')
  } catch {
    ElMessage.error('打包下载失败')
  } finally { dlPacking.value = false }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购部</h1>
        <div class="desc">按项目汇总采购数据表与设计推送附件；表格列可预览，右侧「打包下载」可勾选表格/附件打 zip</div>
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
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="50" fixed />
        <el-table-column label="项目编号" width="116" fixed>
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" min-width="170" show-overflow-tooltip />
        <el-table-column v-if="showOutsource" label="设计师" width="84" align="center">
          <template #default="{ row }">{{ row.designer || '—' }}</template>
        </el-table-column>

        <!-- 外协采购：数据表只做预览 -->
        <el-table-column v-if="showOutsource" label="外协加工表" width="96" align="center">
          <template #default="{ row }">
            <el-button v-if="row.outsource_sheet_id" size="small" link type="primary"
                       @click="openPreview(row.outsource_sheet_id, `${row.code} · 外协加工表`)">
              <el-icon><View /></el-icon>预览
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column v-if="showOutsource" label="不锈钢原料下料单" width="120" align="center">
          <template #default="{ row }">
            <el-button v-if="row.material_sheet_id" size="small" link type="primary"
                       @click="openPreview(row.material_sheet_id, `${row.code} · 不锈钢原料下料单`)">
              <el-icon><View /></el-icon>预览
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <!-- 附件列：只显示上传状态，悬停看附件名 -->
        <el-table-column v-if="showOutsource" label="CAD激光图纸" width="116" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.cad_laser_files.length" placement="top">
              <template #content>
                <div v-for="f in row.cad_laser_files" :key="f.id" class="tip-line">{{ f.name }}</div>
              </template>
              <el-tag size="small" type="success" effect="light" round>已推送 {{ row.cad_laser_files.length }}</el-tag>
            </el-tooltip>
            <span v-else class="muted">待推送</span>
          </template>
        </el-table-column>

        <!-- 标准件采购：数据表只做预览 -->
        <el-table-column v-if="showStandard" label="电工采购单" width="96" align="center">
          <template #default="{ row }">
            <el-button v-if="row.elec_po_sheet_id" size="small" link type="primary"
                       @click="openPreview(row.elec_po_sheet_id, `${row.code} · 电工采购单`)">
              <el-icon><View /></el-icon>预览
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column v-if="showStandard" label="标准件清单" width="96" align="center">
          <template #default="{ row }">
            <el-button v-if="row.standard_sheet_id" size="small" link type="primary"
                       @click="openPreview(row.standard_sheet_id, `${row.code} · 标准件清单`)">
              <el-icon><View /></el-icon>预览
            </el-button>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column v-if="showStandard" label="外购附图" width="110" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.outsource_img_files.length" placement="top">
              <template #content>
                <div v-for="f in row.outsource_img_files" :key="f.id" class="tip-line">{{ f.name }}</div>
              </template>
              <el-tag size="small" type="success" effect="light" round>已推送 {{ row.outsource_img_files.length }}</el-tag>
            </el-tooltip>
            <span v-else class="muted">待推送</span>
          </template>
        </el-table-column>

        <!-- 右侧：打包下载入口 -->
        <el-table-column label="下载" width="84" fixed="right" align="center">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :icon="Download" @click="openDownload(row)">打包</el-button>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无项目" />
    </el-card>

    <!-- 右侧打包下载抽屉 -->
    <el-drawer v-model="dlVisible" :title="`${dlRow?.code || ''} · 采购资料打包下载`"
               direction="rtl" size="440px" destroy-on-close>
      <template v-if="dlRow">
        <div class="dl-tip">勾选需要的表格与附件，一键打包成 zip 下载（表格导出为 Excel）。</div>

        <div class="dl-sec">
          <div class="dl-sec-head">
            <span class="dl-sec-title">📄 采购数据表</span>
            <el-checkbox v-if="dlSheets.length"
                         :model-value="dlSelSheets.length === dlSheets.length"
                         :indeterminate="dlSelSheets.length > 0 && dlSelSheets.length < dlSheets.length"
                         @change="toggleAllSheets">全选</el-checkbox>
          </div>
          <el-checkbox-group v-model="dlSelSheets" class="dl-list">
            <el-checkbox v-for="s in dlSheets" :key="s.id" :value="s.id" class="dl-item">{{ s.label }}</el-checkbox>
          </el-checkbox-group>
          <div v-if="!dlSheets.length" class="muted dl-empty">该项目暂无采购数据表</div>
        </div>

        <div class="dl-sec">
          <div class="dl-sec-head">
            <span class="dl-sec-title">📎 设计推送附件</span>
            <el-checkbox v-if="dlAtts.length"
                         :model-value="dlSelAtts.length === dlAtts.length"
                         :indeterminate="dlSelAtts.length > 0 && dlSelAtts.length < dlAtts.length"
                         @change="toggleAllAtts">全选</el-checkbox>
          </div>
          <el-checkbox-group v-model="dlSelAtts" class="dl-list">
            <el-checkbox v-for="a in dlAtts" :key="a.id" :value="a.id" class="dl-item">
              <span class="dl-kind">{{ a.kind }}</span>{{ a.name }}
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="!dlAtts.length" class="muted dl-empty">暂无设计推送附件</div>
        </div>
      </template>
      <template #footer>
        <el-button @click="dlVisible = false">取消</el-button>
        <el-button type="primary" :loading="dlPacking" :icon="Download"
                   :disabled="!dlSelCount" @click="packDownload">
          打包下载（{{ dlSelCount }}/{{ dlTotal }}）
        </el-button>
      </template>
    </el-drawer>

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
.tip-line { line-height: 1.7; }

/* 打包下载抽屉 */
.dl-tip { font-size: 12.5px; color: var(--el-text-color-secondary); margin-bottom: 14px; line-height: 1.6; }
.dl-sec { margin-bottom: 20px; }
.dl-sec-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.dl-sec-title { font-weight: 600; color: #0f172a; font-size: 13px; }
.dl-list { display: flex; flex-direction: column; gap: 6px; }
.dl-item { width: 100%; margin-right: 0; height: auto; }
.dl-item :deep(.el-checkbox__label) { white-space: normal; word-break: break-all; line-height: 1.5; }
.dl-kind {
  display: inline-block; margin-right: 6px; padding: 0 6px;
  font-size: 11px; color: #1d4ed8; background: #dbeafe; border-radius: 8px;
}
.dl-empty { padding: 4px 0; }
</style>
