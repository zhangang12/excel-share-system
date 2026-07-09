<script setup lang="ts">
// 🆕 v3 用户反馈管理后台（仅 admin/manager）
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Download, Refresh, ChatLineRound } from '@element-plus/icons-vue'
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

// 🆕 系统回信：回复处理意见
const replyVisible = ref(false)
const replyRow = ref<UserFeedbackRow | null>(null)
const replyText = ref('')
const replySaving = ref(false)
function openReply(row: UserFeedbackRow) {
  replyRow.value = row
  replyText.value = row.reply || ''
  replyVisible.value = true
}
async function submitReply() {
  if (!replyRow.value) return
  const t = replyText.value.trim()
  if (!t) { ElMessage.warning('请填写处理意见回复'); return }
  replySaving.value = true
  try {
    const updated = await userFeedbackApi.reply(replyRow.value.id, t)
    const idx = list.value.findIndex(r => r.id === updated.id)
    if (idx >= 0) list.value[idx] = updated
    ElMessage.success('已回复，提出人登录后会收到提醒')
    replyVisible.value = false
  } finally { replySaving.value = false }
}

async function exportHtml() {
  // 走 http(axios) 客户端：统一用拦截器里的 pms_token 鉴权（之前 fetch 读错 key 'token' → 401）
  // 🆕 导出只要待处理的——已处理的是历史记录，导出给管理层看是为了盯着还没解决的，混进已处理的没意义
  try {
    const r = await http.get('/user-feedback/export.html', {
      params: { kind: filterKind.value || undefined, status: 'open' },
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

const previewVisible = ref(false)
const previewSrc = ref('')
const previewLoading = ref(false)
const previewError = ref('')
async function preview(row: UserFeedbackRow) {
  if (!row.shot_file_id) return
  // 对话框照常打开：加载中转圈、成功显示图、失败显示明确占位(不再空白)
  previewVisible.value = true
  previewError.value = ''
  previewLoading.value = true
  if (previewSrc.value) { URL.revokeObjectURL(previewSrc.value); previewSrc.value = '' }
  try {
    const r = await http.get(`/attachments/${row.shot_file_id}/download`, { responseType: 'blob' })
    previewSrc.value = URL.createObjectURL(r.data as Blob)
  } catch (e: any) {
    previewError.value = e?.response?.status === 404
      ? '截图文件已丢失（可能因服务器存储未持久化），无法预览'
      : `截图加载失败（${e?.response?.status || e?.message || '未知'}）`
  } finally {
    previewLoading.value = false
  }
}
function closePreview() {
  if (previewSrc.value) URL.revokeObjectURL(previewSrc.value)
  previewSrc.value = ''
  previewError.value = ''
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>用户反馈</h1>
        <div class="desc">所有用户通过右下角「反馈」小助手提交的问题与建议；可标记已处理、导出待处理项为 HTML</div>
      </div>
      <div class="spacer"></div>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <el-tooltip content="只导出「待处理」的反馈，已处理的不会包含在内" placement="top">
        <el-button type="primary" :icon="Download" @click="exportHtml">导出待处理 HTML</el-button>
      </el-tooltip>
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
      <el-table show-overflow-tooltip :data="list" v-loading="loading" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
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
        <el-table-column label="处理意见回复" min-width="200">
          <template #default="{ row }">
            <span v-if="row.reply" class="reply-cell">{{ row.reply }}</span>
            <span v-else class="muted small">未回复</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }"><StatusPill :text="STATUS_TXT[row.status]" :variant="row.status === 'done' ? 'success' : 'warn'" /></template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :icon="ChatLineRound" @click="openReply(row)">{{ row.reply ? '修改回复' : '回复' }}</el-button>
            <el-button v-if="row.status === 'open'" size="small" link type="success" :icon="Check" @click="markDone(row)">已处理</el-button>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !list.length" text="暂无用户反馈" />
    </el-card>

    <!-- 🆕 回复处理意见 -->
    <el-dialog v-model="replyVisible" title="回复处理意见（系统回信）" width="540px" append-to-body>
      <div v-if="replyRow" class="reply-orig">
        <div class="reply-orig-h">{{ replyRow.user_name || '—' }} · {{ KIND_TXT[replyRow.kind] }} 反馈</div>
        <div class="reply-orig-c">{{ replyRow.content }}</div>
      </div>
      <el-input v-model="replyText" type="textarea" :rows="5" maxlength="2000" show-word-limit
                placeholder="填写处理意见 / 回复内容。提出人下次登录时右下角会弹出提醒，并可在「反馈」小助手的「我的反馈」中查看。" />
      <template #footer>
        <el-button @click="replyVisible = false">取消</el-button>
        <el-button type="primary" :loading="replySaving" @click="submitReply">发送回复</el-button>
      </template>
    </el-dialog>

    <!-- 截图预览 -->
    <el-dialog v-model="previewVisible" title="截图预览" width="80%" top="6vh" @close="closePreview" append-to-body>
      <div v-loading="previewLoading" style="text-align:center; min-height:120px">
        <img v-if="previewSrc" :src="previewSrc" alt="截图" style="max-width:100%;max-height:75vh" />
        <el-empty v-else-if="previewError" :description="previewError" :image-size="80" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.content-cell { white-space: pre-wrap; word-break: break-word; line-height: 1.55; }
.page-code { font-size: 12px; background: var(--el-fill-color-light); padding: 1px 6px; border-radius: 4px; color: var(--el-text-color-secondary); }
.reply-cell { white-space: pre-wrap; word-break: break-word; line-height: 1.5; color: #065f46; }
.reply-orig { background: var(--el-fill-color-light); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.reply-orig-h { font-size: 12.5px; color: var(--el-text-color-secondary); margin-bottom: 4px; }
.reply-orig-c { white-space: pre-wrap; word-break: break-word; line-height: 1.55; color: #1f2937; }
</style>
