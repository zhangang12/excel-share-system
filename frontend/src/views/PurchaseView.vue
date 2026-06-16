<script setup lang="ts">
// 🆕 v3 M06 采购部：采购清单收件箱（电工接单上传的采购清单，撤回即消失）
import { ref, onMounted } from 'vue'
import { Document, Download, Refresh } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
import EmptyHint from '@/components/EmptyHint.vue'
import { fmtDateTime } from '@/utils/format'

interface Att { id: number; name: string }
interface InboxRow {
  project_id: number; code: string; name: string; source: string
  file: Att; received_at: string
}

const auth = useAuthStore()
const loading = ref(false)
const rows = ref<InboxRow[]>([])

// 🆕 采购清单收件箱开关：电工采购清单改为直接进「项目详单·电工采购单」第5表,
// 收件箱已冗余 → 先隐藏(可逆: 改回 true 即恢复收件箱)。
const SHOW_INBOX = false

async function load() {
  if (!SHOW_INBOX) return
  loading.value = true
  try { rows.value = (await http.get<InboxRow[]>('/purchase/inbox')).data }
  finally { loading.value = false }
}
onMounted(load)

function openDetail(pid: number) {
  if (auth.canViewDetail) window.open(`/projects/${pid}`, '_self')
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购部</h1>
        <div class="desc">电工部接单上传的「采购清单」汇总到这里；在项目详单「电工采购单」表补充采购负责人/订购到货日期</div>
      </div>
    </div>

    <!-- 🆕 收件箱已并入「项目详单·电工采购单」, 先隐藏(SHOW_INBOX 可逆) -->
    <el-card v-if="!SHOW_INBOX" shadow="never">
      <el-result icon="info" title="采购清单已并入项目详单"
                 sub-title="电工部上传的采购清单现已直接进入对应项目「项目详单 · 电工采购单」第5表；请在该表内补充采购负责人 / 订购日期 / 到货日期。本收件箱已停用。">
        <template #extra>
          <el-button type="primary" @click="$router.push('/overview')">去项目目录</el-button>
        </template>
      </el-result>
    </el-card>

    <el-card v-else shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <span><el-icon><Document /></el-icon> 采购清单收件箱</span>
          <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="50" />
        <el-table-column label="项目编号" width="120">
          <template #default="{ row }">
            <a v-if="auth.canViewDetail" class="code link" @click="openDetail(row.project_id)">{{ row.code }}</a>
            <b v-else class="code">{{ row.code }}</b>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="source" label="来源" width="100">
          <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.source }}</el-tag></template>
        </el-table-column>
        <el-table-column label="采购清单" min-width="200">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" class="fc" @click="downloadAttachment(row.file)">
              <el-icon><Document /></el-icon>{{ row.file.name }}<el-icon class="dl"><Download /></el-icon>
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="received_at" label="收到时间" width="150">
          <template #default="{ row }">{{ fmtDateTime(row.received_at) }}</template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无采购清单（电工部完成接单上传后出现）" />
    </el-card>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.link { cursor: pointer; font-weight: 600; }
.link:hover { text-decoration: underline; }
.fc { cursor: pointer; }
.fc .dl { margin-left: 4px; }
</style>
