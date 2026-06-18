<script setup lang="ts">
// 🆕 采购部项目列表：按采购员子角色显示两套列
//   外协采购员(buyer_outsource)：项目编号/项目名称/设计师/外协加工(下载)/不锈钢原料下料单(下载)/CAD激光图纸
//   标准件采购员(buyer_standard)：项目编号/项目名称/电工采购单(下载)/标准件清单(下载)/外购附图
//   未细分 buyer / 管理层：两套列全显示
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh, Document } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
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

// 列可见性：仅外协采购员→外协列；仅标准件采购员→标准件列；buyer/管理层/双角色→全部
const isOutsource = computed(() => auth.hasRole('buyer_outsource'))
const isStandard = computed(() => auth.hasRole('buyer_standard'))
const seeAll = computed(() => auth.hasRole('buyer', 'admin', 'manager') || (!isOutsource.value && !isStandard.value))
const showOutsource = computed(() => seeAll.value || isOutsource.value)
const showStandard = computed(() => seeAll.value || isStandard.value)

async function load() {
  loading.value = true
  try { rows.value = (await http.get<Row[]>('/purchase/projects')).data }
  finally { loading.value = false }
}
onMounted(load)

// 数据表「引用·下载」：导出该 sheet 为 xlsx
async function downloadSheet(did: number | null | undefined, label: string) {
  if (!did) { ElMessage.info(`该项目暂无「${label}」`); return }
  try {
    const res = await http.get(`/datasheets/${did}/export`, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a'); a.href = url; a.download = `${label}.xlsx`; a.click()
    URL.revokeObjectURL(url)
  } catch { ElMessage.error('下载失败') }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购部</h1>
        <div class="desc">按项目汇总采购所需数据表（引用·支持下载）与设计师推送的图纸；不同采购员看到对应列</div>
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
        <el-table-column v-if="showOutsource" label="外协加工表" min-width="130">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :disabled="!row.outsource_sheet_id"
                       @click="downloadSheet(row.outsource_sheet_id, '外协加工')">
              <el-icon><Document /></el-icon>{{ row.outsource_sheet_id ? '下载' : '—' }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column v-if="showOutsource" label="不锈钢原料下料单" min-width="150">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :disabled="!row.material_sheet_id"
                       @click="downloadSheet(row.material_sheet_id, '不锈钢原料下料单')">
              <el-icon><Document /></el-icon>{{ row.material_sheet_id ? '下载' : '—' }}
            </el-button>
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

        <!-- 标准件采购列 -->
        <el-table-column v-if="showStandard" label="电工采购单" min-width="130">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :disabled="!row.elec_po_sheet_id"
                       @click="downloadSheet(row.elec_po_sheet_id, '电工采购单')">
              <el-icon><Document /></el-icon>{{ row.elec_po_sheet_id ? '下载' : '—' }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column v-if="showStandard" label="标准件清单" min-width="130">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :disabled="!row.standard_sheet_id"
                       @click="downloadSheet(row.standard_sheet_id, '标准件清单')">
              <el-icon><Document /></el-icon>{{ row.standard_sheet_id ? '下载' : '—' }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column v-if="showStandard" label="外购附图(设计推送)" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="f in row.outsource_img_files" :key="f.id" size="small" effect="plain" class="fc" @click="downloadAttachment(f)">
              {{ f.name }}<el-icon class="dl"><Download /></el-icon>
            </el-tag>
            <span v-if="!row.outsource_img_files.length" class="muted">待设计推送</span>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无项目" />
    </el-card>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
.fc { cursor: pointer; margin: 2px 4px 2px 0; }
.fc .dl { margin-left: 4px; }
</style>
