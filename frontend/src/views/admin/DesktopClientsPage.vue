<script setup lang="ts">
// 🆕 桌面端在线统计（仅 admin/manager）：版本分布条形 + 设备明细表（数据只读，来自统计中间件）
import { ref, computed, onMounted } from 'vue'
import { Refresh, Monitor } from '@element-plus/icons-vue'
import { desktopApi, type DesktopClientItem, type DesktopVersionDist,
         type DesktopReportItem } from '@/api/desktop'
import EmptyHint from '@/components/EmptyHint.vue'
import { fmtRelative } from '@/utils/format'
import { ElMessage } from 'element-plus'
import PageRefresh from '@/components/PageRefresh.vue'   // 反馈#359：每个页面都有刷新

const tab = ref('clients')
const loading = ref(false)
const distribution = ref<DesktopVersionDist[]>([])
const items = ref<DesktopClientItem[]>([])

async function load() {
  loading.value = true
  try {
    const res = await desktopApi.clients()
    distribution.value = res.distribution
    items.value = res.items
  } finally { loading.value = false }
}
onMounted(() => { load(); loadReports() })

// ===== 🆕 故障上报：客户端自动送回来的升级失败/崩溃日志 =====
// 起因：old-uninstaller 崩溃让部分机器永远升不了级，排查时手里什么都没有。
// 现在客户端会在下次启动时把 crash.log 尾部送回来，这里直接看。
const reports = ref<DesktopReportItem[]>([])
const openCount = ref(0)
const repLoading = ref(false)
const onlyOpen = ref(true)
const kindFilter = ref('')

const KIND_LABEL: Record<string, string> = {
  update_failed: '升级失败',
  crash: '客户端崩溃',
  error: '运行报错',
}
const KIND_TYPE: Record<string, string> = {
  update_failed: 'danger',
  crash: 'warning',
  error: 'info',
}

async function loadReports() {
  repLoading.value = true
  try {
    const res = await desktopApi.reports({
      kind: kindFilter.value || undefined,
      only_open: onlyOpen.value || undefined,
    })
    reports.value = res.items
    openCount.value = res.open_count
  } finally { repLoading.value = false }
}

async function toggleHandled(row: DesktopReportItem) {
  await desktopApi.markHandled(row.id, !row.handled)
  ElMessage.success(row.handled ? '已取消标记' : '已标记处理')
  await loadReports()
}

// extra 里的关键信息拍平成一行，省得点开每条看
function extraBrief(r: DesktopReportItem): string {
  const e = r.extra || {}
  const bits: string[] = []
  if (e.target_version) bits.push(`目标 v${e.target_version}`)
  if (e.current_version) bits.push(`实际仍是 v${e.current_version}`)
  if (e.attempts) bits.push(`第 ${e.attempts} 次上报`)
  if (e.where) bits.push(String(e.where))
  return bits.join(' · ')
}

const total = computed(() => distribution.value.reduce((s, d) => s + d.count, 0))
// 各版本占比（百分比，供条形宽度与标注）
const bars = computed(() => distribution.value.map((d) => ({
  ...d,
  percent: total.value ? Math.round((d.count / total.value) * 100) : 0,
})))

