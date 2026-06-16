<script setup lang="ts">
// 🆕 v3 用户反馈管理后台（仅 admin/manager）
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Download, Refresh } from '@element-plus/icons-vue'
import { userFeedbackApi, type UserFeedbackRow } from '@/api/userFeedback'
import { http } from '@/api'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtRelative } from '@/utils/format'

const loading = ref(false)
const list = ref<UserFeedbackRow[]>([])
const filterKind = ref('')
const filterStatus = ref('')

const KIND_TXT: Record<string, string> = { bug: '问题反馈', suggest: '意见建议', other: '其它' }
const KIND_TAG: Record<string, any> = { bug: 'danger', suggest: 'primary', other: 'info' }
const STATUS_TXT: Record<string, string> = { open: '待处理', done: '已处理' }
const STATUS_TAG: Record<string, any> = { open: 'warning', done: 'success' }

async function load() {
  loading.value = true
  try {
    list.value = await userFeedbackApi.list({
      kind: filterKind.value || undefined,
      status: filterStatus.value || undefined,
    })
  } finally { loading.value = false }
}
onMounted(load)

async function markDone(row: UserFeedbackRow) {
  await userFeedbackApi.markDone(row.id)
  ElMessage.success('已标记为已处理')
  row.status = 'done'
}

async function exportHtml() {
  // 走 http(axios) 客户端：统一用拦截器里的 pms_token 鉴权（之前 fetch 读错 key 'token' → 401）
  try {
    const r = await http.get('/user-feedback/export.html', {
      params: { kind: filterKind.value || undefined, status: filterStatus.value || undefined },
      responseType: 'blob',
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(r.data as Blob)
    a.download = `用户反馈导出-${new Date().toISOString().slice(0, 10)}.html`
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
    ElMessage.success('已导出 HTML')
  } catch (e: any) {
    ElMessage.error(`导出失败：${e?.response?.status || e?.message || e}`)
  }
}

const stats = computed(() => ({
  total: list.value.length,
  open: list.value.filter(r => r.status === 'open').length,
  bug: list.value.filter(r => r.kind === 'bug').length,
  suggest: list.value.filter(r => r.kind === 'suggest').length,
}))

const previewSrc = ref('')
async function preview(row: UserFeedbackRow) {
  if (!row.shot_file_id) return
  // 走 http(axios) 拿 blob：用 pms_token 鉴权（之前 fetch 读错 key → 401 → 图裂）
  try {
    const r = await http.get(`/attachments/${row.shot_file_id}/download`, { responseType: 'blob' })
    previewSrc.value = URL.createObjectURL(r.data as Blob)
  } catch {
    ElMessage.error('截图加载失败')
  }
}
function closePreview() {
  if (previewSrc.value) URL.revokeObjectURL(previewSrc.value)
  previewSrc.value = ''
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>用户反馈</h1>
        <div class="desc">所有用户通过右下角「反馈」小助手提交的问题与建议；可标记已处理、按筛选条件导出 HTML</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <el-button type="primary" :icon="Download" @click="exportHtml">导出 HTML</el-button>
    </div>

    <div class="sec-title">概览</div>
    <div class="kpi-grid">
      <div class="kpi is-primary"><div class="kpi-v">{{ stats.total }}</div><div class="kpi-l">反馈总数</div></div>
      <div class="kpi" :class="stats.open ? 'is-warn' : ''"><div class="kpi-v">{{ stats.open }}</div><div class="kpi-l">待处理</div></div>
      <div class="kpi is-bad"><div class="kpi-v">{{ stats.bug }}</div><div class="kpi-l">问题反馈</div></div>
      <div class="kpi is-good"><div class="kpi-v">{{ stats.suggest }}</div><div class="kpi-l">意见建议</div></div>
    </div>

    <el-card shadow="never" style="margin-top:14px">
      <template #header>
        <div style="display:flex;align-items:center;gap:10px">
          <span>反馈列表</span>
          <el-select v-model="filterKind" placeholder="类型(全部)" clearable style="width:150px" @change="load">
            <el-option label="问题反馈" value="bug" />
            <el-option label="意见建议" value="suggest" />
            <el-option label="其它" value="other" />
          </el-select>
          <el-select v-model="filterStatus" placeholder="状态(全部)" clearable style="width:150px" @change="load">
            <el-option label="待处理" value="open" />
            <el-option label="已处理" value="done" />
          </el-select>
        </div>
      </template>
      <el-table :data="list" v-loading="loading" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }"><el-tag size="small" :type="KIND_TAG[row.kind]">{{ KIND_TXT[row.kind] }}</el-tag></template>
        </el-table-column>
        <el-table-column label="提交人" width="140">
          <template #default="{ row }">{{ row.user_name || '—' }}<span class="muted small" v-if="row.user_role">（{{ row.user_role }}）</span></template>
        </el-table-column>
        <el-table-column label="内容" min-width="280">
          <template #default="{ row }"><span class="content-cell">{{ row.content }}</span></template>
        </el-table-column>
        <el-table-column label="页面" width="170">
          <template #default="{ row }"><code class="page-code">{{ row.page_url || '—' }}</code></template>
        </el-table-column>
        <el-table-column label="截图" width="80">
          <template #default="{ row }">
            <el-button v-if="row.shot_file_id" size="small" link type="primary" @click="preview(row)">查看</el-button>
            <span v-else class="muted small">—</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtRelative(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }"><StatusPill :text="STATUS_TXT[row.status]" :variant="row.status === 'done' ? 'success' : 'warn'" /></template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button v-if="row.status === 'open'" size="small" link type="success" :icon="Check" @click="markDone(row)">标记已处理</el-button>
            <span v-else class="muted small">—</span>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !list.length" text="暂无用户反馈" />
    </el-card>

    <!-- 截图预览 -->
    <el-dialog v-model="previewSrc" title="截图预览" width="80%" top="6vh" @close="closePreview" append-to-body>
      <div style="text-align:center"><img v-if="previewSrc" :src="previewSrc" alt="截图" style="max-width:100%;max-height:75vh" /></div>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.content-cell { white-space: pre-wrap; word-break: break-word; line-height: 1.55; }
.page-code { font-size: 12px; background: var(--el-fill-color-light); padding: 1px 6px; border-radius: 4px; color: var(--el-text-color-secondary); }
</style>
