<script setup lang="ts">
// 🆕 桌面端在线统计（仅 admin/manager）：版本分布条形 + 设备明细表（数据只读，来自统计中间件）
import { ref, computed, onMounted } from 'vue'
import { Refresh, Monitor } from '@element-plus/icons-vue'
import { desktopApi, type DesktopClientItem, type DesktopVersionDist } from '@/api/desktop'
import EmptyHint from '@/components/EmptyHint.vue'
import { fmtRelative } from '@/utils/format'

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
onMounted(load)

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
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

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
  </div>
</template>

<style scoped>
.ver-row { display: flex; align-items: center; gap: 12px; padding: 7px 0; }
.ver-label { width: 90px; flex: none; font-weight: 600; font-size: 13px; color: #1f2937; }
.ver-bar-wrap { flex: 1; height: 16px; background: var(--el-fill-color-light); border-radius: 8px; overflow: hidden; }
.ver-bar { height: 100%; background: var(--el-color-primary); border-radius: 8px; transition: width .3s; }
.ver-num { width: 110px; flex: none; text-align: right; font-size: 12.5px; color: var(--el-text-color-secondary); }
.dev-id { font-size: 12px; background: var(--el-fill-color-light); padding: 1px 6px; border-radius: 4px; color: var(--el-text-color-secondary); }
</style>