// device_id 较长，截断显示（完整值由列 tooltip 悬浮展示）
function shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>桌面端</h1>
        <div class="desc">Windows 桌面客户端在线版本分布与设备明细（按客户端请求自动统计，只读）</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" :loading="loading || repLoading"
                 @click="tab === 'clients' ? load() : loadReports()">刷新</el-button>
      <PageRefresh :load="() => { load(); loadReports() }" />
    </div>

    <el-tabs v-model="tab">
    <el-tab-pane name="clients">
      <template #label>
        <span>设备台账</span>
      </template>
    <template v-if="total > 0">
      <div class="sec-title">在线版本分布（共 {{ total }} 台）</div>
      <el-card shadow="never" style="margin-bottom:14px">
        <div v-for="b in bars" :key="b.version" class="ver-row">
          <div class="ver-label">v{{ b.version }}</div>
          <div class="ver-bar-wrap">
            <div class="ver-bar" :style="{ width: Math.max(b.percent, 2) + '%' }"></div>
          </div>
          <div class="ver-num">{{ b.count }} 台 · {{ b.percent }}%</div>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header><span>设备明细</span></template>
        <el-table show-overflow-tooltip :data="items" v-loading="loading" stripe
                  max-height="calc(100vh - 320px)" :scrollbar-always-on="true">
          <el-table-column label="设备 ID" min-width="200">
            <template #default="{ row }"><code class="dev-id" :title="row.device_id">{{ shortId(row.device_id) }}</code></template>
          </el-table-column>
          <el-table-column label="版本" width="120">
            <template #default="{ row }"><el-tag size="small" type="primary">v{{ row.version }}</el-tag></template>
          </el-table-column>
          <el-table-column label="用户名" width="160">
            <template #default="{ row }">{{ row.username || '—' }}</template>
          </el-table-column>
          <el-table-column label="最近在线" width="160">
            <template #default="{ row }">{{ fmtRelative(row.last_seen) }}</template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
    <el-card v-else-if="!loading" shadow="never">
      <EmptyHint :icon="Monitor" text="暂无桌面客户端在线" />
    </el-card>
    </el-tab-pane>

    <!-- 🆕 故障上报：客户端出问题自动送回来，不用再让用户手动发 crash.log -->
    <el-tab-pane name="reports">
      <template #label>
        <el-badge :value="openCount" :hidden="!openCount" :max="99">
          <span style="padding-right:6px">故障上报</span>
        </el-badge>
      </template>

      <div class="filter-bar" style="margin-bottom:12px">
        <el-select v-model="kindFilter" placeholder="全部类型" clearable
                   style="width:150px" @change="loadReports">
          <el-option label="升级失败" value="update_failed" />
          <el-option label="客户端崩溃" value="crash" />
          <el-option label="运行报错" value="error" />
        </el-select>
        <el-checkbox v-model="onlyOpen" @change="loadReports">只看未处理</el-checkbox>
        <span class="hint">客户端出问题时自动上报（升级失败要等下次启动才能回溯——安装器是在客户端退出后才跑的）</span>
      </div>

      <el-card shadow="never" v-if="reports.length">
        <el-table :data="reports" v-loading="repLoading" stripe
                  max-height="calc(100vh - 300px)" :scrollbar-always-on="true">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="log-box">{{ row.detail || '（无日志正文）' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmtRelative(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="(KIND_TYPE[row.kind] as any) || 'info'">
                {{ KIND_LABEL[row.kind] || row.kind }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="用户" width="110">
            <template #default="{ row }">{{ row.username || '—' }}</template>
          </el-table-column>
          <el-table-column label="版本" width="90">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column label="关键信息" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">{{ extraBrief(row) || '展开看日志' }}</template>
          </el-table-column>
          <el-table-column label="设备" width="150">
            <template #default="{ row }">
              <code class="dev-id" :title="row.device_id">{{ shortId(row.device_id) }}</code>
            </template>
          </el-table-column>
          <el-table-column label="处理" width="96" align="center">
            <template #default="{ row }">
              <el-button size="small" :type="row.handled ? 'info' : 'primary'"
                         :link="row.handled" @click="toggleHandled(row)">
                {{ row.handled ? '已处理' : '标记' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
      <el-card v-else-if="!repLoading" shadow="never">
        <EmptyHint :icon="Monitor" text="没有故障上报——客户端一切正常" />
      </el-card>
    </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.ver-row { display: flex; align-items: center; gap: 12px; padding: 7px 0; }
.ver-label { width: 90px; flex: none; font-weight: 600; font-size: 13px; color: #1f2937; }
.ver-bar-wrap { flex: 1; height: 16px; background: var(--el-fill-color-light); border-radius: 8px; overflow: hidden; }
.ver-bar { height: 100%; background: var(--el-color-primary); border-radius: 8px; transition: width .3s; }
.ver-num { width: 110px; flex: none; text-align: right; font-size: 12.5px; color: var(--el-text-color-secondary); }
.filter-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { font-size: 12.5px; color: var(--el-text-color-secondary); }
/* 日志正文：等宽 + 保留换行，crash.log 原样看 */
.log-box {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all;
  max-height: 340px; overflow: auto; padding: 10px 12px;
  background: var(--el-fill-color-light); border-radius: 6px;
}
.dev-id { font-size: 12px; background: var(--el-fill-color-light); padding: 1px 6px; border-radius: 4px; color: var(--el-text-color-secondary); }
</style>
